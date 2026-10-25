"""G3 scope and publication races, using the existing Registry writer fixtures."""
import hashlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from climate_registry.acquisition_review import digest
from climate_registry.publication import _approve, snapshot_entity, stage_snapshot
from test_article_content_dates import _bundle_snapshot, _manual, _stage_complete_bundle, ordered_source_check_fixture_clock
from test_owner_publication_dates import _seed
from test_owner_checks import _ingest, _editorial_manifest, _manifest, _review, approve_owner_check
from test_verified_publication import _reader
from verified_fixtures import seed_verified_articles

CONTENT_SCOPE = "complete_article_content_update"


def _native_contract_review(database, tmp_path, monkeypatch, *, mutation=None):
    from climate_registry.acquisition_review import claim_review
    from climate_registry.publication import prepare_review, review_candidates
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    root, hermes, session = tmp_path/"native-review", tmp_path/"hermes.sqlite3", "cron_g3_exact_content_review"
    packet = prepare_review(database, root)
    with sqlite3.connect(hermes) as db:
        db.execute("CREATE TABLE sessions(id TEXT,source TEXT,end_reason TEXT)")
        db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,tool_calls TEXT,tool_call_id TEXT,content TEXT)")
        db.execute("INSERT INTO sessions VALUES(?,'cron',NULL)", (session,))
        for item in packet["candidates"]:
            call = item["candidate_sha256"]
            path = item["snapshot_path"]
            db.execute("INSERT INTO messages(session_id,role,tool_calls) VALUES(?,'assistant',?)",
                (session, json.dumps([{"id": call, "function": {"name": "read_file", "arguments": json.dumps({"path": path})}}])))
            db.execute("INSERT INTO messages(session_id,role,tool_call_id,content) VALUES(?,'tool',?,?)",
                (session, call, Path(path).read_text(encoding="utf-8")))
    claim = claim_review(root, packet, session_id=session, execution_id=session, hermes_database=hermes,
        reviewer="source-checker" if mutation == "source_actor" else "independent native reviewer")
    conclusions = {item["candidate_sha256"]: {"candidate_sha256": item["candidate_sha256"], "status": "pass",
        "reason": "Complete source, content, summary and date evidence inspected in this exact candidate.",
        "enrichment_contract_sha256": item["enrichment_contract_sha256"]} for item in packet["candidates"]}
    target = next(item for item in packet["candidates"] if item["entity_id"] == "good")
    if mutation and mutation != "source_actor":
        conclusions[target["candidate_sha256"]][mutation] = "0" * 64
    return review_candidates(root, claim["token"], conclusions)


@pytest.mark.parametrize("editorial", [False, True])
@pytest.mark.parametrize("mutation", [None, "candidate_sha256", "enrichment_contract_sha256", "source_actor"])
def test_full_g3_owner_review_keeps_exact_candidate_public(tmp_path, monkeypatch, editorial, mutation):
    from climate_registry.verification import evaluate_snapshot, review_context
    database, a = _published_a(tmp_path)
    before = _reader(database, tmp_path).article("good")
    _, pending = _stage_update(database)
    manifest = (_editorial_manifest if editorial else _manifest)(database)
    from climate_registry.information_checks import _now
    manifest["checked_at"] = _now()
    _ingest(database, tmp_path, manifest)
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = deepcopy(pending["derived_display"])
        sha = stage_snapshot(db, snapshot, enrichment_scope=CONTENT_SCOPE)
    assert _reader(database, tmp_path).article("good")["published_candidate_sha256"] == a
    if mutation:
        if mutation == "source_actor":
            result = _native_contract_review(database, tmp_path, monkeypatch, mutation=mutation)
            with sqlite3.connect(database) as db:
                assert db.execute("SELECT status FROM registry_reviews WHERE review_id=?", (result["review_ids"][0],)).fetchone()[0] == "needs_correction"
        else:
            with pytest.raises(ValueError, match="exact candidate|exact complete enrichment contract"):
                _native_contract_review(database, tmp_path, monkeypatch, mutation=mutation)
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good' AND entity_kind='article'").fetchone()[0] == a
            assert not db.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=? AND status='pass'", (sha,)).fetchone()
        assert _reader(database, tmp_path).article("good") == before
        return
    result = _native_contract_review(database, tmp_path, monkeypatch)
    assert result["reviewed"] >= 1
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_id='good' AND entity_kind='article'").fetchone()[0] == sha
        frozen = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
        verdict = evaluate_snapshot(frozen, reviews=review_context(db)[sha])
        assert verdict["eligible"] is True, verdict["reasons"]
    reader = _reader(database, tmp_path)
    assert "good" in {row["article_id"] for row in reader.articles()["items"]}
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["published_candidate_sha256"] == sha
            assert actual["summary"] == pending["derived_display"]["content_update"]["summary"]
            assert actual["content_version_id"] == "body-b"
            assert actual["owner_agent_verification"]["review_id"] in result["review_ids"]
    _assert_native_content_api(reader, monkeypatch, sha, pending["derived_display"]["content_update"]["summary"])


@pytest.mark.parametrize("explicit", [True, False])
def test_initial_summary_survives_real_native_review_only_for_its_declared_scope(tmp_path, monkeypatch, explicit):
    from climate_registry.enrichment_contract import INITIAL_SCOPE, validate_input
    database = _seed(tmp_path)
    summary = "Flood losses reached 10 billion."
    with sqlite3.connect(database) as db:
        _insert_body(db, "body-a", summary, "2026-10-02T00:00:00Z")
        db.execute("INSERT INTO article_enrichments(enrichment_id,article_id,content_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('initial-exact','good','body-a','complete',?,'[\"Physical Risk\"]','[\"flood\"]','en','manual','fixture','v1','2026-10-02T00:00:00Z')", (summary,))
        seed_verified_articles(db, occurrence_ids={"core:good"})
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = {"summary": summary}
    assert snapshot["information_targets"][0]["fields"]["summary"] == "Summary good"
    if explicit:
        assert validate_input(database, snapshot, INITIAL_SCOPE)["status"] == "valid"
    with sqlite3.connect(database) as db:
        sha = stage_snapshot(db, snapshot, **({"enrichment_scope": INITIAL_SCOPE} if explicit else {}))
    _native_contract_review(database, tmp_path, monkeypatch)
    if explicit:
        from climate_registry.verification import evaluate_snapshot, review_context
        with sqlite3.connect(database) as db:
            approved = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
            proof = evaluate_snapshot(approved, reviews=review_context(db)[sha])["reviewed_content"]["initial_summary"]
            assert proof["content_version_id"] == "body-a"
            assert proof["summary_sha256"] == hashlib.sha256(summary.encode()).hexdigest()
            assert proof["body_sha256"] == hashlib.sha256(summary.encode()).hexdigest()
    reader = _reader(database, tmp_path)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["published_candidate_sha256"] == sha
            assert actual["summary"] == (summary if explicit else "Summary good")
    _assert_native_content_api(reader, monkeypatch, sha, summary if explicit else "Summary good")


def _assert_native_content_api(reader, monkeypatch, sha, summary, *, categories=None, keywords=None):
    from fastapi.testclient import TestClient
    import api_server
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    api_server._clear_registry_list_cache()
    client = TestClient(api_server.app)
    detail = client.get("/api/registry/articles/good")
    assert detail.status_code == 200
    assert detail.json()["published_candidate_sha256"] == sha
    assert detail.json()["summary"] == summary
    if categories is not None:
        assert detail.json()["categories"] == categories
    if keywords is not None:
        assert detail.json()["keywords"] == keywords
    listing = client.get("/api/registry/articles?page=1&page_size=100&include_pdf=true")
    assert listing.status_code == 200
    actual = next(row for row in listing.json()["items"] if row["article_id"] == "good")
    assert actual["summary"] == summary
    if categories is not None:
        category_response = client.get("/api/registry/article-categories")
        assert category_response.status_code == 200
        category_item = next(row for row in category_response.json()["items"] if row["article_id"] == "good")
        assert category_item["categories"] == categories


