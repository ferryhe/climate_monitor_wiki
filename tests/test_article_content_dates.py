"""G1: exact date authority and complete current content packages."""
import hashlib
import json
import sqlite3
from copy import deepcopy

import pytest

from climate_registry.article_dates import content_date_projection, validate_content_update, select_article_dates
from climate_registry.errors import RegistryInputError
from climate_registry.owner_checks import plan_owner_check
from climate_registry.owner_dates import MANUAL_AUTHORIZATION, inventory_date_overlays
from climate_registry.publication import stage_snapshot, snapshot_entity, _approve, export_public_snapshot, public_wiki_pages
from climate_registry.read_api import RegistryReader, sort_articles_by_publication_date
from test_owner_publication_dates import _seed, _date_manifest, _date_fields_manifest, _date_review, _approve as approve_date
from test_owner_checks import _ingest
from test_verified_publication import _reader


@pytest.fixture(autouse=True)
def ordered_source_check_fixture_clock(monkeypatch):
    # These sequential fixture checks must retain their order even if the host wall clock steps backward.
    from datetime import datetime, timedelta, timezone
    from climate_registry import information_checks
    import verified_fixtures
    base, tick = datetime.now(timezone.utc), 0
    def next_time():
        nonlocal tick
        tick += 1
        return (base + timedelta(microseconds=tick)).isoformat()
    monkeypatch.setattr(information_checks, "_now", next_time)
    monkeypatch.setattr(verified_fixtures, "_now", next_time)


def _manual(database, *, source="native", fields=None, scope="record"):
    fields = fields or {"publication_date": "2026-10-02"}
    manifest = plan_owner_check(database, "pdf_article" if source == "pdf" else "article",
        "pdf-good" if source == "pdf" else "good", date_fields=list(fields), human_date_override=True)
    manifest.update(actor={"id": "operator", "task_id": "human-instruction-execution"},
        checked_at="2026-10-24T12:00:00Z", authorization_reference="human-message:277/explicit", audit_reference="audit:277",
        manual_authorization={"instruction": "I confirmed record good: set the selected date fields to these values: " + json.dumps(fields),
            "authorized_by": "owner", "scope": scope},
        observations=[{"target": manifest["plan"]["targets"][0], "public_fields": fields,
            "field_evidence": {field: {"text": value, "precision": "day"} for field, value in fields.items()}}])
    return manifest


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
def test_explicit_human_override_needs_no_source_pass_or_second_approval(tmp_path, source):
    database = _seed(tmp_path, source)
    reader = _reader(database, tmp_path)
    before = reader.pdf_article("pdf-good") if source == "pdf" else reader.article("good")
    result = _ingest(database, tmp_path, _manual(database, source=source))
    assert result["status"] == "approved"
    assert result["authorization_basis"] == MANUAL_AUTHORIZATION
    for direct in [True, False]:
        with reader.public_snapshot(direct=direct):
            after = reader.pdf_article("pdf-good") if source == "pdf" else reader.article("good")
            assert after["publication_date"] == "2026-10-02"
            assert after["publication_date_verification"]["authority"] == "human_override"
            assert after["publication_date_evidence"]["authority"] == "human_override"
            assert "reviewer" not in after["publication_date_verification"]
            decision = after["manual_date_overrides"]["publication_date"]
            assert decision["old_value"] == before.get("publication_date")
            assert decision["new_value"] == "2026-10-02" and decision["scope"] == "record"
            assert decision["authorized_by"] == "owner" and decision["executed_at"] == "2026-10-24T12:00:00Z"
            for field in ("summary", "keywords", "categories", "title", "updated_date", "revision_date"):
                assert after.get(field) == before.get(field), field
    with sqlite3.connect(database) as db:
        receipt = json.loads(db.execute("SELECT receipt_json FROM registry_reviews WHERE candidate_sha256=?", (result["candidate_sha256"],)).fetchone()[0])
        assert receipt["decision"] == "authorized" and receipt["schema_version"] == MANUAL_AUTHORIZATION
        assert "reviewed_at" not in receipt
        assert db.execute("SELECT count(*) FROM article_date_observations WHERE source_system='owner_agent'").fetchone()[0] == 0
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    exported = RegistryReader.from_public_export(root)
    item = exported.pdf_article("pdf-good") if source == "pdf" else exported.article("good")
    assert item["publication_date_evidence"]["authority"] == "human_override"
    if source != "native":
        direct_occurrence = reader.pdf_article("pdf-good")["occurrences"][0]
        static_occurrence = exported.pdf_article("pdf-good")["occurrences"][0]
        for key in ("authority", "instruction_reference"):
            assert static_occurrence["publication_date_evidence"][key] == direct_occurrence["publication_date_evidence"][key]
        assert static_occurrence["publication_date_evidence"]["authority"] == "human_override"


@pytest.mark.parametrize("mutation", ["invalid_date", "ambiguous", "system_field", "missing_instruction", "wrong_record", "version_scope"])
def test_manual_invalid_or_ambiguous_instruction_cannot_write(tmp_path, mutation):
    database = _seed(tmp_path)
    manifest = _manual(database)
    if mutation == "invalid_date":
        manifest["observations"][0]["field_evidence"]["publication_date"]["text"] = "2026-02-30"
    elif mutation == "ambiguous":
        manifest["observations"][0]["field_evidence"]["publication_date"]["text"] = "10/02/2026"
    elif mutation == "system_field":
        manifest["plan"]["date_fields"] = ["collected_at"]
    elif mutation == "missing_instruction":
        manifest["manual_authorization"]["instruction"] = ""
    elif mutation == "wrong_record":
        manifest["observations"][0]["target"]["entity_id"] = "unchecked"
    else:
        manifest["manual_authorization"]["scope"] = "content_version"
    with pytest.raises(RegistryInputError):
        _ingest(database, tmp_path, manifest)


