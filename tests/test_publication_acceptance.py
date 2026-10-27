"""G6 contract fixtures. These tests are not production surface acceptance."""
import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from climate_registry import acceptance
from climate_registry.cli import main as registry_main
from climate_registry.acquisition_review import digest
from climate_registry.publication import _approve, export_public_snapshot, public_revision, snapshot_entity, stage_snapshot
from test_article_content_dates import _manual, ordered_source_check_fixture_clock
from test_enrichment_contract import _published_a, _stage_update, _native_contract_review
from test_owner_checks import _ingest
from test_owner_publication_dates import _seed


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _entry(target, identity="good", kind="article"):
    return next(row for row in target["entities"] if row["entity_id"] == identity and row["entity_kind"] == kind)


def _approved_update(tmp_path, monkeypatch):
    database, old = _published_a(tmp_path)
    _ingest(database, tmp_path, _manual(database))
    sha, _ = _stage_update(database)
    _native_contract_review(database, tmp_path, monkeypatch)
    return database, old, sha


def _observation(target):
    pdf_ids, calendar_ids = acceptance._pdf_identities(target)
    records = list({tuple(row["public_entity"]): {"entity_kind": row["public_entity"][0], "entity_id": row["public_entity"][1],
        "payload": deepcopy(row["facts"])} for row in target["entities"] if row["present"]}.values())
    mappings = [{"entity_kind": row["entity_kind"], "entity_id": row["entity_id"],
        "candidate_sha256": row["published_candidate_sha256"], "facts_sha256": digest(row["facts"])}
        for row in target["entities"] if row["present"]]
    documents, queries = {}, []
    for row in target["entities"]:
        if not row["present"]:
            continue
        pages = [page for page in row["pages"] if page in target["pages"]]
        if not pages:
            continue
        page = pages[0]
        content = target["pages"][page]
        docid = "corpus/" + page
        documents[docid] = {"page": page, "markdown": content}
        retrieval = row["retrieval_facts"]
        excerpt = retrieval.get("summary") or retrieval.get("name") or retrieval.get("title")
        if not excerpt or excerpt not in content:
            excerpt = next(line for line in content.splitlines() if line and not line.startswith("#"))
        if retrieval.get("content_update"):
            excerpt = content
        answer = " ".join(value["text"] for value in row["query_requirements"]) or excerpt
        if retrieval.get("event_id"):
            excerpt = content
            answer = (retrieval.get("name") or "") + ". " + (retrieval.get("summary") or "") + "\n" + "\n".join(
                ("Deadline" if field == "deadline_date" else field.replace("_", " ").capitalize()) + ": " + str(retrieval[field]) for field in (
                    "start_date", "end_date", "deadline_date", "date_precision", "deadline_type") if retrieval.get(field))
        hit = {"entity_kind": row["serving_entity"][0], "entity_id": row["serving_entity"][1],
            "document_id": docid, "candidate_sha256": row["serving_candidate_sha256"], "excerpt": excerpt,
            "date": retrieval.get("content_date") or "", "date_kind": retrieval.get("content_date_basis"),
            "date_metadata": {key: deepcopy(retrieval.get(key)) for key in acceptance.RETRIEVAL_DATE_FIELDS}}
        queries.append({"query": "current content and what changed for " + row["entity_id"],
            "target": {key: row[key] for key in ("entity_kind", "entity_id")}, "hits": [hit], "answer": answer,
            "citations": [{"document_id": docid, "candidate_sha256": hit["candidate_sha256"], "excerpt": excerpt,
                **{key: deepcopy(hit[key]) for key in ("date", "date_kind", "date_metadata")}}]})
    return {"activation": {"state": "active", "generation_id": "observed-generation",
            "registry_revision": target["registry_revision"], "mappings": mappings},
        "records": records, "pages": target["pages"], "documents": documents, "queries": queries,
        "record_totals": {kind: sum(row["entity_kind"] == kind for row in records) for kind in ("article", "pdf_article", "meeting", "pdf_meeting")},
        "corpus_document_count": len(documents),
        "article_order": target["article_order"],
        "manifest": {"registry_revision": target["registry_revision"], "pages": {key: _hash(value) for key, value in target["pages"].items()},
            "pdf_occurrence_ids": sorted(pdf_ids), "pdf_calendar_occurrence_ids": sorted(calendar_ids)}}


def _capture(tmp_path, observed, *, kind="fixture", name="capture"):
    path = tmp_path / (name + ".json")
    path.write_text(json.dumps(observed), encoding="utf-8")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "observed_at": datetime.now(timezone.utc).isoformat(), "endpoint": "https://user:secret@example.org/api/registry/articles/good?token=SECRET",
        "command": "read surface; authorization=SECRET", "evidence_kind": kind}


def test_frozen_registry_and_each_surface_have_separate_results(tmp_path, monkeypatch):
    database, old, sha = _approved_update(tmp_path, monkeypatch)
    target = acceptance.freeze_target(database)
    item = _entry(target)
    assert item["published_candidate_sha256"] == sha and item["receipt_references"]
    assert item["facts"]["publication_date"] == "2026-10-02"
    assert item["facts"]["content_date"] == "2026-10-16"
    assert item["facts"]["content_date_basis"] == "updated_date"
    assert item["facts"]["summary"] == "Flood losses rose to 20 billion."
    assert item["facts"]["content_update"]["change_note"]
    assert item["comparison_baseline"]["candidate_sha256"] == old
    assert item["approval_baseline"]["candidate_sha256"] != old
    assert item["published_changes"]["status"] == "compared"
    assert item["evidence_references"]
    evidence = _capture(tmp_path, _observation(target))
    for surface in acceptance.SURFACES:
        result = acceptance.check_surface(target, surface, evidence)
        assert result["status"] == "fixture_match", (surface, result)
    report = acceptance.accept_publication(database, selection=[{"entity_kind": "article", "entity_id": "good", "candidate_sha256": sha}],
        captures={name: evidence for name in acceptance.SURFACES})
    assert report["registry_publication"]["status"] == "pass"
    assert report["local_display"]["status"] == "pass"
    assert report["status"] == "blocking"  # A fixture cannot certify a production observation.
    encoded = json.dumps(report)
    assert "SECRET" not in encoded and "secret" not in encoded and "Flood losses" not in encoded
    assert report["surfaces"]["display_api"]["observed"]["endpoint"] == "https://example.org/api/registry/articles/good"


@pytest.mark.parametrize("surface", acceptance.SURFACES)
@pytest.mark.parametrize("change", ["date_old_summary", "summary_old_note", "body", "version", "date_basis", "manual_label"])
def test_mixed_current_fields_fail_on_each_surface(tmp_path, monkeypatch, surface, change):
    database, _, _ = _approved_update(tmp_path, monkeypatch)
    target = acceptance.freeze_target(database)
    observation = _observation(target)
    if surface in {"display_api", "wiki_export"}:
        record = observation["records"][0]["payload"]
        if change == "date_old_summary": record["summary"] = "Flood losses reached 10 billion."
        elif change == "summary_old_note": record["content_update"]["change_note"] = "Old initial version."
        elif change == "body": record["available_content"]["markdown"] = "Old retained body"
        elif change == "version": record["content_version_id"] = "body-a"
        elif change == "date_basis": record["content_date_basis"] = "operation_time"
        else: record["content_date_authority"] = "human_override"
    else:
        document = next(iter(observation["documents"].values()))
        document["markdown"] = document["markdown"].replace("20 billion", "10 billion")
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observation))
    assert result["status"] == "fail", result


@pytest.mark.parametrize("surface", acceptance.SURFACES)
@pytest.mark.parametrize("change", ["prepared", "missing_mapping", "old_revision", "generic_answer", "history_only"])
def test_generation_strings_generic_answers_and_history_are_not_current_proof(tmp_path, surface, change):
    database = _seed(tmp_path)
    target = acceptance.freeze_target(database)
    observation = _observation(target)
    if change == "prepared": observation["activation"]["state"] = "prepared"
    elif change == "missing_mapping": observation["activation"]["mappings"] = []
    elif change == "old_revision": observation["activation"]["registry_revision"] = "old-registry-revision"
    elif change == "generic_answer":
        observation["queries"] = [{"query": "climate", "answer": "Climate change matters.", "hits": [], "citations": []}]
    else:
        for query in observation["queries"]:
            for hit in query["hits"]: hit["role"] = "history"
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observation))
    if change in {"generic_answer", "history_only"} and surface in {"display_api", "wiki_export"}:
        assert result["status"] == "fixture_match"
    elif change in {"generic_answer", "history_only", "old_revision", "prepared"}:
        assert result["status"] == "fail", result
    else:
        assert result["status"] == "unverified", result


def test_registry_publication_failure_survives_a_display_refresh_rollback(tmp_path, monkeypatch):
    database = _seed(tmp_path)
    with sqlite3.connect(database) as db:
        pending = snapshot_entity(db, "article", "good")
        pending.setdefault("derived_display", {})["categories"] = ["Transition Risk"]
        pending_sha = stage_snapshot(db, pending)
    # A refresh failure in the approval transaction cannot be treated as a published candidate.
    def fail_refresh(*args, **kwargs):
        raise RuntimeError("display unavailable")
    monkeypatch.setattr("climate_registry.display.refresh_display", fail_refresh)
    with pytest.raises(RuntimeError):
        with sqlite3.connect(database) as db:
            _approve(db, pending_sha, {"basis": "isolated fixture"}, status="accepted_legacy")
            fail_refresh(db)
    report = acceptance.accept_publication(database, selection=[{"entity_kind": "article", "entity_id": "good",
        "candidate_sha256": pending_sha, "disposition": "approved"}])
    assert report["registry_publication"]["status"] == "fail"
    assert report["surfaces"]["display_api"]["status"] == "unverified"