@pytest.mark.parametrize("source", ["v1", "v2_hold", "v2_new"])
def test_full_g3_review_inherits_only_approved_owner_editorial_fields(tmp_path, monkeypatch, source):
    from datetime import datetime, timedelta, timezone
    from climate_registry.information_checks import _now
    database, _ = _published_a(tmp_path)
    manifest = _editorial_manifest(database)
    manifest["checked_at"] = _now()
    a = _ingest(database, tmp_path, manifest)["candidate_sha256"]
    receipt = _review(database, tmp_path, a)
    receipt["reviewed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    approve_owner_check(database, receipt, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    before = _reader(database, tmp_path).article("good")
    assert before["categories"] == ["Physical Risk"]
    assert before["keywords"] == ["flood", "exposure", "study region"]
    _, pending = _stage_update(database)
    manifest = (_manifest(database) if source == "v1" else
        _editorial_manifest(database, disposition="hold" if source == "v2_hold" else "complete"))
    if source == "v2_new":
        metadata = manifest["observations"][0]["editorial_metadata"]
        metadata["keywords"] = ["flood exposure", "study region", "flood"]
        proof = metadata["evidence"]["keywords"][0]
        metadata["evidence"]["keywords"] = [{**proof, "value": value} for value in metadata["keywords"]]
    manifest["checked_at"] = _now()
    _ingest(database, tmp_path, manifest)
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = deepcopy(pending["derived_display"])
        sha = stage_snapshot(db, snapshot, enrichment_scope=CONTENT_SCOPE)
        assert snapshot["derived_display"]["categories"] == before["categories"]
        assert snapshot["derived_display"]["keywords"] == before["keywords"]
    _native_contract_review(database, tmp_path, monkeypatch)
    reader = _reader(database, tmp_path)
    expected_keywords = manifest["observations"][0]["editorial_metadata"]["keywords"] if source == "v2_new" else before["keywords"]
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["published_candidate_sha256"] == sha
            assert actual["categories"] == before["categories"]
            assert actual["keywords"] == expected_keywords
            if source != "v2_new":
                assert actual["editorial_metadata_provenance"] == before["editorial_metadata_provenance"]
    _assert_native_content_api(reader, monkeypatch, sha, pending["derived_display"]["summary"],
        categories=before["categories"], keywords=expected_keywords)


@pytest.mark.parametrize("hold", [False, True])
def test_full_g3_source_only_review_still_excludes_unreviewed_legacy_taxonomy(tmp_path, monkeypatch, hold):
    from climate_registry.information_checks import _now
    database, _ = _published_a(tmp_path)
    _, pending = _stage_update(database)
    manifest = _editorial_manifest(database, disposition="hold") if hold else _manifest(database)
    manifest["checked_at"] = _now()
    _ingest(database, tmp_path, manifest)
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = deepcopy(pending["derived_display"])
        sha = stage_snapshot(db, snapshot, enrichment_scope=CONTENT_SCOPE)
        assert snapshot["derived_display"]["categories"]
        assert "editorial" not in snapshot["enrichment_scope"]["approval_inheritance_baseline"]
    _native_contract_review(database, tmp_path, monkeypatch)
    reader = _reader(database, tmp_path)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["published_candidate_sha256"] == sha
            assert actual["categories"] == [] and actual["keywords"] == []
    _assert_native_content_api(reader, monkeypatch, sha, pending["derived_display"]["summary"], categories=[], keywords=[])


def _category_only_plan(database, path, reader, categories):
    from climate_monitor.taxonomy import load_article_taxonomy
    taxonomy = load_article_taxonomy()
    payload = reader.article("good")
    with sqlite3.connect(database) as db:
        binding = db.execute("SELECT is_visible,published_candidate_sha256,latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()
    plan = {"schema_version": "registry-category-correction.v1",
        "taxonomy": {"taxonomy_id": taxonomy.taxonomy_id, "sha256": taxonomy.sha256, "definition_yaml": taxonomy.definition_yaml},
        "records": [{"entity_kind": "article", "entity_id": "good", "canonical_url": payload["canonical_url"],
            "expected_binding": dict(zip(("is_visible", "published_candidate_sha256", "latest_candidate_sha256"), binding)),
            "expected_categories": payload["categories"], "categories": categories,
            "evidence_basis": "Retained climate source evidence for this explicit category-only correction."}]}
    path.write_text(json.dumps(plan))
    return path


def _published_initial_g3(tmp_path, monkeypatch):
    from climate_registry.enrichment_contract import INITIAL_SCOPE
    database = _seed(tmp_path)
    summary = "Flood losses reached 10 billion."
    with sqlite3.connect(database) as db:
        _insert_body(db, "body-a", summary, "2026-10-02T00:00:00Z")
        db.execute("INSERT INTO article_enrichments(enrichment_id,article_id,content_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('initial-exact','good','body-a','complete',?,'[\"Physical Risk\"]','[\"flood\"]','en','manual','fixture','v1','2026-10-02T00:00:00Z')", (summary,))
        seed_verified_articles(db, occurrence_ids={"core:good"})
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = {"summary": summary}
        origin = stage_snapshot(db, snapshot, enrichment_scope=INITIAL_SCOPE)
    _native_contract_review(database, tmp_path, monkeypatch)
    return database, origin, summary


def test_category_only_publication_retains_original_exact_initial_content_approval(tmp_path, monkeypatch):
    from climate_registry.category_correction import correct_article_categories
    from climate_registry.enrichment_contract import INITIAL_SCOPE
    database, origin, summary = _published_initial_g3(tmp_path, monkeypatch)
    reader = _reader(database, tmp_path)
    assert reader.article("good")["summary"] == summary
    with sqlite3.connect(database) as db:
        old_receipts = db.execute("SELECT * FROM registry_reviews WHERE candidate_sha256=?", (origin,)).fetchall()
        original = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (origin,)).fetchone()[0])
    saved_reference = None
    for index, categories in enumerate((["Physical Risk", "Climate & Health"], ["Physical Risk"])):
        path = _category_only_plan(database, tmp_path/(str(index) + "-category.json"), reader, categories)
        assert correct_article_categories(database=database, manifest=path, apply=True, backup_dir=tmp_path/"backups")["status"] == "applied"
        for direct in (True, False):
            with reader.public_snapshot(direct=direct):
                actual = reader.article("good")
                assert actual["summary"] == summary
                assert actual["categories"] == categories
                assert actual["published_candidate_sha256"] != origin
        sha = actual["published_candidate_sha256"]
        _assert_native_content_api(reader, monkeypatch, sha, summary, categories=categories)
        with sqlite3.connect(database) as db:
            frozen = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
            reference = frozen["enrichment_scope"]["approval_inheritance_baseline"]["content_review"]
            assert reference["candidate_sha256"] == origin
            from climate_registry.enrichment_contract import review_projection
            assert reference["enrichment_contract_sha256"] == digest(review_projection(original))
            assert reference["scope"] == INITIAL_SCOPE
            assert reference["content_version_id"] == "body-a"
            assert reference["summary_sha256"] == hashlib.sha256(summary.encode()).hexdigest()
            assert reference["body_sha256"] == hashlib.sha256(summary.encode()).hexdigest()
            assert reference["review_id"] == old_receipts[0][0]
            if saved_reference is not None:
                assert reference == saved_reference
            saved_reference = reference
            current_receipt = json.loads(db.execute("SELECT receipt_json FROM registry_reviews WHERE candidate_sha256=?", (sha,)).fetchone()[0])
            assert current_receipt["scope"] == "approved-public-category-only"
            assert "conclusion" not in current_receipt
            assert current_receipt["inherited_content_review"]["candidate_sha256"] == origin
            assert current_receipt["inherited_content_review"]["review_id"] == old_receipts[0][0]
            assert current_receipt["inherited_content_review"]["receipt_json"] == old_receipts[0][4]
            assert db.execute("SELECT * FROM registry_reviews WHERE candidate_sha256=?", (origin,)).fetchall() == old_receipts
        assert correct_article_categories(database=database, manifest=path, apply=True, backup_dir=tmp_path/"backups")["status"] == "already_applied"
    # The existing date-only freeze must retain the same content authorization too.
    _ingest(database, tmp_path, _manual(database))
    actual = reader.article("good")
    assert actual["summary"] == summary and actual["categories"] == ["Physical Risk"]


@pytest.mark.parametrize("drift", ["body", "version", "summary"])
def test_category_content_inheritance_rejects_drift_without_publication(tmp_path, monkeypatch, drift):
    from climate_registry.category_correction import correct_article_categories
    from climate_registry.enrichment_contract import bind_category_content, reviewed_content_receipt
    from climate_registry.verification import review_context
    database, _, summary = _published_initial_g3(tmp_path, monkeypatch)
    reader = _reader(database, tmp_path)
    path = _category_only_plan(database, tmp_path/"category.json", reader, ["Physical Risk", "Climate & Health"])
    correct_article_categories(database=database, manifest=path, apply=True, backup_dir=tmp_path/"backups")
    before = reader.article("good")
    with sqlite3.connect(database) as db:
        sha = before["published_candidate_sha256"]
        original = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
        changed = deepcopy(original)
        if drift == "body":
            next(row for row in changed["tables"]["article_content_versions"] if row["content_version_id"] == "body-a")["markdown_content"] += " Changed losses."
        elif drift == "version":
            changed["tables"]["articles"][0]["current_content_version_id"] = "another-version"
        else:
            changed["derived_display"]["summary"] = "Another complete summary."
            next(row for row in changed["tables"]["article_enrichments"] if row.get("content_version_id") == "body-a")["summary"] = "Another complete summary."
        assert reviewed_content_receipt(changed, review_context(db)[sha]) is None
        with pytest.raises(ValueError, match="body, version or summary differs"):
            bind_category_content(db, original, changed)
        with pytest.raises(ValueError):
            stage_snapshot(db, changed)
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0] == sha
    assert reader.article("good") == before
    assert reader.article("good")["summary"] == summary


def _published_g3_kind(tmp_path, monkeypatch, kind):
    if kind == "initial":
        return _published_initial_g3(tmp_path, monkeypatch)
    database, _ = _published_a(tmp_path)
    origin, frozen = _stage_update(database)
    _native_contract_review(database, tmp_path, monkeypatch)
    return database, origin, frozen["derived_display"]["summary"]


@pytest.mark.parametrize("kind", ["initial", "complete"])
def test_g3_date_then_category_preserves_real_content_and_human_authorities(tmp_path, monkeypatch, kind):
    from climate_registry.category_correction import correct_article_categories
    database, origin, summary = _published_g3_kind(tmp_path, monkeypatch, kind)
    _ingest(database, tmp_path, _manual(database, fields={"publication_date": "2026-10-22"}))
    reader = _reader(database, tmp_path)
    before = reader.article("good")
    decision = deepcopy(before["manual_date_overrides"])
    path = _category_only_plan(database, tmp_path/"category-after-date.json", reader, ["Physical Risk", "Climate & Health"])
    assert correct_article_categories(database=database, manifest=path)["status"] == "dry_run"
    assert reader.article("good") == before
    assert correct_article_categories(database=database, manifest=path, apply=True, backup_dir=tmp_path/"backups")["status"] == "applied"
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["summary"] == summary
            assert actual["manual_date_overrides"] == decision
            assert actual["content_date"] == "2026-10-22" and actual["content_date_authority"] == "human_override"
            assert actual["categories"] == ["Physical Risk", "Climate & Health"]
    _assert_native_content_api(reader, monkeypatch, actual["published_candidate_sha256"], summary,
        categories=["Physical Risk", "Climate & Health"])
    with sqlite3.connect(database) as db:
        current = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (actual["published_candidate_sha256"],)).fetchone()[0])
        assert current["enrichment_scope"]["approval_inheritance_baseline"]["content_review"]["candidate_sha256"] == origin


@pytest.mark.parametrize("kind", ["initial", "complete"])
@pytest.mark.parametrize("source", ["v1", "v2_hold"])
def test_unchanged_g3_content_accepts_real_owner_source_recheck(tmp_path, monkeypatch, kind, source):
    from datetime import datetime, timedelta, timezone
    from climate_registry.information_checks import _now
    from climate_registry.enrichment_contract import inspect_candidate
    database, origin, summary = _published_g3_kind(tmp_path, monkeypatch, kind)
    reader = _reader(database, tmp_path)
    before = reader.article("good")
    fields = {key: before[key] for key in ("title", "summary")}
    factory = _manifest if source == "v1" else _editorial_manifest
    manifest = factory(database, fields=fields, body=fields["title"] + "\n" + fields["summary"],
        **({"disposition": "hold"} if source == "v2_hold" else {}))
    manifest["checked_at"] = _now()
    sha = _ingest(database, tmp_path, manifest)["candidate_sha256"]
    assert sha != origin and reader.article("good") == before
    contract = inspect_candidate(database, sha)["contract"]
    assert contract["scope"] == "owner_source_fields." + ("v1" if source == "v1" else "v2")
    receipt = _review(database, tmp_path, sha)
    receipt["reviewed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    approve_owner_check(database, receipt, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["published_candidate_sha256"] == sha and actual["summary"] == summary
            authority = actual["owner_agent_verification"]
            assert authority["review_id"] == digest([sha, "pass", receipt])
            assert authority["candidate_sha256"] == sha and authority["reviewer"] == receipt["actor"]
            assert authority["content_approval"]["candidate_sha256"] == origin
            if kind == "complete":
                assert actual["content_update"] == before["content_update"]
    _assert_native_content_api(reader, monkeypatch, sha, summary)


@pytest.mark.parametrize("kind", ["initial", "complete"])
@pytest.mark.parametrize("before_operation", ["none", "date", "category"])
def test_failed_refetch_retains_g3_content_through_native_review(tmp_path, monkeypatch, kind, before_operation):
    from climate_registry.capture import capture_enrich_registry
    from climate_registry.category_correction import correct_article_categories
    from climate_registry.enrichment_contract import inspect_candidate
    from test_climate_registry_capture import FakeTransport, _response, PUBLIC_V4
    database, origin, summary = _published_g3_kind(tmp_path, monkeypatch, kind)
    reader = _reader(database, tmp_path)
    if before_operation == "date":
        _ingest(database, tmp_path, _manual(database, fields={"publication_date": "2026-10-22"}))
    elif before_operation == "category":
        path = _category_only_plan(database, tmp_path/"category.json", reader, ["Physical Risk", "Climate & Health"])
        correct_article_categories(database=database, manifest=path, apply=True, backup_dir=tmp_path/"backups")
    before = reader.article("good")
    captured = capture_enrich_registry(database, tmp_path/"capture-backups", article_ids=["good"], refresh=True,
        resolver=lambda host, port: [PUBLIC_V4], transport=FakeTransport(_response(403, b"")), clock=lambda: "2026-10-26T00:00:00Z")
    assert captured["counts"] == {"failed": 1}
    with sqlite3.connect(database) as db:
        sha = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0]
        frozen = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
        assert frozen["tables"]["articles"][0]["current_content_version_id"] == ("body-a" if kind == "initial" else "body-b")
        assert frozen["derived_display"]["summary"] == summary
        assert frozen["enrichment_scope"]["approval_inheritance_baseline"]["content_review"]["candidate_sha256"] == origin
    assert reader.article("good") == before
    assert inspect_candidate(database, sha)["contract"]["proposed"]["derived_display"]["summary"] == summary
    second = tmp_path/"second-review"; second.mkdir()
    _native_contract_review(database, second, monkeypatch)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["published_candidate_sha256"] == sha and actual["summary"] == summary
            assert actual.get("manual_date_overrides") == before.get("manual_date_overrides")
            assert actual["categories"] == before["categories"]
            if kind == "complete":
                assert actual["content_update"] == before["content_update"]
    _assert_native_content_api(reader, monkeypatch, sha, summary)


