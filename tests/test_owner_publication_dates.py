"""A date correction inherits approved facts, never reclassifies them."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from climate_registry.acquisition_review import digest
from climate_registry.errors import RegistryInputError
from climate_registry.owner_checks import plan_owner_check, ingest_owner_check, approve_owner_check, owner_review_template
from climate_registry.owner_dates import OVERLAY, DATE_CHECK_VERSION, date_public_view
from climate_registry.publication import stage_entities, export_public_snapshot, snapshot_entity, stage_snapshot
from climate_registry.verification import evaluate_snapshot, permitted_view
from climate_registry.read_api import RegistryReader
from test_verified_publication import _database, _reader, _approve_all, _check
from test_registry_publication import _pdf
from test_owner_checks import _review, _ingest, _editorial_manifest


def _seed(tmp_path, source="native"):
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        keywords = ["approved keyword " + str(i) for i in range(12)]
        db.execute("INSERT INTO article_enrichments(enrichment_id,article_id,article_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('e-good','good','v-good','complete','Summary good','[\"Physical Risk\"]',?,'en','manual','approved-generator','v1','2026-09-01T00:00:00Z')", (json.dumps(keywords),))
        if source != "native":
            _pdf(db, identity="pdf-good", url="https://example.org/" + ("good" if source == "linked" else "pdf-only"),
                occurrence={"summary": "Original PDF observation", "summary_basis": "verbatim_pdf_article_row",
                    "categories": ["Physical Risk"], "keywords": keywords})
            db.execute("UPDATE pdf_intake_articles SET type_safe_classification_json=?", (json.dumps({"label": "article", "reason": "approved classifier reason"}),))
        from verified_fixtures import seed_verified_articles
        seed_verified_articles(db, occurrence_ids={"core:good"} if source == "native"
            else {"core:good", "occurrence"} if source == "linked" else {"occurrence"})
    _approve_all(database)
    if source != "native":
        from climate_registry.publication import _approve as approve_snapshot
        with sqlite3.connect(database) as db:
            snapshot = snapshot_entity(db, "pdf_article", "pdf-good")
            snapshot["derived_display"] = {"categories": ["Physical Risk"], "keywords": keywords,
                "metadata_provenance": {"categories": "approved-source", "keywords": "approved-source"}}
            sha = stage_snapshot(db, snapshot)
            approve_snapshot(db, sha, {"basis": "approved complete legacy metadata"}, status="accepted_legacy")
            from climate_registry.display import refresh_display
            refresh_display(db)
    return database


def _date_manifest(database, *, source="native", date="2026-10-02", date_kind="updated", alternate=False):
    manifest = plan_owner_check(database, "article" if source == "native" else "pdf_article",
        "good" if source == "native" else "pdf-good", publication_date_only=True)
    target = next(item for item in manifest["plan"]["targets"]
        if item["occurrence_id"] == ("core:good" if source == "native" else "occurrence"))
    body = "Title good\nOriginal PDF title\nOfficial record for " + target["source_url"] + "\nLast Updated: " + date
    raw = body.encode()
    actual_url = "https://example.org/research-directory" if alternate else target["source_url"]
    manifest.update(actor={"id": "date-checker", "task_id": "date-check-task"},
        authorization_reference="owner-authorized-date-completion", audit_reference="audit/source",
        checked_at="2026-10-16T12:00:00Z", observations=[{
            "target": target, "access_status": "accessible", "public_fields": {"publication_date": date},
            "original_comparisons": {"publication_date": {"status": "conflict"}} if target["fields"].get("publication_date") else {},
            "excluded_fields": [], "field_evidence": {"publication_date": {"value": date, "excerpt": date,
                "record_context": "Official record for " + target["source_url"], "date_kind": date_kind}},
            "source_identity": {"article_url": target["source_url"], "excerpt": "Official record for " + target["source_url"],
                "reason": "Official record links the exact canonical article."},
            "capture": {"requested_url": actual_url, "final_url": actual_url, "title": "Official article record",
                "captured_at": "2026-10-16T11:59:00Z", "tool": "browser", "text": body,
                "bytes_base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()}}])
    return manifest


def _date_review(database, tmp_path, sha):
    receipt = _review(database, tmp_path, sha)
    receipt["reviewed_at"] = "2026-10-16T12:01:00Z"
    return receipt


def _approve(database, tmp_path, receipt):
    return approve_owner_check(database, receipt, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)


def _item(reader, source):
    return reader.pdf_article("pdf-good") if source == "pdf" else reader.article("good")


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
def test_date_only_inherits_all_approved_facts_and_every_public_reader(tmp_path, source):
    database = _seed(tmp_path, source)
    reader = _reader(database, tmp_path)
    before = _item(reader, source)
    with sqlite3.connect(database) as db:
        original = {table: db.execute("SELECT * FROM " + table).fetchall()
            for table in ("articles", "article_versions", "article_enrichments", "pdf_intake_articles", "pdf_intake_article_occurrences")}
        old_candidates = db.execute("SELECT * FROM registry_candidates").fetchall()
        old_reviews = db.execute("SELECT * FROM registry_reviews").fetchall()
    manifest = _date_manifest(database, source=source, alternate=True)
    staged = _ingest(database, tmp_path, manifest)
    assert _item(reader, source) == before
    assert _ingest(database, tmp_path, manifest)["status"] == "no-op"
    receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
    assert receipt["source_checks"]["nondate_scope_sha256"] == manifest["plan"]["nondate_scope_sha256"]
    _approve(database, tmp_path, receipt)
    assert _approve(database, tmp_path, receipt)["status"] == "no-op"

    def check(current):
        item = _item(current, source)
        assert item["publication_date"] == "2026-10-02"
        for key in ("title", "summary", "categories", "keywords", "type_safe_classification", "enrichment", "metadata_provenance"):
            assert item.get(key) == before.get(key), key
        assert len(item["keywords"]) == 12
        assert item["publication_date_evidence"]["source_url"] == "https://example.org/research-directory"
        assert item["publication_date_verification"]["review_scope"] == "article_publication_date_only"
        if source != "native":
            pdf = current.pdf_article("pdf-good")
            assert pdf["publication_date"] == "2026-10-02"
            assert pdf["occurrences"][0]["publication_date"] == "2026-10-02"
            if source == "pdf":
                assert pdf.get("type_safe_classification") == before.get("type_safe_classification")
    check(reader)
    with reader.public_snapshot(direct=True): check(reader)
    with reader.public_snapshot(): check(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    check(RegistryReader.from_public_export(root))
    with sqlite3.connect(database) as db:
        for table, rows in original.items():
            assert db.execute("SELECT * FROM " + table).fetchall() == rows
        assert db.execute("SELECT * FROM registry_candidates").fetchall()[:len(old_candidates)] == old_candidates
        assert db.execute("SELECT * FROM registry_reviews").fetchall()[:len(old_reviews)] == old_reviews
        latest = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?",
            (manifest["plan"]["entity_kind"], manifest["plan"]["entity_id"])).fetchone()[0]
        assert stage_snapshot(db, snapshot_entity(db, manifest["plan"]["entity_kind"], manifest["plan"]["entity_id"])) == latest


@pytest.mark.parametrize("mutation", ["actor", "binding", "publication", "visibility", "latest", "live"])
def test_date_only_rejects_stale_or_self_review_without_public_changes(tmp_path, mutation):
    database = _seed(tmp_path)
    before = _reader(database, tmp_path).article("good")
    manifest = _date_manifest(database)
    staged = _ingest(database, tmp_path, manifest)
    receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
    with sqlite3.connect(database) as db:
        if mutation == "actor": receipt["actor"] = manifest["actor"]
        elif mutation == "binding": receipt["source_checks"]["date_evidence_sha256"] = "f"*64
        elif mutation == "publication": db.execute("UPDATE registry_publication SET published_candidate_sha256=NULL WHERE entity_id='good'")
        elif mutation == "visibility": db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id='good'")
        elif mutation == "latest": db.execute("UPDATE registry_publication SET latest_candidate_sha256=published_candidate_sha256 WHERE entity_id='good'")
        elif mutation == "live": db.execute("UPDATE article_versions SET observed_summary='Unapproved changed source' WHERE article_id='good'")
        reviews = db.execute("SELECT * FROM registry_reviews").fetchall()
    with pytest.raises(RegistryInputError): _approve(database, tmp_path, receipt)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM registry_reviews").fetchall() == reviews
    if mutation not in {"visibility", "publication"}:
        assert _reader(database, tmp_path).article("good") == before


def test_date_only_frozen_inheritance_is_standalone_and_rejects_cross_snapshot(tmp_path):
    database = _seed(tmp_path)
    staged = _ingest(database, tmp_path, _date_manifest(database))
    receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
    with sqlite3.connect(database) as db:
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (staged["candidate_sha256"],)).fetchone()[0])
    proof = {"candidate_sha256": digest(snapshot), "review_id": digest([digest(snapshot), "pass", receipt]), "status": "pass", "receipt_json": json.dumps(receipt)}
    verdict = evaluate_snapshot(snapshot, reviews=[proof])
    assert verdict["eligible"]
    assert permitted_view(snapshot, verdict)["derived_display"]["publication_date"] == "2026-10-02"
    for mutation in ("base", "receipt", "projection", "date", "scope"):
        bad = deepcopy(snapshot)
        overlay = bad[OVERLAY]
        if mutation == "base": bad["tables"]["article_versions"][0]["observed_title"] = "Cross candidate title"
        elif mutation == "receipt": overlay["manifest"]["plan"]["baseline_reviews"][0]["candidate_sha256"] = "a"*64
        elif mutation == "projection": overlay["manifest"]["plan"]["baseline_public_projection_sha256"] = "b"*64
        elif mutation == "date": overlay["manifest"]["observations"][0]["public_fields"]["publication_date"] = "2026-10-03"
        else: overlay["manifest"]["plan"]["nondate_scope_sha256"] = "c"*64
        assert not evaluate_snapshot(bad, reviews=[proof])["eligible"]
    assert not evaluate_snapshot(snapshot)["eligible"]


def test_date_only_batch_preflight_rolls_back_dates_and_display(tmp_path):
    database = _seed(tmp_path)
    _check(database, tmp_path, ids={"core:unchecked"}, body="Title unchecked. Summary unchecked.")
    _approve_all(database)
    first = _date_manifest(database)
    second = deepcopy(first)
    planned = plan_owner_check(database, "article", "unchecked", publication_date_only=True)
    second["plan"] = planned["plan"]
    second["observations"][0]["target"] = planned["plan"]["targets"][0]
    target = second["observations"][0]["target"]
    second["observations"][0]["source_identity"]["article_url"] = target["source_url"]
    entry = second["observations"][0]
    for key in ("requested_url", "final_url"):
        entry["capture"][key] = target["source_url"]
    entry["capture"]["text"] = entry["capture"]["text"].replace("https://example.org/good", target["source_url"])
    raw = entry["capture"]["text"].encode()
    entry["capture"].update(bytes_base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
    entry["source_identity"]["excerpt"] = "Official record for " + target["source_url"]
    entry["field_evidence"]["publication_date"]["record_context"] = entry["source_identity"]["excerpt"]
    staged = ingest_owner_check(database, [first, second], queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    receipts = [_date_review(database, tmp_path, item["candidate_sha256"]) for item in staged["results"]]
    receipts[1]["source_checks"]["nondate_scope_sha256"] = "bad"
    with sqlite3.connect(database) as db:
        before = {table: db.execute("SELECT * FROM " + table).fetchall() for table in ("registry_publication", "registry_reviews", "display_articles")}
    with pytest.raises(RegistryInputError): _approve(database, tmp_path, receipts)
    with sqlite3.connect(database) as db:
        for table, rows in before.items(): assert db.execute("SELECT * FROM " + table).fetchall() == rows


def test_date_only_preserves_previously_approved_owner_editorial_scope(tmp_path):
    database = _database(tmp_path)
    staged = _ingest(database, tmp_path, _editorial_manifest(database))
    _approve(database, tmp_path, _review(database, tmp_path, staged["candidate_sha256"]))
    reader = _reader(database, tmp_path)
    before = reader.article("good")
    staged = _ingest(database, tmp_path, _date_manifest(database))
    _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    after = reader.article("good")
    assert after["owner_agent_verification"] == before["owner_agent_verification"]
    assert after["categories"] == before["categories"] and after["keywords"] == before["keywords"]
    assert after["publication_date"] == "2026-10-02"


@pytest.mark.parametrize("mutation", ["title", "no_identity", "date_kind", "nonliteral", "capture_hash"])
def test_date_only_manifest_rejects_nondate_and_unbound_evidence(tmp_path, mutation):
    database = _seed(tmp_path)
    manifest = _date_manifest(database)
    entry = manifest["observations"][0]
    if mutation == "title": entry["public_fields"]["title"] = "replacement"
    elif mutation == "no_identity": entry.pop("source_identity")
    elif mutation == "date_kind": entry["field_evidence"]["publication_date"]["date_kind"] = "collection"
    elif mutation == "nonliteral": entry["field_evidence"]["publication_date"]["excerpt"] = "3 October 2026"
    else: entry["capture"]["sha256"] = "a"*64
    with pytest.raises(RegistryInputError): _ingest(database, tmp_path, manifest)


def test_date_only_subsequent_correction_retains_frozen_approved_chain(tmp_path):
    database = _seed(tmp_path, "pdf")
    original = _reader(database, tmp_path).pdf_article("pdf-good")
    first = _ingest(database, tmp_path, _date_manifest(database, source="pdf"))
    _approve(database, tmp_path, _date_review(database, tmp_path, first["candidate_sha256"]))
    second = _ingest(database, tmp_path, _date_manifest(database, source="pdf", date="2026-10-03", date_kind="published"))
    _approve(database, tmp_path, _date_review(database, tmp_path, second["candidate_sha256"]))
    current = _reader(database, tmp_path).pdf_article("pdf-good")
    assert current["publication_date"] == "2026-10-03"
    assert current["publication_date_evidence"]["date_kind"] == "published"
    assert current["keywords"] == original["keywords"]
    assert current["type_safe_classification"] == original["type_safe_classification"]


def test_date_only_refuses_reader_derived_nondate_dto_drift(tmp_path, monkeypatch):
    database = _seed(tmp_path)
    before = _reader(database, tmp_path).article("good")
    staged = _ingest(database, tmp_path, _date_manifest(database))
    receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
    original = RegistryReader._article_from_snapshot
    def changed(self, snapshot):
        payload = original(self, snapshot)
        if snapshot.get("derived_display", {}).get("publication_date_verification"):
            payload["summary"] = "Unexpected non-date reader behavior"
        return payload
    monkeypatch.setattr(RegistryReader, "_article_from_snapshot", changed)
    with pytest.raises(RegistryInputError, match="non-date public DTOs"):
        _approve(database, tmp_path, receipt)
    assert _reader(database, tmp_path).article("good") == before


@pytest.mark.skipif(__import__('os').name == 'nt', reason='Actual API imports POSIX management')
@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
def test_date_only_actual_api_uses_same_approved_date_and_metadata(tmp_path, monkeypatch, source):
    import api_server
    from fastapi.testclient import TestClient
    database = _seed(tmp_path, source)
    before = _item(_reader(database, tmp_path), source)
    staged = _ingest(database, tmp_path, _date_manifest(database, source=source))
    _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    api_server._clear_registry_list_cache()
    endpoint = "/api/registry/pdf-intake/articles/pdf-good" if source == "pdf" else "/api/registry/articles/good"
    response = TestClient(api_server.app).get(endpoint)
    assert response.status_code == 200
    payload = response.json()
    assert payload["publication_date"] == "2026-10-02"
    assert payload["keywords"] == before["keywords"] and payload["categories"] == before["categories"]


@pytest.mark.parametrize("literal,expected", [("23rd September 2026", "2026-09-23"),
    ("21st of September 2026", "2026-09-21"), ("26 June, 2026", "2026-06-26"),
    ("06-Nov-2018", "2018-11-06")])
def test_date_only_retains_literal_ordinal_byline_date(tmp_path, literal, expected):
    database = _seed(tmp_path)
    manifest = _date_manifest(database, date=expected)
    entry = manifest["observations"][0]
    raw = entry["capture"]["text"].replace(expected, literal).encode()
    entry["capture"].update(text=raw.decode(), bytes_base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
    entry["field_evidence"]["publication_date"]["excerpt"] = literal
    staged = _ingest(database, tmp_path, manifest)
    _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    item = _reader(database, tmp_path).article("good")
    assert item["publication_date"] == expected
    assert item["publication_date_evidence"]["text"] == literal


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
def test_newer_date_check_never_replaces_t1_terminal_or_requeues_content(tmp_path, monkeypatch, source):
    from climate_registry import information_checks as checks
    import verified_fixtures
    monkeypatch.setattr(checks, "_now", lambda: "2026-10-15T12:00:00Z")
    monkeypatch.setattr(verified_fixtures, "_now", lambda: "2026-10-15T12:00:00Z")
    monkeypatch.setattr(checks, "deterministic_enrichment", lambda body: {
        "summary": "Approved source summary", "categories": ["Physical Risk"], "keywords": ["risk", "flood", "insurance"]})
    database = _seed(tmp_path, source)
    ids = {"core:good"} if source == "native" else {"occurrence"}
    targets = checks.check_targets(database, "articles", ids)
    calls = []
    def forbid_fetch(identity, url):
        calls.append(identity)
        raise AssertionError("date-only completion must not fetch or re-review approved content")
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        verified = checks._currently_verified_targets(db, "article", targets)
        latest = checks._latest_target_attempts(db, "article", targets)
        assert all(not json.loads(row["packet_json"]).get("enrichment_error") for row in latest.values())
        runs = [dict(row) for row in db.execute("SELECT * FROM article_check_runs")]
        raw_item = None
        if source != "native":
            row = db.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='occurrence'").fetchone()
            raw_item = {**json.loads(row["occurrence_json"]), **dict(row)}
            raw_item.pop("occurrence_json")
            original_state = checks.latest_checks(db, "articles", raw_item)
            frozen_state = checks.latest_checks_from_attempts("articles", raw_item,
                [dict(row) for row in db.execute("SELECT * FROM article_check_attempts")])
    assert verified == {(target["occurrence_id"], target["source_url"]) for target in targets}
    manifest = _date_manifest(database, source=source)
    staged = _ingest(database, tmp_path, manifest)
    for approved in (False, True):
        if approved:
            _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
        with sqlite3.connect(database) as db:
            db.row_factory = sqlite3.Row
            assert checks._currently_verified_targets(db, "article", targets) == verified
            assert checks._latest_target_attempts(db, "article", targets) == latest
            for run in runs:
                assert dict(db.execute("SELECT * FROM article_check_runs WHERE run_id=?", (run["run_id"],)).fetchone()) == run
            if raw_item:
                assert checks.latest_checks(db, "articles", raw_item) == original_state
                assert checks.latest_checks_from_attempts("articles", raw_item,
                    [dict(row) for row in db.execute("SELECT * FROM article_check_attempts")]) == frozen_state
        for incremental in (False, True):
            outcome = checks.run_checks(database, kind="articles", backup_dir=tmp_path/"backups",
                occurrence_ids=ids, incremental=incremental, fetcher=forbid_fetch,
                verifier=lambda *args: pytest.fail("date completion must not rerun semantic review"))
            assert outcome["item_count"] == 0 and calls == []


def test_date_only_does_not_guess_ambiguous_slash_date(tmp_path):
    database = _seed(tmp_path)
    manifest = _date_manifest(database, date="2025-11-05")
    entry = manifest["observations"][0]
    literal = "11/05/2025"
    raw = entry["capture"]["text"].replace("2025-11-05", literal).encode()
    entry["capture"].update(text=raw.decode(), bytes_base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
    entry["field_evidence"]["publication_date"]["excerpt"] = literal
    with pytest.raises(RegistryInputError, match="explicit matching date_format"):
        _ingest(database, tmp_path, manifest)


@pytest.mark.parametrize("literal,expected,date_format", [
    ("2026/09/21", "2026-09-21", None), ("AUG. 24, 2026", "2026-08-24", None),
    ("Sep. 23, 2026", "2026-09-23", None), ("Sept. 23, 2026", "2026-09-23", None),
    ("May. 23, 2026", "2026-05-23", None),
    ("11/05/2025", "2025-11-05", "MM/DD/YYYY"), ("11/05/2025", "2025-05-11", "DD/MM/YYYY"),
    ("6/26/2026", "2026-06-26", "MM/DD/YYYY"), ("26/6/2026", "2026-06-26", "DD/MM/YYYY")])
def test_date_only_explicit_source_formats_remain_bound_and_literal(tmp_path, literal, expected, date_format):
    database = _seed(tmp_path)
    manifest = _date_manifest(database, date=expected)
    entry = manifest["observations"][0]
    raw = entry["capture"]["text"].replace(expected, literal).encode()
    entry["capture"].update(text=raw.decode(), bytes_base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
    evidence = entry["field_evidence"]["publication_date"]
    evidence["excerpt"] = literal
    if date_format:
        evidence["date_format"] = date_format
    staged = _ingest(database, tmp_path, manifest)
    review = _date_review(database, tmp_path, staged["candidate_sha256"])
    assert review["source_checks"]["date_evidence_sha256"] == digest(manifest["observations"])
    _approve(database, tmp_path, review)
    item = _reader(database, tmp_path).article("good")
    assert item["publication_date"] == expected
    assert item["publication_date_evidence"]["text"] == literal
    assert item["publication_date_evidence"].get("date_format") == date_format


@pytest.mark.parametrize("literal,date_format,expected", [
    ("11/05/2025", None, "2025-11-05"), ("11/05/2025", "YYYY/DD/MM", "2025-11-05"),
    ("11/05/2025", "DD/MM/YYYY", "2025-11-05"), ("02/30/2026", "MM/DD/YYYY", "2026-02-28"),
    ("2026/06/26", "MM/DD/YYYY", "2026-06-26"), ("June 26 2026", "MM/DD/YYYY", "2026-06-26")])
def test_date_only_rejects_missing_invalid_or_mismatched_source_format(tmp_path, literal, date_format, expected):
    database = _seed(tmp_path)
    manifest = _date_manifest(database, date=expected)
    entry = manifest["observations"][0]
    raw = entry["capture"]["text"].replace(expected, literal).encode()
    entry["capture"].update(text=raw.decode(), bytes_base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
    evidence = entry["field_evidence"]["publication_date"]
    evidence["excerpt"] = literal
    if date_format:
        evidence["date_format"] = date_format
    with pytest.raises(RegistryInputError):
        _ingest(database, tmp_path, manifest)


def test_date_only_skips_native_queue_and_rejects_generic_or_legacy_promotion(tmp_path, monkeypatch):
    from climate_registry.publication import prepare_review, information_ready, _approve as approve_snapshot
    from climate_registry.acquisition_review import claim_review
    database = _seed(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    before = _reader(database, tmp_path).article("good")
    staged = _ingest(database, tmp_path, _date_manifest(database))
    sha = staged["candidate_sha256"]
    root = tmp_path/"native-review"
    packet = prepare_review(database, root)
    assert all(item["candidate_sha256"] != sha for item in packet["candidates"])
    with pytest.raises(ValueError, match="no current T1-completed candidates"):
        claim_review(root, packet, session_id="cron_date_native", execution_id="cron_date_native",
            hermes_database=tmp_path/"unused-hermes.sqlite3", reviewer="native")
    with sqlite3.connect(database) as db:
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
        assert not information_ready(db, snapshot)
        reviews = db.execute("SELECT * FROM registry_reviews").fetchall()
        pointers = db.execute("SELECT * FROM registry_publication").fetchall()
        for status in ("pass", "accepted_legacy"):
            with pytest.raises(ValueError, match="date.*review"):
                approve_snapshot(db, sha, {"basis": "ordinary native or legacy review"}, status=status)
        assert db.execute("SELECT * FROM registry_reviews").fetchall() == reviews
        assert db.execute("SELECT * FROM registry_publication").fetchall() == pointers
    assert _reader(database, tmp_path).article("good") == before
    _approve(database, tmp_path, _date_review(database, tmp_path, sha))
    assert _reader(database, tmp_path).article("good")["publication_date"] == "2026-10-02"


def test_inflight_native_review_cannot_publish_date_scope_without_date_receipt(tmp_path, monkeypatch):
    import climate_registry.publication as publication
    from climate_registry.acquisition_review import claim_review
    database = _seed(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    before = _reader(database, tmp_path).article("good")
    staged = _ingest(database, tmp_path, _date_manifest(database))
    sha = staged["candidate_sha256"]
    root, hermes = tmp_path/"native-review", tmp_path/"hermes.sqlite3"
    session = "cron_date_native_inflight"
    # Model a real claim already issued by the pre-fix application's readiness gate.
    original_ready = publication.information_ready
    with monkeypatch.context() as old_gate:
        old_gate.setattr(publication, "information_ready", lambda connection, snapshot, **kwargs:
            OVERLAY in snapshot or original_ready(connection, snapshot, **kwargs))
        packet = publication.prepare_review(database, root)
        assert [item["candidate_sha256"] for item in packet["candidates"]] == [sha]
        with sqlite3.connect(hermes) as db:
            db.execute("CREATE TABLE sessions(id TEXT,source TEXT,end_reason TEXT)")
            db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,tool_calls TEXT,tool_call_id TEXT,content TEXT)")
            db.execute("INSERT INTO sessions VALUES(?, 'cron', NULL)", (session,))
            candidate = packet["candidates"][0]
            call = {"id": "read-date", "function": {"name": "read_file", "arguments": json.dumps({"path": candidate["snapshot_path"]})}}
            db.execute("INSERT INTO messages(session_id,role,tool_calls) VALUES(?, 'assistant', ?)", (session, json.dumps([call])))
            db.execute("INSERT INTO messages(session_id,role,tool_call_id,content) VALUES(?, 'tool','read-date',?)",
                (session, Path(candidate["snapshot_path"]).read_text(encoding="utf-8")))
        claim = claim_review(root, packet, session_id=session, execution_id=session, hermes_database=hermes, reviewer="native")
    with sqlite3.connect(database) as db:
        pointers = db.execute("SELECT * FROM registry_publication").fetchall()
    outcome = publication.review_candidates(root, claim["token"], {
        sha: {"candidate_sha256": sha, "status": "pass", "reason": "Actual complete native snapshot inspection"}})
    assert outcome["reviewed"] == 1
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM registry_publication").fetchall() == pointers
        assert db.execute("SELECT status FROM registry_reviews WHERE review_id=?", (outcome["review_ids"][0],)).fetchone()[0] == "needs_correction"
    assert _reader(database, tmp_path).article("good") == before
    _approve(database, tmp_path, _date_review(database, tmp_path, sha))
    assert _reader(database, tmp_path).article("good")["publication_date"] == "2026-10-02"


@pytest.mark.parametrize("publication_day,collections", [
    ("2026-10-02", {"good": "2026-09-01T12:00:00Z", "unchecked": "2026-10-01T12:00:00Z"}),
    (None, {"good": "2026-10-02T12:00:00Z"}),
    (None, {}),
])
def test_materialized_core_pages_keep_direct_dates_order_and_collection_fallback(tmp_path, publication_day, collections):
    database = _seed(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE articles SET last_seen='2026-10-01' WHERE article_id='good'")
        db.execute("UPDATE articles SET last_seen='2026-10-20' WHERE article_id='unchecked'")
        for identity, collected_at in collections.items():
            body = "Title " + identity + ". Summary " + identity + "."
            body_sha = hashlib.sha256(body.encode()).hexdigest()
            content_id = "collected-content-" + identity
            url = "https://example.org/" + identity
            db.execute("INSERT INTO article_content_versions VALUES(?,?,?,?,?,'text/markdown',?,'fixture','v1',?)",
                (content_id, identity, body_sha, body, body_sha, len(body), collected_at))
            db.execute("UPDATE articles SET current_content_version_id=? WHERE article_id=?", (content_id, identity))
            db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES(?,?,?,?,?,'success',200,?)",
                ("collected-fetch-" + identity, identity, url, url, collected_at, content_id))
    for identity in ("good", "unchecked"):
        _check(database, tmp_path, ids={"core:" + identity}, body="Title " + identity + ". Summary " + identity + ".")
    _approve_all(database)
    if publication_day:
        staged = _ingest(database, tmp_path, _date_manifest(database, date=publication_day))
        _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    reader = _reader(database, tmp_path)
    direct = [reader.articles(page=page, page_size=1) for page in (1, 2)]
    with reader.public_snapshot():
        materialized = [reader.articles(page=page, page_size=1) for page in (1, 2)]
        assert reader.articles(source="s")["pagination"]["total"] == 2
        assert [item["article_id"] for item in reader.articles(query="unchecked")["items"]] == ["unchecked"]
    for pages in (direct, materialized):
        assert [page["items"][0]["article_id"] for page in pages] == ["good", "unchecked"]
        for number, page in enumerate(pages, 1):
            assert page["pagination"] == {"page": number, "page_size": 1, "total": 2, "pages": 2}
            item = page["items"][0]
            assert item["publication_date"] == (publication_day if item["article_id"] == "good" else None)
            assert item["collected_at"] == collections.get(item["article_id"])
    for direct_page, projected_page in zip(direct, materialized):
        actual, expected = projected_page["items"][0], direct_page["items"][0]
        assert actual == {key: expected[key] for key in actual}
    private = RegistryReader(database, repository_root=tmp_path/"app", public=False)
    assert [private.articles(page=page, page_size=1)["items"][0]["article_id"] for page in (1, 2)] == ["unchecked", "good"]


@pytest.mark.parametrize("publication_day", ["2026-10-02", None])
def test_pdf_pages_use_public_dates_and_keep_private_discovery_order(tmp_path, publication_day):
    database = _database(tmp_path)
    first_collection = "2026-09-01T12:00:00Z" if publication_day else "2026-10-02T12:00:00Z"
    with sqlite3.connect(database) as db:
        for identity, occurrence, url, document, report_day, collected_at in (
            ("pdf-good", "occurrence", "https://example.org/pdf-only", "f"*64, "2026-10-01", first_collection),
            ("pdf-a-recent", "recent-occurrence", "https://example.org/pdf-recent", "b"*64, "2026-10-20", "2026-10-01T12:00:00Z"),
        ):
            db.execute("INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at) VALUES(?,'/evidence/source.pdf','source.pdf','application/pdf',1,?,'{}',?)",
                (document, "e"*64, collected_at))
            db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES(?,?,'Original PDF title',?)",
                (identity, url, collected_at))
            db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,2,?,?,NULL,?,?,?)",
                (occurrence, identity, document, url, report_day, "d"*64, "c"*64,
                    json.dumps({"title": "Original PDF title", "report_date": report_day,
                        "summary": "Original PDF observation", "summary_basis": "verbatim_pdf_article_row"})))
        from verified_fixtures import seed_verified_articles
        seed_verified_articles(db, occurrence_ids={"occurrence", "recent-occurrence"})
    _approve_all(database)
    if publication_day:
        staged = _ingest(database, tmp_path, _date_manifest(database, source="pdf", date=publication_day))
        _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    reader = _reader(database, tmp_path)
    direct = [reader.pdf_articles(page=page, page_size=1) for page in (1, 2)]
    with reader.public_snapshot():
        materialized = [reader.pdf_articles(page=page, page_size=1) for page in (1, 2)]
    for pages in (direct, materialized):
        assert [page["items"][0]["article_id"] for page in pages] == ["pdf-good", "pdf-a-recent"]
        assert pages[0]["items"][0]["publication_date"] == publication_day
        assert pages[0]["items"][0]["collected_at"] == first_collection
        for number, page in enumerate(pages, 1):
            assert page["pagination"] == {"page": number, "page_size": 1, "total": 2, "pages": 2}
    private = RegistryReader(database, repository_root=tmp_path/"app", public=False)
    raw_order = [item["article_id"] for item in private.pdf_articles_all()]
    assert raw_order == ["pdf-a-recent", "pdf-good"]
    assert [private.pdf_articles(page=page, page_size=1)["items"][0]["article_id"] for page in (1, 2)] == raw_order