def test_automation_cannot_replace_manual_and_later_staging_retains_record_instruction(tmp_path):
    database = _seed(tmp_path)
    with sqlite3.connect(database) as db:
        old_pending = snapshot_entity(db, "article", "good")
    _ingest(database, tmp_path, _manual(database))
    before = _reader(database, tmp_path).article("good")
    with sqlite3.connect(database) as db:
        old_pending["derived_display"] = {"summary": "pending changed wording"}
        sha = stage_snapshot(db, old_pending)
        frozen = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
        assert frozen["derived_display"]["publication_date"] == "2026-10-02"
        assert frozen["derived_display"]["manual_date_overrides"]["publication_date"]["authorization_id"] == before["manual_date_overrides"]["publication_date"]["authorization_id"]
    correction = _date_fields_manifest(database, fields=("publication_date",))
    staged = _ingest(database, tmp_path, correction)
    with pytest.raises(RegistryInputError, match="active human date override"):
        approve_date(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    assert _reader(database, tmp_path).article("good")["publication_date"] == "2026-10-02"


def _bundle_snapshot():
    old = "Flood losses reached 10 billion."
    new = "Flood losses reached 20 billion. Updated: 16 October 2026"
    summary = "Flood losses rose to 20 billion."
    sha = lambda text: hashlib.sha256(text.encode()).hexdigest()
    snapshot = {"entity_kind": "article", "entity_id": "good", "tables": {
        "articles": [{"article_id": "good", "canonical_url": "https://example.org/good", "current_content_version_id": "body-b"}],
        "article_content_versions": [
            {"article_id": "good", "content_version_id": "body-a", "markdown_content": old, "first_fetched_at": "2026-10-02T00:00:00Z"},
            {"article_id": "good", "content_version_id": "body-b", "markdown_content": new, "first_fetched_at": "2026-10-16T01:00:00Z"}],
        "article_fetches": [{"article_id": "good", "content_version_id": "body-b", "fetch_status": "success", "http_status": 200, "fetched_at": "2026-10-16T01:00:00Z"}]},
        "derived_display": {"summary": summary, "content_update": {
            "schema_version": "article-content-update.v1", "content_version_id": "body-b", "body_sha256": sha(new),
            "previous_content_version_id": "body-a", "previous_body_sha256": sha(old),
            "summary": summary, "summary_sha256": sha(summary), "summary_content_version_id": "body-b", "summary_support": "complete",
            "change_note": "Flood loss estimate increased from 10 billion to 20 billion.",
            "change_evidence": {"basis": "body_comparison", "previous_excerpt": old, "current_excerpt": "Flood losses reached 20 billion."},
            "first_observed_at": "2026-10-16T01:00:00Z", "date_evidence": {"updated_date": {
                "value": "2026-10-16", "date_kind": "updated", "source_date_precision": "day", "text": "16 October 2026",
                "content_version_id": "body-b", "source_url": "https://example.org/good", "final_url": "https://example.org/good",
                "captured_at": "2026-10-16T01:00:00Z"}}}}}
    return snapshot


@pytest.mark.parametrize("field", ["updated_date", "revision_date"])
def test_complete_content_package_sorts_current_dynamic_date_without_rewriting_publication(field):
    snapshot = _bundle_snapshot()
    bundle = snapshot["derived_display"]["content_update"]
    if field == "revision_date":
        proof = bundle["date_evidence"].pop("updated_date"); proof["date_kind"] = "revision"
        bundle["date_evidence"][field] = proof
    record = {"article_id": "good", "publication_date": "2026-10-02", "published_candidate_sha256": "a"*64, **snapshot["derived_display"]}
    record.update(content_date_projection(record, snapshot))
    assert record["publication_date"] == "2026-10-02"
    assert record["content_date"] == "2026-10-16" and record["content_date_basis"] == field
    other = {"article_id": "other", "publication_date": "2026-10-10"}
    assert sort_articles_by_publication_date([other, record])[0]["article_id"] == "good"


@pytest.mark.parametrize("mutation", ["summary_version", "summary_hash", "summary_incomplete", "summary_splice", "body_hash", "wrong_article", "date_version", "date_kind", "future_discovery", "invented_note", "only_formatting"])
def test_incomplete_stale_or_refetch_bundle_is_rejected(mutation):
    snapshot = _bundle_snapshot(); bundle = snapshot["derived_display"]["content_update"]
    if mutation == "summary_version": bundle["summary_content_version_id"] = "body-a"
    elif mutation == "summary_hash": bundle["summary_sha256"] = "0"*64
    elif mutation == "summary_incomplete": bundle["summary_support"] = "partial"
    elif mutation == "summary_splice": snapshot["derived_display"]["summary"] = "Old summary"
    elif mutation == "body_hash": bundle["body_sha256"] = "0"*64
    elif mutation == "wrong_article": snapshot["tables"]["article_content_versions"][1]["article_id"] = "other"
    elif mutation == "date_version": bundle["date_evidence"]["updated_date"]["content_version_id"] = "body-a"
    elif mutation == "date_kind": bundle["date_evidence"]["updated_date"]["date_kind"] = "published"
    elif mutation == "future_discovery": bundle["first_observed_at"] = "2026-10-24T01:00:00Z"
    elif mutation == "invented_note": bundle["change_evidence"]["current_excerpt"] = "New insurance commitments."
    else:
        old = snapshot["tables"]["article_content_versions"][0]["markdown_content"]
        current = "## " + old + "\n"
        snapshot["tables"]["article_content_versions"][1]["markdown_content"] = current
        bundle["body_sha256"] = hashlib.sha256(current.encode()).hexdigest()
    with pytest.raises(ValueError): validate_content_update(snapshot)


def test_discovery_fallback_stable_on_refetch_and_review_and_no_unbound_update_sort():
    snapshot = _bundle_snapshot(); bundle = snapshot["derived_display"]["content_update"]
    bundle["date_evidence"] = {}
    record = {"publication_date": "2026-10-02", **snapshot["derived_display"]}
    first = content_date_projection(record, snapshot)
    assert first["content_date_basis"] == "content_version_discovery"
    assert first["content_date"] == "2026-10-16" and first["content_date_authority"] == "trusted_observation"
    snapshot["tables"]["article_fetches"].append({**snapshot["tables"]["article_fetches"][0], "fetched_at": "2026-10-24T01:00:00Z"})
    assert content_date_projection(record, snapshot) == first
    plain = content_date_projection({"publication_date": "2026-10-02", "updated_date": "2026-10-24", "report_date": "2026-10-24", "updated_at": "2026-10-24T00:00:00Z"})
    assert plain["content_date"] == "2026-10-02" and plain["content_date_basis"] == "publication_date"
    assert content_date_projection({"report_date": "2026-10-24", "updated_at": "2026-10-24T00:00:00Z"})["content_date"] is None


def test_month_midpoint_and_year_unknown_do_not_invent_precision():
    evidence = {"date_kind": "published", "date_precision": "month", "date_text": "October 2026"}
    dates = select_article_dates([{"kind": "page_information", "observed_at": "2026-10", "evidence": evidence}])
    assert dates["publication_date"] == "2026-10-15"
    record = {"article_id": "month", **dates}; record.update(content_date_projection(record))
    assert record["content_date_precision"] == "month" and record["content_date_normalization"] == "month_midpoint"
    assert sort_articles_by_publication_date([{"article_id": "day", "publication_date": "2026-10-10"}, record])[0]["article_id"] == "month"
    assert select_article_dates([{"kind": "page_information", "observed_at": "2026", "evidence": {"date_kind": "published", "date_precision": "year"}}])["publication_date"] is None


def test_new_updated_publication_overlay_is_rejected(tmp_path):
    database = _seed(tmp_path)
    with pytest.raises(RegistryInputError, match="published date"):
        _ingest(database, tmp_path, _date_manifest(database, date_kind="updated"))
    assert inventory_date_overlays(database) == {"schema_version": "article-date-overlay-inventory.v1", "records": [], "count": 0, "write_performed": False}


def test_human_replacement_and_revocation_keep_history_without_second_approval(tmp_path):
    database = _seed(tmp_path)
    first = _ingest(database, tmp_path, _manual(database))
    replacement = _manual(database, fields={"publication_date": "2026-10-03"})
    second = _ingest(database, tmp_path, replacement)
    assert second["candidate_sha256"] != first["candidate_sha256"]
    item = _reader(database, tmp_path).article("good")
    assert item["manual_date_overrides"]["publication_date"]["old_value"] == "2026-10-02"
    revoke = _manual(database)
    revoke["manual_authorization"].update(action="revoke", instruction="Revoke record good publication_date override.")
    revoke["observations"][0].update(public_fields={"publication_date": None},
        field_evidence={"publication_date": {"text": "revoke", "precision": "unknown"}})
    _ingest(database, tmp_path, revoke)
    after = _reader(database, tmp_path).article("good")
    assert after["publication_date"] is None and not after.get("manual_date_overrides")
    with sqlite3.connect(database) as db:
        for sha in [first["candidate_sha256"], second["candidate_sha256"]]:
            assert db.execute("SELECT 1 FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()
            assert db.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=?", (sha,)).fetchone()


def test_frozen_legacy_updated_overlay_remains_readable_but_cannot_be_newly_approved(tmp_path):
    from climate_registry.acquisition_review import digest
    from climate_registry.owner_dates import OVERLAY, _run, _attempt, date_review_template
    database = _seed(tmp_path)
    legacy = _date_manifest(database, date_kind="updated")
    legacy["plan"].pop("date_semantics")
    with sqlite3.connect(database) as db:
        base_sha = legacy["plan"]["baseline_candidate_sha256"]
        base = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (base_sha,)).fetchone()[0])
        run = _run(legacy)
        snapshot = {**base, OVERLAY: {"baseline_candidate_sha256": base_sha, "manifest": legacy,
            "run": run, "attempt": _attempt(legacy, run["run_id"])}}
        sha = digest(snapshot)
        receipt = date_review_template(snapshot)
        receipt.update(actor={"id": "historical-reviewer", "task_id": "historical-review"},
            authorization_reference="legacy-owner-authorization", audit_reference="legacy-audit", reviewed_at="2026-10-16T12:01:00Z", decision="pass", reason="Historical original interpretation.")
        db.execute("INSERT INTO registry_candidates VALUES(?,?,?,?,?,?)", (sha, "article", "good", json.dumps(snapshot), "2026-10-16T12:00:00Z", "historical-fixture"))
        db.execute("INSERT INTO registry_reviews VALUES(?,?,?,?,?)", (digest([sha, "pass", receipt]), sha, "pass", "2026-10-16T12:01:00Z", json.dumps(receipt)))
        db.execute("UPDATE registry_publication SET published_candidate_sha256=?,latest_candidate_sha256=? WHERE entity_kind='article' AND entity_id='good'", (sha, sha))
        from climate_registry.display import refresh_display
        refresh_display(db)
        with pytest.raises(ValueError, match="current typed-date policy"):
            _approve(db, sha, receipt)
    item = _reader(database, tmp_path).article("good")
    assert item["publication_date"] == "2026-10-02"
    assert item["publication_date_evidence"]["date_kind"] == "updated"
    inventory = inventory_date_overlays(database)
    assert inventory["count"] == 1 and inventory["records"][0]["published"]
    assert inventory["records"][0]["candidate_sha256"] == sha and not inventory["write_performed"]