@pytest.mark.parametrize("drift", ["body", "version", "summary", "update_package"])
def test_peripheral_content_approval_rejects_content_drift(tmp_path, monkeypatch, drift):
    from climate_registry.enrichment_contract import bind_existing_content, reviewed_content_receipt
    from climate_registry.verification import review_context
    database, origin, summary = _published_g3_kind(tmp_path, monkeypatch, "complete")
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", "good")
        assert bind_existing_content(db, snapshot)
        sha = stage_snapshot(db, snapshot)
    second = tmp_path/"second-review"; second.mkdir()
    _native_contract_review(database, second, monkeypatch)
    reader = _reader(database, tmp_path)
    before = reader.article("good")
    with sqlite3.connect(database) as db:
        current_sha = before["published_candidate_sha256"]
        frozen = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (current_sha,)).fetchone()[0])
        changed = deepcopy(frozen)
        if drift == "body":
            next(row for row in changed["tables"]["article_content_versions"] if row["content_version_id"] == "body-b")["markdown_content"] += " New result."
        elif drift == "version":
            changed["tables"]["articles"][0]["current_content_version_id"] = "body-a"
        elif drift == "summary":
            changed["derived_display"]["summary"] = "A changed complete summary."
        else:
            changed["derived_display"]["content_update"]["change_note"] += " Another editorial claim."
        assert reviewed_content_receipt(changed, review_context(db)[current_sha]) is None
        with pytest.raises(ValueError):
            stage_snapshot(db, changed)
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0] == current_sha
    assert reader.article("good") == before and before["summary"] == summary


@pytest.mark.parametrize("kind", ["initial", "complete"])
def test_owner_recheck_date_category_and_refetch_keep_separate_approvals(tmp_path, monkeypatch, kind):
    from datetime import datetime, timedelta, timezone
    from climate_registry.category_correction import correct_article_categories
    from climate_registry.capture import capture_enrich_registry
    from climate_registry.information_checks import _now
    from test_climate_registry_capture import FakeTransport, _response, PUBLIC_V4
    database, origin, summary = _published_g3_kind(tmp_path, monkeypatch, kind)
    reader = _reader(database, tmp_path)
    for index, source in enumerate(("v1", "v2_hold")):
        before = reader.article("good")
        fields = {key: before[key] for key in ("title", "summary")}
        manifest = (_manifest(database, fields=fields, body="\n".join(fields.values())) if source == "v1" else
            _editorial_manifest(database, fields=fields, body="\n".join(fields.values()), disposition="hold"))
        manifest["checked_at"] = _now()
        sha = _ingest(database, tmp_path, manifest)["candidate_sha256"]
        receipt = _review(database, tmp_path, sha)
        receipt["reviewed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        approve_owner_check(database, receipt, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
        assert reader.article("good")["summary"] == summary
        if index == 0:
            _ingest(database, tmp_path, _manual(database, fields={"publication_date": "2026-10-22"}))
            path = _category_only_plan(database, tmp_path/"chained-category.json", reader, ["Physical Risk", "Climate & Health"])
            correct_article_categories(database=database, manifest=path, apply=True, backup_dir=tmp_path/"backups")
            assert capture_enrich_registry(database, tmp_path/"capture-backups", article_ids=["good"], refresh=True,
                resolver=lambda host, port: [PUBLIC_V4], transport=FakeTransport(_response(403, b"")), clock=lambda: "2026-10-26T00:00:00Z")["counts"] == {"failed": 1}
            second = tmp_path/"second-review"; second.mkdir()
            _native_contract_review(database, second, monkeypatch)
    actual = reader.article("good")
    assert actual["summary"] == summary and actual["content_date"] == "2026-10-22"
    assert actual["categories"] == ["Physical Risk", "Climate & Health"]
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            projected = reader.article("good")
            for field in ("published_candidate_sha256", "summary", "content_date", "categories", "manual_date_overrides", "content_update"):
                assert projected.get(field) == actual.get(field)
    _assert_native_content_api(reader, monkeypatch, actual["published_candidate_sha256"], summary,
        categories=["Physical Risk", "Climate & Health"])


def test_category_correction_after_human_revocation_keeps_authority_revoked(tmp_path, monkeypatch):
    from climate_registry.category_correction import correct_article_categories
    from test_article_content_dates import _revoke_date
    database, origin, summary = _published_initial_g3(tmp_path, monkeypatch)
    reader = _reader(database, tmp_path)
    _ingest(database, tmp_path, _manual(database, fields={"publication_date": "2026-10-22"}))
    for index, categories in enumerate((["Physical Risk", "Climate & Health"], ["Physical Risk"])):
        if index:
            _ingest(database, tmp_path, _revoke_date(database, "publication_date"))
            revoked = reader.article("good")
            assert not revoked.get("manual_date_overrides")
        path = _category_only_plan(database, tmp_path/(str(index)+"-revoked-category.json"), reader, categories)
        correct_article_categories(database=database, manifest=path, apply=True, backup_dir=tmp_path/"backups")
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["summary"] == summary and actual["categories"] == ["Physical Risk"]
            assert actual.get("manual_date_overrides") == revoked.get("manual_date_overrides")
            assert actual.get("content_date") == revoked.get("content_date")
            assert actual.get("publication_date_verification") == revoked.get("publication_date_verification")


@pytest.mark.parametrize("entry", ["validator", "stage"])
@pytest.mark.parametrize("field,value", [
    ("categories", "Physical Risk"), ("keywords", "flood"),
    ("categories", ["Not In Taxonomy"]), ("categories", ["Climate & Health"]),
    ("keywords", ["coastal risk", "claim estimates", "flood region"]),
    ("is_activity", "yes"), ("metadata_provenance", ["source"]),
    ("activity_dates", "2026-11-01"), ("activity_dates", [{"start_date": "2026-11-01"}]),
])
def test_g3_rejects_untyped_or_unbound_optional_fields(tmp_path, entry, field, value):
    from climate_registry.enrichment_contract import validate_input
    database, a = _published_a(tmp_path)
    _, snapshot = _stage_update(database)
    snapshot["derived_display"][field] = value
    before = _reader(database, tmp_path).article("good")
    if entry == "validator":
        with pytest.raises(ValueError):
            validate_input(database, snapshot, CONTENT_SCOPE)
    else:
        with sqlite3.connect(database) as db:
            count = db.execute("SELECT COUNT(*) FROM registry_candidates").fetchone()[0]
            with pytest.raises(ValueError):
                stage_snapshot(db, snapshot)
            assert db.execute("SELECT COUNT(*) FROM registry_candidates").fetchone()[0] == count
            assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0] == a
    assert _reader(database, tmp_path).article("good") == before


@pytest.mark.parametrize("mutation", ["missing", "wrong"])
def test_g3_editorial_values_require_the_exact_frozen_taxonomy(tmp_path, mutation):
    from climate_registry.enrichment_contract import validate_input
    from climate_registry.information_checks import _now
    database, _ = _published_a(tmp_path)
    _, pending = _stage_update(database)
    manifest = _editorial_manifest(database)
    manifest["checked_at"] = _now()
    _ingest(database, tmp_path, manifest)
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", "good")
    snapshot["derived_display"] = deepcopy(pending["derived_display"])
    snapshot["derived_display"]["keywords"] = manifest["observations"][0]["editorial_metadata"]["keywords"]
    row = next(row for row in snapshot["tables"]["article_check_attempts"] if "owner-agent-information-check.v2" in row["packet_json"])
    packet = json.loads(row["packet_json"])
    if mutation == "missing":
        packet.pop("taxonomy")
    else:
        packet["taxonomy"]["sha256"] = "0" * 64
    row["packet_json"] = json.dumps(packet)
    row["packet_sha256"] = hashlib.sha256(row["packet_json"].encode()).hexdigest()
    with pytest.raises(ValueError):
        validate_input(database, snapshot, CONTENT_SCOPE)
    with sqlite3.connect(database) as db:
        with pytest.raises(ValueError):
            stage_snapshot(db, snapshot, enrichment_scope=CONTENT_SCOPE)


def test_new_g3_editorial_proposal_stages_with_exact_v2_evidence_before_content_pass(tmp_path, monkeypatch):
    from climate_registry.enrichment_contract import validate_input, inspect_candidate
    from climate_registry.information_checks import _now
    database, a = _published_a(tmp_path)
    _ingest(database, tmp_path, _manual(database, fields={"publication_date": "2026-10-22"}))
    _, pending = _stage_update(database)
    manifest = _editorial_manifest(database)
    manifest["checked_at"] = _now()
    metadata = manifest["observations"][0]["editorial_metadata"]
    metadata["categories"] = ["Physical Risk", "Climate & Health"]
    metadata["keywords"] = ["flood exposure", "regional health", "claim estimates"]
    for field in ("categories", "keywords"):
        proof = metadata["evidence"][field][0]
        metadata["evidence"][field] = [{**proof, "value": value} for value in metadata[field]]
    _ingest(database, tmp_path, manifest)
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = {**deepcopy(pending["derived_display"]),
            "categories": metadata["categories"], "keywords": metadata["keywords"],
            "metadata_provenance": {"categories": "owner_reviewed_editorial_classification", "keywords": "owner_reviewed_editorial_classification"}}
        before = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0]
    assert validate_input(database, snapshot, CONTENT_SCOPE)["status"] == "valid"
    with sqlite3.connect(database) as db:
        sha = stage_snapshot(db, snapshot, enrichment_scope=CONTENT_SCOPE)
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0] == before
        assert not db.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=? AND status='pass'", (sha,)).fetchone()
    contract = inspect_candidate(database, sha)["contract"]
    assert contract["taxonomy_bindings"] == [manifest["taxonomy"]]
    _native_contract_review(database, tmp_path, monkeypatch)
    reader = _reader(database, tmp_path)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["categories"] == metadata["categories"] and actual["keywords"] == metadata["keywords"]
            assert actual["publication_date"] == "2026-10-22"
            assert actual["publication_date_verification"]["authority"] == "human_override"
    _assert_native_content_api(reader, monkeypatch, sha, pending["derived_display"]["summary"],
        categories=metadata["categories"], keywords=metadata["keywords"])