def test_exact_approved_subset_checks_held_new_and_held_existing(tmp_path):
    database = _seed(tmp_path)
    initial = acceptance.freeze_target(database)
    approved = _entry(initial)["published_candidate_sha256"]
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES('brand-new','https://example.org/new','s','2026-10-24','2026-10-24')")
        db.execute("INSERT INTO article_versions VALUES('v-new','brand-new','New title','New title','New summary','new-fingerprint','report-title-summary','2026-10-24','2026-10-24')")
        db.execute("UPDATE articles SET current_version_id='v-new' WHERE article_id='brand-new'")
        for identity in ("good", "brand-new"):
            snapshot = snapshot_entity(db, "article", identity)
            snapshot["derived_display"] = {"summary": "Pending " + identity}
            stage_snapshot(db, snapshot)
    selection = [{"entity_kind": "article", "entity_id": "good", "disposition": "held", "baseline_candidate_sha256": approved},
        {"entity_kind": "article", "entity_id": "brand-new", "disposition": "held", "baseline_candidate_sha256": None}]
    target = acceptance.freeze_target(database, selection)
    assert target["publication"]["status"] == "pass"
    assert _entry(target)["facts"]["summary"] == "Summary good"
    assert not _entry(target, "brand-new")["present"]
    observed = _observation(target)
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))["status"] == "fixture_match"
    assert acceptance.check_surface(target, "wiki_export", _capture(tmp_path, observed))["status"] == "fixture_match"
    observed["records"].append({"entity_kind": "article", "entity_id": "brand-new", "payload": {"summary": "Pending brand-new"}})
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))["status"] == "fail"


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
def test_pdf_provenance_relations_and_manual_dates_use_shared_target(tmp_path, source):
    database = _seed(tmp_path, source)
    result = _ingest(database, tmp_path, _manual(database, source="pdf" if source == "pdf" else "native"))
    target = acceptance.freeze_target(database)
    item = _entry(target, "pdf-good", "pdf_article") if source == "pdf" else _entry(target)
    assert item["facts"]["publication_date"] == "2026-10-02"
    assert item["source_fact_acceptance"] == "inherited_not_repeated"
    assert item["approval_baseline"]["candidate_sha256"] != result["candidate_sha256"]
    assert item["published_changes"]["summary_unchanged"]
    if source == "native":
        return
    observation = _observation(target)
    assert acceptance.check_surface(target, "wiki_export", _capture(tmp_path, observation))["status"] == "fixture_match"
    if source == "pdf":
        record = next(row["payload"] for row in observation["records"] if row["entity_kind"] == "pdf_article")
        record["occurrences"][0]["page"] = 999
        assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observation))["status"] == "fail"
    observation["manifest"]["pdf_occurrence_ids"] = []
    assert acceptance.check_surface(target, "wiki_export", _capture(tmp_path, observation))["status"] == "fail"


def test_unreachable_and_changed_revision_preserve_observed_unknown(tmp_path, monkeypatch):
    database = _seed(tmp_path)
    target = acceptance.freeze_target(database)
    result = acceptance.check_surface(target, "rag", {"path": str(tmp_path/"unreachable.json"),
        "last_observed_generation_id": "last-observed-only"})
    assert result["status"] == "unverified" and result["last_observed_generation_id"] == "last-observed-only"
    assert "generation_id" not in result
    real_freeze = acceptance.freeze_target
    def freeze_and_change(*args, **kwargs):
        frozen = real_freeze(*args, **kwargs)
        _ingest(database, tmp_path, _manual(database))
        return frozen
    monkeypatch.setattr(acceptance, "freeze_target", freeze_and_change)
    report = acceptance.accept_publication(database)
    assert report["frozen_target"]["registry_revision"] == target["registry_revision"]
    assert not report["frozen_target_is_latest"] and report["status"] == "blocking"
    assert report["gaps"][0]["code"] == "registry_revision_changed_after_freeze"


def test_public_export_is_read_and_hash_checked_but_cannot_prove_activation(tmp_path):
    database = _seed(tmp_path)
    path = tmp_path/"public-registry.json"
    export_public_snapshot(database, path)
    target = acceptance.freeze_target(database)
    before = path.read_bytes()
    result = acceptance.check_local_export(target, path)
    assert result["content_status"] == "match", result
    assert result["status"] == "unverified" and path.read_bytes() == before
    value = json.loads(before)
    value["articles"][0]["summary"] = "stale"
    path.write_text(json.dumps(value))
    assert acceptance.check_local_export(target, path)["status"] == "unverified"


def test_readonly_entrypoint_denies_business_mutations_and_only_writes_designated_report(tmp_path, monkeypatch):
    database = _seed(tmp_path)
    selection = tmp_path/"selection.json"
    target = acceptance.freeze_target(database)
    selection.write_text(json.dumps([{"entity_kind": "article", "entity_id": "good",
        "candidate_sha256": _entry(target)["published_candidate_sha256"]}]))
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    original_connect = sqlite3.connect
    writes = []
    write_actions = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_ATTACH}
    def readonly_connect(*args, **kwargs):
        assert "mode=ro" in args[0]
        connection = original_connect(*args, **kwargs)
        def authorize(action, *details):
            if action in write_actions:
                writes.append((action, details))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)
        return connection
    monkeypatch.setattr(sqlite3, "connect", readonly_connect)
    report = tmp_path/"acceptance.json"
    assert registry_main(["accept-publication", "--database", str(database), "--selection", str(selection), "--report", str(report)]) == 1
    assert not writes
    assert json.loads(report.read_text())["business_writes"] is False
    for path, content in before.items():
        assert path.read_bytes() == content, path
    assert {path for path in tmp_path.rglob("*") if path.is_file()} == set(before) | {report}
    assert registry_main(["accept-publication", "--database", str(database), "--report", str(report)]) == 2


def test_rag_collector_reads_the_actual_existing_corpus_and_search(tmp_path, monkeypatch):
    from agentic_wiki.wiki_agent import WikiKnowledgeBase
    database, _, _ = _approved_update(tmp_path, monkeypatch)
    target = acceptance.freeze_target(database)
    # Existing KB construction is a fixture setup. The collector receives the loaded instance.
    wiki = tmp_path/"wiki"; wiki.mkdir()
    sources = tmp_path/"sources"; sources.mkdir()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    kb = WikiKnowledgeBase(wiki_dir=wiki, source_dir=sources)
    observed = acceptance.observe_rag(kb, [{"entity_kind": "article", "entity_id": "good", "query": query}
        for query in ("Flood losses 20 billion", "What changed flood loss estimate 10 billion 20 billion")])
    assert observed["queries"][0]["hits"]
    hit = observed["queries"][0]["hits"][0]
    assert "20 billion" in observed["documents"][hit["document_id"]]["markdown"]
    result = acceptance.check_surface(target, "rag", _capture(tmp_path, observed))
    assert result["status"] == "unverified"  # Loaded does not assert activated.
    assert "active_generation_not_observed" in {gap["code"] for gap in result["gaps"]}


@pytest.mark.parametrize("change", ["sha", "time", "location", "records_shape"])
def test_bad_capture_never_becomes_a_content_pass(tmp_path, change):
    target = acceptance.freeze_target(_seed(tmp_path))
    observation = _observation(target)
    capture = _capture(tmp_path, observation)
    if change == "sha": capture["sha256"] = "0" * 64
    elif change == "time": capture["observed_at"] = "2026-01-01T00:00:00"
    elif change == "location": capture["endpoint"] = ""
    else:
        capture = _capture(tmp_path, {"records": ["invalid"]})
    assert acceptance.check_surface(target, "display_api", capture)["status"] == "unverified"


def test_pdf_dates_keep_event_identity_interval_and_provenance(tmp_path):
    from test_verified_publication import _database, _pdf_pair, _check, _approve_all
    database = _database(tmp_path)
    candidate = _pdf_pair(database)
    _check(database, tmp_path, kind="meetings", ids={"target-occ"}, candidate=candidate)
    _approve_all(database)
    target = acceptance.freeze_target(database)
    item = _entry(target, "target", "pdf_meeting")
    assert item["present"] and item["facts"]["event_id"] == "target"
    assert item["facts"]["occurrence_id"] == "target-occ"
    assert item["facts"]["start_date"] == "2027-06-10" and item["facts"]["end_date"] == "2027-06-12"
    assert item["facts"]["page"] == 9 and item["facts"]["source_document_sha256"]
    observed = _observation(target)
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))["status"] == "fixture_match"
    assert acceptance.check_surface(target, "wiki_export", _capture(tmp_path, observed))["status"] == "fixture_match"
    missing_manifest = deepcopy(observed)
    missing_manifest["manifest"]["pdf_calendar_occurrence_ids"] = []
    assert acceptance.check_surface(target, "wiki_export", _capture(tmp_path, missing_manifest))["status"] == "fail"
    next(row for row in observed["records"] if row["entity_id"] == "target")["payload"]["deadline_date"] = "2099-01-01"
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))["status"] == "fail"


def test_manual_date_only_keeps_body_summary_note_and_separate_baselines(tmp_path, monkeypatch):
    database, old, sha = _approved_update(tmp_path, monkeypatch)
    before = acceptance.freeze_target(database)
    _ingest(database, tmp_path, _manual(database, fields={"publication_date": "2026-10-03"}))
    target = acceptance.freeze_target(database)
    item = _entry(target)
    assert item["facts"]["summary"] == _entry(before)["facts"]["summary"]
    assert item["facts"]["content_update"] == _entry(before)["facts"]["content_update"]
    assert item["published_changes"]["summary_unchanged"] and item["published_changes"]["content_package_unchanged"]
    assert item["source_fact_acceptance"] == "inherited_not_repeated"
    assert item["approval_baseline"]["candidate_sha256"] == sha
    assert item["comparison_baseline"]["candidate_sha256"] == old
    assert item["facts"]["publication_date"] == "2026-10-03"
    assert item["facts"]["content_date"] == "2026-10-16"


@pytest.mark.parametrize("surface", ["display_api", "wiki_export"])
def test_observed_sort_order_must_follow_g1_current_content_date(tmp_path, surface):
    target = acceptance.freeze_target(_seed(tmp_path))
    extra = deepcopy(_entry(target))
    extra["entity_id"] = "other"
    extra["public_entity"] = ["article", "other"]
    extra["facts"]["article_id"] = "other"
    extra["facts"]["publication_date"] = "2026-10-10"
    target["entities"].append(extra)
    target["article_order"] = [["article", "good"], ["article", "other"]]
    target["public_article_order"] = deepcopy(target["article_order"])
    observed = _observation(target)
    observed["article_order"] = list(reversed(target["article_order"]))
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == "fail" and "article_sort_order_differs" in {gap["code"] for gap in result["gaps"]}


def test_actual_api_body_compared_independently_from_stored_display(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api_server
    from test_verified_publication import _reader
    database = _seed(tmp_path)
    target = acceptance.freeze_target(database)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
    response = TestClient(api_server.app).get("/api/registry/articles/good")
    assert response.status_code == 200
    observed = _observation(target)
    observed["records"][0]["payload"] = response.json()
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))["status"] == "fixture_match"
    with sqlite3.connect(database) as db:
        value = json.loads(db.execute("SELECT dto FROM display_details WHERE kind='article' AND identity='good'").fetchone()[0])
        value["summary"] = "Old display summary"
        db.execute("UPDATE display_details SET dto=? WHERE kind='article' AND identity='good'", (json.dumps(value),))
    report = acceptance.accept_publication(database, selection=[{"entity_kind": "article", "entity_id": "good",
        "candidate_sha256": _entry(target)["published_candidate_sha256"]}])
    assert report["registry_publication"]["status"] == "pass" and report["local_display"]["status"] == "fail"


def test_missing_runtime_does_not_mean_remote_generation_stayed_old(tmp_path):
    target = acceptance.freeze_target(_seed(tmp_path))
    result = acceptance.check_local_runtime(target, tmp_path/"unavailable-runtime")
    assert result == {"status": "unverified", "reason": "active_pointer_missing"}