def _stage_complete_bundle(database, *, source_dates=True, date_field="updated_date"):
    packet = _bundle_snapshot()
    bundle = packet["derived_display"]["content_update"]
    if date_field == "revision_date":
        current = packet["tables"]["article_content_versions"][1]
        current["markdown_content"] = current["markdown_content"].replace("Updated:", "Revised:")
        bundle["body_sha256"] = hashlib.sha256(current["markdown_content"].encode()).hexdigest()
        proof = bundle["date_evidence"].pop("updated_date")
        proof["date_kind"] = "revision"
        bundle["date_evidence"][date_field] = proof
    if not source_dates:
        bundle["date_evidence"] = {}
    with sqlite3.connect(database) as db:
        for row in packet["tables"]["article_content_versions"]:
            body_hash = hashlib.sha256(row["markdown_content"].encode()).hexdigest()
            db.execute("INSERT INTO article_content_versions VALUES(?,?,?,?,?,'text/html',NULL,'fixture','v1',?)",
                (row["content_version_id"], "good", body_hash, row["markdown_content"], body_hash, row["first_fetched_at"]))
        db.execute("UPDATE articles SET current_content_version_id='body-b' WHERE article_id='good'")
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES('fetch-b','good','https://example.org/good','https://example.org/good','2026-10-16T01:00:00Z','success',200,'body-b')")
        from verified_fixtures import seed_verified_articles
        seed_verified_articles(db, occurrence_ids={"core:good"})
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = packet["derived_display"]
        sha = stage_snapshot(db, snapshot)
    return sha, bundle


def test_complete_package_coherent_across_direct_materialized_static_wiki_chat_and_pending(tmp_path):
    database = _seed(tmp_path)
    initial_date = _ingest(database, tmp_path, _date_fields_manifest(database, fields=("publication_date",)))
    approve_date(database, tmp_path, _date_review(database, tmp_path, initial_date["candidate_sha256"]))
    reader = _reader(database, tmp_path)
    before = reader.article("good")
    sha, bundle = _stage_complete_bundle(database)
    assert reader.article("good")["published_candidate_sha256"] == before["published_candidate_sha256"]
    assert reader.article("good")["summary"] == before["summary"]
    with sqlite3.connect(database) as db:
        _approve(db, sha, {"basis": "isolated complete-package exact candidate approval"}, status="accepted_legacy")
        from climate_registry.display import refresh_display
        refresh_display(db)
    def check(current, direct=None):
        if direct is None:
            detail = current.article("good"); card = current.articles()["items"][0]
        else:
            with current.public_snapshot(direct=direct):
                detail = current.article("good"); card = current.articles()["items"][0]
        for item in [detail, card]:
            assert item["publication_date"] == "2026-10-02"
            assert item["content_date"] == "2026-10-16" and item["content_date_basis"] == "updated_date"
            assert item["summary"] == bundle["summary"]
            assert item["content_update"]["change_note"] == bundle["change_note"]
            assert item["content_update"]["content_version_id"] == "body-b"
            assert item["content_date_precision"] == "day"
        return detail
    check(reader, True); detail = check(reader, False)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    check(RegistryReader.from_public_export(root))
    page = public_wiki_pages(database, reader=reader, page="article-good.md")["article-good.md"]
    assert "Publication date: 2026-10-02" in page and "Content sort date: 2026-10-16" in page
    assert bundle["change_note"] in page and bundle["summary"] in page
    from agentic_wiki.chat_evidence import article_text
    evidence = article_text(detail, {"substantive_updated_at": "2026-10-24T00:00:00Z"})
    assert "Updated 2026-10-16" in evidence and bundle["change_note"] in evidence
    assert "System record changed: 2026-10-24T00:00:00Z" in evidence
    from agentic_wiki.wiki_agent import WikiDocument, _document_catalog_presentation
    from dataclasses import fields
    kwargs = {field.name: "" for field in fields(WikiDocument) if field.default is __import__('dataclasses').MISSING and field.default_factory is __import__('dataclasses').MISSING}
    kwargs.update(type="article", markdown=page)
    presentation = _document_catalog_presentation(WikiDocument(**kwargs))
    assert presentation["display_date"] == "2026-10-16" and presentation["display_date_basis"] == "updated_date"
    assert presentation["content_date_precision"] == "day"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM registry_candidates WHERE entity_id='good'").fetchone()[0] >= 3
        assert db.execute("SELECT count(*) FROM articles WHERE article_id='good'").fetchone()[0] == 1