def _native_editorial_proposal(tmp_path, *, initial=False):
    from climate_monitor.taxonomy import load_article_taxonomy, validate_semantic_bundle
    from climate_registry.semantic_import import _bundle_sha256
    if initial:
        database = _seed(tmp_path)
        summary = "Flood losses reached 10 billion."
        with sqlite3.connect(database) as db:
            _insert_body(db, "body-a", summary, "2026-10-02T00:00:00Z")
            db.execute("INSERT INTO article_enrichments(enrichment_id,article_id,content_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('initial-exact','good','body-a','complete',?,'[]','[]','en','manual','fixture','v1','2026-10-02T00:00:00Z')", (summary,))
            seed_verified_articles(db, occurrence_ids={"core:good"})
        pending = {"derived_display": {"summary": summary}}
    else:
        database, _ = _published_a(tmp_path)
        _, pending = _stage_update(database)
    taxonomy = load_article_taxonomy()
    semantics = validate_semantic_bundle({"schema_version": "article-semantic-bundle.v1", "taxonomy_id": taxonomy.taxonomy_id,
        "taxonomy_sha256": taxonomy.sha256, "summary": ("This retained report describes flood exposure, regional health and claims evidence. " * 3).strip(),
        "categories": ["Physical Risk", "Climate & Health"], "keywords": ["flood exposure", "regional health", "claim estimates"]}, taxonomy=taxonomy)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO reports VALUES('editorial-report','2026-10-19','editorial-report.md','Editorial report',?,'weekly','weekly-pillars-v1',1,1,0,'[]')", ("e"*64,))
        db.execute("INSERT INTO discoveries VALUES('editorial-discovery','editorial-report',1,'pillar-a','A','good','v-good','https://example.org/good','Title good','Summary good',1,NULL)")
        db.execute("INSERT INTO report_appearances(report_id,article_id,version_id,discovery_id,section,pillar,ordinal,disposition) VALUES('editorial-report','good','v-good','editorial-discovery','pillar-a','A',1,'new')")
        db.execute("INSERT INTO article_semantics VALUES('editorial-report',?,'good','https://example.org/good','Title good',?,?,?,?,?,?,'2026-10-20T00:00:00Z')",
            ("e"*64, semantics["summary"], json.dumps(semantics["categories"]), json.dumps(semantics["keywords"]), taxonomy.taxonomy_id, taxonomy.sha256, _bundle_sha256(semantics)))
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = {**deepcopy(pending["derived_display"]), "categories": semantics["categories"], "keywords": semantics["keywords"],
            "metadata_provenance": {"categories": "source_report", "keywords": "source_report"}}
    return database, snapshot, taxonomy, semantics, pending


def test_new_g3_editorial_values_bind_existing_native_semantic_import(tmp_path, monkeypatch):
    from climate_registry.enrichment_contract import validate_input, inspect_candidate
    database, snapshot, taxonomy, semantics, pending = _native_editorial_proposal(tmp_path)
    assert validate_input(database, snapshot, CONTENT_SCOPE)["status"] == "valid"
    with sqlite3.connect(database) as db:
        sha = stage_snapshot(db, snapshot, enrichment_scope=CONTENT_SCOPE)
        assert not db.execute("SELECT 1 FROM registry_reviews WHERE candidate_sha256=? AND status='pass'", (sha,)).fetchone()
    assert inspect_candidate(database, sha)["contract"]["native_taxonomy_bindings"][0]["taxonomy_raw_sha256"] == taxonomy.sha256
    _native_contract_review(database, tmp_path, monkeypatch)
    _assert_native_content_api(_reader(database, tmp_path), monkeypatch, sha, pending["derived_display"]["summary"],
        categories=semantics["categories"], keywords=semantics["keywords"])


def test_native_g3_editorial_approval_survives_owner_source_only_hold_with_exact_receipt(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from climate_registry.enrichment_contract import inspect_candidate
    from climate_registry.information_checks import _now
    test_new_g3_editorial_values_bind_existing_native_semantic_import(tmp_path, monkeypatch)
    database = tmp_path/"external/registry.sqlite3"
    reader = _reader(database, tmp_path)
    before = reader.article("good")
    origin = before["published_candidate_sha256"]
    original_contract = inspect_candidate(database, origin)["contract"]
    with sqlite3.connect(database) as db:
        review_id = db.execute("SELECT review_id FROM registry_reviews WHERE candidate_sha256=? AND status='pass'", (origin,)).fetchone()[0]
    for _ in range(2):
        fields = {key: before[key] for key in ("title", "summary")}
        manifest = _editorial_manifest(database, fields=fields, body=fields["title"] + "\n" + fields["summary"], disposition="hold")
        manifest["checked_at"] = _now()
        sha = _ingest(database, tmp_path, manifest)["candidate_sha256"]
        contract = inspect_candidate(database, sha)["contract"]
        inherited = contract["approval_inheritance_baseline"].get("editorial", {})
        assert set(inherited) == {"categories", "keywords"}
        for field in inherited:
            authority = inherited[field]["authority"]
            assert authority["content_review"] == {"candidate_sha256": origin, "enrichment_contract_sha256": digest(original_contract),
                "scope": CONTENT_SCOPE, "review_id": review_id}
            assert authority["native_taxonomy_bindings"] == original_contract["native_taxonomy_bindings"]
        receipt = _review(database, tmp_path, sha)
        receipt["reviewed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        assert approve_owner_check(database, receipt, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)["status"] == "approved"
        expected_categories, expected_keywords = before["categories"], before["keywords"]
        for direct in (True, False):
            with reader.public_snapshot(direct=direct):
                actual = reader.article("good")
                assert actual["published_candidate_sha256"] == sha
                assert actual["categories"] == expected_categories and actual["keywords"] == expected_keywords
                assert actual["metadata_provenance"] == before["metadata_provenance"]
                assert actual["summary"] == before["summary"]
                assert actual["content_update"] == before["content_update"]
        _assert_native_content_api(reader, monkeypatch, sha, before["summary"], categories=expected_categories, keywords=expected_keywords)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT review_id FROM registry_reviews WHERE candidate_sha256=? AND status='pass'", (origin,)).fetchone()[0] == review_id


@pytest.mark.parametrize("field", ["categories", "keywords"])
def test_exact_native_content_receipt_cannot_inherit_different_editorial_values(tmp_path, monkeypatch, field):
    from climate_registry.enrichment_contract import _editorial_inheritance
    test_new_g3_editorial_values_bind_existing_native_semantic_import(tmp_path, monkeypatch)
    database = tmp_path/"external/registry.sqlite3"
    origin = _reader(database, tmp_path).article("good")["published_candidate_sha256"]
    with sqlite3.connect(database) as db:
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (origin,)).fetchone()[0])
        for change in ("value", "provenance"):
            proposal = deepcopy(snapshot)
            if change == "value":
                proposal["derived_display"][field] = snapshot["derived_display"][field][:1]
            else:
                proposal["derived_display"]["metadata_provenance"][field] = "unreviewed"
            inherited = _editorial_inheritance(db, proposal, origin)
            assert field not in inherited
            assert set(inherited) == {"categories", "keywords"} - {field}


@pytest.mark.parametrize("g3_scope", [False, True])
def test_source_report_without_exact_g3_content_receipt_has_no_editorial_inheritance(tmp_path, g3_scope):
    from climate_registry.enrichment_contract import _editorial_inheritance
    database, snapshot, _, _, _ = _native_editorial_proposal(tmp_path)
    snapshot.pop("enrichment_scope", None)
    with sqlite3.connect(database) as db:
        sha = stage_snapshot(db, snapshot, **({"enrichment_scope": CONTENT_SCOPE} if g3_scope else {}))
        _approve(db, sha, {"basis": "isolated legacy approval without an exact G3 contract receipt"})
        assert _editorial_inheritance(db, snapshot, sha) == {}