def test_runtime_manifest_snapshot_and_actual_pages_are_read_together(tmp_path):
    from climate_registry.pdf_pipeline import PROJECTION_MANIFEST_SCHEMA
    database = _seed(tmp_path)
    target = acceptance.freeze_target(database)
    root = tmp_path/"runtime"
    generation = root/"generations/g1"; generation.mkdir(parents=True)
    snapshot = root/"registry-snapshots/g1.sqlite3"; snapshot.parent.mkdir()
    snapshot.write_bytes(database.read_bytes())
    for page, content in target["pages"].items():
        (generation/page).write_text(content, encoding="utf-8")
    manifest = {"schema_version": PROJECTION_MANIFEST_SCHEMA, "generation_id": "g1", "web_items": [],
        "pdf_occurrence_ids": [], "pdf_calendar_occurrence_ids": []}
    manifest_path = generation/"intake-manifest.json"; manifest_path.write_text(json.dumps(manifest))
    metadata = {"generation_id": "g1", "path": str(generation), "registry_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "public_registry_snapshot": str(snapshot), "public_registry_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "public_registry_revision": target["registry_revision"], "registry_database": str(database),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
    (root/"active.json").write_text(json.dumps(metadata))
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = acceptance.check_local_runtime(target, root)
    assert result["status"] == "pass", result
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    page = next(iter(target["pages"]))
    (generation/page).write_text("# Old content")
    assert acceptance.check_local_runtime(target, root)["status"] == "fail"
    (generation/page).write_text(target["pages"][page])
    metadata["public_registry_revision"] = "newer-unbacked-revision"
    (root/"active.json").write_text(json.dumps(metadata))
    assert acceptance.check_local_runtime(target, root)["status"] == "unverified"


@pytest.mark.parametrize("source", ["site", "search"])
def test_website_rotation_and_search_use_the_same_approved_target(tmp_path, source):
    from test_issue112_acquisition import _batch, _item, _database
    from climate_registry.acquisition import store_acquisition_batch
    from climate_registry.publication import migrate_publication, stage_entities
    from climate_registry.display import refresh_display
    from verified_fixtures import seed_verified_articles
    database = _database(tmp_path)
    migrate_publication(database, tmp_path/"backups", apply=True)
    item = _item(url="https://example.org/article", body="Climate insurance source evidence.", discovery_kind=source,
        discovery_ref="site-page" if source == "site" else "result-1", discovery_search_ref=None if source == "site" else "search-1")
    payload = _batch([item], searches=[] if source == "site" else None,
        search_decision={"status": "no_search", "reason": "website rotation"} if source == "site" else None)
    store_acquisition_batch(database, payload)
    with sqlite3.connect(database) as db:
        seed_verified_articles(db)
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "isolated source contract fixture"}, status="accepted_legacy")
        refresh_display(db)
    target = acceptance.freeze_target(database)
    assert any(source in row["source_paths"] and row["present"] for row in target["entities"])
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, _observation(target)))["status"] == "fixture_match"


def test_identity_history_is_retained_but_old_approval_cannot_replace_current_mapping(tmp_path):
    from test_identity_relations import _seed as identity_seed, _manifest, _ingest as ingest_identity, _approve as approve_identity
    from climate_registry.identity import _scope
    database = identity_seed(tmp_path)
    old = _entry(acceptance.freeze_target(database), "unchecked")["published_candidate_sha256"]
    manifest = _manifest(database, [("article", "unchecked")])
    manifest["aliases"] = [{"source": _scope(manifest["plan"]["participants"][1]), "same_object": True, "evidence_ids": ["semantic-proof"]}]
    staged = ingest_identity(database, tmp_path, manifest)
    approve_identity(database, tmp_path, staged)
    target = acceptance.freeze_target(database)
    alias = _entry(target, "unchecked")
    assert alias["public_entity"] == ["article", "good"] and alias["present"]
    assert alias["published_candidate_sha256"] == old
    assert alias["facts"]["published_candidate_sha256"] != old
    observed = _observation(target)
    assert acceptance.check_surface(target, "rag", _capture(tmp_path, observed))["status"] == "fixture_match"
    for query in observed["queries"]:
        for hit in query["hits"]:
            if query["target"]["entity_id"] == "unchecked": hit["candidate_sha256"] = old
    assert acceptance.check_surface(target, "rag", _capture(tmp_path, observed))["status"] == "fail"


def test_held_new_corpus_page_and_duplicate_current_records_are_detected(tmp_path):
    target = acceptance.freeze_target(_seed(tmp_path))
    held = deepcopy(_entry(target))
    held.update(entity_id="new-unpublished", public_entity=["article", "new-unpublished"], facts=None, present=False, disposition="held")
    target["entities"].append(held)
    observed = _observation(target)
    observed["documents"]["rogue"] = {"page": "article-new-unpublished.md", "markdown": "Held content"}
    assert acceptance.check_surface(target, "rag", _capture(tmp_path, observed))["status"] == "fail"
    observed = _observation(target)
    observed["records"].append(deepcopy(observed["records"][0]))
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))["status"] == "fail"
    observed["records"][-1]["role"] = "history"
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))["status"] == "fixture_match"


def test_native_dates_keep_approved_interval_deadline_and_article_link(tmp_path):
    from test_issue136_meetings import _database as meeting_database, _candidate
    from climate_monitor.meetings import process_batch
    from climate_registry.publication import stage_entities
    from verified_fixtures import seed_verified_native_meetings
    body = "World Climate Summit 2027 meets June 10–12, 2027 in New York. Example hosts it. World Climate Summit 2027 registration closes May 1, 2027. Register https://example.com/register. Climate risk agenda."
    database = meeting_database(tmp_path, [body])
    result = process_batch(database, "batch", prompt_text="source evidence", prompt_version="v1", provider="fixture", model="fixture",
        extractor=lambda request: {"events": [_candidate(deadline_evidence="World Climate Summit 2027 registration closes May 1, 2027")]})
    assert result["status"] == "succeeded"
    with sqlite3.connect(database) as db:
        seed_verified_native_meetings(db)
        stage_entities(db)
        event, sha = db.execute("SELECT entity_id,latest_candidate_sha256 FROM registry_publication WHERE entity_kind='meeting'").fetchone()
        _approve(db, sha, {"basis": "isolated exact Dates fixture"}, status="accepted_legacy")
    target = acceptance.freeze_target(database)
    item = _entry(target, event, "meeting")
    assert item["present"] and item["facts"]["deadline_date"] == "2027-05-01"
    assert item["facts"]["start_date"] == "2027-06-10" and item["facts"]["end_date"] == "2027-06-12"
    assert item["facts"]["sources"][0]["article_id"] == "article-1"
    observation = _observation(target)
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observation))["status"] == "fixture_match"
    observation["records"][0]["payload"]["sources"] = []
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observation))["status"] == "fail"


def test_month_literal_precision_and_midpoint_are_part_of_acceptance(tmp_path):
    import base64
    from test_owner_publication_dates import _date_manifest, _date_review, _approve as approve_date
    database = _seed(tmp_path)
    manifest = _date_manifest(database, date="2026-09-15")
    entry = manifest["observations"][0]
    raw = entry["capture"]["text"].replace("2026-09-15", "Sep 2026").encode()
    entry["capture"].update(text=raw.decode(), bytes_base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
    entry["field_evidence"]["publication_date"].update(excerpt="Sep 2026", date_normalization="month_midpoint", source_date_precision="month")
    staged = _ingest(database, tmp_path, manifest)
    approve_date(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    target = acceptance.freeze_target(database)
    facts = _entry(target)["facts"]
    assert facts["publication_date"] == "2026-09-15" and facts["content_date_precision"] == "month"
    assert facts["publication_date_evidence"]["text"] == "Sep 2026"
    observed = _observation(target)
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))["status"] == "fixture_match"
    observed["records"][0]["payload"]["content_date_precision"] = "day"
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))["status"] == "fail"


def test_latest_revision_unqueryable_retains_the_frozen_evidence(tmp_path, monkeypatch):
    database = _seed(tmp_path)
    original = acceptance.public_revision
    def read_revision(database, *, connection=None):
        if connection is None:
            raise OSError("unavailable secret connection address")
        return original(database, connection=connection)
    monkeypatch.setattr(acceptance, "public_revision", read_revision)
    report = acceptance.accept_publication(database)
    assert report["frozen_target"]["registry_revision"]
    assert report["observed_latest_registry_revision"] is None
    assert not report["frozen_target_is_latest"]
    assert report["gaps"][0]["code"] == "latest_registry_revision_unqueryable"
    assert "secret connection" not in json.dumps(report)


def test_unavailable_registry_writes_only_an_unverified_report(tmp_path):
    report = tmp_path/"unverified.json"
    assert registry_main(["accept-publication", "--database", str(tmp_path/"missing.sqlite3"), "--report", str(report)]) == 1
    result = json.loads(report.read_text())
    assert result["registry_publication"]["status"] == "unverified" and result["status"] == "blocking"
    assert all(row["status"] == "unverified" for row in result["surfaces"].values())
    assert not (tmp_path/"missing.sqlite3").exists()


def test_partial_inventory_cannot_prove_an_absent_held_entity(tmp_path):
    target = acceptance.freeze_target(_seed(tmp_path))
    observed = _observation(target)
    observed.pop("record_totals")
    result = acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))
    assert result["status"] == "unverified"
    assert "absence_inventory_incomplete:article" in {gap["code"] for gap in result["gaps"]}
    observed = _observation(target)
    observed.pop("corpus_document_count")
    assert acceptance.check_surface(target, "rag", _capture(tmp_path, observed))["status"] == "unverified"


@pytest.mark.parametrize("endpoint,expected", [
    ("https://user:secret@localhost:8000/api/registry/articles/good?token=SECRET", "https://localhost:8000/api/registry/articles/good"),
    ("http://[::1]:8000/api/registry/articles/good", "http://[::1]:8000/api/registry/articles/good"),
    ("/private/corpus/generation.json", "local-surface-sha256:" + _hash("/private/corpus/generation.json")),
])
def test_sanitized_observation_location_preserves_service_port_and_hides_private_paths(tmp_path, endpoint, expected):
    target = acceptance.freeze_target(_seed(tmp_path))
    capture = _capture(tmp_path, _observation(target))
    capture["endpoint"] = endpoint
    result = acceptance.check_surface(target, "display_api", capture)
    assert result["status"] == "fixture_match"
    assert result["observed"]["endpoint"] == expected


def test_default_inventory_never_claims_pending_rows_were_published(tmp_path):
    from test_verified_publication import _database
    from climate_registry.publication import stage_entities
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        stage_entities(db)
    target = acceptance.freeze_target(database)
    publication = target["publication"]
    assert publication["status"] == "unverified" and publication["mode"] == "inventory"
    assert publication["coverage"]["selected_count"] == 0
    assert publication["coverage"]["unpublished_count"] == len(target["entities"]) > 0
    assert all(row["disposition"] == "inventory" and row["publication_state"] == "unpublished"
        and not row["selected_for_acceptance"] and row["source_fact_acceptance"] == "not_published" for row in target["entities"])