def test_date_label_only_change_cannot_create_substantive_update():
    snapshot = _bundle_snapshot(); bundle = snapshot["derived_display"]["content_update"]
    old = "Flood losses reached 20 billion. Updated: 2 October 2026"
    snapshot["tables"]["article_content_versions"][0]["markdown_content"] = old
    bundle["previous_body_sha256"] = hashlib.sha256(old.encode()).hexdigest()
    with pytest.raises(ValueError, match="substantive bodies"):
        validate_content_update(snapshot)


def test_hidden_record_override_is_retained_and_stale_pending_candidate_cannot_restore_date(tmp_path):
    database = _seed(tmp_path)
    with sqlite3.connect(database) as db:
        pending = snapshot_entity(db, "article", "good")
        pending["derived_display"] = {"summary": "Pending summary wording"}
        stale_sha = stage_snapshot(db, pending)
    _ingest(database, tmp_path, _manual(database))
    with sqlite3.connect(database) as db:
        with pytest.raises(ValueError, match="changed candidate"):
            _approve(db, stale_sha, {"basis": "stale exact candidate"}, status="accepted_legacy")
        db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_kind='article' AND entity_id='good'")
        latest = stage_snapshot(db, snapshot_entity(db, "article", "good"))
        frozen = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (latest,)).fetchone()[0])
        # A same-source stage can retain the entire exact manual overlay.
        if "publication_date_only" not in frozen:
            assert frozen["derived_display"]["manual_date_overrides"]["publication_date"]["new_value"] == "2026-10-02"
        else:
            assert frozen["publication_date_only"]["manifest"]["manual_authorization"]["scope"] == "record"


def test_collection_fallback_uses_first_trusted_collection_not_refetch_day():
    dates = select_article_dates([], collection_times=["2026-10-02T00:00:00Z", "2026-10-24T00:00:00Z"])
    projection = content_date_projection(dates)
    assert dates["collected_at"] == "2026-10-24T00:00:00Z"
    assert projection["content_date"] == "2026-10-02" and projection["content_date_basis"] == "collection_time"


@pytest.mark.parametrize("mutation", [None, "date_only", "summary_only", "old_summary_new_date", "missing_version", "wrong_version"])
def test_recent_source_content_route_requires_complete_current_package(tmp_path, mutation):
    from contextlib import nullcontext
    from test_chat_evidence import owner
    snapshot = _bundle_snapshot()
    item = {"article_id": "good", "title": "Climate insurance findings", "canonical_url": "https://example.org/good",
        "publication_date": "2026-10-02", "published_candidate_sha256": "a"*64,
        **snapshot["derived_display"]}
    item.update(content_date_projection(item, snapshot))
    if mutation in {"date_only", "summary_only"}:
        item.pop("content_update")
        item.update(content_date="2026-10-02", content_date_basis="publication_date", updated_date="2026-10-23")
        if mutation == "summary_only": item["summary"] = "Reworded existing findings."
    elif mutation == "old_summary_new_date": item["summary"] = "Old incomplete summary."
    elif mutation == "missing_version": item.pop("content_version_id")
    elif mutation == "wrong_version": item["content_version_id"] = "body-a"
    class Reader:
        def public_snapshot(self, *, direct=False): return nullcontext()
        def knowledge_chronology(self):
            return {"article:good": {"first_ingested_at": "2024-01-01T00:00:00Z", "substantive_updated_at": "2026-10-23T12:00:00Z"}}
        def articles(self, **kwargs): return {"items": [deepcopy(item)], "pagination": {"pages": 1}}
        def pdf_articles_all(self): return []
        def article(self, identity): return deepcopy(item)
    chat = owner(tmp_path); chat.reader_factory = Reader
    source = chat.answer("What climate insurance content was recently updated from 2026-10-01 to 2026-10-24?")
    if mutation is None:
        assert any(row.get("evidence_id") == "good" for row in source["sources"])
        assert "2026-10-16" in source["text"] and "content_date (complete approved source update)" in source["text"]
    else:
        assert not any(row.get("evidence_id") == "good" for row in source["sources"])
    system = chat.answer("What knowledge base climate insurance content was recently updated from 2026-10-01 to 2026-10-24?")
    assert any(row.get("evidence_id") == "good" for row in system["sources"])
    assert "System record changed: 2026-10-23T12:00:00Z" in system["text"]
    assert "substantive_updated_at (system records)" in system["text"]


def test_existing_added_and_source_publication_windows_remain_separate(tmp_path):
    from contextlib import nullcontext
    from test_chat_evidence import owner
    item = {"article_id": "good", "title": "Climate insurance findings", "canonical_url": "https://example.org/good",
        "summary": "Existing insurance findings.", "publication_date": "2026-10-02", "published_candidate_sha256": "a"*64}
    class Reader:
        def public_snapshot(self, *, direct=False): return nullcontext()
        def knowledge_chronology(self):
            return {"article:good": {"first_ingested_at": "2026-10-23T12:00:00Z", "substantive_updated_at": "2026-10-23T12:00:00Z"}}
        def articles(self, **kwargs): return {"items": [deepcopy(item)], "pagination": {"pages": 1}}
        def pdf_articles_all(self): return []
        def article(self, identity): return deepcopy(item)
    chat = owner(tmp_path); chat.reader_factory = Reader
    added = chat.answer("What climate insurance content was recently added from 2026-10-20 to 2026-10-24?")
    assert any(row.get("evidence_id") == "good" for row in added["sources"])
    assert "First added: 2026-10-23T12:00:00Z" in added["text"]
    published = chat.answer("What recently published climate insurance articles appeared from 2026-10-20 to 2026-10-24?")
    assert not any(row.get("evidence_id") == "good" for row in published["sources"])
    assert "publication_date (source publication)" in published["text"]


@pytest.mark.parametrize("mutation", ["summary", "date", "candidate", "version", "missing_package"])
def test_partial_static_projection_cannot_promote_an_update(mutation):
    from climate_registry.article_dates import validate_exported_content_update
    snapshot = _bundle_snapshot()
    item = {"article_id": "good", "publication_date": "2026-10-02", "published_candidate_sha256": "a"*64,
        **snapshot["derived_display"]}
    item.update(content_date_projection(item, snapshot))
    validate_exported_content_update(item)
    if mutation == "summary": item["summary"] = "Old summary."
    elif mutation == "date": item["content_date"] = "2026-10-24"
    elif mutation == "candidate": item["published_candidate_sha256"] = "b"*64
    elif mutation == "version": item["content_version_id"] = "body-a"
    else: item.pop("content_update")
    with pytest.raises(ValueError): validate_exported_content_update(item)