def test_unreviewed_source_report_taxonomy_is_cleared_by_owner_hold(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from climate_registry.information_checks import _now
    database, snapshot, _, _, _ = _native_editorial_proposal(tmp_path)
    snapshot.pop("enrichment_scope", None)
    with sqlite3.connect(database) as db:
        sha = stage_snapshot(db, snapshot)
        _approve(db, sha, {"basis": "isolated legacy approval; source_report is not an editorial review"})
    reader = _reader(database, tmp_path)
    before = reader.article("good")
    fields = {key: before[key] for key in ("title", "summary")}
    manifest = _editorial_manifest(database, fields=fields, body=fields["title"] + "\n" + fields["summary"], disposition="hold")
    manifest["checked_at"] = _now()
    held = _ingest(database, tmp_path, manifest)["candidate_sha256"]
    receipt = _review(database, tmp_path, held)
    receipt["reviewed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    approve_owner_check(database, receipt, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["published_candidate_sha256"] == held
            assert actual["categories"] == [] and actual["keywords"] == []
    _assert_native_content_api(reader, monkeypatch, held, before["summary"], categories=[], keywords=[])


def _same_body_owner_manifest(database, reader, *, editorial=False, hold=False):
    from climate_registry.information_checks import _now
    public = reader.article("good")
    fields = {key: public[key] for key in ("title", "summary")}
    body = "\n".join(fields.values())
    if editorial and not hold:
        body += "\nClimate risk findings\nFlood exposure increased in the study region."
    manifest = (_editorial_manifest(database, fields=fields, body=body, disposition="hold" if hold else "complete")
        if editorial else _manifest(database, fields=fields, body=body))
    manifest["checked_at"] = _now()
    return manifest


def _approve_same_body_owner(database, tmp_path, sha):
    from datetime import datetime, timedelta, timezone
    receipt = _review(database, tmp_path, sha)
    receipt["reviewed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    assert approve_owner_check(database, receipt, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)["status"] == "approved"


@pytest.mark.parametrize("kind", ["initial", "complete"])
def test_next_review_can_be_prepared_after_valid_g3_owner_v2_editorial_publication(tmp_path, monkeypatch, kind):
    from climate_registry.enrichment_contract import inspect_candidate
    from climate_registry.publication import prepare_review, stage_entities
    database, _, _ = _published_g3_kind(tmp_path, monkeypatch, kind)
    reader = _reader(database, tmp_path)
    previous_public = reader.article("good")
    manifest = _same_body_owner_manifest(database, reader, editorial=True)
    staged = _ingest(database, tmp_path, manifest)
    assert reader.article("good") == previous_public
    _approve_same_body_owner(database, tmp_path, staged["candidate_sha256"])
    public = reader.article("good")
    assert public["categories"] == manifest["observations"][0]["editorial_metadata"]["categories"]
    assert public["keywords"] == ["flood", "exposure", "study region"]
    prepare_review(database, tmp_path/"next-review")
    with sqlite3.connect(database) as db:
        stage_entities(db)
        assert db.execute("SELECT published_candidate_sha256,latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone() == (staged["candidate_sha256"], staged["candidate_sha256"])
    assert reader.article("good") == public
    for editorial in (False, True):
        following = _same_body_owner_manifest(database, reader, editorial=editorial, hold=editorial)
        candidate = _ingest(database, tmp_path, following)["candidate_sha256"]
        contract = inspect_candidate(database, candidate)["contract"]
        inherited = contract["approval_inheritance_baseline"]["editorial"]
        assert set(inherited) == {"categories", "keywords"}
        assert contract["proposed"]["derived_display"]["categories"] == public["categories"]
        assert contract["proposed"]["derived_display"]["keywords"] == public["keywords"]
        assert reader.article("good") == public
        _approve_same_body_owner(database, tmp_path, candidate)
        for direct in (True, False):
            with reader.public_snapshot(direct=direct):
                actual = reader.article("good")
                assert actual["categories"] == public["categories"] and actual["keywords"] == public["keywords"]
                assert actual["summary"] == public["summary"]
        _assert_native_content_api(reader, monkeypatch, candidate, public["summary"], categories=public["categories"], keywords=public["keywords"])
        assert _ingest(database, tmp_path, following)["status"] == "no-op"
        prepare_review(database, tmp_path/("after-v2" if editorial else "after-v1"))
        public = reader.article("good")


@pytest.mark.parametrize("kind", ["initial", "complete"])
@pytest.mark.parametrize("after_approval", [False, True])
@pytest.mark.parametrize("editorial", [False, True])
def test_identical_owner_input_remains_idempotent_on_inherited_g3(tmp_path, monkeypatch, kind, after_approval, editorial):
    database, _, _ = _published_g3_kind(tmp_path, monkeypatch, kind)
    reader = _reader(database, tmp_path)
    manifest = _same_body_owner_manifest(database, reader, editorial=editorial)
    staged = _ingest(database, tmp_path, manifest)
    if after_approval:
        _approve_same_body_owner(database, tmp_path, staged["candidate_sha256"])
    assert _ingest(database, tmp_path, manifest)["status"] == "no-op"
    with sqlite3.connect(database) as db:
        db.execute("UPDATE article_versions SET observed_summary='Changed real source summary' WHERE article_id='good'")
    before = database.read_bytes()
    from climate_registry.errors import RegistryInputError
    with pytest.raises(RegistryInputError, match="changed live sources or candidate"):
        _ingest(database, tmp_path, manifest)
    assert database.read_bytes() == before


def test_identical_owner_v1_input_legacy_control(tmp_path):
    database = _seed(tmp_path)
    manifest = _same_body_owner_manifest(database, _reader(database, tmp_path))
    _ingest(database, tmp_path, manifest)
    assert _ingest(database, tmp_path, manifest)["status"] == "no-op"


@pytest.mark.parametrize("initial", [False, True])
def test_approved_native_editorial_inheritance_survives_next_configured_taxonomy(tmp_path, monkeypatch, initial):
    import climate_monitor.taxonomy as taxonomy_module
    from climate_registry.enrichment_contract import INITIAL_SCOPE, inspect_candidate
    configured = taxonomy_module.load_article_taxonomy()
    taxonomy_file = tmp_path/"configured-taxonomy.yaml"
    taxonomy_file.write_text(configured.definition_yaml.replace(configured.taxonomy_id, "configured-taxonomy-v2"))
    monkeypatch.setattr(taxonomy_module, "DEFAULT_TAXONOMY_PATH", taxonomy_file)
    database, snapshot, taxonomy, _, _ = _native_editorial_proposal(tmp_path, initial=initial)
    assert taxonomy.taxonomy_id == "configured-taxonomy-v2"
    with sqlite3.connect(database) as db:
        origin = stage_snapshot(db, snapshot, enrichment_scope=INITIAL_SCOPE if initial else CONTENT_SCOPE)
    _native_contract_review(database, tmp_path, monkeypatch)
    reader = _reader(database, tmp_path)
    public = reader.article("good")
    original_contract = inspect_candidate(database, origin)["contract"]
    assert original_contract["native_taxonomy_bindings"][0]["definition_yaml"] == taxonomy.definition_yaml
    with sqlite3.connect(database) as db:
        original_reviews = db.execute("SELECT * FROM registry_reviews WHERE candidate_sha256=?", (origin,)).fetchall()
        frozen = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (origin,)).fetchone()[0])
    taxonomy_file.write_text(configured.definition_yaml.replace(configured.taxonomy_id, "configured-taxonomy-v3"))
    manifest = _same_body_owner_manifest(database, reader, editorial=True, hold=True)
    staged = _ingest(database, tmp_path, manifest)
    assert staged["status"] == "staged"
    assert reader.article("good") == public
    inherited = inspect_candidate(database, staged["candidate_sha256"])["contract"]["approval_inheritance_baseline"]["editorial"]
    for field in ("categories", "keywords"):
        assert inherited[field]["authority"]["content_review"]["candidate_sha256"] == origin
        assert inherited[field]["authority"]["native_taxonomy_bindings"] == original_contract["native_taxonomy_bindings"]
    _approve_same_body_owner(database, tmp_path, staged["candidate_sha256"])
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["categories"] == public["categories"] and actual["keywords"] == public["keywords"]
            assert actual["summary"] == public["summary"]
    _assert_native_content_api(reader, monkeypatch, staged["candidate_sha256"], public["summary"], categories=public["categories"], keywords=public["keywords"])
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM registry_reviews WHERE candidate_sha256=?", (origin,)).fetchall() == original_reviews
        tampered = deepcopy(frozen)
        tampered["enrichment_scope"]["native_taxonomies"][0]["definition_yaml"] = taxonomy_file.read_text()
        with pytest.raises(ValueError, match="frozen identity"):
            stage_snapshot(db, tampered, enrichment_scope=INITIAL_SCOPE if initial else CONTENT_SCOPE)


@pytest.mark.parametrize("field,value", [("categories", "Physical Risk"), ("keywords", "flood"), ("categories", ["Not In Taxonomy"])])
def test_preexisting_pending_g3_candidate_cannot_bypass_editorial_validation_at_approval(tmp_path, field, value):
    database, published = _published_a(tmp_path)
    _, snapshot = _stage_update(database)
    snapshot["derived_display"][field] = value
    sha = digest(snapshot)
    before = _reader(database, tmp_path).article("good")
    with sqlite3.connect(database) as db:
        # Model a candidate frozen by the previous writer, before this deterministic guard existed.
        db.execute("INSERT INTO registry_candidates VALUES(?,?,?,?,?,?)", (sha, "article", "good", json.dumps(snapshot), "2026-10-24T00:00:00Z", "pending"))
        count = db.execute("SELECT COUNT(*) FROM registry_reviews").fetchone()[0]
        with pytest.raises(ValueError):
            _approve(db, sha, {"basis": "isolated approval-transaction structural guard probe"})
        assert db.execute("SELECT COUNT(*) FROM registry_reviews").fetchone()[0] == count
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0] == published
    assert _reader(database, tmp_path).article("good") == before


def test_g3_optional_guard_retains_approved_human_activity_range_on_refetch(tmp_path, monkeypatch):
    from climate_registry.capture import capture_enrich_registry
    from test_climate_registry_capture import FakeTransport, _response, PUBLIC_V4
    from test_owner_activity_dates import _human_activity
    database, origin, summary = _published_initial_g3(tmp_path, monkeypatch)
    _ingest(database, tmp_path, _human_activity(database, start="2026-11-04", end="2026-11-06"))
    reader = _reader(database, tmp_path)
    before = reader.article("good")
    assert before["is_activity"] is True and before["activity_dates"][0]["authority"] == "human_override"
    captured = capture_enrich_registry(database, tmp_path/"capture-backups", article_ids=["good"], refresh=True,
        resolver=lambda host, port: [PUBLIC_V4], transport=FakeTransport(_response(403, b"")), clock=lambda: "2026-10-26T00:00:00Z")
    assert captured["counts"] == {"failed": 1}
    second = tmp_path/"second-review"; second.mkdir()
    _native_contract_review(database, second, monkeypatch)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["summary"] == summary and actual["is_activity"] is True
            assert actual["activity_dates"] == before["activity_dates"]
            assert actual["manual_date_overrides"] == before["manual_date_overrides"]


def test_g3_approved_editorial_and_human_dates_survive_configured_taxonomy_change(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import climate_monitor.taxonomy as taxonomy_module
    from climate_registry.enrichment_contract import validate_input
    from climate_registry.information_checks import _now
    database, _ = _published_a(tmp_path)
    manifest = _editorial_manifest(database)
    manifest["checked_at"] = _now()
    old = _ingest(database, tmp_path, manifest)["candidate_sha256"]
    receipt = _review(database, tmp_path, old)
    receipt["reviewed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    approve_owner_check(database, receipt, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    _ingest(database, tmp_path, _manual(database, fields={"publication_date": "2026-10-22"}))
    before = _reader(database, tmp_path).article("good")
    _, snapshot = _stage_update(database)
    configured = taxonomy_module.load_article_taxonomy()
    future = taxonomy_module._parse_article_taxonomy_bytes(configured.definition_yaml.replace(configured.taxonomy_id, "future-taxonomy").encode())
    monkeypatch.setattr(taxonomy_module, "load_article_taxonomy", lambda *args, **kwargs: future)
    assert validate_input(database, snapshot, CONTENT_SCOPE)["status"] == "valid"
    with sqlite3.connect(database) as db:
        sha = stage_snapshot(db, snapshot)
        saved = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
    assert saved["derived_display"]["categories"] == before["categories"]
    assert saved["derived_display"]["keywords"] == before["keywords"]
    assert saved["derived_display"]["manual_date_overrides"] == before["manual_date_overrides"]
    inherited = saved["enrichment_scope"]["approval_inheritance_baseline"]["editorial"]["categories"]["authority"]["editorial_metadata_provenance"]
    assert inherited["taxonomy_sha256"] == manifest["taxonomy"]["sha256"]
    assert _reader(database, tmp_path).article("good") == before


def _contract_cli_subprocess(command, args, *, documented=False):
    import os
    import re
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    module = "climate_registry"
    if documented:
        text = (root/"docs"/"owner-agent-verification.md").read_text(encoding="utf-8")
        module = re.search(r"python -m ([\w.]+) " + re.escape(command) + r"\b", text).group(1)
    env = dict(os.environ)
    env.pop("CLIMATE_REGISTRY_DB", None)
    return subprocess.run([sys.executable, "-m", module, command, *args], cwd=root, env=env, text=True, capture_output=True)


@pytest.mark.parametrize("command", ["enrichment-capabilities", "validate-enrichment-contract", "inspect-enrichment-contract"])
def test_documented_g3_commands_execute_the_existing_package_entrypoint(tmp_path, command):
    if command == "enrichment-capabilities":
        result = _contract_cli_subprocess(command, [], documented=True)
        assert result.returncode == 0 and result.stdout, (result.stdout, result.stderr)
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == "registry-enrichment-contract.v1" and payload["capabilities"]
    elif command == "validate-enrichment-contract":
        database, input_path = tmp_path/"missing.sqlite3", tmp_path/"missing.json"
        result = _contract_cli_subprocess(command, ["--database", str(database), "--input", str(input_path), "--scope", CONTENT_SCOPE], documented=True)
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert json.loads(result.stdout)["kind"] == "input" and "Traceback" not in result.stderr
        assert not database.exists() and not input_path.exists()
    else:
        database, _ = _published_a(tmp_path)
        sha, _ = _stage_update(database)
        before = database.read_bytes()
        result = _contract_cli_subprocess(command, ["--database", str(database), "--candidate-sha256", sha], documented=True)
        assert result.returncode == 0 and result.stdout, (result.stdout, result.stderr)
        payload = json.loads(result.stdout)
        assert payload["contract"]["candidate_sha256"] == sha and payload["write_performed"] is False
        assert database.read_bytes() == before


@pytest.mark.parametrize("field", [name + suffix for name in ("publication_date", "updated_date", "revision_date", "activity_dates")
    for suffix in ("_evidence", "_verification")])
@pytest.mark.parametrize("value", ["source", []])
def test_real_g3_cli_returns_input_error_for_nonmapping_date_proofs(tmp_path, field, value):
    database, _ = _published_a(tmp_path)
    _, snapshot = _stage_update(database, publication_text=True)
    snapshot["derived_display"][field] = value
    if field.startswith("publication_date_"):
        snapshot["derived_display"]["publication_date"] = "2026-10-02"
    path = tmp_path/"invalid-date-proof.json"
    path.write_text(json.dumps(snapshot))
    before = database.read_bytes()
    result = _contract_cli_subprocess("validate-enrichment-contract", ["--database", str(database), "--input", str(path), "--scope", CONTENT_SCOPE])
    assert database.read_bytes() == before
    assert result.returncode == 2, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["kind"] == "input" and field in payload["message"]
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("field", [name + suffix for name in ("publication_date", "updated_date", "revision_date", "activity_dates")
    for suffix in ("_evidence", "_verification")])
@pytest.mark.parametrize("typed_source_date", [True, False])
def test_real_g3_null_date_proofs_validate_readonly_and_publish_without_invented_authority(tmp_path, monkeypatch, field, typed_source_date):
    database, _ = _published_a(tmp_path)
    _, snapshot = _stage_update(database)
    snapshot["derived_display"][field] = None
    for name in ("publication_date", "updated_date", "revision_date"):
        snapshot["derived_display"][name] = None
    if not typed_source_date:
        snapshot["derived_display"]["content_update"]["date_evidence"] = {}
    path = tmp_path/"unknown-date-proof.json"
    path.write_text(json.dumps(snapshot))
    before = database.read_bytes()
    result = _contract_cli_subprocess("validate-enrichment-contract", ["--database", str(database), "--input", str(path), "--scope", CONTENT_SCOPE])
    assert database.read_bytes() == before
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["status"] == "valid" and payload["write_performed"] is False
    assert payload["contract"]["proposed"]["derived_display"][field] is None
    for name in ("publication_date", "updated_date", "revision_date"):
        assert payload["contract"]["proposed"]["derived_display"][name] is None
    with sqlite3.connect(database) as db:
        sha = stage_snapshot(db, snapshot)
        frozen_json = db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0]
        assert json.loads(frozen_json)["derived_display"][field] is None
        for name in ("publication_date", "updated_date", "revision_date"):
            assert json.loads(frozen_json)["derived_display"][name] is None
    result = _native_contract_review(database, tmp_path, monkeypatch)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0] == frozen_json
        assert db.execute("SELECT status FROM registry_reviews WHERE candidate_sha256=? AND review_id=?", (sha, result["review_ids"][0])).fetchone()[0] == "pass"
    reader = _reader(database, tmp_path)
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            actual = reader.article("good")
            assert actual["published_candidate_sha256"] == sha
            assert actual.get("publication_date") is None and actual.get("revision_date") is None
            assert not actual.get("activity_dates") and not actual.get("manual_date_overrides")
            expected_updated = "2026-10-16" if typed_source_date else None
            assert actual.get("updated_date") == expected_updated
            for stem in ("publication_date", "updated_date", "revision_date", "activity_dates"):
                assert actual.get(stem + "_verification") is None
    _assert_native_content_api(reader, monkeypatch, sha, snapshot["derived_display"]["summary"])
    from fastapi.testclient import TestClient
    import api_server
    response = TestClient(api_server.app).get("/api/registry/articles/good")
    assert response.status_code == 200
    assert response.json().get("publication_date") is None and response.json().get("revision_date") is None
    assert not response.json().get("activity_dates") and not response.json().get("manual_date_overrides")
    assert response.json().get("updated_date") == expected_updated


def _insert_body(db, version, text, fetched):
    sha = hashlib.sha256(text.encode()).hexdigest()
    db.execute("INSERT INTO article_content_versions VALUES(?,?,?,?,?,'text/html',NULL,'fixture','v1',?)",
        (version, "good", sha, text, sha, fetched))
    db.execute("UPDATE articles SET current_content_version_id=? WHERE article_id='good'", (version,))
    db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_version_id) VALUES(?,'good','https://example.org/good','https://example.org/good',?,'success',200,?)",
        ("fetch-" + version, fetched, version))
    seed_verified_articles(db, occurrence_ids={"core:good"})