def test_default_inventory_separates_pending_and_unselected_from_exact_acceptance(tmp_path):
    database = _seed(tmp_path)
    with sqlite3.connect(database) as db:
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = {"categories": ["Transition Risk"]}
        stage_snapshot(db, snapshot)
    inventory = acceptance.freeze_target(database)
    assert inventory["publication"]["status"] == "unverified"
    assert inventory["publication"]["coverage"]["pending_candidate_count"] >= 1
    assert _entry(inventory)["publication_state"] == "published" and _entry(inventory)["pending_candidate"]
    target = acceptance.freeze_target(database, [{"entity_kind": "article", "entity_id": "good",
        "candidate_sha256": _entry(inventory)["published_candidate_sha256"]}])
    assert target["publication"]["status"] == "pass" and target["publication"]["mode"] == "exact_selection"
    assert target["publication"]["coverage"]["selected_count"] == 1
    assert any(row["entity_id"] == "unchecked" for row in target["publication"]["coverage"]["unselected"])


@pytest.mark.parametrize("change", ["missing_citations", "wrong_citation", "empty_current_hits", "empty_excerpt"])
def test_accessible_chat_content_mismatches_fail_but_unqueryable_stays_unknown(tmp_path, change):
    target = acceptance.freeze_target(_seed(tmp_path))
    observed = _observation(target)
    query = observed["queries"][0]
    if change == "missing_citations": query.pop("citations")
    elif change == "wrong_citation": query["citations"][0]["document_id"] = "old-document"
    elif change == "empty_current_hits": query["hits"] = []
    else: query["hits"][0]["excerpt"] = ""
    assert acceptance.check_surface(target, "chat", _capture(tmp_path, observed))["status"] == "fail"
    query["status"] = "unqueryable"
    assert acceptance.check_surface(target, "chat", _capture(tmp_path, observed))["status"] == "unverified"
    observed["queries"] = []
    assert acceptance.check_surface(target, "chat", _capture(tmp_path, observed))["status"] == "unverified"


@pytest.mark.parametrize("path,metadata,expected", [
    ("wiki/article-other.md", {}, {"entity_kind": "article", "entity_id": "other"}),
    ("wiki/registry-meetings.md", {"entity_kind": "meeting", "entity_id": "actual-event"}, {"entity_kind": "meeting", "entity_id": "actual-event"}),
    ("wiki/registry-source-observation-occurrence-7.md", {}, {"entity_kind": None, "entity_id": None, "occurrence_id": "occurrence-7"}),
])
def test_rag_collection_never_stamps_requested_identity_onto_unrelated_hit(path, metadata, expected):
    from types import SimpleNamespace
    document = SimpleNamespace(path=path, file=Path(path).name, markdown="# Actual other document")
    chunk = SimpleNamespace(path=path, markdown="Actual other document", date_metadata=metadata)
    kb = SimpleNamespace(documents=[document], public_registry_revision="observed-revision",
        search=lambda query: [SimpleNamespace(chunk=chunk)])
    request = {"entity_kind": "article", "entity_id": "requested-good", "query": "climate risk"}
    observed = acceptance.observe_rag(kb, [request])
    hit = observed["queries"][0]["hits"][0]
    assert observed["queries"][0]["target"] == {"entity_kind": "article", "entity_id": "requested-good"}
    assert all(hit[key] == value for key, value in expected.items())
    assert hit["entity_id"] != "requested-good"


def test_existing_registry_module_is_the_only_acceptance_cli(tmp_path):
    import subprocess
    import sys
    database = _seed(tmp_path)
    report = tmp_path/"cli-report.json"
    result = subprocess.run([sys.executable, "-m", "climate_registry", "accept-publication", "--database", str(database),
        "--report", str(report)], capture_output=True, text=True)
    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocking" and payload["registry_publication_status"] == "unverified"
    assert payload["report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert json.loads(report.read_text())["command"] == "python -m climate_registry accept-publication"
    assert not hasattr(acceptance, "main")


@pytest.mark.parametrize("change", ["prepared", "wrong_candidate", "wrong_content"])
def test_observed_wrong_activation_or_existing_mapping_fails_missing_stays_unverified(tmp_path, change):
    target = acceptance.freeze_target(_seed(tmp_path))
    observed = _observation(target)
    if change == "prepared": observed["activation"]["state"] = "prepared"
    elif change == "wrong_candidate": observed["activation"]["mappings"][0]["candidate_sha256"] = "0" * 64
    else: observed["activation"]["mappings"][0]["facts_sha256"] = "0" * 64
    result = acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))
    assert result["status"] == "fail", result
    if change == "prepared": observed["activation"].pop("state")
    elif change == "wrong_candidate": observed["activation"]["mappings"][0].pop("candidate_sha256")
    else: observed["activation"]["mappings"][0].pop("facts_sha256")
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))["status"] == "unverified"


@pytest.mark.parametrize("kind", ["pdf_article", "pdf_meeting"])
def test_unknown_pdf_or_dates_hit_identity_requires_unique_observed_page_and_candidate(tmp_path, kind):
    if kind == "pdf_article":
        target = acceptance.freeze_target(_seed(tmp_path, source="pdf"))
    else:
        from test_verified_publication import _database, _pdf_pair, _check, _approve_all
        database = _database(tmp_path)
        candidate = _pdf_pair(database)
        _check(database, tmp_path, kind="meetings", ids={"target-occ"}, candidate=candidate)
        _approve_all(database)
        target = acceptance.freeze_target(database)
    observed = _observation(target)
    query = next(row for row in observed["queries"] if row["target"]["entity_kind"] == kind)
    hit = query["hits"][0]
    sha = hit.pop("candidate_sha256")
    hit.update(entity_kind=None, entity_id=None)
    result = acceptance.check_surface(target, "rag", _capture(tmp_path, observed))
    assert result["status"] == "unverified", result
    assert "retrieved_identity_unverified" in {gap["code"] for gap in result["gaps"]}
    hit["candidate_sha256"] = sha
    assert acceptance.check_surface(target, "rag", _capture(tmp_path, observed))["status"] == "fixture_match"
    hit["candidate_sha256"] = "0" * 64
    assert acceptance.check_surface(target, "rag", _capture(tmp_path, observed))["status"] == "fail"


def test_unknown_hit_with_observed_old_page_content_still_fails(tmp_path):
    target = acceptance.freeze_target(_seed(tmp_path))
    observed = _observation(target)
    hit = observed["queries"][0]["hits"][0]
    hit.update(entity_kind=None, entity_id=None, candidate_sha256=None)
    observed["documents"][hit["document_id"]]["markdown"] = "# Old page content"
    assert acceptance.check_surface(target, "rag", _capture(tmp_path, observed))["status"] == "fail"


@pytest.mark.parametrize("surface", ["rag", "chat"])
def test_unchanged_current_page_excerpt_cannot_verify_substantive_update(tmp_path, monkeypatch, surface):
    database, _, _ = _approved_update(tmp_path, monkeypatch)
    target = acceptance.freeze_target(database)
    observed = _observation(target)
    query = observed["queries"][0]
    unchanged = "# Title good"
    assert unchanged in next(iter(target["pages"].values()))
    query["hits"][0]["excerpt"] = unchanged
    query["citations"][0]["excerpt"] = unchanged
    query["answer"] = unchanged
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == "fail", result
    gaps = [gap for gap in result["gaps"] if gap["code"] == "approved_content_coverage_missing"]
    assert any(set(gap["requirements"]) == {"current_content", "change_note"} for gap in gaps)


@pytest.mark.parametrize("answer", [
    "Flood losses have climbed to 20 billion. The flood-loss estimate has risen from 10 billion to 20 billion.",
    "The revised estimate is $20 billion, double the earlier $10 billion [1].",
])
def test_faithful_synthesized_paraphrase_uses_current_citations_without_copying_excerpt(tmp_path, monkeypatch, answer):
    database, _, _ = _approved_update(tmp_path, monkeypatch)
    target = acceptance.freeze_target(database)
    observed = _observation(target)
    query = observed["queries"][0]
    query["answer"] = answer
    assert query["hits"][0]["excerpt"] not in answer
    result = acceptance.check_surface(target, "chat", _capture(tmp_path, observed))
    assert result["status"] == "fixture_match", result
    assert result["content_coverage"][0]["answered"] == ["change_note", "current_content"]


@pytest.mark.parametrize("answer", [
    "Climate risk remains important for insurers.",
    "We cannot verify that flood losses have increased to 20 billion. We cannot verify that the flood-loss estimate increased from 10 billion to 20 billion.",
    "Flood losses have not increased to 20 billion. The flood-loss estimate increased from 10 billion to 20 billion.",
    "Flood losses increased to 10 billion. The flood-loss estimate increased from 20 billion to 10 billion.",
    "The revised estimate is $10 billion, double the earlier $20 billion [1].",
])
def test_generic_negated_or_reversed_answer_cannot_borrow_a_correct_current_citation(tmp_path, monkeypatch, answer):
    database, _, _ = _approved_update(tmp_path, monkeypatch)
    target = acceptance.freeze_target(database)
    observed = _observation(target)
    observed["queries"][0]["answer"] = answer
    assert acceptance.check_surface(target, "chat", _capture(tmp_path, observed))["status"] == "fail"


@pytest.mark.parametrize("surface", ["rag", "chat"])
@pytest.mark.parametrize("field,value", [
    ("date", "2026-10-02"), ("date_kind", "publication_date"),
    ("content_date", "2026-10-02"), ("content_date_basis", "publication_date"),
    ("content_date_precision", "month"), ("content_date_authority", "operation_time"),
    ("content_version_id", "body-a"), ("published_candidate_sha256", "0" * 64),
])
def test_actual_retrieval_date_or_version_metadata_cannot_hide_behind_current_page(tmp_path, monkeypatch, surface, field, value):
    database, _, _ = _approved_update(tmp_path, monkeypatch)
    target = acceptance.freeze_target(database)
    observed = _observation(target)
    hit = observed["queries"][0]["hits"][0]
    if field in {"date", "date_kind"}: hit[field] = value
    else: hit["date_metadata"][field] = value
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == "fail", result
    assert any(gap["code"] == "retrieval_date_metadata_differs" and field in gap["fields"] for gap in result["gaps"])


@pytest.mark.parametrize("surface", ["rag", "chat"])
def test_missing_actual_retrieval_metadata_remains_unverified(tmp_path, monkeypatch, surface):
    database, _, _ = _approved_update(tmp_path, monkeypatch)
    target = acceptance.freeze_target(database)
    observed = _observation(target)
    hit = observed["queries"][0]["hits"][0]
    for field in ("date", "date_kind", "date_metadata"): hit.pop(field)
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == "unverified", result
    assert "retrieval_date_metadata_missing" in {gap["code"] for gap in result["gaps"]}