def test_acquisition_only_publication_keeps_original_value_and_evidence_for_manual_audit():
    from climate_registry.owner_dates import inherited_article_date
    evidence = {"date_kind": "published", "text": "2 October 2026", "source_url": "https://example.org/good",
        "final_url": "https://example.org/good", "captured_at": "2026-10-03T00:00:00Z"}
    snapshot = {"tables": {"acquisition_items": [{"publication_date": "2026-10-02",
        "publication_date_evidence_json": json.dumps(evidence), "processing_status": "complete", "selection_status": "selected"}]}}
    inherited = inherited_article_date(snapshot)
    assert inherited == {"publication_date": "2026-10-02", "publication_date_evidence": evidence}


@pytest.mark.parametrize("editorial", [False, True])
def test_full_owner_check_cannot_publish_updated_as_publication(tmp_path, editorial):
    from test_owner_checks import _manifest, _editorial_manifest
    database = _seed(tmp_path)
    fields = {"title": "Climate risk findings", "summary": "Flood exposure increased in the study region.",
        "publication_date": "2026-10-16"}
    body = fields["title"] + "\n" + fields["summary"] + "\nPublished: 2026-10-02\nUpdated: 2026-10-16"
    manifest = (_editorial_manifest if editorial else _manifest)(database, fields=fields, body=body)
    manifest["observations"][0]["field_evidence"]["publication_date"]["date_kind"] = "updated"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(RegistryInputError, match="published date"):
        _ingest(database, tmp_path, manifest)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_frozen_full_owner_updated_publication_remains_readable_but_new_approval_rejects(tmp_path, monkeypatch):
    from climate_registry import owner_checks
    from test_owner_checks import _manifest, _review
    from test_verified_publication import _database
    database = _database(tmp_path)
    fields = {"title": "Climate risk findings", "summary": "Flood exposure increased in the study region.",
        "publication_date": "2026-10-16"}
    manifest = _manifest(database, fields=fields, body="\n".join(fields.values()))
    manifest["observations"][0]["field_evidence"]["publication_date"]["date_kind"] = "updated"
    original = owner_checks.validate_observation
    with monkeypatch.context() as legacy:
        legacy.setattr(owner_checks, "validate_observation", lambda *args, **kwargs:
            original(*args, **{**kwargs, "strict_date_types": False}))
        staged = _ingest(database, tmp_path, manifest)
        receipt = _review(database, tmp_path, staged["candidate_sha256"])
    with pytest.raises(RegistryInputError, match="published date"):
        approve_date(database, tmp_path, receipt)
    with monkeypatch.context() as legacy:
        legacy.setattr(owner_checks, "validate_observation", lambda *args, **kwargs:
            original(*args, **{**kwargs, "strict_date_types": False}))
        approve_date(database, tmp_path, receipt)
    assert _reader(database, tmp_path).article("good")["publication_date"] == "2026-10-16"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    inventory = inventory_date_overlays(database)
    assert not inventory["write_performed"] and hashlib.sha256(database.read_bytes()).hexdigest() == before
    affected = [row for row in inventory["records"] if row["entity_id"] == "good" and row["path_kind"] == "full_owner_observation"]
    assert affected and any(row["published"] and row["candidate_sha256"] == staged["candidate_sha256"] for row in affected)
    assert all(row["evidence_kind"] == "updated" and row["attempt_id"] in row["evidence_path"] for row in affected)


@pytest.mark.parametrize("field", ["updated_date", "revision_date"])
def test_approved_date_correction_supplies_date_for_same_complete_discovery_version(tmp_path, field):
    database = _seed(tmp_path)
    sha, bundle = _stage_complete_bundle(database, source_dates=False)
    with sqlite3.connect(database) as db:
        _approve(db, sha, {"basis": "complete fixture package approval"}, status="accepted_legacy")
    _source_date_correction(database, tmp_path, field)
    reader = _reader(database, tmp_path)
    def check(item):
        assert item["content_date"] == "2026-10-20" and item["content_date_basis"] == field
        assert item["content_date_authority"] == "source"
        assert item["content_date_evidence"] == item[field + "_evidence"]
        assert item["content_date_evidence"]["table"] == "registry_reviews"
        assert item[field + "_verification"]["content_version_id"] == "body-b"
        assert item["content_update"]["date_evidence"] == {} and item["summary"] == bundle["summary"]
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            check(reader.article("good")); check(reader.articles()["items"][0])
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    item = RegistryReader.from_public_export(root).article("good"); check(item)
    item[field + "_verification"]["content_version_id"] = "other-body"
    assert content_date_projection(item)["content_date_basis"] == "content_version_discovery"
    item.pop("content_update")
    assert content_date_projection(item)["content_date_basis"] not in {field, "content_version_discovery"}


def test_pdf_collection_observation_projects_truthful_fallback_without_using_import_time(tmp_path):
    database = _seed(tmp_path, "pdf")
    reader = _reader(database, tmp_path)
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "pdf_article", "pdf-good")
    with reader.public_snapshot(direct=True):
        assert reader._pdf_article_from_snapshot(snapshot)["content_date"] is None
    evidence = {"source_url": "https://example.org/pdf-only", "match_basis": "exact_pdf_article",
        "source_system": "fixture", "database": "registry", "table": "pdf_article_date_observations", "record_id": "collection"}
    snapshot["tables"]["pdf_article_date_observations"] = [{"article_id": "pdf-good", "observation_id": "collection",
        "observation_kind": "collection", "observed_at": "2026-10-02T05:00:00Z", "recorded_at": "2026-10-02T05:00:01Z",
        "evidence_json": json.dumps(evidence)}]
    with reader.public_snapshot(direct=True):
        item = reader._pdf_article_from_snapshot(snapshot)
    assert item["date_observations"][0]["kind"] == "collection"
    assert item["collected_at"] == item["first_collected_at"] == "2026-10-02T05:00:00Z"
    assert item["content_date"] == "2026-10-02" and item["content_date_basis"] == "collection_time"
    assert item["content_date_label"] == "Collected on" and item["content_date_authority"] == "collection_fallback"
    from climate_registry.publication import _pdf_dto
    dto = _pdf_dto(item)
    assert dto["first_collected_at"] == item["first_collected_at"] and dto["content_date"] == item["content_date"]


def _source_date_correction(database, tmp_path, field, *, value="2026-10-20", excerpt="20 October 2026"):
    import base64
    manifest = _date_fields_manifest(database, fields=(field,))
    entry = manifest["observations"][0]
    body = entry["capture"]["text"].replace(entry["field_evidence"][field]["excerpt"], excerpt)
    entry["public_fields"][field] = value
    entry["field_evidence"][field].update(value=value, excerpt=excerpt)
    entry["capture"].update(text=body, bytes_base64=base64.b64encode(body.encode()).decode(), sha256=hashlib.sha256(body.encode()).hexdigest())
    staged = _ingest(database, tmp_path, manifest)
    approve_date(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))