def _published_a(tmp_path):
    database = _seed(tmp_path)
    packet = _bundle_snapshot()
    with sqlite3.connect(database) as db:
        old = packet["tables"]["article_content_versions"][0]
        _insert_body(db, "body-a", old["markdown_content"], old["first_fetched_at"])
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = {"summary": "Flood losses reached 10 billion.",
            "categories": ["Physical Risk"], "keywords": ["approved baseline keyword"],
            "metadata_provenance": {"categories": "approved-source", "keywords": "approved-source"}}
        candidate = stage_snapshot(db, snapshot)
        _approve(db, candidate, {"basis": "explicit isolated baseline fixture approval"})
    return database, candidate


def _stage_update(database, *, version="body-b", explicit=True, publication_text=False, date_field="updated_date"):
    packet = _bundle_snapshot()
    bundle = packet["derived_display"]["content_update"]
    if version == "body-c":
        body = "Flood losses reached 30 billion. Updated: 16 October 2026"
        bundle.update(content_version_id=version, body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            summary_content_version_id=version, summary="Flood losses reached 30 billion.",
            summary_sha256=hashlib.sha256(b"Flood losses reached 30 billion.").hexdigest(),
            change_note="Flood losses rose from 10 to 30 billion.")
        bundle["change_evidence"]["current_excerpt"] = "Flood losses reached 30 billion."
        bundle["date_evidence"]["updated_date"]["content_version_id"] = version
    else:
        body = packet["tables"]["article_content_versions"][1]["markdown_content"]
    if date_field == "revision_date":
        body = body.replace("Updated:", "Revised:")
        proof = bundle["date_evidence"].pop("updated_date")
        proof["date_kind"] = "revision"
        bundle["date_evidence"]["revision_date"] = proof
    if publication_text:
        body += "\nPublished: 2 October 2026"
    bundle["body_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    with sqlite3.connect(database) as db:
        if not db.execute("SELECT 1 FROM article_content_versions WHERE content_version_id='body-a'").fetchone():
            old = packet["tables"]["article_content_versions"][0]
            _insert_body(db, "body-a", old["markdown_content"], old["first_fetched_at"])
        _insert_body(db, version, body, "2026-10-16T01:00:00Z")
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = {"summary": bundle["summary"], "content_update": bundle}
        sha = stage_snapshot(db, snapshot, **({"enrichment_scope": CONTENT_SCOPE} if explicit else {}))
        frozen = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
    return sha, frozen


def test_capabilities_are_frozen_input_scopes_not_arbitrary_public_fields():
    from climate_registry.enrichment_contract import capability, capability_matrix
    for source in ("pdf", "site", "search"):
        assert capability(source, "article", CONTENT_SCOPE)["status"] == ("conditional" if source == "pdf" else "supported")
        standalone = capability(source, "pdf_article", CONTENT_SCOPE)
        assert standalone["status"] == "unsupported"
        assert standalone["reason"] == "content_update_requires_canonical_article_and_retained_body_versions"
        assert capability(source, "pdf_article", "owner_source_fields.v1")["status"] == ("supported" if source == "pdf" else "unsupported")
        assert capability(source, "pdf_article", "human_article_dates")["status"] == ("supported" if source == "pdf" else "unsupported")
        assert capability(source, "meeting", "article_date_fields_only")["status"] == "unsupported"
    assert capability("unknown", "article", CONTENT_SCOPE)["status"] == "unsupported"
    assert all(row["required"] and row["evidence"] for row in capability_matrix()["capabilities"])


def test_explicit_update_needs_a_real_published_body_but_legacy_v1_keeps_its_scope(tmp_path):
    database = _seed(tmp_path)
    with pytest.raises(ValueError, match="required_missing: published_content_baseline"):
        _stage_update(database)
    # No new G3 scope is implied by the existing G1 package.
    sha, _ = _stage_complete_bundle(database)
    with sqlite3.connect(database) as db:
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
        assert "enrichment_scope" not in snapshot
        _approve(db, sha, {"basis": "explicit legacy G1 isolated fixture approval"})


def test_initial_content_has_explicit_not_applicable_comparison(tmp_path):
    from climate_registry.enrichment_contract import INITIAL_SCOPE, inspect_candidate
    database = _seed(tmp_path)
    with sqlite3.connect(database) as db:
        old = _bundle_snapshot()["tables"]["article_content_versions"][0]
        _insert_body(db, "body-a", old["markdown_content"], old["first_fetched_at"])
        db.execute("INSERT INTO article_enrichments(enrichment_id,article_id,content_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('e-initial','good','body-a','complete','Summary good','[\"Physical Risk\"]','[\"flood\"]','en','manual','fixture','v1','2026-10-02T00:00:00Z')")
        seed_verified_articles(db, occurrence_ids={"core:good"})
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = {"summary": "Summary good"}
        sha = stage_snapshot(db, snapshot, enrichment_scope=INITIAL_SCOPE)
        _approve(db, sha, {"basis": "explicit isolated initial-content approval"})
    result = inspect_candidate(database, sha)
    assert result["contract"]["scope"] == INITIAL_SCOPE
    assert result["contract"]["content_comparison_baseline"] == {"status": "not_applicable", "reason": "initial_content_publication"}
    assert result["contract"]["field_states"]["content_update"] == "unknown"
    assert result["publication"]["published_candidate_sha256"] == sha


def test_date_override_rebases_only_inheritance_and_preserves_pending_evidence(tmp_path):
    from climate_registry.enrichment_contract import inspect_candidate, review_projection
    database, a = _published_a(tmp_path)
    b, frozen = _stage_update(database)
    with sqlite3.connect(database) as db:
        old_review = _approve(db, b, {"basis": "retained pending review, not a PASS"}, status="needs_correction")
        before = dict(db.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates"))
        receipts = dict(db.execute("SELECT review_id,receipt_json FROM registry_reviews"))
    manual = _ingest(database, tmp_path, _manual(database))
    a_prime = manual["candidate_sha256"]
    decision = _reader(database, tmp_path).article("good")["manual_date_overrides"]["publication_date"]
    with sqlite3.connect(database) as db:
        with pytest.raises(ValueError, match="changed candidate"):
            _approve(db, b, {"basis": "stale candidate cannot publish"})
        b_prime = stage_snapshot(db, deepcopy(frozen))
        assert b_prime != b
        latest = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (b_prime,)).fetchone()[0])
        assert latest["enrichment_scope"]["content_comparison_baseline"]["candidate_sha256"] == a
        assert latest["enrichment_scope"]["approval_inheritance_baseline"]["candidate_sha256"] == a_prime
        assert latest["derived_display"]["manual_date_overrides"]["publication_date"] == decision
        for field in ("categories", "keywords"):
            assert latest["derived_display"][field] == frozen["derived_display"][field]
        assert latest["derived_display"]["content_update"] == frozen["derived_display"]["content_update"]
        assert before.items() <= dict(db.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates")).items()
        assert receipts.items() <= dict(db.execute("SELECT review_id,receipt_json FROM registry_reviews")).items()
        assert old_review in receipts
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0] == a_prime
        replayed = {"conclusion": {"candidate_sha256": b_prime, "enrichment_contract_sha256": digest(review_projection(frozen))}}
        with pytest.raises(ValueError, match="exact complete enrichment contract"):
            _approve(db, b_prime, replayed)
        _approve(db, b_prime, {"basis": "explicit isolated new-scope fixture approval"})
    result = inspect_candidate(database, b_prime)
    assert result["contract"]["candidate_sha256"] == b_prime
    assert result["contract"]["authority"]["human_decisions"]["publication_date"] == decision
    assert result["activation"]["status"] == "unknown" and result["write_performed"] is False