def test_actual_chat_source_date_metadata_is_independent_from_correct_rag_metadata(tmp_path, monkeypatch):
    database, _, _ = _approved_update(tmp_path, monkeypatch)
    target = acceptance.freeze_target(database)
    observed = _observation(target)
    citation = observed["queries"][0]["citations"][0]
    citation["date_metadata"]["content_date_basis"] = "publication_date"
    result = acceptance.check_surface(target, "chat", _capture(tmp_path, observed))
    assert result["status"] == "fail" and "citation_date_metadata_differs" in {gap["code"] for gap in result["gaps"]}
    for field in ("date", "date_kind", "date_metadata"): citation.pop(field)
    result = acceptance.check_surface(target, "chat", _capture(tmp_path, observed))
    assert result["status"] == "unverified" and "citation_date_metadata_missing" in {gap["code"] for gap in result["gaps"]}


def test_rag_collector_preserves_actual_chunk_date_and_all_source_metadata():
    from types import SimpleNamespace
    metadata = {"published_candidate_sha256": "a" * 64, "content_date": "2026-10-02", "content_date_basis": "publication_date",
        "content_date_precision": "month", "content_date_authority": "source", "content_version_id": "body-a"}
    chunk = SimpleNamespace(path="wiki/article-good.md", markdown="Actual old metadata", date="2026-10-02", date_metadata=metadata)
    kb = SimpleNamespace(documents=[SimpleNamespace(path=chunk.path, file="article-good.md", markdown=chunk.markdown)],
        public_registry_revision="observed", search=lambda query: [SimpleNamespace(chunk=chunk)])
    observed = acceptance.observe_rag(kb, [{"entity_kind": "article", "entity_id": "good", "query": "current update"}])
    hit = observed["queries"][0]["hits"][0]
    assert hit["date"] == "2026-10-02" and hit["date_metadata"] == metadata
    assert hit["date_kind"] == "publication_date"


def test_exact_linked_pdf_selection_binds_canonical_serving_without_fabricating_pdf_approval(tmp_path):
    from test_verified_publication import _reader
    database = _seed(tmp_path, source="linked")
    pdf = _reader(database, tmp_path).pdf_article("pdf-good")
    sha = pdf["published_candidate_sha256"]
    target = acceptance.freeze_target(database, [{"entity_kind": "pdf_article", "entity_id": "pdf-good",
        "candidate_sha256": sha, "candidate_basis": "canonical_serving"}])
    assert target["publication"]["status"] == "pass", target["publication"]
    item = target["entities"][0]
    assert item["published_candidate_sha256"] == sha == item["serving_candidate_sha256"]
    assert item["publication_entity"] == item["serving_entity"] == ["article", "good"]
    assert item["original_publication_candidate_sha256"] is None and item["original_receipt_references"] == []
    assert item["serving_receipt_references"] and item["occurrence_ownership"] == {"occurrence": "good"}
    assert item["pages"] == ["article-good.md"] and "registry-source-observation-occurrence.md" not in target["pages"]
    observed = _observation(target)
    for surface in acceptance.SURFACES:
        assert acceptance.check_surface(target, surface, _capture(tmp_path, observed))["status"] == "fixture_match"
    observed["records"][0]["payload"]["core_article_id"] = "wrong-owner"
    assert acceptance.check_surface(target, "display_api", _capture(tmp_path, observed))["status"] == "fail"


def test_absorbed_pdf_keeps_original_candidate_and_receipt_distinct_from_serving_core(tmp_path):
    from climate_registry.display import refresh_display
    from verified_fixtures import seed_verified_articles
    database = _seed(tmp_path, source="same_url")
    original = _entry(acceptance.freeze_target(database), "pdf-good", "pdf_article")
    sha = original["published_candidate_sha256"]
    with sqlite3.connect(database) as db:
        db.execute("UPDATE pdf_intake_articles SET core_article_id='good',confirmation_basis='exact_url_eligible_detail' WHERE article_id='pdf-good'")
        seed_verified_articles(db, occurrence_ids={"core:good", "occurrence"})
        snapshot = snapshot_entity(db, "article", "good")
        core_sha = stage_snapshot(db, snapshot)
        _approve(db, core_sha, {"basis": "isolated exact approved occurrence owner"}, status="accepted_legacy")
        refresh_display(db)
    target = acceptance.freeze_target(database, [{"entity_kind": "pdf_article", "entity_id": "pdf-good", "candidate_sha256": sha}])
    item = target["entities"][0]
    assert target["publication"]["status"] == "pass", target["publication"]
    assert item["original_publication_candidate_sha256"] == item["published_candidate_sha256"] == sha != core_sha
    assert item["serving_candidate_sha256"] == core_sha
    assert item["original_receipt_references"] == original["receipt_references"]
    assert item["serving_receipt_references"] != item["original_receipt_references"]


def test_round2_unknown_held_identity_cannot_complete_an_exact_approved_subset(tmp_path):
    database = _seed(tmp_path)
    current = _entry(acceptance.freeze_target(database))
    selection = [{"entity_kind": "article", "entity_id": "good", "candidate_sha256": current["published_candidate_sha256"]},
        {"entity_kind": "article", "entity_id": "typo-not-in-registry", "disposition": "held", "baseline_candidate_sha256": None}]
    target = acceptance.freeze_target(database, selection)
    assert target["publication"]["status"] != "pass", target["publication"]


def _round2_article_date_relation(tmp_path):
    from test_identity_relations import _seed as identity_seed, _manifest, _ingest as ingest_identity, _approve as approve_identity
    from test_verified_publication import _pdf_pair, _check, _approve_all, _reader
    from climate_registry.identity import _body, _scope
    from climate_registry.publication import _public_article_dto
    database = identity_seed(tmp_path)
    body = "Title good. Summary good. Same climate report landing and download. Conference 2027 announcement analysis."
    with sqlite3.connect(database) as db:
        sha = _hash(body)
        db.execute("INSERT INTO article_content_versions VALUES('body-good','good',?,?,?,'text/html',NULL,'fixture','v1','2026-10-01T00:00:00Z')", (sha, body, sha))
        db.execute("UPDATE articles SET current_content_version_id='body-good' WHERE article_id='good'")
    event = _pdf_pair(database)
    _check(database, tmp_path, ids={"core:good"}, body=body)
    _check(database, tmp_path, kind="meetings", ids={"target-occ"}, candidate=event)
    _approve_all(database)
    manifest = _manifest(database, [("pdf_meeting", "target")])
    manifest["disposition"] = "article_linked_to_dates"
    manifest["relations"] = [{"article_body": _body(manifest["plan"]["participants"][0]["snapshot"]),
        "event": _scope(manifest["plan"]["participants"][1]), "evidence_ids": ["semantic-proof"]}]
    staged = ingest_identity(database, tmp_path, manifest)
    approve_identity(database, tmp_path, staged)
    actual = _public_article_dto(_reader(database, tmp_path).article("good"))
    assert actual["publisher"] and actual["article_event_relations"][0]["article_body"]["content_version_id"] == "body-good"
    target = acceptance.freeze_target(database, [{"entity_kind": "article", "entity_id": "good",
        "candidate_sha256": actual["published_candidate_sha256"]}])
    return target, actual


@pytest.mark.parametrize("surface", ["display_api", "wiki_export"])
@pytest.mark.parametrize("change", ["none", "publisher_missing", "publisher_wrong", "relation_missing", "relation_body", "relation_event", "relation_facts"])
def test_round2_native_publisher_and_complete_body_bound_relation_are_compared(tmp_path, surface, change):
    target, actual = _round2_article_date_relation(tmp_path)
    observed = _observation(acceptance.freeze_target(target["_database"]))
    observed["records"][0]["payload"] = deepcopy(actual)
    payload = observed["records"][0]["payload"]
    if change == "publisher_missing": payload.pop("publisher")
    elif change == "publisher_wrong": payload["publisher"] = "Wrong current publisher"
    elif change == "relation_missing": payload.pop("article_event_relations")
    elif change == "relation_body": payload["article_event_relations"][0]["article_body"]["content_version_id"] = "body-old"
    elif change == "relation_event": payload["article_event_relations"][0]["event"]["entity_id"] = "wrong-event"
    elif change == "relation_facts": payload["article_event_relations"][0]["facts"]["start_date"] = "2099-01-01"
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == ("fixture_match" if change == "none" else "fail"), result


def _round2_qualified_update(tmp_path, monkeypatch):
    from test_article_content_dates import _bundle_snapshot
    from test_enrichment_contract import _insert_body, CONTENT_SCOPE
    database, _ = _published_a(tmp_path)
    packet = _bundle_snapshot()
    summary = "The flood loss estimate remains uncertain at 20 billion."
    note = "The flood loss estimate increased from 10 billion to 20 billion but remains unconfirmed."
    body = "Flood losses reached 20 billion. " + summary + " " + note + " Updated: 16 October 2026"
    bundle = packet["derived_display"]["content_update"]
    bundle.update(summary=summary, summary_sha256=_hash(summary), body_sha256=_hash(body), change_note=note)
    with sqlite3.connect(database) as db:
        _insert_body(db, "body-b", body, "2026-10-16T01:00:00Z")
        snapshot = snapshot_entity(db, "article", "good")
        snapshot["derived_display"] = {"summary": summary, "content_update": bundle}
        stage_snapshot(db, snapshot, enrichment_scope=CONTENT_SCOPE)
    _native_contract_review(database, tmp_path, monkeypatch)
    return acceptance.freeze_target(database)


@pytest.mark.parametrize("answer,expected", [
    ("The revised estimate is $20 billion, double the earlier $10 billion [1].", "fail"),
    ("The flood loss estimate remains uncertain at 20 billion. The flood loss estimate increased from 10 billion to 20 billion but remains unconfirmed.", "fixture_match"),
    ("Flood losses are now estimated at 20 billion, up from 10 billion, but the estimate remains uncertain and unconfirmed.", "fixture_match"),
])
def test_round2_quantitative_paraphrase_preserves_approved_qualifications(tmp_path, monkeypatch, answer, expected):
    target = _round2_qualified_update(tmp_path, monkeypatch)
    observed = _observation(target)
    observed["queries"][0]["answer"] = answer
    result = acceptance.check_surface(target, "chat", _capture(tmp_path, observed))
    assert result["status"] == expected, result


@pytest.mark.parametrize("answer,expected", [
    ("Flood losses are now estimated at 20 billion, up from 10 billion.", "fixture_match"),
    ("Flood losses are now estimated at 10 billion, up from 20 billion.", "fail"),
    ("Flood losses are not now estimated at 20 billion, up from 10 billion.", "fail"),
    ("The revised estimate is $20 billion, double the earlier $10 billion [1].", "fixture_match"),
    ("Climate change remains important for the economy.", "fail"),
])
def test_round2_supported_numeric_transition_does_not_require_the_approved_token_phrase(tmp_path, monkeypatch, answer, expected):
    database, _, _ = _approved_update(tmp_path, monkeypatch)
    target = acceptance.freeze_target(database)
    observed = _observation(target)
    observed["queries"][0]["answer"] = answer
    result = acceptance.check_surface(target, "chat", _capture(tmp_path, observed))
    assert result["status"] == expected, result