def _stage_next_complete_bundle(database, bundle, date_basis="updated_date"):
    result = deepcopy(bundle)
    label = {"updated_date": "Updated", "revision_date": "Revised"}.get(date_basis)
    new = "Flood losses reached 30 billion." + (f" {label}: 20 October 2026" if label else "")
    summary = "Flood losses rose to 30 billion."
    sha = lambda text: hashlib.sha256(text.encode()).hexdigest()
    with sqlite3.connect(database) as db:
        old = db.execute("SELECT markdown_content FROM article_content_versions WHERE content_version_id='body-b'").fetchone()[0]
        db.execute("INSERT INTO article_content_versions VALUES(?,?,?,?,?,'text/html',NULL,'fixture','v1',?)",
            ("body-c", "good", sha(new), new, sha(new), "2026-10-20T01:00:00Z"))
        db.execute("UPDATE articles SET current_content_version_id='body-c' WHERE article_id='good'")
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES('fetch-c','good','https://example.org/good','https://example.org/good','2026-10-20T01:00:00Z','success',200,'body-c')")
        from verified_fixtures import seed_verified_articles
        seed_verified_articles(db, occurrence_ids={"core:good"})
        proof = {"value": "2026-10-20", "date_kind": "updated" if date_basis == "updated_date" else "revision",
            "source_date_precision": "day", "text": "20 October 2026", "content_version_id": "body-c",
            "source_url": "https://example.org/good", "final_url": "https://example.org/good", "captured_at": "2026-10-20T01:00:00Z"}
        result.update(content_version_id="body-c", body_sha256=sha(new), previous_content_version_id="body-b", previous_body_sha256=sha(old),
            summary=summary, summary_sha256=sha(summary), summary_content_version_id="body-c", first_observed_at="2026-10-20T01:00:00Z",
            change_note="Flood loss estimate increased from 20 billion to 30 billion.",
            change_evidence={"basis": "body_comparison", "previous_excerpt": "Flood losses reached 20 billion.", "current_excerpt": "Flood losses reached 30 billion."},
            date_evidence={} if date_basis == "content_version_discovery" else {date_basis: proof})
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = {"summary": summary, "content_update": result}
        candidate = stage_snapshot(db, snapshot)
    return candidate, result


def _assert_date_transition_readers(database, tmp_path, check):
    reader = _reader(database, tmp_path)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            check(reader.article("good"))
            check(next(item for item in reader.articles()["items"] if item["article_id"] == "good"))
            for linked in reader.pdf_articles_all(include_linked=True):
                check(reader.pdf_article(linked["article_id"]))
    root = tmp_path/"static-transition"; (root/"wiki").mkdir(parents=True, exist_ok=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    static = RegistryReader.from_public_export(root)
    check(static.article("good")); check(next(item for item in static.articles()["items"] if item["article_id"] == "good"))
    for linked in static.pdf_articles_all(include_linked=True):
        check(static.pdf_article(linked["article_id"]))
    return reader


@pytest.mark.parametrize("date_basis", ["updated_date", "revision_date", "content_version_discovery"])
def test_complete_update_remains_queryable_after_publication_override(tmp_path, date_basis):
    from test_chat_evidence import owner
    database = _seed(tmp_path)
    candidate, bundle = _stage_complete_bundle(database,
        source_dates=date_basis != "content_version_discovery",
        date_field="revision_date" if date_basis == "revision_date" else "updated_date")
    with sqlite3.connect(database) as db:
        _approve(db, candidate, {"basis": "independent complete update fixture"})
    before = _reader(database, tmp_path).article("good")
    assert before["content_date_basis"] == date_basis
    _ingest(database, tmp_path, _manual(database, fields={"publication_date": "2026-10-22"}))
    def check(item):
        assert item["content_date"] == "2026-10-22" and item["content_date_basis"] == "publication_date"
        assert item["content_date_authority"] == "human_override"
        assert item["content_update"] == bundle and item["summary"] == before["summary"]
    reader = _assert_date_transition_readers(database, tmp_path, check)
    static = RegistryReader.from_public_export(tmp_path/"static-transition")
    for index, current in enumerate((reader, static)):
        directory = tmp_path/f"chat-override-{index}"; directory.mkdir()
        chat = owner(directory); chat.reader_factory = lambda: current
        for wording in ("updated", "added or updated"):
            result = chat.answer(f"What flood losses content was recently {wording} from 2026-10-01 to 2026-10-24?")
            assert [item["evidence_id"] for item in result["sources"]] == ["good"]
        assert not chat.answer("What flood losses content was recently updated from 2026-10-01 to 2026-10-20?")["sources"]


def test_publication_override_without_complete_package_cannot_create_update(tmp_path):
    from test_chat_evidence import owner
    database = _seed(tmp_path)
    _ingest(database, tmp_path, _manual(database, fields={"publication_date": "2026-10-22"}))
    reader = _reader(database, tmp_path)
    assert not reader.article("good").get("content_update")
    directory = tmp_path/"chat-date-only"; directory.mkdir()
    chat = owner(directory); chat.reader_factory = lambda: reader
    assert not chat.answer("What content was recently updated from 2026-10-01 to 2026-10-24?")["sources"]


@pytest.mark.parametrize("source", ["native", "linked"])
@pytest.mark.parametrize("new_body", [False, True])
def test_first_collection_stays_stable_across_refetch_and_new_body(tmp_path, source, new_body):
    from verified_fixtures import seed_verified_articles
    database = _seed(tmp_path, source)
    with sqlite3.connect(database) as db:
        for identity, body, at in [("first-body", "Flood losses reached 10 billion.", "2026-10-02T00:00:00Z"),
                *([("new-body", "Flood losses reached 20 billion.", "2026-10-24T00:00:00Z")] if new_body else [])]:
            sha = hashlib.sha256(body.encode()).hexdigest()
            db.execute("INSERT INTO article_content_versions VALUES(?,?,?,?,?,'text/html',NULL,'fixture','v1',?)",
                (identity, "good", sha, body, sha, at))
        current = "new-body" if new_body else "first-body"
        db.execute("UPDATE articles SET current_content_version_id=? WHERE article_id='good'", (current,))
        for identity, version, at, status, http in [
                ("not-modified", "first-body", "2026-10-01T00:00:00Z", "not_modified", 304),
                ("first", "first-body", "2026-10-02T00:00:00Z", "success", 200),
                ("refetch", current, "2026-10-24T00:00:00Z", "success", 200)]:
            db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES(?,'good','https://example.org/good','https://example.org/good',?,?,?,?)",
                (identity, at, status, http, version))
        seed_verified_articles(db, occurrence_ids={"core:good"} if source == "native" else {"core:good", "occurrence"})
        candidate = stage_snapshot(db, snapshot_entity(db, "article", "good"))
        _approve(db, candidate, {"basis": "independent collection fixture"})
    def check(item):
        assert item["collected_at"] == "2026-10-24T00:00:00Z"
        assert item["first_collected_at"] == "2026-10-02T00:00:00Z"
        assert item["content_date"] == "2026-10-02" and item["content_date_basis"] == "collection_time"
        assert not item.get("content_update")
    reader = _assert_date_transition_readers(database, tmp_path, check)
    page = public_wiki_pages(database, reader=reader, page="article-good.md")["article-good.md"]
    assert "Publication date fallback (first collection date): 2026-10-02" in page
    assert "Collected at: 2026-10-24T00:00:00Z" in page
    assert "Content sort date: 2026-10-02" in page