def test_restage_stale_b_cannot_rollback_a_newly_published_c(tmp_path):
    database, a = _published_a(tmp_path)
    b, frozen = _stage_update(database)
    c, _ = _stage_update(database, version="body-c")
    with sqlite3.connect(database) as db:
        _approve(db, c, {"basis": "explicit isolated body-c fixture approval"})
        before = dict(db.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates"))
        with pytest.raises(ValueError, match="content comparison baseline mismatch"):
            stage_snapshot(db, deepcopy(frozen))
        assert before == dict(db.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates"))
        assert db.execute("SELECT published_candidate_sha256,latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone() == (c, c)
    assert _reader(database, tmp_path).article("good")["content_version_id"] == "body-c"


def test_old_owner_pass_cannot_approve_new_content_scope(tmp_path):
    from climate_registry.owner_checks import validate_review
    database, _ = _published_a(tmp_path)
    b, frozen = _stage_update(database)
    receipt = {"schema_version": "owner-agent-information-review.v2", "candidate_sha256": b, "decision": "pass"}
    with pytest.raises(ValueError, match="cannot approve complete content update scope"):
        validate_review(frozen, receipt)
    with sqlite3.connect(database) as db:
        with pytest.raises(ValueError, match="cannot approve complete content update scope"):
            _approve(db, b, receipt)
        with pytest.raises(ValueError, match="not legacy acceptance"):
            _approve(db, b, {"basis": "old acceptance cannot authorize a new scope"}, status="accepted_legacy")


def test_native_review_packet_and_same_body_restaging_use_one_frozen_contract(tmp_path):
    from climate_registry.enrichment_contract import review_projection
    from climate_registry.publication import prepare_review, stage_entities
    database, _ = _published_a(tmp_path)
    b, frozen = _stage_update(database)
    packet = prepare_review(database, tmp_path/"native-review")
    item = next(item for item in packet["candidates"] if item["entity_id"] == "good")
    assert item["candidate_sha256"] == b
    assert item["enrichment_contract"] == review_projection(frozen)
    assert item["enrichment_contract_sha256"] == digest(item["enrichment_contract"])
    with sqlite3.connect(database) as db:
        _approve(db, b, {"basis": "explicit isolated scope approval"})
        for _ in range(2):
            stage_entities(db, article_ids={"good"})
            assert db.execute("SELECT latest_candidate_sha256,published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone() == (b, b)


@pytest.mark.parametrize("hold", [False, True])
def test_owner_review_projection_keeps_shared_capture_uses_and_hold_explicit(tmp_path, hold):
    from climate_registry.enrichment_contract import inspect_candidate
    from climate_registry.owner_checks import owner_review_template
    database = _seed(tmp_path)
    manifest = _editorial_manifest(database, disposition="hold" if hold else "complete")
    manifest["checked_at"] = "2099-10-15T23:00:00Z"
    result = _ingest(database, tmp_path, manifest)
    sha = result["candidate_sha256"]
    contract = inspect_candidate(database, sha)["contract"]
    fields = contract["proposed"]["source_fields"]
    if hold:
        assert fields[0]["editorial_metadata"] is None
        assert contract["field_states"]["taxonomy"] == "source_only_hold"
    else:
        assert fields[0]["capture"]["sha256"] == fields[0]["editorial_metadata"]["capture_sha256"]
    assert fields[0]["field_evidence"]["summary"]["support"] == "complete"
    assert fields[0]["original_comparisons"] == manifest["observations"][0]["original_comparisons"]
    assert fields[0]["excluded_fields"] == manifest["observations"][0]["excluded_fields"]
    assert contract["taxonomy_bindings"] == [manifest["taxonomy"]]
    assert owner_review_template(database, sha)["enrichment_contract"] == contract
    assert contract["inherited"]["fields"] == "none_implied"
    assert contract["content_comparison_baseline"]["status"] == "not_applicable"


def test_same_url_pdf_does_not_supply_g3_content_identity(tmp_path):
    from climate_registry.enrichment_contract import inspect_candidate
    database = _seed(tmp_path, "same_url")
    with sqlite3.connect(database) as db:
        sha = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0]
    contract = inspect_candidate(database, sha)["contract"]
    pdf = next(row for row in contract["source_entries"]["pdf_links"] if row["pdf_article_id"] == "pdf-good")
    assert pdf["core_article_id"] is None and pdf["content_binding"] == "unavailable"
    assert pdf["reason"] == "no_stored_canonical_article_link"


@pytest.mark.parametrize("field", ["publication_date", "updated_date", "revision_date"])
def test_human_date_projection_retains_real_authority_on_first_and_replacement_decisions(tmp_path, field):
    from climate_registry.enrichment_contract import inspect_candidate
    database, _ = _published_a(tmp_path)
    b, _ = _stage_update(database)
    with sqlite3.connect(database) as db:
        _approve(db, b, {"basis": "explicit isolated G3 fixture approval"})
    for value in ("2026-10-22", "2026-10-23"):
        result = _ingest(database, tmp_path, _manual(database, fields={field: value}))
        contract = inspect_candidate(database, result["candidate_sha256"])["contract"]
        assert contract["date_and_sort_basis"]["content_date"] == value
        assert contract["date_and_sort_basis"]["content_date_authority"] == "human_override"
        assert contract["authority"]["new_human_authorization"]["independent_source_pass"] == "not_applicable"
        assert contract["inherited"]["fields"] == "all_approved_fields_except_selected_dates"
        assert contract["content_comparison_baseline"]["content_version_id"] == "body-a"
        actual = _reader(database, tmp_path).article("good")
        for key in ("content_date", "content_date_basis", "content_date_authority", "content_date_precision"):
            assert contract["date_and_sort_basis"][key] == actual[key]
    revoke = _manual(database, fields={field: "2026-10-23"})
    revoke["manual_authorization"].update(action="revoke", instruction="Revoke record good " + field + " override.")
    revoke["observations"][0].update(public_fields={field: None},
        field_evidence={field: {"text": "revoke", "precision": "unknown"}})
    result = _ingest(database, tmp_path, revoke)
    contract = inspect_candidate(database, result["candidate_sha256"])["contract"]
    actual = _reader(database, tmp_path).article("good")
    for key in ("content_date", "content_date_basis", "content_date_authority", "content_date_precision"):
        assert contract["date_and_sort_basis"][key] == actual[key]
    assert actual["content_date"] == "2026-10-16"
    assert actual["content_date_authority"] == "source"
    assert not actual.get("manual_date_overrides")


@pytest.mark.parametrize("mutation", [None, "wrong_schema", "wrong_version", "arbitrary_field", "wrong_scope", "unrelated_capture", "unknown_capture", "invented_human_decision", "invented_human_authority", "wrong_existing_human_value", "wrong_existing_human_reference"])
def test_cli_validation_is_read_only_and_rejects_invalid_scope_and_binding(tmp_path, capsys, mutation):
    from climate_registry.cli import main
    database, _ = _published_a(tmp_path)
    if mutation in {"wrong_existing_human_value", "wrong_existing_human_reference"}:
        _ingest(database, tmp_path, _manual(database))
    _, snapshot = _stage_update(database)
    scope = CONTENT_SCOPE
    if mutation == "wrong_schema":
        snapshot["schema_version"] = "arbitrary.v1"
    elif mutation == "wrong_version":
        snapshot["derived_display"]["content_update"]["summary_content_version_id"] = "body-a"
    elif mutation == "arbitrary_field":
        snapshot["derived_display"]["unreviewed_public_fact"] = "invented"
    elif mutation == "wrong_scope":
        scope = "initial_article_content"
    elif mutation == "unrelated_capture":
        snapshot["derived_display"]["content_update"]["date_evidence"]["updated_date"]["final_url"] = "https://example.org/unrelated"
    elif mutation == "unknown_capture":
        snapshot["derived_display"]["content_update"]["date_evidence"]["updated_date"]["captured_at"] = "2026-10-18T00:00:00Z"
    elif mutation == "invented_human_decision":
        snapshot["derived_display"]["manual_date_overrides"] = {"publication_date": {"scope": "record", "new_value": "2026-10-22", "instruction_reference": "invented"}}
    elif mutation == "invented_human_authority":
        snapshot["derived_display"]["publication_date_evidence"] = {"authority": "human_override", "instruction_reference": "invented"}
    elif mutation == "wrong_existing_human_value":
        snapshot["derived_display"]["publication_date"] = "2026-10-29"
    elif mutation == "wrong_existing_human_reference":
        snapshot["derived_display"]["publication_date_evidence"]["instruction_reference"] = "invented"
    path = tmp_path/"input.json"
    path.write_text(json.dumps(snapshot))
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    code = main(["validate-enrichment-contract", "--database", str(database), "--input", str(path), "--scope", scope])
    output = json.loads(capsys.readouterr().out)
    if mutation:
        assert code == 2 and output["kind"] == "input"
    else:
        assert code == 0 and output["status"] == "valid"
        assert output["write_performed"] is False and output["approval"] == "not_performed"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("editorial", [False, True])
def test_owner_proposed_publication_date_sort_matches_exact_approved_version(tmp_path, editorial):
    from climate_registry.enrichment_contract import review_projection
    database = _seed(tmp_path)
    factory = _editorial_manifest if editorial else _manifest
    manifest = factory(database, fields={"title": "Climate risk findings",
        "summary": "Flood exposure increased in the study region.", "publication_date": "2026-10-09"},
        body="Climate risk findings\nFlood exposure increased in the study region.\nPublished: 2026-10-09")
    manifest["checked_at"] = "2099-10-15T23:00:00Z"
    sha = _ingest(database, tmp_path, manifest)["candidate_sha256"]
    with sqlite3.connect(database) as db:
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
    proposed = review_projection(snapshot)["date_and_sort_basis"]
    assert proposed["content_date"] == "2026-10-09"
    assert proposed["content_date_basis"] == "publication_date"
    assert proposed["content_date_authority"] == "source"
    assert proposed["content_date_evidence"]["text"] == "2026-10-09"
    review = _review(database, tmp_path, sha)
    review["reviewed_at"] = "2099-10-15T23:01:00Z"
    approve_owner_check(database, review, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    actual = _reader(database, tmp_path).article("good")
    for field in ("content_date", "content_date_basis", "content_date_authority", "content_date_precision"):
        assert proposed[field] == actual[field]


@pytest.mark.parametrize("field", ["publication_date", "updated_date", "revision_date"])
@pytest.mark.parametrize("boundary", ["validate", "stage", "approve"])
def test_g3_date_values_cannot_be_published_without_bound_evidence(tmp_path, field, boundary):
    from climate_registry.enrichment_contract import validate_input
    database, a = _published_a(tmp_path)
    _, snapshot = _stage_update(database)
    snapshot["derived_display"][field] = "2030-01-01"
    with sqlite3.connect(database) as db:
        before = db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0]
        assert before == a
        with pytest.raises(ValueError, match="date.*(bound|evidence|inherit)"):
            if boundary == "validate":
                validate_input(database, snapshot, CONTENT_SCOPE)
            elif boundary == "stage":
                stage_snapshot(db, snapshot)
            else:
                # A candidate retained before the validator fix still needs final checks.
                sha = digest(snapshot)
                db.execute("INSERT INTO registry_candidates VALUES(?,?,?,?,?,?)", (sha, "article", "good", json.dumps(snapshot), "2026-10-25", "retained earlier G3 candidate"))
                db.execute("UPDATE registry_publication SET latest_candidate_sha256=? WHERE entity_kind='article' AND entity_id='good'", (sha,))
                _approve(db, sha, {"basis": "explicit isolated rejected-date fixture"})
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0] == before


@pytest.mark.parametrize("field", ["publication_date", "updated_date", "revision_date"])
def test_g3_unchanged_approved_date_needs_no_new_proof(tmp_path, field):
    from climate_registry.enrichment_contract import validate_input
    from test_owner_publication_dates import _date_fields_manifest, _date_review, _approve as approve_date
    database, _ = _published_a(tmp_path)
    if field != "publication_date":
        b, snapshot = _stage_update(database)
        with sqlite3.connect(database) as db:
            _approve(db, b, {"basis": "explicit isolated same-body fixture approval"})
    manifest = _date_fields_manifest(database, fields=(field,))
    correction = _ingest(database, tmp_path, manifest)
    approve_date(database, tmp_path, _date_review(database, tmp_path, correction["candidate_sha256"]))
    if field == "publication_date":
        _, snapshot = _stage_update(database)
    date = manifest["observations"][0]["public_fields"][field]
    snapshot["derived_display"][field] = date
    snapshot["derived_display"].pop(field + "_evidence", None)
    snapshot["derived_display"].pop(field + "_verification", None)
    assert validate_input(database, snapshot, CONTENT_SCOPE)["status"] == "valid"
    with sqlite3.connect(database) as db:
        candidate = stage_snapshot(db, snapshot)
        _approve(db, candidate, {"basis": "explicit isolated inherited-date fixture approval"})
    assert _reader(database, tmp_path).article("good")[field] == date


@pytest.mark.parametrize("field", ["publication_date", "updated_date", "revision_date"])
def test_source_date_overlay_inspection_matches_exact_approved_sort(tmp_path, capsys, field):
    import base64
    from climate_registry.cli import main
    from test_owner_publication_dates import _date_fields_manifest, _date_review, _approve as approve_date
    database, _ = _published_a(tmp_path)
    b, frozen = _stage_update(database)
    with sqlite3.connect(database) as db:
        _approve(db, b, {"basis": "explicit isolated complete content approval"})
    before = _reader(database, tmp_path).article("good")
    assert before["content_date"] == "2026-10-16"
    manifest = _date_fields_manifest(database, fields=(field,))
    entry = manifest["observations"][0]
    entry["public_fields"][field] = "2026-10-20"
    entry["field_evidence"][field].update(value="2026-10-20", excerpt="20 October 2026")
    entry["original_comparisons"] = {field: {"status": "conflict"}} if entry["target"]["fields"].get(field) else {}
    raw = (entry["source_identity"]["excerpt"] + "\n" + field + ": 20 October 2026").encode()
    entry["capture"].update(text=raw.decode(), bytes_base64=base64.b64encode(raw).decode(),
        sha256=hashlib.sha256(raw).hexdigest(), captured_at="2026-10-20T11:59:00Z")
    manifest["checked_at"] = "2026-10-20T12:00:00Z"
    sha = _ingest(database, tmp_path, manifest)["candidate_sha256"]
    assert main(["inspect-enrichment-contract", "--database", str(database), "--candidate-sha256", sha]) == 0
    inspection = json.loads(capsys.readouterr().out)
    proposed = inspection["contract"]["date_and_sort_basis"]
    assert inspection["write_performed"] is False
    assert proposed["content_date"] == "2026-10-20"
    assert proposed["content_date_basis"] == field
    assert proposed["content_date_authority"] == "source"
    assert proposed["content_date_evidence"]["text"] == "20 October 2026"
    from climate_registry.owner_dates import date_proposed_view
    with sqlite3.connect(database) as db:
        candidate = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
    verification = date_proposed_view(candidate)["derived_display"][field + "_verification"]
    assert verification["review_status"] == "pending"
    assert verification["content_version_id"] == "body-b"
    assert not {"reviewer", "review_id"} & verification.keys()
    assert _reader(database, tmp_path).article("good")["content_date"] == "2026-10-16"
    receipt = _date_review(database, tmp_path, sha)
    receipt["reviewed_at"] = "2026-10-20T12:01:00Z"
    approve_date(database, tmp_path, receipt)
    actual = _reader(database, tmp_path).article("good")
    for key in ("content_date", "content_date_basis", "content_date_authority", "content_date_precision", "content_date_normalization"):
        assert actual[key] == proposed[key]
    for key in ("text", "date_kind", "source_url", "final_url", "capture_sha256", "source_date_precision"):
        assert actual["content_date_evidence"].get(key) == proposed["content_date_evidence"].get(key)
    assert actual["content_update"] == frozen["derived_display"]["content_update"]
    for key in ("summary", "categories", "keywords"):
        assert actual[key] == before[key]


def test_inspected_explicit_human_revocation_restores_unknown_date(tmp_path):
    from climate_registry.enrichment_contract import inspect_candidate
    database = _seed(tmp_path)
    before = _reader(database, tmp_path).article("good")
    assert before["content_date_basis"] == "unknown"
    _ingest(database, tmp_path, _manual(database))
    revoke = _manual(database)
    revoke["manual_authorization"].update(action="revoke", instruction="Revoke record good publication_date override.")
    revoke["observations"][0].update(public_fields={"publication_date": None},
        field_evidence={"publication_date": {"text": "revoke", "precision": "unknown"}})
    result = _ingest(database, tmp_path, revoke)
    proposed = inspect_candidate(database, result["candidate_sha256"])["contract"]["date_and_sort_basis"]
    actual = _reader(database, tmp_path).article("good")
    assert proposed["content_date"] is None
    assert proposed["content_date_basis"] == "unknown"
    assert proposed["content_date_authority"] == "unknown"
    for key in ("content_date", "content_date_basis", "content_date_authority", "content_date_precision"):
        assert actual[key] == proposed[key] == before[key]


@pytest.mark.parametrize("field", ["publication_date", "updated_date", "revision_date"])
def test_source_date_proposal_keeps_applicable_human_authority_until_replacement(tmp_path, field):
    from climate_registry.enrichment_contract import inspect_candidate
    from climate_registry.errors import RegistryInputError
    from test_owner_publication_dates import _date_fields_manifest, _date_review, _approve as approve_date
    database, _ = _published_a(tmp_path)
    b, _ = _stage_update(database)
    with sqlite3.connect(database) as db:
        _approve(db, b, {"basis": "explicit isolated complete content approval"})
    _ingest(database, tmp_path, _manual(database, fields={field: "2026-10-22"}))
    manifest = _date_fields_manifest(database, fields=(field,))
    sha = _ingest(database, tmp_path, manifest)["candidate_sha256"]
    contract = inspect_candidate(database, sha)["contract"]
    assert contract["proposed"]["source_fields"][0]["public_fields"][field] == manifest["observations"][0]["public_fields"][field]
    assert contract["date_and_sort_basis"]["content_date"] == "2026-10-22"
    assert contract["date_and_sort_basis"]["content_date_authority"] == "human_override"
    with pytest.raises(RegistryInputError, match="active human date override"):
        approve_date(database, tmp_path, _date_review(database, tmp_path, sha))
    assert _reader(database, tmp_path).article("good")["content_date"] == "2026-10-22"


def test_activity_range_inspection_retains_article_sort_and_exact_proposed_bounds(tmp_path, capsys):
    import base64
    from climate_registry.cli import main
    from test_owner_activity_dates import _manifest as activity_manifest
    from test_owner_publication_dates import _date_review, _approve as approve_date
    database, _ = _published_a(tmp_path)
    b, _ = _stage_update(database)
    with sqlite3.connect(database) as db:
        _approve(db, b, {"basis": "explicit isolated complete content approval"})
    manifest = activity_manifest(database)
    entry = manifest["observations"][0]
    value = entry["public_fields"]["activity_dates"][0]
    value["evidence"].update(event_end_date="2026-10-23", date_text="21–23 October 2026", text="21–23 October 2026")
    body = entry["source_identity"]["excerpt"] + "\nClimate risk conference\n21–23 October 2026"
    raw = body.encode()
    entry["field_evidence"]["activity_dates"].update(excerpt=body)
    entry["capture"].update(text=body, bytes_base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
    sha = _ingest(database, tmp_path, manifest)["candidate_sha256"]
    assert main(["inspect-enrichment-contract", "--database", str(database), "--candidate-sha256", sha]) == 0
    contract = json.loads(capsys.readouterr().out)["contract"]
    bounds = contract["proposed"]["source_fields"][0]["public_fields"]["activity_dates"][0]["evidence"]
    assert (bounds["event_start_date"], bounds["event_end_date"]) == ("2026-10-21", "2026-10-23")
    assert contract["date_and_sort_basis"]["content_date"] == "2026-10-16"
    assert not _reader(database, tmp_path).article("good").get("activity_dates")
    approve_date(database, tmp_path, _date_review(database, tmp_path, sha))
    actual = _reader(database, tmp_path).article("good")
    assert (actual["activity_dates"][0]["start_date"], actual["activity_dates"][0]["end_date"]) == ("2026-10-21", "2026-10-23")
    assert actual["content_date"] == contract["date_and_sort_basis"]["content_date"]
    assert actual["content_date_basis"] == contract["date_and_sort_basis"]["content_date_basis"]


@pytest.mark.parametrize("field", ["publication_date", "updated_date", "revision_date"])
def test_g3_dates_accept_current_body_evidence_without_changing_the_content_package(tmp_path, field):
    from climate_registry.enrichment_contract import validate_input, review_projection
    database, _ = _published_a(tmp_path)
    _, snapshot = _stage_update(database, publication_text=True,
        date_field="revision_date" if field == "revision_date" else "updated_date")
    bundle = deepcopy(snapshot["derived_display"]["content_update"])
    proof = ({"value": "2026-10-02", "date_kind": "published", "text": "2 October 2026",
        "source_date_precision": "day", "content_version_id": "body-b", "source_url": "https://example.org/good",
        "final_url": "https://example.org/good", "captured_at": "2026-10-16T01:00:00Z"}
        if field == "publication_date" else deepcopy(bundle["date_evidence"][field]))
    snapshot["derived_display"].update({field: proof["value"], field + "_evidence": proof})
    assert validate_input(database, snapshot, CONTENT_SCOPE)["status"] == "valid"
    proposed = review_projection(snapshot)["date_and_sort_basis"]
    with sqlite3.connect(database) as db:
        sha = stage_snapshot(db, snapshot)
        _approve(db, sha, {"basis": "explicit isolated current-source-date fixture approval"})
    actual = _reader(database, tmp_path).article("good")
    assert actual[field] == proof["value"] and actual[field + "_evidence"] == proof
    assert actual["content_update"] == bundle
    assert actual["content_date"] == proposed["content_date"]


@pytest.mark.parametrize("mutation", ["version", "source_url", "final_url", "capture_time", "precision", "literal_text"])
def test_g3_publication_source_date_requires_exact_current_capture(tmp_path, mutation):
    from climate_registry.enrichment_contract import validate_input
    database, _ = _published_a(tmp_path)
    _, snapshot = _stage_update(database, publication_text=True)
    proof = {"value": "2026-10-02", "date_kind": "published", "text": "2 October 2026",
        "source_date_precision": "day", "content_version_id": "body-b", "source_url": "https://example.org/good",
        "final_url": "https://example.org/good", "captured_at": "2026-10-16T01:00:00Z"}
    field, value = {"version": ("content_version_id", "body-a"), "source_url": ("source_url", "https://example.org/other"),
        "final_url": ("final_url", "https://example.org/other"), "capture_time": ("captured_at", "2026-10-17T00:00:00Z"),
        "precision": ("source_date_precision", "month"), "literal_text": ("text", "1 January 2030")}[mutation]
    proof[field] = value
    snapshot["derived_display"].update(publication_date=proof["value"], publication_date_evidence=proof)
    with pytest.raises(ValueError):
        validate_input(database, snapshot, CONTENT_SCOPE)