@pytest.mark.parametrize("surface", ["rag", "chat"])
@pytest.mark.parametrize("page_state,expected", [
    ("current", "fixture_match"), ("contaminated", "fail"), ("missing", "unverified"),
    ("partial", "unverified"), ("history_only", "unverified"), ("current_with_history", "fixture_match"),
])
def test_round3_unhit_shared_dates_page_is_independently_checked(tmp_path, surface, page_state, expected):
    from test_verified_publication import _pdf_pair
    from verified_fixtures import seed_verified_pdf_meetings
    from climate_registry.publication import stage_entities
    from climate_registry.display import refresh_display
    database = _seed(tmp_path)
    _pdf_pair(database, verified_source=True)
    with sqlite3.connect(database) as db:
        seed_verified_pdf_meetings(db)
        for candidate in stage_entities(db, entities={("pdf_meeting", "target"), ("pdf_meeting", "source")}):
            identity = db.execute("SELECT entity_id FROM registry_candidates WHERE candidate_sha256=?", (candidate,)).fetchone()[0]
            if identity == "target":
                _approve(db, candidate, {"basis": "isolated approved Dates fixture"}, status="accepted_legacy")
        refresh_display(db)
        held = json.loads(db.execute("SELECT item_json FROM pdf_intake_calendar_items WHERE event_id='source'").fetchone()[0])
    current = _entry(acceptance.freeze_target(database))
    target = acceptance.freeze_target(database, [
        {"entity_kind": "article", "entity_id": "good", "candidate_sha256": current["published_candidate_sha256"]},
        {"entity_kind": "pdf_meeting", "entity_id": "source", "disposition": "held", "baseline_candidate_sha256": None},
    ])
    page = "registry-meetings.md"
    assert target["publication"]["status"] == "pass" and page in target["pages"] and not target["absent_pages"]
    assert not _entry(target, "source", "pdf_meeting")["present"]
    observed = _observation(target)
    assert all(hit["entity_kind"] == "article" for query in observed["queries"] for hit in query["hits"])
    contaminated = target["pages"][page] + "\n## Held observation\n" + held["occurrence_id"] + "\n" + held["summary"]
    if page_state != "missing":
        document = {"page": page, "markdown": contaminated if page_state in {"contaminated", "history_only"} else target["pages"][page]}
        if page_state == "history_only": document["role"] = "history"
        if page_state == "partial": document.update(markdown=target["pages"][page][:60], complete=False)
        observed["documents"]["corpus/dates-current-or-history"] = document
    if page_state == "current_with_history":
        observed["documents"]["history/dates-older"] = {"page": page, "markdown": contaminated, "role": "history"}
    observed["corpus_document_count"] = len(observed["documents"])
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == expected, result


@pytest.mark.parametrize("surface", ["display_api", "wiki_export"])
@pytest.mark.parametrize("list_state,expected", [
    ("current", "fixture_match"), ("reversed", "fail"), ("partial_order", "unverified"),
    ("missing_totals", "unverified"), ("partial_records", "unverified"),
])
def test_round3_single_selected_update_keeps_full_public_sort_context(tmp_path, monkeypatch, surface, list_state, expected):
    from verified_fixtures import seed_verified_articles
    from climate_registry.publication import stage_entities
    from climate_registry.display import refresh_display
    database, _, candidate = _approved_update(tmp_path, monkeypatch)
    with sqlite3.connect(database) as db:
        seed_verified_articles(db, occurrence_ids={"core:unchecked"})
        for sha in stage_entities(db, entities={("article", "unchecked")}):
            _approve(db, sha, {"basis": "isolated unrelated published article"}, status="accepted_legacy")
        refresh_display(db)
    full = acceptance.freeze_target(database)
    assert full["article_order"] == [["article", "good"], ["article", "unchecked"]]
    assert _entry(full)["facts"]["content_date"] == "2026-10-16" and not _entry(full, "unchecked")["facts"]["content_date"]
    target = acceptance.freeze_target(database, [{"entity_kind": "article", "entity_id": "good", "candidate_sha256": candidate}])
    assert target["article_order"] == [["article", "good"]] and len(target["entities"]) == 1
    observed = _observation(full)
    if list_state == "reversed": observed["article_order"] = list(reversed(observed["article_order"]))
    elif list_state == "partial_order": observed["article_order"] = target["article_order"]
    elif list_state == "missing_totals": observed.pop("record_totals")
    elif list_state == "partial_records": observed["records"] = [row for row in observed["records"] if row["entity_id"] != "unchecked"]
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == expected, result


@pytest.mark.parametrize("framing,expected", [
    ("{claim}.", "fixture_match"),
    ("The current report states that {claim}.", "fixture_match"),
    ("It is incorrect to say that {claim}.", "fail"),
    ("The claim that {claim} is incorrect.", "fail"),
    ("There is insufficient evidence to conclude that {claim}.", "fail"),
    ("There is a lack of reliable evidence that {claim}.", "fail"),
    ("The assertion that {claim} is unsupported.", "fail"),
    ("It is possible that {claim}.", "fail"),
    ("Reports merely allege that {claim}.", "fail"),
    ("It is inaccurate to report that {claim}.", "fail"),
    ("{claim}. This statement is incorrect.", "fail"),
])
def test_round4_answer_must_assert_the_approved_proposition(tmp_path, monkeypatch, framing, expected):
    database, _, _ = _approved_update(tmp_path, monkeypatch)
    target = acceptance.freeze_target(database)
    observed = _observation(target)
    requirements = _entry(target)["query_requirements"]
    observed["queries"][0]["answer"] = " ".join(framing.format(claim=row["text"].rstrip(".")) for row in requirements)
    result = acceptance.check_surface(target, "chat", _capture(tmp_path, observed))
    assert result["status"] == expected, result


def _round4_dates_target(tmp_path, source, precision="day"):
    from climate_registry.publication import stage_entities
    from climate_registry.display import refresh_display
    from verified_fixtures import seed_verified_native_meetings, seed_verified_pdf_meetings
    start, end, literal = ("2027-06-10", "2027-06-12", "June 10–12, 2027") if precision == "day" else ("2027-06", None, "June 2027")
    if source == "native":
        from test_issue136_meetings import _database as meeting_database, _candidate
        from climate_monitor.meetings import process_batch
        name = "World Climate Summit 2027"
        body = f"{name} meets {literal} in New York. Example hosts it. {name} registration closes May 1, 2027. Register https://example.com/register. Climate risk agenda."
        database = meeting_database(tmp_path, [body])
        candidate = _candidate(start_date=start, end_date=end, date_precision=precision, raw_time_text=literal,
            date_evidence=literal, deadline_evidence=f"{name} registration closes May 1, 2027")
        result = process_batch(database, "batch", prompt_text="source evidence", prompt_version="v1", provider="fixture", model="fixture",
            extractor=lambda request: {"events": [candidate]})
        assert result["status"] == "succeeded"
        kind = "meeting"
        with sqlite3.connect(database) as db:
            seed_verified_native_meetings(db)
            identity = db.execute("SELECT event_id FROM climate_events").fetchone()[0]
    else:
        from test_verified_publication import _database, DOC
        database = _database(tmp_path)
        kind, identity = "pdf_meeting", "target"
        with sqlite3.connect(database) as db:
            item = {"event_id": identity, "occurrence_id": "target-occ", "name": "IAIS Annual Conference 2027", "kind": "event",
                "start_date": start, "end_date": end, "raw_date": literal, "date_precision": precision,
                "deadline_date": "2027-05-01", "deadline_type": "registration", "summary": "Registration closes May 1, 2027.",
                "page": 9, "source_document_sha256": DOC, "source_urls": ["https://example.org/target"]}
            db.execute("INSERT INTO pdf_intake_calendar_items VALUES(?,?,?,?,?,'event',?,?,?,?,?,?,NULL,?)",
                (item["occurrence_id"], identity, DOC, 9, item["name"], literal, precision, start, end, item["summary"], "c" * 64, json.dumps(item)))
            seed_verified_pdf_meetings(db)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db, entities={(kind, identity)}):
            _approve(db, sha, {"basis": "isolated approved event date facts"}, status="accepted_legacy")
        refresh_display(db)
    current = _entry(acceptance.freeze_target(database), identity, kind)
    target = acceptance.freeze_target(database, [{"entity_kind": kind, "entity_id": identity,
        "candidate_sha256": current["published_candidate_sha256"]}])
    assert target["publication"]["status"] == "pass" and current["facts"]["deadline_date"] == "2027-05-01"
    return target


@pytest.mark.parametrize("source", ["native", "pdf"])
@pytest.mark.parametrize("surface", ["rag", "chat"])
@pytest.mark.parametrize("change,expected", [
    ("current", "fixture_match"), ("natural", "fixture_match"), ("text_only", "fixture_match"), ("name_only", "unverified"),
    ("hit_missing_deadline", "unverified"), ("hit_start", "fail"), ("hit_end", "fail"), ("hit_deadline", "fail"),
    ("hit_precision", "fail"), ("citation_missing_dates", "unverified"), ("citation_deadline", "fail"),
    ("answer_no_interval", "unverified"), ("answer_no_deadline", "unverified"),
    ("answer_interval", "fail"), ("answer_deadline", "fail"), ("answer_denied", "fail"), ("hit_basis", "fail"),
])
def test_round4_dates_require_actual_interval_and_deadline(tmp_path, source, surface, change, expected):
    target = _round4_dates_target(tmp_path, source)
    entity = next(row for row in target["entities"] if row["present"])
    facts = entity["facts"]
    observed = _observation(target)
    query = observed["queries"][0]
    hit, citation = query["hits"][0], query["citations"][0]
    name = facts["name"]
    for item in (hit, citation):
        item["excerpt"] = target["pages"]["registry-meetings.md"]
        item["date_metadata"].update({field: facts[field] for field in ("start_date", "end_date", "deadline_date", "date_precision", "deadline_type")})
    query["answer"] = f"{name}. Start date: 2027-06-10. End date: 2027-06-12. Deadline: 2027-05-01. Deadline type: registration. Date precision: day."
    if facts.get("summary"): query["answer"] += " " + facts["summary"]
    if change in {"natural", "text_only"}:
        query["answer"] = f"{name} runs from June 10 to June 12, 2027. Registration closes May 1, 2027."
        if change == "text_only":
            for item in (hit, citation): item["date_metadata"] = {}
            if source == "pdf": expected = "unverified"  # The current PDF page does not render its approved deadline.
    elif change == "name_only":
        for item in (hit, citation): item.update(excerpt=name, date_metadata={})
        query["answer"] = name
    elif change == "hit_missing_deadline":
        for item in (hit, citation): item["excerpt"] = name
        hit["date_metadata"].pop("deadline_date")
    elif change == "hit_basis": hit["date_kind"] = "publication_date"
    elif change.startswith("hit_"):
        field = {"hit_start": "start_date", "hit_end": "end_date", "hit_deadline": "deadline_date", "hit_precision": "date_precision"}[change]
        hit["date_metadata"][field] = "month" if field == "date_precision" else "2027-06-20"
    elif change == "citation_missing_dates":
        if surface == "chat":
            for item in (hit, citation): item["excerpt"] = name
        citation["date_metadata"] = {}
    elif change == "citation_deadline": citation["date_metadata"]["deadline_date"] = "2027-05-20"
    elif change == "answer_no_interval": query["answer"] = f"{name}. Registration closes May 1, 2027."
    elif change == "answer_no_deadline": query["answer"] = f"{name} runs from June 10 to June 12, 2027."
    elif change == "answer_interval": query["answer"] = f"{name} runs from June 20 to June 22, 2027. Registration closes May 1, 2027."
    elif change == "answer_deadline": query["answer"] = f"{name} runs from June 10 to June 12, 2027. Registration closes May 20, 2027."
    elif change == "answer_denied": query["answer"] += " These statements are incorrect."
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    if surface == "rag" and change.startswith(("citation_", "answer_")):
        expected = "fixture_match"  # A RAG result does not validate a Chat answer/citation.
    assert result["status"] == expected, result