@pytest.mark.parametrize("field", ["updated_date", "revision_date"])
@pytest.mark.parametrize("source_dates", [False, True])
@pytest.mark.parametrize("source", ["native", "linked"])
def test_same_body_restage_keeps_approved_date_value_evidence_and_verification(tmp_path, field, source_dates, source):
    database = _seed(tmp_path, source)
    sha, bundle = _stage_complete_bundle(database, source_dates=source_dates)
    with sqlite3.connect(database) as db:
        _approve(db, sha, {"basis": "fresh body-b fixture pass"})
    _source_date_correction(database, tmp_path, field)
    before = _reader(database, tmp_path).article("good")
    with sqlite3.connect(database) as db:
        old_candidates = dict(db.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates"))
        old_receipts = dict(db.execute("SELECT review_id,receipt_json FROM registry_reviews"))
        for repeat in range(2):
            snapshot = snapshot_entity(db, "article", "good")
            snapshot["derived_display"] = {"summary": bundle["summary"], "content_update": bundle}
            candidate = stage_snapshot(db, snapshot)
            _approve(db, candidate, {"basis": "fresh repeat body-b fixture pass", "repeat": repeat})
        assert old_candidates.items() <= dict(db.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates")).items()
        assert old_receipts.items() <= dict(db.execute("SELECT review_id,receipt_json FROM registry_reviews")).items()
    def check(item):
        for key in (field, field + "_evidence", field + "_verification"):
            assert item[key] == before[key]
        assert item["content_date"] == "2026-10-20" and item["content_date_basis"] == field
        assert item["content_update"] == bundle and item["content_version_id"] == "body-b"
    _assert_date_transition_readers(database, tmp_path, check)


@pytest.mark.parametrize("scope", ["content_version", "record"])
@pytest.mark.parametrize("field", ["publication_date", "updated_date", "revision_date"])
@pytest.mark.parametrize("date_basis", ["updated_date", "revision_date", "content_version_discovery"])
@pytest.mark.parametrize("source", ["native", "linked"])
def test_new_body_expires_only_version_override_and_uses_its_own_date(tmp_path, scope, field, date_basis, source):
    database = _seed(tmp_path, source)
    stage = _ingest(database, tmp_path, _date_fields_manifest(database, fields=("publication_date",)))
    approve_date(database, tmp_path, _date_review(database, tmp_path, stage["candidate_sha256"]))
    source_publication = _reader(database, tmp_path).article("good")["publication_date_evidence"]
    sha, bundle = _stage_complete_bundle(database)
    with sqlite3.connect(database) as db:
        _approve(db, sha, {"basis": "fresh body-b fixture pass"})
    manifest = _manual(database, fields={field: "2026-10-22"}, scope=scope)
    if scope == "content_version":
        manifest["manual_authorization"]["content_version_id"] = "body-b"
    decision_sha = _ingest(database, tmp_path, manifest)["candidate_sha256"]
    decision = _reader(database, tmp_path).article("good")["manual_date_overrides"][field]
    def check_before(item):
        assert item[field] == item["content_date"] == "2026-10-22"
        assert item["content_date_authority"] == "human_override" and item["content_version_id"] == "body-b"
    _assert_date_transition_readers(database, tmp_path, check_before)
    candidate, current_bundle = _stage_next_complete_bundle(database, bundle, date_basis)
    with sqlite3.connect(database) as db:
        prior_receipt = db.execute("SELECT receipt_json FROM registry_reviews WHERE candidate_sha256=?", (decision_sha,)).fetchone()[0]
        _approve(db, candidate, {"basis": "fresh body-c fixture pass"})
        assert db.execute("SELECT receipt_json FROM registry_reviews WHERE candidate_sha256=?", (decision_sha,)).fetchone()[0] == prior_receipt
    def check(item):
        assert item["content_version_id"] == "body-c" and item["content_update"] == current_bundle
        if scope == "record":
            assert item["manual_date_overrides"][field] == decision and item[field] == "2026-10-22"
            assert item["content_date"] == "2026-10-22" and item["content_date_basis"] == field
            assert item["content_date_authority"] == "human_override"
        else:
            assert field not in item.get("manual_date_overrides", {})
            assert item["publication_date"] == "2026-10-02" and item["publication_date_evidence"] == source_publication
            assert item["content_date"] == "2026-10-20" and item["content_date_basis"] == date_basis
            assert item["content_date_authority"] != "human_override"
            for selected in ("publication_date", "updated_date", "revision_date"):
                assert (item.get(selected + "_evidence") or {}).get("authority") != "human_override"
                assert (item.get(selected + "_verification") or {}).get("authority") != "human_override"
        other = {"article_id": "other", "publication_date": "2026-10-21"}
        expected_first = item["article_id"] if scope == "record" else "other"
        assert sort_articles_by_publication_date([item, other])[0]["article_id"] == expected_first
    reader = _assert_date_transition_readers(database, tmp_path, check)
    page = public_wiki_pages(database, reader=reader, page="article-good.md")["article-good.md"]
    assert "Content sort date: " + ("2026-10-22" if scope == "record" else "2026-10-20") in page


@pytest.mark.parametrize("field", ["updated_date", "revision_date"])
def test_approval_rejects_same_version_candidate_missing_current_source_correction(tmp_path, field):
    from climate_registry.acquisition_review import digest
    database = _seed(tmp_path)
    sha, bundle = _stage_complete_bundle(database, source_dates=False)
    with sqlite3.connect(database) as db:
        _approve(db, sha, {"basis": "fresh body-b fixture pass"})
    _source_date_correction(database, tmp_path, field)
    with sqlite3.connect(database) as db:
        candidate = snapshot_entity(db, "article", "good")
        candidate["derived_display"] = {"summary": bundle["summary"], "content_update": bundle}
        stale = digest(candidate)
        db.execute("INSERT OR IGNORE INTO registry_candidates VALUES(?,?,?,?,?,?)",
            (stale, "article", "good", json.dumps(candidate), "2026-10-24T13:00:00Z", "retained stale fixture"))
        db.execute("UPDATE registry_publication SET latest_candidate_sha256=? WHERE entity_id='good' AND entity_kind='article'", (stale,))
        with pytest.raises(ValueError, match="stale approved article date"):
            _approve(db, stale, {"basis": "must reject stale body-b fixture"})


@pytest.mark.parametrize("field", ["publication_date", "updated_date", "revision_date"])
def test_approval_and_sort_reject_expired_effective_human_date_without_decision_map(tmp_path, field):
    from climate_registry.acquisition_review import digest
    database = _seed(tmp_path)
    sha, bundle = _stage_complete_bundle(database)
    with sqlite3.connect(database) as db:
        _approve(db, sha, {"basis": "fresh body-b fixture pass"})
    manual = _manual(database, fields={field: "2026-10-22"}, scope="content_version")
    manual["manual_authorization"]["content_version_id"] = "body-b"
    _ingest(database, tmp_path, manual)
    prior = _reader(database, tmp_path).article("good")
    candidate, next_bundle = _stage_next_complete_bundle(database, bundle)
    with sqlite3.connect(database) as db:
        stale = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (candidate,)).fetchone()[0])
        for key in (field, field + "_evidence", field + "_verification"):
            stale["derived_display"][key] = prior[key]
        identity = digest(stale)
        db.execute("INSERT INTO registry_candidates VALUES(?,?,?,?,?,?)",
            (identity, "article", "good", json.dumps(stale), "2026-10-24T13:00:00Z", "expired authority fixture"))
        db.execute("UPDATE registry_publication SET latest_candidate_sha256=? WHERE entity_id='good' AND entity_kind='article'", (identity,))
        with pytest.raises(ValueError, match="stale.*date"):
            _approve(db, identity, {"basis": "must reject orphan effective authority"})
    record = {"article_id": "good", "content_version_id": "body-c", **stale["derived_display"]}
    assert content_date_projection(record, stale)["content_date"] == "2026-10-20"


def _revoke_date(database, field):
    manifest = _manual(database, fields={field: "2026-10-22"})
    manifest["manual_authorization"].update(action="revoke", instruction=f"Revoke good {field}.")
    manifest["observations"][0].update(public_fields={field: None}, field_evidence={field: {"text": "revoke", "precision": "unknown"}})
    return manifest


@pytest.mark.parametrize("field", ["updated_date", "revision_date"])
@pytest.mark.parametrize("source", ["native", "linked"])
@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("source_date", [False, True])
def test_revoke_record_override_resumes_current_body_source_and_restage(tmp_path, field, source, replacement, source_date):
    database = _seed(tmp_path, source)
    candidate, bundle = _stage_complete_bundle(database, date_field=field)
    with sqlite3.connect(database) as db:
        _approve(db, candidate, {"basis": "fresh body-b fixture pass"})
    old_decision = _ingest(database, tmp_path, _manual(database, fields={field: "2026-10-22"}))
    stale_revoke = _revoke_date(database, field)
    candidate, bundle = _stage_next_complete_bundle(database, bundle, field if source_date else "content_version_discovery")
    with sqlite3.connect(database) as db:
        _approve(db, candidate, {"basis": "fresh body-c fixture pass"})
        old_snapshot = db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (old_decision["candidate_sha256"],)).fetchone()[0]
        old_receipt = db.execute("SELECT receipt_json FROM registry_reviews WHERE candidate_sha256=?", (old_decision["candidate_sha256"],)).fetchone()[0]
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(RegistryInputError, match="stale"):
        _ingest(database, tmp_path, stale_revoke)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    if replacement:
        _ingest(database, tmp_path, _manual(database, fields={field: "2026-10-23"}))
        underlying = _reader(database, tmp_path).article("good")["manual_date_overrides"][field]["source_effective"]
        assert underlying.get(field) == ("2026-10-20" if source_date else None)
    _ingest(database, tmp_path, _revoke_date(database, field))
    def check(item):
        assert item["content_update"] == bundle and item["content_version_id"] == "body-c"
        assert field not in item.get("manual_date_overrides", {})
        assert item.get(field) == ("2026-10-20" if source_date else None)
        if source_date:
            assert item[field + "_evidence"] == bundle["date_evidence"][field]
        else:
            assert not item.get(field + "_evidence")
        assert not item.get(field + "_verification")
        assert item["content_date"] == "2026-10-20"
        assert item["content_date_basis"] == (field if source_date else "content_version_discovery")
        from agentic_wiki.chat_evidence import article_text
        text = article_text(item, {})
        assert f"{field}: 2026-10-16" not in text and f"{field}: 2026-10-22" not in text
    reader = _assert_date_transition_readers(database, tmp_path, check)
    page = public_wiki_pages(database, reader=reader, page="article-good.md")["article-good.md"]
    assert "Content sort date: 2026-10-20" in page
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = {"summary": bundle["summary"], "content_update": bundle}
        current = stage_snapshot(db, snapshot)
        _approve(db, current, {"basis": "fresh unchanged body-c fixture pass"})
        assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (old_decision["candidate_sha256"],)).fetchone()[0] == old_snapshot
        assert db.execute("SELECT receipt_json FROM registry_reviews WHERE candidate_sha256=?", (old_decision["candidate_sha256"],)).fetchone()[0] == old_receipt
    _assert_date_transition_readers(database, tmp_path, check)


@pytest.mark.parametrize("field", ["updated_date", "revision_date"])
def test_revoke_prefers_current_version_independent_correction_over_bundle_source(tmp_path, field):
    database = _seed(tmp_path)
    candidate, _ = _stage_complete_bundle(database, date_field=field)
    with sqlite3.connect(database) as db:
        _approve(db, candidate, {"basis": "fresh complete fixture pass"})
    _source_date_correction(database, tmp_path, field, value="2026-10-21", excerpt="21 October 2026")
    expected = _reader(database, tmp_path).article("good")
    _ingest(database, tmp_path, _manual(database, fields={field: "2026-10-22"}))
    _ingest(database, tmp_path, _manual(database, fields={field: "2026-10-23"}))
    _ingest(database, tmp_path, _revoke_date(database, field))
    def check(item):
        for key in (field, field + "_evidence", field + "_verification"):
            assert item[key] == expected[key]
        assert item["content_date"] == "2026-10-21" and item["content_date_evidence"] == expected[field + "_evidence"]
    _assert_date_transition_readers(database, tmp_path, check)


def test_legacy_manual_source_basis_frozen_read_and_new_revoke_use_distinct_policy(tmp_path, monkeypatch):
    from climate_registry import owner_dates
    database = _seed(tmp_path)
    candidate, bundle = _stage_complete_bundle(database)
    with sqlite3.connect(database) as db:
        _approve(db, candidate, {"basis": "fresh complete fixture pass"})
    original = owner_dates.date_plan
    def legacy_plan(*args, **kwargs):
        result = original(*args, **kwargs)
        result["plan"].pop("source_date_resolution", None)
        return result
    with monkeypatch.context() as legacy:
        legacy.setattr(owner_dates, "date_plan", legacy_plan)
        decision = _ingest(database, tmp_path, _manual(database, fields={"updated_date": "2026-10-22"}))
    candidate, _ = _stage_next_complete_bundle(database, bundle)
    with sqlite3.connect(database) as db:
        _approve(db, candidate, {"basis": "fresh body-c fixture pass"})
        prior = db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (decision["candidate_sha256"],)).fetchone()[0]
    assert _reader(database, tmp_path).article("good")["manual_date_overrides"]["updated_date"]["source_effective"]["updated_date"] == "2026-10-16"
    _ingest(database, tmp_path, _revoke_date(database, "updated_date"))
    item = _reader(database, tmp_path).article("good")
    assert item["updated_date"] == item["content_date"] == "2026-10-20"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (decision["candidate_sha256"],)).fetchone()[0] == prior