@pytest.mark.parametrize("source", ["native", "pdf"])
@pytest.mark.parametrize("surface", ["rag", "chat"])
@pytest.mark.parametrize("midpoint", [False, True])
def test_round4_event_month_literal_is_not_a_source_stated_midpoint(tmp_path, source, surface, midpoint):
    target = _round4_dates_target(tmp_path, source, precision="month")
    facts = target["entities"][0]["facts"]
    observed = _observation(target)
    query = observed["queries"][0]
    for item in (query["hits"][0], query["citations"][0]):
        item["excerpt"] = target["pages"]["registry-meetings.md"]
        item["date_metadata"].update({field: facts[field] for field in ("start_date", "end_date", "deadline_date", "date_precision", "deadline_type")})
        if midpoint: item["date_metadata"].update(start_date="2027-06-15", date_precision="day")
    query["answer"] = f"{facts['name']} takes place in June 2027. Registration closes May 1, 2027."
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == ("fail" if midpoint else "fixture_match"), result


@pytest.mark.parametrize("source", ["article", "qualified_article", "native_dates", "pdf_dates"])
@pytest.mark.parametrize("withdrawal", ["", " This is false.", " This is untrue."])
def test_round5_correct_claims_cannot_be_withdrawn_by_a_trailing_denial(tmp_path, monkeypatch, source, withdrawal):
    if source == "article":
        database, _, _ = _approved_update(tmp_path, monkeypatch)
        target = acceptance.freeze_target(database)
    elif source == "qualified_article":
        target = _round2_qualified_update(tmp_path, monkeypatch)
    else:
        target = _round4_dates_target(tmp_path, "native" if source == "native_dates" else "pdf")
    observed = _observation(target)
    query = observed["queries"][0]
    if source.endswith("_dates"):
        facts = next(row for row in target["entities"] if row["present"])["facts"]
        for item in (query["hits"][0], query["citations"][0]):
            item["date_metadata"].update({field: facts[field] for field in acceptance.EVENT_DATE_FIELDS})
    query["answer"] = query["answer"].rstrip().rstrip(".") + "." + withdrawal
    result = acceptance.check_surface(target, "chat", _capture(tmp_path, observed))
    assert result["status"] == ("fail" if withdrawal else "fixture_match"), result


@pytest.mark.parametrize("surface", ["rag", "chat"])
@pytest.mark.parametrize("copy_role", ["single", "current", "history"])
def test_round5_current_corpus_page_body_has_one_document_identity(tmp_path, surface, copy_role):
    target = acceptance.freeze_target(_seed(tmp_path))
    observed = _observation(target)
    original_id, original = next(iter(observed["documents"].items()))
    if copy_role != "single":
        duplicate_id = "other-corpus/" + original_id
        assert duplicate_id != original_id
        observed["documents"][duplicate_id] = {**deepcopy(original), "role": copy_role}
    observed["corpus_document_count"] = len(observed["documents"])
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == ("fail" if copy_role == "current" else "fixture_match"), result


@pytest.mark.parametrize("surface", ["rag", "chat"])
@pytest.mark.parametrize("mode", ["mixed", "unrelated_only", "unobserved_query"])
def test_round6_real_search_unrelated_report_is_not_selected_target_evidence(tmp_path, monkeypatch, surface, mode):
    from agentic_wiki.wiki_agent import WikiKnowledgeBase
    database = _seed(tmp_path)
    current = _entry(acceptance.freeze_target(database))
    target = acceptance.freeze_target(database, [{"entity_kind": "article", "entity_id": "good",
        "candidate_sha256": current["published_candidate_sha256"]}])
    wiki, sources = tmp_path/"wiki", tmp_path/"sources"
    wiki.mkdir(); sources.mkdir()
    for page, markdown in target["pages"].items():
        (wiki/page).write_text(markdown, encoding="utf-8")
    (wiki/"2026-10-05.md").write_text("# Climate Monitor 2026-10-05\n\n## Summary\nSummary good from the archived report.\n", encoding="utf-8")
    monkeypatch.delenv("CLIMATE_REGISTRY_DB", raising=False)
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", raising=False)
    kb = WikiKnowledgeBase(wiki_dir=wiki, source_dir=sources)
    observed = acceptance.observe_rag(kb, [{"entity_kind": "article", "entity_id": "good", "query": "Summary good"}])
    observed["activation"] = _observation(target)["activation"]  # Fixture activation only; search/content came from the actual KB.
    query = observed["queries"][0]
    report_hits = [hit for hit in query["hits"] if observed["documents"][hit["document_id"]]["page"] == "2026-10-05.md"]
    assert report_hits and any(hit["entity_id"] == "good" for hit in query["hits"])
    assert all(hit["entity_id"] is None and hit["candidate_sha256"] is None for hit in report_hits)
    if mode == "unrelated_only": query["hits"] = report_hits
    query["answer"] = "Summary good"
    query["citations"] = [{key: deepcopy(hit[key]) for key in (
        "document_id", "candidate_sha256", "excerpt", "date", "date_kind", "date_metadata")} for hit in query["hits"]]
    if mode == "unobserved_query": observed["queries"] = []
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == ("fixture_match" if mode == "mixed" else "unverified"), result
    assert all(hit["entity_id"] is None for hit in report_hits)  # Never borrow the overlapping target words as identity.


@pytest.mark.parametrize("source", ["native", "pdf"])
@pytest.mark.parametrize("mode,expected", [
    ("combined", "fixture_match"), ("split", "fixture_match"), ("complete_then_partial", "fixture_match"),
    ("missing_deadline", "unverified"), ("conflict_then_complete", "fail"), ("missing_citation_dates", "unverified"),
    ("conflicting_citation_dates", "fail"), ("wrong_current_candidate", "fail"),
])
def test_round6_event_answer_date_fields_aggregate_only_after_valid_query_evidence(tmp_path, source, mode, expected):
    target = _round4_dates_target(tmp_path, source)
    facts = target["entities"][0]["facts"]
    observed = _observation(target)
    original = observed["queries"][0]
    for item in (original["hits"][0], original["citations"][0]):
        item["date_metadata"].update({field: facts[field] for field in acceptance.EVENT_DATE_FIELDS})
    interval = f"{facts['name']} runs from June 10 to June 12, 2027."
    deadline = f"{facts['name']}. Registration closes May 1, 2027."
    first, second = deepcopy(original), deepcopy(original)
    first.update(query="What is the approved event interval?", answer=interval)
    second.update(query="What is the approved registration deadline?", answer=deadline)
    combined = deepcopy(original)
    combined["answer"] = interval + " Registration closes May 1, 2027."
    observed["queries"] = [first, second]
    if mode == "combined": observed["queries"] = [combined]
    elif mode == "complete_then_partial": observed["queries"] = [combined, first]
    elif mode == "missing_deadline": observed["queries"] = [first]
    elif mode == "conflict_then_complete":
        first["answer"] = interval + " Registration closes May 20, 2027."
        observed["queries"] = [first, combined]
    elif mode == "missing_citation_dates":
        for item in (second["hits"][0], second["citations"][0]): item["excerpt"] = facts["name"]
        second["citations"][0]["date_metadata"] = {}
    elif mode == "conflicting_citation_dates": second["citations"][0]["date_metadata"]["deadline_date"] = "2027-05-20"
    elif mode == "wrong_current_candidate": second["hits"][0]["candidate_sha256"] = "0" * 64
    result = acceptance.check_surface(target, "chat", _capture(tmp_path, observed))
    assert result["status"] == expected, result
    if expected == "fixture_match":
        assert not any(gap["code"] == "chat_event_date_facts_missing" for gap in result["gaps"])
    if mode == "missing_deadline":
        missing = next(gap for gap in result["gaps"] if gap["code"] == "chat_event_date_facts_missing")
        assert set(missing["fields"]) == {"deadline_date", "deadline_type"}
    if mode == "conflict_then_complete":
        assert any(gap["code"] == "chat_event_date_facts_differs" for gap in result["gaps"])


@pytest.mark.parametrize("surface", ["rag", "chat"])
@pytest.mark.parametrize("copy_kind", ["single", "current_copy", "history_copy", "unrelated"])
def test_round7_selected_body_is_unique_across_current_page_names(tmp_path, surface, copy_kind):
    target = acceptance.freeze_target(_seed(tmp_path))
    observed = _observation(target)
    original = next(iter(observed["documents"].values()))
    if copy_kind != "single":
        observed["documents"]["corpus/article-legacy.md"] = {
            "page": "article-legacy.md", "markdown": "# A distinct unrelated body\n" if copy_kind == "unrelated" else original["markdown"],
            "role": "history" if copy_kind == "history_copy" else "current"}
    observed["corpus_document_count"] = len(observed["documents"])
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == ("fail" if copy_kind == "current_copy" else "fixture_match"), result


@pytest.mark.parametrize("actual", ["unknown", "omitted", "concrete_precision", "concrete_interval"])
def test_round7_unknown_interval_precision_is_optional_matching_evidence(actual):
    facts = {"event_id": "deadline-event", "name": "Climate Consultation Deadline", "date_precision": "unknown",
        "start_date": None, "end_date": None, "deadline_date": "2027-05-01", "deadline_type": "consultation"}
    metadata = {field: facts[field] for field in acceptance.EVENT_DATE_FIELDS}
    if actual == "omitted": metadata.pop("date_precision")
    elif actual == "concrete_precision": metadata["date_precision"] = "day"
    elif actual == "concrete_interval": metadata["start_date"] = "2027-05-01"
    missing, mismatched = acceptance._event_date_evidence(facts, {"date_metadata": metadata,
        "excerpt": "Deadline: 2027-05-01\nDeadline type: consultation"})
    assert not missing
    assert bool(mismatched) == actual.startswith("concrete"), (missing, mismatched)


def _round7_native_deadline_target(tmp_path):
    from test_issue136_meetings import _database as meeting_database, _candidate
    from climate_monitor.meetings import process_batch
    from climate_registry.publication import stage_entities
    from climate_registry.display import refresh_display
    from verified_fixtures import seed_verified_native_meetings
    body = "Example opens World Climate Summit 2027. Registration closes May 1, 2027. Climate risk agenda."
    database = meeting_database(tmp_path, [body])
    candidate = _candidate(event_type="deadline", date_precision="unknown", start_date=None, end_date=None,
        raw_time_text=None, date_evidence=None, location=None, online_url=None, deadline_evidence="May 1, 2027")
    result = process_batch(database, "batch", prompt_text="source evidence", prompt_version="v1", provider="fixture", model="fixture",
        extractor=lambda request: {"events": [candidate]})
    assert result["status"] == "succeeded"
    with sqlite3.connect(database) as db:
        seed_verified_native_meetings(db)
        identity = db.execute("SELECT event_id FROM climate_events").fetchone()[0]
        for sha in stage_entities(db, entities={("meeting", identity)}):
            _approve(db, sha, {"basis": "isolated native deadline-only fixture"}, status="accepted_legacy")
        refresh_display(db)
    current = _entry(acceptance.freeze_target(database), identity, "meeting")
    return acceptance.freeze_target(database, [{"entity_kind": "meeting", "entity_id": identity,
        "candidate_sha256": current["published_candidate_sha256"]}])


@pytest.mark.parametrize("surface", ["rag", "chat"])
@pytest.mark.parametrize("change,expected", [("matching", "fixture_match"), ("precision", "fail"), ("interval", "fail"),
    ("wrong_deadline", "fail"), ("missing_deadline", "unverified")])
def test_round7_native_deadline_only_unknown_interval_keeps_precise_deadline_checks(tmp_path, surface, change, expected):
    target = _round7_native_deadline_target(tmp_path)
    entity = target["entities"][0]; facts = entity["facts"]
    assert facts["date_precision"] == "unknown" and facts["start_date"] is None and facts["end_date"] is None
    assert not any(row.get("field") == "date_precision" for row in entity["query_requirements"])
    observed = _observation(target); query = observed["queries"][0]
    for item in (query["hits"][0], query["citations"][0]):
        item["date_metadata"].update({field: facts[field] for field in acceptance.EVENT_DATE_FIELDS})
    query["answer"] = f"{facts['name']}. Deadline: 2027-05-01. Deadline type: registration."
    metadata = query["hits"][0]["date_metadata"]
    if change == "precision": metadata["date_precision"] = "day"
    elif change == "interval": metadata["start_date"] = "2027-05-01"
    elif change == "wrong_deadline": metadata["deadline_date"] = "2027-05-20"
    elif change == "missing_deadline":
        for item in (query["hits"][0], query["citations"][0]): item.update(excerpt=facts["name"], date_metadata={"date_precision": "unknown"})
    result = acceptance.check_surface(target, surface, _capture(tmp_path, observed))
    assert result["status"] == expected, result


@pytest.mark.parametrize("corpus", ["wiki", "source"])
@pytest.mark.parametrize("mode", ["faithful", "approved_uncertainty", "denied", "unsupported_qualification", "unmatched_source_title"])
def test_round7_actual_offline_brief_framing_preserves_approved_claim_stance(tmp_path, monkeypatch, corpus, mode):
    from agentic_wiki.wiki_agent import AgenticWikiResponder, WikiChunk, SearchHit
    if mode == "approved_uncertainty": target = _round2_qualified_update(tmp_path, monkeypatch)
    else:
        database, _, _ = _approved_update(tmp_path, monkeypatch)
        target = acceptance.freeze_target(database)
    observed = _observation(target); query = observed["queries"][0]
    original = deepcopy(query["hits"][0]); title = _entry(target)["facts"]["title"]
    title = "Unmatched source title" if mode == "unmatched_source_title" else title
    rendered_hits, actual_hits, citations = [], [], []
    for index, requirement in enumerate(_entry(target)["query_requirements"]):
        text = requirement["text"]
        assert text in target["pages"]["article-good.md"]
        rendered_text = "It is possible that " + text if mode == "unsupported_qualification" else text
        chunk = WikiChunk(id=str(index), title=title, path=original["document_id"], heading="Approved content", type="article",
            date=original["date"], text=rendered_text, markdown=text, links=[], corpus=corpus, date_metadata=deepcopy(original["date_metadata"]))
        rendered_hits.append(SearchHit(chunk=chunk, score=1.0, reason="current fixture source"))
        actual_hits.append({**deepcopy(original), "excerpt": text})
        citations.append({key: deepcopy(actual_hits[-1][key]) for key in (
            "document_id", "candidate_sha256", "excerpt", "date", "date_kind", "date_metadata")})
    responder = object.__new__(AgenticWikiResponder)
    query["answer"] = responder._offline_answer(query["query"], rendered_hits, "en", "brief")
    assert f"- [1] ({'raw source' if corpus == 'source' else 'wiki'}) {title}: " in query["answer"]
    if mode == "denied": query["answer"] += "\nThis is false."
    query["hits"], query["citations"] = actual_hits, citations
    result = acceptance.check_surface(target, "chat", _capture(tmp_path, observed))
    assert result["status"] == ("fixture_match" if mode in {"faithful", "approved_uncertainty"} else "fail"), result


def _round8_real_offline_observation(tmp_path, monkeypatch, *, updated=False, raw_source=False):
    from agentic_wiki.wiki_agent import WikiKnowledgeBase, AgenticWikiResponder
    if updated: database, _, _ = _approved_update(tmp_path, monkeypatch)
    else: database = _seed(tmp_path)
    current = _entry(acceptance.freeze_target(database))
    target = acceptance.freeze_target(database, [{"entity_kind": "article", "entity_id": "good",
        "candidate_sha256": current["published_candidate_sha256"]}])
    wiki, sources = tmp_path/"wiki", tmp_path/"sources"
    wiki.mkdir(); sources.mkdir()
    for page, markdown in target["pages"].items(): (wiki/page).write_text(markdown, encoding="utf-8")
    if raw_source:
        (sources/"archive-notes.md").write_text("# Archive notes\n\n## Summary\nSummary good from an unrelated retained raw source.\n", encoding="utf-8")
    monkeypatch.delenv("CLIMATE_REGISTRY_DB", raising=False)
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", raising=False)
    kb = WikiKnowledgeBase(wiki_dir=wiki, source_dir=sources)
    question = "Flood losses 20 billion" if updated else "Summary good"
    hits = kb.search(question)
    observed = acceptance.observe_rag(kb, [{"entity_kind": "article", "entity_id": "good", "query": question}])
    query = observed["queries"][0]
    assert [(hit["document_id"], hit["excerpt"]) for hit in query["hits"]] == [(hit.chunk.path, hit.chunk.markdown) for hit in hits]
    current_hits = [hit for hit in hits if hit.chunk.path in observed["documents"] and observed["documents"][hit.chunk.path]["page"] == "article-good.md"]
    assert current_hits and all(hit.chunk.title == "article-good" for hit in current_hits)
    assert target["pages"]["article-good.md"].startswith("# Title good\n")
    if raw_source: assert any(hit.chunk.corpus == "source" for hit in hits)
    observed["activation"] = _observation(target)["activation"]  # Fixture activation; every source/hit/answer is from actual local interfaces.
    responder = object.__new__(AgenticWikiResponder)
    query["answer"] = responder._offline_answer(question, hits, "en", "brief")
    query["citations"] = [{"index": index + 1, **{key: deepcopy(hit[key]) for key in (
        "document_id", "candidate_sha256", "excerpt", "date", "date_kind", "date_metadata")}} for index, hit in enumerate(query["hits"])]
    assert "(wiki) article-good:" in query["answer"]
    if raw_source: assert "(raw source)" in query["answer"]
    return target, observed


@pytest.mark.parametrize("raw_source", [False, True])
@pytest.mark.parametrize("mode", ["native", "unframed", "denied", "qualified", "unmatched_title", "other_bullet_only",
    "wrong_index", "wrong_citation", "wrong_version"])
def test_round8_real_kb_offline_brief_credits_only_its_current_source_claim(tmp_path, monkeypatch, raw_source, mode):
    target, observed = _round8_real_offline_observation(tmp_path, monkeypatch, raw_source=raw_source)
    assert acceptance.check_surface(target, "rag", _capture(tmp_path, observed, name="rag-control"))["status"] == "fixture_match"
    query = observed["queries"][0]; summary = _entry(target)["facts"]["summary"]
    if mode == "unframed": query["answer"] = summary
    elif mode == "denied": query["answer"] += "\nThis is false."
    elif mode == "qualified": query["answer"] = query["answer"].replace(summary, "It is possible that " + summary)
    elif mode == "unmatched_title": query["answer"] = query["answer"].replace("(wiki) article-good:", "(wiki) Unmatched-title:")
    elif mode == "other_bullet_only":
        import re
        query["answer"] = re.sub(r"(?m)^- \[\d+\] \(wiki\)[^\n]*(?:\n|$)", "", query["answer"])
        if raw_source: assert summary in query["answer"]  # A different raw-source bullet cannot stand in for the current Wiki hit.
    elif mode == "wrong_index":
        import re
        query["answer"] = re.sub(r"(?m)^- \[\d+\] \(wiki\)", "- [999] (wiki)", query["answer"])
    elif mode == "wrong_citation":
        for citation in query["citations"]:
            if citation["document_id"] in observed["documents"]: citation["candidate_sha256"] = "0" * 64
    elif mode == "wrong_version":
        for hit in query["hits"]:
            if hit["document_id"] in observed["documents"]: hit["candidate_sha256"] = "0" * 64
    result = acceptance.check_surface(target, "chat", _capture(tmp_path, observed))
    assert result["status"] == ("fixture_match" if mode in {"native", "unframed"} else "fail"), result
    if mode in {"native", "unframed"}: assert result["content_coverage"][0]["answered"] == ["current_content"]


def test_round8_real_updated_brief_credits_visible_summary_without_inventing_truncated_note(tmp_path, monkeypatch):
    target, observed = _round8_real_offline_observation(tmp_path, monkeypatch, updated=True)
    assert _entry(target)["facts"]["content_date"] == "2026-10-16"
    rag = acceptance.check_surface(target, "rag", _capture(tmp_path, observed, name="updated-rag"))
    assert rag["status"] == "fixture_match", rag
    query = observed["queries"][0]
    summary, note = (_entry(target)["facts"]["content_update"][field] for field in ("summary", "change_note"))
    assert summary in query["answer"] and note not in query["answer"]
    result = acceptance.check_surface(target, "chat", _capture(tmp_path, observed))
    coverage = result["content_coverage"][0]["answered"]
    assert "current_content" in coverage, result
    assert "change_note" not in coverage and result["status"] == "fail", result
    assert any(gap["code"] == "approved_content_coverage_missing" and gap["coverage"] == "answered"
        and gap["requirements"] == ["change_note"] for gap in result["gaps"])
