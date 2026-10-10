"""Direct regressions for the independent acquisition/review/report contracts."""
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from climate_registry.acquisition_review import (
    digest, record_knowledge, reserve_rotation, native_events, read_evidence,
)
from climate_registry.schema import apply_migrations
from climate_registry.contract import validate_registry_contract
from climate_delivery import report_review
from climate_delivery.io import atomic_write_json
from climate_delivery.templates.adapters import adapt_range_report
from dataclasses import asdict
from climate_monitor import schedule
from test_issue113_range_reports import _approve_public_fixture


@pytest.fixture(autouse=True)
def historical_activation_contract(request,monkeypatch):
    # These cases exercise frozen schema20 two-database activation history.
    # Current canonical v21 review/publishing has separate direct regressions.
    if request.node.originalname not in {
        "test_first_ingestion_in_window_survives_current_update_after_cutoff",
        "test_unchanged_meeting_preserves_pdf_provenance_and_true_import",
    }:return
    from climate_registry.schema import apply_migrations as real
    import climate_registry.pdf_intake as storage
    import climate_registry.information_checks as checks
    import climate_registry.persistent as persistent
    import climate_monitor.meetings as meetings
    import climate_registry.publication as publication
    from test_information_checks import legacy_schema_writer,legacy_pdf_binding
    import climate_registry.pdf_pipeline as pipeline
    monkeypatch.setattr(pipeline,"_validate_pdf_binding",legacy_pdf_binding)
    monkeypatch.setattr(publication,"require_publication_migration",legacy_schema_writer)
    def migrate(connection,*,target_version=None):
        return real(connection,target_version=20 if target_version is None else target_version)
    monkeypatch.setattr(sys.modules[__name__],"apply_migrations",migrate)
    monkeypatch.setattr(storage,"apply_migrations",migrate)
    monkeypatch.setattr(storage,"LATEST_SCHEMA_VERSION",20)
    monkeypatch.setattr(checks,"apply_migrations",migrate)
    monkeypatch.setattr(persistent,"apply_migrations",migrate)
    monkeypatch.setattr(meetings,"SCHEMA_VERSION",20)



def _queue_after_actual_information(binding_path,binding,payload,**kwargs):
    # Real terminal T1 attempts precede the existing native T10 fixture review.
    from test_registry_publication import _complete_fixture_information
    from climate_registry.acquisition_review import queue_acquisition_review
    from climate_registry.acquisition import load_acquisition_batch
    loaded=load_acquisition_batch(binding["registry_database"],binding["acquisition_batch_id"])
    ids={item["article_id"] for item in loaded["items"] if item["selection_status"]=="selected"}
    _complete_fixture_information(Path(binding["registry_database"]),entity_ids=ids)
    return queue_acquisition_review(binding_path,binding,payload,**kwargs)


def test_transaction_times_ignore_retries_and_check_metadata():
    db = sqlite3.connect(":memory:")
    apply_migrations(db)
    assert validate_registry_contract(db) == 22
    for ref, when, text in (("first", "2026-09-29T02:00:00Z", "same"),
                           ("retry", "2026-10-02T02:00:00Z", "same"),
                           ("check", "2026-10-04T02:00:00Z", "changed")):
        record_knowledge(db, kind="article", entity_id="a", source_kind="site", source_ref=ref,
            fields={"summary": text, "publication_date": "2020-01-01"}, evidence={"checked_at": when}, recorded_at=when)
    rows = db.execute("SELECT first_ingested_at,substantive_updated_at FROM knowledge_versions ORDER BY rowid").fetchall()
    assert rows == [("2026-09-29T02:00:00Z", None), ("2026-09-29T02:00:00Z", None),
                    ("2026-09-29T02:00:00Z", "2026-10-04T02:00:00Z")]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM knowledge_versions")


def test_rotation_reserves_one_identity_and_surviving_successor(tmp_path):
    keys = list("abcdef")
    first = reserve_rotation(tmp_path, "2026-10-06", keys)
    assert first["source_keys"] == list("abcde")
    assert reserve_rotation(tmp_path, "2026-10-06", list("xyz")) == first
    second = reserve_rotation(tmp_path, "2026-10-07", list("abcdeg"))
    assert second["source_keys"] == list("abcde")  # f removed; its surviving successor a.
    assert second["run_id"] != first["run_id"]
    assert reserve_rotation(tmp_path, "2026-10-08", ["x", "y"])["source_keys"] == ["x", "y"]


def _native(path, session, packet=None):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,source TEXT,started_at TEXT,end_reason TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,tool_calls TEXT,tool_call_id TEXT,tool_name TEXT,content TEXT)")
        db.execute("INSERT INTO sessions(id,source,started_at) VALUES(?, 'cron', '2026-10-12T12:10:00Z')", (session,))
        if packet:
            revision_root = path.parent / "artifacts" / "reports" / packet["occurrence"] / "revisions" / f"{packet['revision']:04d}"
            rows = [("read_file", {"path": str((revision_root / packet["text_path"]).resolve())}, (revision_root / packet["text_path"]).read_text())]
            rows += [("vision_analyze", {"image_url": str((revision_root / p["path"]).resolve())}, "Image attached natively for the main model (12 KB). Answer using built-in vision.") for p in packet["pages"]]
            for index, (tool, args, result) in enumerate(rows):
                call = f"{session}-call-{index}"
                db.execute("INSERT INTO messages(session_id,role,tool_calls) VALUES(?, 'assistant', ?)",
                    (session, json.dumps([{"id": call, "function": {"name": tool, "arguments": json.dumps(args)}}])))
                db.execute("INSERT INTO messages(session_id,role,tool_call_id,tool_name,content) VALUES(?,'tool',?,?,?)", (session, call, tool, result))


def _report(tmp_path):
    from test_issue113_range_reports import _database
    from climate_registry.read_api import RegistryReader
    database = _database(tmp_path,target_version=22)
    with sqlite3.connect(database) as db:
        record_knowledge(db, kind="article", entity_id="article-a", source_kind="site", source_ref="a-1",
            fields={"title": "old original", "publication_date": "2020-01-01"}, evidence={"content_version_id": "content-article-a"},
            recorded_at="2026-09-30T12:00:00Z")
    _approve_public_fixture(database)
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    from climate_registry.web_ingest_pipeline import _batch_items, _manifest_item
    manifest = {"web_items": [_manifest_item(i) for i in _batch_items(database, "batch") if i["acquisition_item_id"] == "a-1"],
        "pdf_occurrence_ids": [], "pdf_calendar_occurrence_ids": []}
    state = report_review.freeze_biweekly(reader, tmp_path / "artifacts", occurrence="2026-10-12",
        web_reader=reader, manifest=manifest, generated_at=datetime(2026, 10, 12, 12, tzinfo=timezone.utc))
    assert state["status"] == "pending_review"
    assert state["packet"]["pages"]
    return state


def test_actual_pdf_revision_independent_recheck_and_exact_no_send(tmp_path, monkeypatch):
    state = _report(tmp_path)
    root = tmp_path / "artifacts"
    occurrence = "2026-10-12"
    native = tmp_path / "hermes.db"
    at = datetime(2026, 10, 12, 12, 10, tzinfo=timezone.utc)
    _native(native, "cron_t5_first", state["packet"])
    claim = report_review.claim_report(root, occurrence, session_id="cron_t5_first", execution_id="first",
        hermes_database=native, reviewer="native-reviewer", now=at)
    changed = report_review.submit_report_review(root, occurrence, claim["token"], status="changes_requested",
        reason="Improve derived title", changes={"title": "Revised climate report"}, now=at)
    assert changed["revision"] == 2 and changed["status"] == "pending_review"
    assert (root / "reports" / occurrence / "revisions" / "0001" / "report.pdf").exists()
    with pytest.raises(ValueError, match="independent"):
        report_review.claim_report(root, occurrence, session_id="cron_t5_first", execution_id="first",
            hermes_database=native, reviewer="native-reviewer", now=at)
    _native(native, "cron_t5_second", changed["packet"])
    claim = report_review.claim_report(root, occurrence, session_id="cron_t5_second", execution_id="second",
        hermes_database=native, reviewer="native-reviewer", now=at)
    approved = report_review.submit_report_review(root, occurrence, claim["token"], status="pass", reason="All text and page images checked", now=at)
    assert approved["approval"]["packet"]["pdf_sha256"] == changed["packet"]["pdf_sha256"]
    monkeypatch.setattr("climate_delivery.config.load_delivery_config", lambda *_: pytest.fail("no-send loaded SMTP configuration"))
    with pytest.raises(ValueError, match="60 minutes"):
        report_review.send_approved(root, occurrence, now=at + timedelta(minutes=59))
    assert report_review.send_approved(root, occurrence, now=at + timedelta(minutes=60))["status"] == "validated_no_send"
    pdf = root / "reports" / occurrence / "revisions" / "0002" / changed["packet"]["pdf_path"]
    pdf.write_bytes(pdf.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="artifact changed"):
        report_review.send_approved(root, occurrence, now=at + timedelta(minutes=60))


def test_missing_real_page_inspection_prevents_approval(tmp_path):
    state = _report(tmp_path)
    native = tmp_path / "hermes.db"
    _native(native, "cron_t5_empty")
    at = datetime(2026, 10, 12, 12, 10, tzinfo=timezone.utc)
    claim = report_review.claim_report(tmp_path / "artifacts", "2026-10-12", session_id="cron_t5_empty",
        execution_id="empty", hermes_database=native, reviewer="native-reviewer", now=at)
    with pytest.raises(ValueError, match="full text read"):
        report_review.submit_report_review(tmp_path / "artifacts", "2026-10-12", claim["token"], status="pass", reason="claimed checked", now=at)
    assert not (tmp_path / "artifacts" / "reports" / "2026-10-12" / "archive.json").exists()


def test_scheduler_dst_and_independent_roles():
    for value, due_hour in (("2026-10-06T10:00:00+00:00", True), ("2026-10-06T11:00:00+00:00", False),
                            ("2026-11-03T11:00:00+00:00", True), ("2026-11-03T10:00:00+00:00", False)):
        assert schedule.pipeline_due("T2", datetime.fromisoformat(value)) == due_hour
    assert schedule.pipeline_due("T3", datetime.fromisoformat("2026-10-11T12:00:00+00:00"))
    assert not schedule.pipeline_due("T4", datetime.fromisoformat("2026-10-05T12:00:00+00:00"))
    assert schedule.pipeline_due("T4", datetime.fromisoformat("2026-10-12T12:00:00+00:00"))


def test_scheduled_ingest_cli_reaches_management(monkeypatch):
    from scripts import run_agent_acquisition as runner
    calls = []
    monkeypatch.setattr(runner.ManagementService, "from_environment", lambda: SimpleNamespace(start=lambda **kw: calls.append(kw) or {"accepted": True}))
    monkeypatch.setattr("sys.argv", ["run_agent_acquisition.py", "--scheduled-start", "--ingest-only"])
    assert runner.main() == 0
    assert calls == [{"trigger": "scheduled", "execution_mode": "ingest_only"}]


def _pending_version(database):
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        for table, identity, replacements in (
            ("article_content_versions", "content_version_id", {"content_version_id": "content-pending", "content_sha256": "a" * 64,
                "markdown_sha256": "a" * 64, "markdown_content": "Pending body must stay private"}),
            ("article_versions", "version_id", {"version_id": "version-pending", "observed_title": "PendingTitle", "observed_summary": "PendingSummary", "content_fingerprint": "b" * 64}),
            ("article_fetches", "fetch_id", {"fetch_id": "fetch-pending", "content_version_id": "content-pending"}),
            ("acquisition_items", "acquisition_item_id", {"acquisition_item_id": "item-pending", "ordinal": 90,
                "content_version_id": "content-pending", "fetch_id": "fetch-pending", "title": "PendingTitle", "summary": "PendingSummary", "discovered_at": "2026-10-04T12:00:00Z"}),
        ):
            row = dict(db.execute(f"SELECT * FROM {table} WHERE article_id='article-a' LIMIT 1").fetchone())
            row.update(replacements)
            db.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        db.execute("UPDATE articles SET current_content_version_id='content-pending',current_version_id='version-pending' WHERE article_id='article-a'")


def test_public_detail_and_list_pin_old_active_version(tmp_path, monkeypatch):
    from test_issue113_range_reports import _database
    from climate_registry.read_api import RegistryReader
    from climate_registry.web_ingest_pipeline import _batch_items, _manifest_item
    import api_server
    database = _database(tmp_path,target_version=20)
    identity = next(_manifest_item(i) for i in _batch_items(database, "batch") if i["acquisition_item_id"] == "a-1")
    _pending_version(database)
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    public_path = tmp_path / "public"
    public_path.mkdir()
    public = RegistryReader(_database(public_path,target_version=20), repository_root=tmp_path / "application")
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (reader, None, {"web_items": [identity], "pdf_occurrence_ids": []}))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: public)
    monkeypatch.setattr(api_server, "registry_pdf_articles", lambda **kw: {"items": [], "pagination": {"page": 1, "page_size": 100, "total": 0, "pages": 0}})
    detail = api_server.registry_article("article-a")
    assert detail["title"] == "Observed article-a"
    assert detail["content"]["content_version_id"] == "content-article-a"
    assert "Pending body" not in detail["content"].get("markdown", "")
    listing = api_server.registry_articles(include_pdf=True)
    assert next(i for i in listing["items"] if i["article_id"] == "article-a")["title"] == "Observed article-a"
    assert api_server.registry_articles(include_pdf=True, query="PendingTitle")["items"] == []


def test_t10_partial_approval_correction_and_writer_request(tmp_path,monkeypatch):
    from test_issue113_range_reports import _database
    from climate_registry import acquisition_review as review
    from climate_registry.web_ingest_pipeline import read_web_activation_request
    database = _database(tmp_path,target_version=22)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    run = tmp_path / "run"
    run.mkdir()
    binding = {"registry_database": str(database), "run_id": "run", "acquisition_batch_id": "batch",
        "attempt": 1, "task_version": 3, "source_keys": ["example"]}
    atomic_write_json(run / "attempt-1-result.json", {"run_id": "run", "attempt": 1, "finished_at": "2026-10-06T12:00:00Z"})
    state = _queue_after_actual_information(run / "binding.json", binding, {"source_outcomes": []})
    native = tmp_path / "hermes.db"
    _native(native, "cron_t10_first")
    with sqlite3.connect(native) as db:
        for candidate in state["packet"]["candidates"]:
            key = candidate["identity"]["acquisition_item_id"]
            db.execute("INSERT INTO messages(session_id,role,tool_calls) VALUES('cron_t10_first','assistant',?)", (json.dumps([{
                "id": key, "function": {"name": "read_file", "arguments": json.dumps({"path": str((run / "acquisition-review" / candidate["body_path"]).resolve())})}}]),))
            db.execute("INSERT INTO messages(session_id,role,tool_call_id,tool_name,content) VALUES('cron_t10_first','tool',?,'read_file',?)", (key, candidate["identity"]["markdown_content"]))
            if candidate.get("registry_snapshot_path"):
                path=Path(candidate["registry_snapshot_path"])
                call=key+"-registry"
                db.execute("INSERT INTO messages(session_id,role,tool_calls) VALUES('cron_t10_first','assistant',?)", (json.dumps([{"id":call,"function":{"name":"read_file","arguments":json.dumps({"path":str(path)})}}]),))
                db.execute("INSERT INTO messages(session_id,role,tool_call_id,tool_name,content) VALUES('cron_t10_first','tool',?,'read_file',?)",(call,path.read_text(encoding="utf-8")))
    at = datetime(2026, 10, 6, 12, 10, tzinfo=timezone.utc)
    claim = review.claim_acquisition(run / "acquisition-review", session_id="cron_t10_first", execution_id="first",
        hermes_database=native, reviewer="reviewer", now=at)
    approved = {c["identity"]["acquisition_item_id"]: {"candidate_sha256": c["candidate_sha256"], "status": "pass", "reason": "Body and date match"}
        for c in state["packet"]["candidates"] if c["identity"]["acquisition_item_id"] in {"a-1", "b-1"}}
    result = review.review_acquisition(run / "acquisition-review", claim["token"], {"items": approved}, now=at)
    assert set(result["item_reviews"]) == {"a-1", "b-1"}
    assert {i["acquisition_item_id"] for i in result["history"][-1]["inspections"]} == {"a-1", "b-1"}
    queue = tmp_path / "queue"
    queue.mkdir()
    request = review.activate_approved(run / "acquisition-review", queue_dir=queue, database=database, repository_root=tmp_path / "application")
    assert {i["acquisition_item_id"] for i in request["web_items"]} == {"a-1", "b-1"}
    assert read_web_activation_request(queue, request["batch_id"])["registry_sha256"] == request["registry_sha256"]
    from climate_registry.web_ingest_pipeline import WebIngestPipeline
    from climate_registry.pdf_pipeline import load_active_projection, load_projection_manifest
    from test_issue181_ingest_only import _ack
    runtime = tmp_path / "runtime"
    failed = WebIngestPipeline(queue, database, runtime,
        lambda _: (_ for _ in ()).throw(RuntimeError("reload unavailable")), repository_root=tmp_path / "application").process(request["batch_id"])
    assert failed["stage"] == "failed" and failed["indexed"]
    # Preserve the same immutable request and retry activation without reacquisition.
    retried = review.activate_approved(run / "acquisition-review", queue_dir=queue, database=database, repository_root=tmp_path / "application")
    assert retried["registry_sha256"] == request["registry_sha256"]
    # A real approved activation retry must check its saved binding before an
    # unavailable snapshot can consume that retry or overwrite review history.
    request_path = next((queue / "web").glob("*/request.json"))
    snapshot_path = request_path.parent / "registry.sqlite3"
    saved_request, saved_snapshot = request_path.read_bytes(), snapshot_path.read_bytes()
    other = tmp_path / "other.sqlite3"
    other.write_bytes(database.read_bytes())
    for artifact in ("missing", "corrupt"):
        if artifact == "missing": snapshot_path.unlink()
        else: snapshot_path.write_bytes(b"unavailable historical snapshot")
        for has_binding in (True, False):
            stored = json.loads(saved_request)
            if not has_binding: stored.pop("source_registry_database")
            atomic_write_json(request_path, stored)
            for selected in (database, other):
                monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(selected))
                if has_binding and selected == database: continue
                files = lambda: {str(p): p.read_bytes() for directory in (run, queue, runtime) for p in directory.rglob("*") if p.is_file()}
                before, old_bytes, new_bytes = files(), database.read_bytes(), other.read_bytes()
                with pytest.raises(ValueError, match="frozen task binding" if selected == other else "no frozen Registry binding"):
                    review.activate_approved(run / "acquisition-review", queue_dir=queue, database=database, repository_root=tmp_path / "application")
                assert files() == before and database.read_bytes() == old_bytes and other.read_bytes() == new_bytes
        request_path.write_bytes(saved_request)
        snapshot_path.write_bytes(saved_snapshot)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    ready = WebIngestPipeline(queue, database, runtime, _ack(queue),
        repository_root=tmp_path / "application").process(request["batch_id"])
    assert ready["chat_ready"], ready["error"]
    revised = review.correct_candidate(run / "acquisition-review", "a-1", {"summary": "Derived correction"}, reason="Actual evidence correction")
    assert set(revised["item_reviews"]) == {"b-1"}
    request2 = review.activate_approved(run / "acquisition-review", queue_dir=queue, database=database, repository_root=tmp_path / "application")
    assert {i["acquisition_item_id"] for i in request2["web_items"]} == {"b-1"}
    assert request2["batch_id"] != request["batch_id"]
    ready = WebIngestPipeline(queue, database, runtime, _ack(queue),
        repository_root=tmp_path / "application").process(request2["batch_id"])
    assert ready["chat_ready"], ready["error"]
    generation, metadata = load_active_projection(runtime, queue / "active.json")
    active = load_projection_manifest(generation, metadata)
    assert {item["acquisition_item_id"] for item in active["web_items"]} == {"a-1", "b-1"}
    assert "Derived correction" not in "\n".join(p.read_text() for p in generation.glob("*.md"))
    observed = review.acquisition_state(run / "acquisition-review")
    assert {item["stage"] for item in observed["writer_activations"]} == {"chat_ready"}
    assert observed["claim"].get("released_at")


def test_independent_observer_keeps_scheduler_and_business_separate():
    from scripts.export_scheduler_status import project_independent
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE executions(job_id TEXT,status TEXT,claimed_at TEXT,started_at TEXT,finished_at TEXT)")
    now = datetime(2026, 10, 12, 12, 2, tzinfo=timezone.utc)
    ids = {role: "real-" + role for role in schedule.PIPELINE_SLOTS}
    definitions = [{"id": value, "enabled": True, "no_agent": role not in {"T5", "T10"}} for role, value in ids.items()]
    db.execute("INSERT INTO executions VALUES('real-T5','completed','2026-10-12T12:00:00Z','2026-10-12T12:00:01Z','2026-10-12T12:01:00Z')")
    snapshot = project_independent(db, ids, now=now, definitions=definitions,
        business={"T5": {"status": "pending_review"}, "T6": {"status": "not_sent"}})
    assert snapshot["jobs"]["T5"]["state"] == "completed"
    assert snapshot["business"]["T5"]["status"] == "pending_review"
    assert snapshot["jobs"]["T2"]["scheduled_for"] != snapshot["jobs"]["T3"]["scheduled_for"]
    with pytest.raises(ValueError, match="not observed"):
        project_independent(db, ids, now=now, business={}, definitions=definitions[:-1])


def test_native_full_text_chunks_require_actual_content_and_complete_coverage(tmp_path):
    path = (tmp_path / "full-text.txt").resolve()
    text = "first\nsecond\nthird\nfourth\n"
    def chunk(offset, lines, **extra):
        return {"tool": "read_file", "arguments": {"path": str(path), "offset": offset, "limit": 2000},
            "tool_call_id": str(offset), "result": json.dumps({"content": lines, "total_lines": 4, **extra})}
    first = chunk(1, "1|first\n2|second", truncated=True, truncated_by="bytes", next_offset=3)
    with pytest.raises(ValueError, match="full text"):
        read_evidence([first, chunk(4, "4|fourth", truncated=False)], path, text=text)
    with pytest.raises(ValueError, match="full text"):
        read_evidence([chunk(1, "1|first\n2|incorrect\n3|third\n4|fourth", truncated=False)], path, text=text)
    result = read_evidence([first, chunk(2, "2|second\n3|third\n4|fourth", truncated=False)], path, text=text)
    assert result["line_count"] == 4 and len(result["events"]) == 2
    with pytest.raises(ValueError, match="full text"):
        read_evidence([chunk(1, "1|first\n2|second\n3|third\n4|fourth", truncated_lines=[2])], path, text=text)


def test_native_claim_waits_for_owner_completion_and_retains_failure(tmp_path):
    from climate_registry.acquisition_review import claim_review
    native = tmp_path / "hermes.db"
    _native(native, "cron_owner")
    _native(native, "cron_successor")
    at = datetime(2026, 10, 12, 12, 10, tzinfo=timezone.utc)
    context = dict(execution_id="execution", hermes_database=native, reviewer="reviewer", timeout_seconds=1)
    old = claim_review(tmp_path / "review", {"revision": 1}, session_id="cron_owner", now=at, **context)
    with pytest.raises(ValueError, match="owned"):
        claim_review(tmp_path / "review", {"revision": 1}, session_id="cron_successor", now=at + timedelta(minutes=2), **context)
    with sqlite3.connect(native) as db:
        db.execute("UPDATE sessions SET end_reason='cron_error' WHERE id='cron_owner'")
    new = claim_review(tmp_path / "review", {"revision": 1}, session_id="cron_successor", now=at + timedelta(minutes=2), **context)
    assert new["token"] != old["token"]
    history = json.loads((tmp_path / "review" / "claim-history" / (old["token"] + ".json")).read_text())
    assert history["status"] == "review_failed" and history["end_reason"] == "cron_error"


def test_knowledge_reversion_is_a_new_material_update_but_whitespace_is_not():
    db = sqlite3.connect(":memory:")
    apply_migrations(db)
    for text, at in (("original body", "2026-09-29T00:00:00Z"), ("**original**  body\n", "2026-09-30T00:00:00Z"),
                     ("new information", "2026-10-01T00:00:00Z"), ("original body", "2026-10-02T00:00:00Z")):
        record_knowledge(db, kind="article", entity_id="a", source_kind="site", source_ref="item",
            fields={"content": text}, evidence={}, recorded_at=at)
    rows = db.execute("SELECT substantive_updated_at FROM knowledge_versions ORDER BY rowid").fetchall()
    assert rows == [(None,), ("2026-10-01T00:00:00Z",), ("2026-10-02T00:00:00Z",)]


def test_report_freeze_is_immutable_and_substantive_update_is_selected(tmp_path):
    state = _report(tmp_path)
    from climate_registry.read_api import RegistryReader
    reader = RegistryReader(tmp_path / "registry.sqlite3", repository_root=tmp_path / "application")
    # Discover fixture database without assuming its name.
    database = next(path for path in tmp_path.glob("*.sqlite3") if path.is_file())
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    root = tmp_path / "artifacts"
    before = (root / "reports" / "2026-10-12" / "snapshot.json").read_bytes()
    with sqlite3.connect(database) as db:
        record_knowledge(db, kind="article", entity_id="article-a", source_kind="site", source_ref="a-1",
            fields={"title": "new supported information"}, evidence={}, recorded_at="2026-10-13T12:00:00Z")
    assert report_review.freeze_biweekly(reader, root, occurrence="2026-10-12") == state
    assert (root / "reports" / "2026-10-12" / "snapshot.json").read_bytes() == before
    # This still-unarchived source was eligible in the previous period. Its new
    # supported change is selected in its real period, never assigned a fake date.
    _approve_public_fixture(database)
    newer = report_review.freeze_biweekly(reader, root, occurrence="2026-10-26")
    assert newer["status"] == "pending_review"
    snapshot = json.loads((root / "reports" / "2026-10-26" / "snapshot.json").read_text())
    assert any(v["selection_reason"] == "substantive_update" for v in snapshot["material_versions"])


def test_late_review_preserves_real_ingestion_and_original_publication(tmp_path,monkeypatch):
    from test_issue113_range_reports import _database
    database = _database(tmp_path,target_version=22)
    from climate_registry.read_api import RegistryReader
    from climate_registry.web_ingest_pipeline import _batch_items, _manifest_item
    database = tmp_path / "registry.sqlite3"
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    item = next(_manifest_item(i) for i in _batch_items(database, "batch") if i["acquisition_item_id"] == "a-1")
    raw = next(i for i in _batch_items(database, "batch") if i["acquisition_item_id"] == "a-1")
    with sqlite3.connect(database) as db:
        record_knowledge(db, kind="article", entity_id=raw["article_id"], source_kind="site", source_ref="a-1",
            fields={"title":raw["title"],"summary":raw["summary"],"publication_date":raw["publication_date"],"content":raw["markdown_content"]},
            evidence={"content_version_id":raw["content_version_id"]},recorded_at="2026-09-30T12:00:00Z")
    from climate_registry import publication
    from test_registry_publication import _approve_native_packet
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    monkeypatch.setattr(publication,"now_stamp",lambda:"2026-10-20T12:00:00Z")
    _approve_native_packet(database,tmp_path/"final-review",tmp_path/"native-final.db","cron_late_review")
    item["review"] = {"status":"pass","approved_at": "2026-10-20T12:00:00Z"}
    state = report_review.freeze_biweekly(reader, tmp_path / "artifacts", occurrence="2026-10-26",
        web_reader=reader, manifest={"web_items": [item], "pdf_occurrence_ids": [], "pdf_calendar_occurrence_ids": []},
        generated_at=datetime(2026, 10, 26, 12, tzinfo=timezone.utc))
    assert state["status"] == "pending_review"
    snapshot = json.loads((tmp_path / "artifacts" / "reports" / "2026-10-26" / "snapshot.json").read_text())
    version = next(v for v in snapshot["material_versions"] if v["source_ref"] == "a-1")
    assert version["selection_reason"] == "late_review_carryforward"
    assert version["first_ingested_at"] == version["original_period_time"] == "2026-09-30T12:00:00Z"
    assert version["intended_report_date"] == "2026-10-12"
    assert snapshot["articles"][0]["publication_date"] == "2026-09-20"


def test_new_york_window_includes_start_excludes_end_and_has_dst_calendar_days(tmp_path):
    from test_issue113_range_reports import _database
    from climate_registry.read_api import RegistryReader
    database = _database(tmp_path,target_version=22)
    for entity, ref, at in (("article-a", "a-1", "2026-10-26T04:00:00Z"), ("article-b", "b-1", "2026-11-09T05:00:00Z")):
        with sqlite3.connect(database) as db:
            record_knowledge(db, kind="article", entity_id=entity, source_kind="site", source_ref=ref,
                fields={"summary": entity}, evidence={}, recorded_at=at)
    _approve_public_fixture(database)
    root = tmp_path / "artifacts"
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    report_review.freeze_biweekly(reader, root, occurrence="2026-11-09")
    snapshot = json.loads((root / "reports" / "2026-11-09" / "snapshot.json").read_text())
    assert {v["source_ref"] for v in snapshot["material_versions"]} == {"a-1"}
    window = snapshot["selection_window"]
    assert (datetime.fromisoformat(window["end"]) - datetime.fromisoformat(window["start"])).total_seconds() == 337 * 3600
    # A later window with no material update produces no placeholder PDF.
    empty = report_review.freeze_biweekly(reader, root, occurrence="2026-12-07")
    assert empty["status"] == "no_eligible_information" and empty["revision"] == 0
    assert not (root / "reports" / "2026-12-07" / "revisions").exists()


def test_metadata_only_material_uses_all_approved_sources_for_selected_body(tmp_path,monkeypatch):
    from test_issue113_range_reports import _database
    from test_registry_publication import _approve_native_packet
    from climate_registry.read_api import RegistryReader
    database=_database(tmp_path,target_version=22)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    with sqlite3.connect(database) as db:
        db.execute("UPDATE articles SET display_policy='metadata_only' WHERE article_id='article-a'")
        record_knowledge(db,kind="article",entity_id="article-a",source_kind="site",source_ref="a-1",
            fields={"summary":"Supported metadata-only information"},evidence={"content_version_id":"content-article-a"},
            recorded_at="2026-09-30T12:00:00Z")
    _approve_native_packet(database,tmp_path/"review",tmp_path/"native.db","cron_metadata_material")
    reader=RegistryReader(database,repository_root=tmp_path/"application")
    detail=reader.article("article-a")
    assert detail["available_content"]["acquisition_item_id"]=="a-2"
    assert "markdown" not in detail["available_content"] and "supporting_excerpt" not in detail["available_content"]
    state=report_review.freeze_biweekly(reader,tmp_path/"artifacts",occurrence="2026-10-12",generated_at=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert state["status"]=="pending_review"
    snapshot=json.loads((tmp_path/"artifacts/reports/2026-10-12/snapshot.json").read_text())
    assert {item["source_ref"] for item in snapshot["material_versions"] if item["source_kind"]=="site"}=={"a-1"}
    article=snapshot["articles"][0]
    assert article["summary"]==detail["summary"] and article["content"] is None
    assert article["content_version_id"]==detail["available_content"]["content_version_id"]=="content-article-a"
    assert article["publication_date"]=="2026-09-20"


def test_code_blocker_and_invalid_display_cannot_release_or_send(tmp_path):
    state = _report(tmp_path)
    root = tmp_path / "artifacts"
    native = tmp_path / "hermes.db"
    _native(native, "cron_t5_code", state["packet"])
    at = datetime(2026, 10, 12, 12, 10, tzinfo=timezone.utc)
    claim = report_review.claim_report(root, "2026-10-12", session_id="cron_t5_code", execution_id="code",
        hermes_database=native, reviewer="reviewer", now=at)
    with pytest.raises(ValueError, match="identity"):
        report_review.submit_report_review(root, "2026-10-12", claim["token"], status="changes_requested",
            reason="invalid evidence edit", changes={"updates": {"0": {"citations": []}}}, now=at)
    revision = root / "reports" / "2026-10-12" / "revisions" / "0001"
    assert "released_at" not in json.loads((revision / "claim.json").read_text())
    proposal = {key: "Reproducible clipping on page 1" for key in
        ("reason", "pdf_evidence", "root_cause", "files", "suggested_change", "pr_title", "pr_description", "validation")}
    blocked = report_review.submit_report_review(root, "2026-10-12", claim["token"], status="blocked_code_change",
        reason="Template clips evidence", proposal=proposal, now=at)
    assert blocked["status"] == "blocked_code_change"
    with pytest.raises(ValueError, match="not approved"):
        report_review.send_approved(root, "2026-10-12", now=at + timedelta(hours=2))
    repaired = report_review.regenerate_report(root, "2026-10-12", reason="Deployed reviewed template repair")
    assert repaired["revision"] == 2 and repaired["packet"]["parent_context"] == "cron_t5_code"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_exact_sender_retains_recipients_and_never_retries_unknown(tmp_path, monkeypatch, ambiguous):
    import smtplib
    from climate_delivery.config import DeliveryConfig, Recipient, SMTPConfig
    from climate_delivery.errors import LockStateError
    state = _report(tmp_path)
    native = tmp_path / "hermes.db"
    _native(native, "cron_sender_review", state["packet"])
    root = tmp_path / "artifacts"
    at = datetime(2026, 10, 12, 12, 10, tzinfo=timezone.utc)
    claim = report_review.claim_report(root, "2026-10-12", session_id="cron_sender_review", execution_id="review",
        hermes_database=native, reviewer="reviewer", now=at)
    approved = report_review.submit_report_review(root, "2026-10-12", claim["token"], status="pass", reason="Actual complete inspection", now=at)
    config = DeliveryConfig(SMTPConfig("smtp.example.test", 587, "fixture", "fixture", "from@example.test", "Fixture", "starttls"),
        tuple(Recipient(value, value + "@example.test") for value in ("alpha", "beta", "gamma", "delta")))
    monkeypatch.setattr("climate_delivery.config.load_delivery_config", lambda _: config)
    attachments = []
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def starttls(self, context=None): pass
        def login(self, username, password): pass
        def send_message(self, message):
            attachments.append(next(message.iter_attachments()).get_content())
            if ambiguous:
                raise smtplib.SMTPServerDisconnected("unknown outcome")
            return {}
    kwargs = dict(no_send=False, config_path=tmp_path / "private-config", smtp_factory=SMTP, now=at + timedelta(minutes=60))
    if ambiguous:
        with pytest.raises(LockStateError, match="unknown"):
            report_review.send_approved(root, "2026-10-12", **kwargs)
        saved = report_review.report_states(root)[0]
        assert saved["recipient_receipts"][0]["recipients"]["alpha"]["status"] == "unknown"
    else:
        assert report_review.send_approved(root, "2026-10-12", **kwargs)["status"] == "sent"
        assert len(attachments) == 4
    kwargs["smtp_factory"] = lambda *_a, **_kw: pytest.fail("duplicate SMTP attempt")
    if ambiguous:
        with pytest.raises(LockStateError, match="reconciliation"):
            report_review.send_approved(root, "2026-10-12", **kwargs)
    else:
        assert report_review.send_approved(root, "2026-10-12", **kwargs)["status"] == "already-sent"
    exact = root / "reports" / "2026-10-12" / "revisions" / "0001" / "report.pdf"
    assert all(content == exact.read_bytes() for content in attachments)
    assert approved["approval"]["packet"]["pdf_sha256"] == hashlib.sha256(exact.read_bytes()).hexdigest()


def test_daily_checks_dedupe_each_kind_without_cross_approving_reports(tmp_path, monkeypatch):
    from scripts import check_information
    at = datetime(2026, 10, 6, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(check_information, "datetime", SimpleNamespace(now=lambda _tz: at))
    calls = []
    def run(database, **kwargs):
        calls.append(kwargs["kind"])
        return {"run_id": kwargs["kind"] + "-run", "status": "complete", "item_count": 1, "completed_count": 1}
    monkeypatch.setattr(check_information, "run_checks", run)
    for kind in ("articles", "meetings"):
        result = tmp_path / (kind + "-daily-check.json")
        argv = ["--kind", kind, "--database", str(tmp_path / "registry.sqlite3"), "--backup-dir", str(tmp_path / "backups"),
            "--scheduled", "--result", str(result)]
        assert check_information.main(argv) == 0
        assert check_information.main(argv) == 0
        receipt = json.loads(result.read_text())
        assert receipt["occurrence"] == "2026-10-06T09:00:00Z"
        assert "approval" not in receipt
    assert calls == ["articles", "meetings"]


def test_weekly_search_does_not_run_rotation_reader(monkeypatch):
    from scripts import run_agent_acquisition as runner
    monkeypatch.delenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", raising=False)
    search = runner._controlled_site_context({"acquisition_kind": "weekly_search"})
    assert search["status"] == "completed" and search["source_results"] == []
    assert search["attempts"] == [] and search["acquisition_kind"] == "weekly_search"
    website = runner._controlled_site_context({"acquisition_kind": "website_rotation"})
    assert website["status"] == "not_configured"  # Never pretend absence means no new data.


def test_t10_recovery_reuses_binding_and_refusal_cannot_create_a_new_run(tmp_path,monkeypatch):
    from test_issue113_range_reports import _database
    from climate_registry import acquisition_review as review
    database = _database(tmp_path,target_version=22)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    run = tmp_path / "run"
    run.mkdir()
    binding = {"registry_database": str(database), "run_id": "run", "acquisition_batch_id": "batch",
        "attempt": 1, "task_version": 3, "source_keys": ["example"], "budgets": {"runtime_seconds": 4500}}
    atomic_write_json(run / "attempt-1-result.json", {"run_id": "run", "attempt": 1, "finished_at": "2026-10-06T12:00:00Z"})
    root = run / "acquisition-review"
    native = tmp_path / "hermes.db"
    _native(native, "cron_t10_recovery")
    at = datetime(2026, 10, 6, 12, 10, tzinfo=timezone.utc)
    state = _queue_after_actual_information(run / "binding.json", binding, {"source_outcomes": [
        {"source": "example", "status": "failed", "coverage_status": "rejected", "failure_reason": "access refused"}]})
    claim = review.claim_acquisition(root, session_id="cron_t10_recovery", execution_id="recovery", hermes_database=native, reviewer="reviewer", now=at)
    calls = []
    service = SimpleNamespace(binding=lambda run_id: binding, resume=lambda run_id: calls.append(run_id) or {"run_id": run_id, "attempt": 2})
    with pytest.raises(ValueError, match="refusal"):
        review.recover_acquisition(root, claim["token"], service, now=at)
    assert calls == []
    state["packet"]["source_outcomes"][0].update(coverage_status="partial", failure_reason="temporary timeout")
    state["packet_sha256"] = digest(state["packet"])
    atomic_write_json(root / "state.json", state)
    atomic_write_json(root / "claim.json", {**claim, "packet_sha256": state["packet_sha256"]})
    service.binding = lambda _: {**binding, "task_version": 4}
    with pytest.raises(ValueError, match="binding"):
        review.recover_acquisition(root, claim["token"], service, now=at)
    service.binding = lambda _: binding
    assert review.recover_acquisition(root, claim["token"], service, now=at) == {"run_id": "run", "attempt": 2}
    assert calls == ["run"] and binding["budgets"] == {"runtime_seconds": 4500}


def test_v18_pdf_known_commit_survives_migration_without_fabricating_unknown_time(tmp_path):
    from test_issue113_range_reports import _database
    from climate_registry.read_api import RegistryReader
    database = _database(tmp_path,target_version=20)
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE knowledge_versions")
        db.execute("DELETE FROM schema_migrations WHERE version IN (19, 20)")
        db.execute("PRAGMA user_version=18")
        db.execute("UPDATE pdf_intake_articles SET imported_at='unknown' WHERE article_id='pdf-month'")
        db.commit()
        assert apply_migrations(db,target_version=20) == [19, 20]
    before = database.read_bytes()
    state = report_review.freeze_biweekly(RegistryReader(database, repository_root=tmp_path / "application"),
        tmp_path / "artifacts", occurrence="2026-10-12")
    assert state["status"] == "pending_review"
    snapshot = json.loads((tmp_path / "artifacts" / "reports" / "2026-10-12" / "snapshot.json").read_text())
    version = next(v for v in snapshot["material_versions"] if v["source_ref"] == "pdf-occ-a")
    assert version["first_ingested_at"] == "2026-09-30T12:00:00Z"
    assert version["time_basis"] == "historical_pdf_article_imported_at"
    assert snapshot["articles"][0]["publication_date"] == "2026-09-20"
    assert any(g["source_ref"] == "pdf-occ-month" for g in snapshot["coverage_gaps"])
    assert database.read_bytes() == before  # T4 does not perform backfill writes.


def test_unchanged_legacy_web_regrab_keeps_unknown_time_until_supported_change(tmp_path):
    from test_issue112_acquisition import _batch, _item
    from climate_registry.acquisition import store_acquisition_batch
    database = tmp_path / "registry.sqlite3"
    with sqlite3.connect(database) as db:
        apply_migrations(db, target_version=18)
    # Historical SQL facts use the schema18 columns, not a new writer bypass.
    seed=tmp_path/"fixture-current.sqlite3"
    with sqlite3.connect(seed) as db:apply_migrations(db)
    store_acquisition_batch(seed,_batch([_item()],batch_id="legacy"))
    with sqlite3.connect(seed) as source, sqlite3.connect(database) as legacy:
        for (table,) in legacy.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_migrations'").fetchall():
            columns=[row[1] for row in legacy.execute(f"PRAGMA table_info({table})")]
            rows=source.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
            legacy.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",rows)
        assert legacy.execute("PRAGMA foreign_key_check").fetchall()==[]
    from climate_registry.publication import migrate_publication
    migrate_publication(database,tmp_path/"backups",apply=True)
    store_acquisition_batch(database, _batch([_item()], batch_id="unchanged"))
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT DISTINCT first_ingested_at,substantive_updated_at FROM knowledge_versions").fetchall() == [(None, None)]
    store_acquisition_batch(database, _batch([_item(body="# Full article\n\nSubstantive new findings.")], batch_id="changed"))
    with sqlite3.connect(database) as db:
        first, updated = db.execute("SELECT first_ingested_at,substantive_updated_at FROM knowledge_versions ORDER BY rowid DESC LIMIT 1").fetchone()
        assert first is None and datetime.fromisoformat(updated.replace("Z", "+00:00")).tzinfo is not None


def test_information_check_requires_explicit_v18_migration_with_exact_backup(tmp_path):
    from climate_registry.information_checks import prepare_database,run_checks
    from climate_registry.errors import RegistryInputError
    from climate_registry.publication import migrate_publication
    database = tmp_path / "registry.sqlite3"
    with sqlite3.connect(database) as db:
        apply_migrations(db, target_version=18)
    db.close()
    before = database.read_bytes()
    backups = tmp_path / "backups"
    calls = []
    def unexpected(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("old-schema T1 reached fetch/model before explicit migration")
    with pytest.raises(RegistryInputError, match="migrate-publication"):
        run_checks(database, kind="articles", backup_dir=backups, fetcher=unexpected, verifier=unexpected)
    assert database.read_bytes() == before and not backups.exists() and not calls
    with sqlite3.connect(database) as db:
        assert validate_registry_contract(db) == 18
        assert db.execute("SELECT count(*) FROM article_check_runs").fetchone()[0] == 0
    db.close()
    migrated = migrate_publication(database, backups, apply=True)
    assert migrated["accepted_legacy"] == 0
    result = run_checks(database, kind="articles", backup_dir=backups, fetcher=unexpected, verifier=unexpected)
    assert result["status"] == "complete" and result["item_count"] == 0 and not calls
    with sqlite3.connect(database) as db:
        assert validate_registry_contract(db) == 22
        assert db.execute("SELECT count(*) FROM knowledge_versions").fetchone()[0] == 0
    saved = list(backups.glob("*.bak"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == before and str(saved[0]) == migrated["backup"]
    with sqlite3.connect(saved[0]) as backup:
        assert validate_registry_contract(backup) == 18
    prepare_database(database, backups)
    assert list(backups.glob("*.bak")) == saved


def test_native_measured_image_envelope_and_lossless_long_line_read(tmp_path):
    from climate_registry.acquisition_review import freeze_readable, read_verified_text
    path = tmp_path / "body.txt"
    text = 'Climate "insurance"\n' + "risk " * 2000
    path.write_text(text)
    view = freeze_readable(tmp_path / "readable.json", text)
    value = Path(view["path"]).read_text()
    assert "".join(json.loads(value)) == text
    lines = value.splitlines()
    event = {"tool": "read_file", "arguments": {"path": view["path"]}, "tool_call_id": "read",
        "result": json.dumps({"content": "\n".join(f"{i}|{line}" for i, line in enumerate(lines, 1)), "total_lines": len(lines)})}
    assert read_verified_text([event], path, text, view)["raw_sha256"]
    Path(view["path"]).write_text(value.replace("insurance", "fiction"))
    with pytest.raises(ValueError, match="identity changed"):
        read_verified_text([event], path, text, view)
    image_event = {"tool": "vision_analyze", "arguments": {"image_url": str(path.resolve())},
        "tool_call_id": "vision", "result": "Image loaded into your context — you can see it natively now. Use your built-in vision to answer. Question...\n[screenshot]"}
    assert read_evidence([image_event], path, image=True) == image_event
    image_event["arguments"]["image_url"] += ".other.png"
    with pytest.raises(ValueError, match="image inspection"):
        read_evidence([image_event], path, image=True)
    image_event["arguments"]["image_url"] = str(path.resolve())
    image_event["result"] = "[screenshot]"
    with pytest.raises(ValueError, match="image inspection"):
        read_evidence([image_event], path, image=True)


def test_linked_pdf_material_freeze_preserves_entity_and_document_provenance(tmp_path):
    from test_issue113_range_reports import _database
    from climate_registry.read_api import RegistryReader
    database = _database(tmp_path, pdf_period=("2026-09-28", "2026-10-11"))
    before = database.read_bytes()
    state = report_review.freeze_biweekly(RegistryReader(database, repository_root=tmp_path / "application"),
        tmp_path / "artifacts", occurrence="2026-10-12")
    assert state["status"] == "pending_review"
    snapshot = json.loads((tmp_path / "artifacts/reports/2026-10-12/snapshot.json").read_text())
    article = next(a for a in snapshot["articles"] if a["article_id"] == "article-a")
    linked = next(v for v in article["material_versions"] if v["source_ref"] == "pdf-occ-a")
    assert linked["entity_id"] == "pdf-a" and article["publication_date"] == "2026-09-20"
    update = next(u for u in snapshot["pdf_source_updates"] if u["observation_id"] == "pdf-occ-month")
    assert update["material_versions"][0]["entity_id"] == "pdf-month"
    assert update["source_observations"][0]["filename"] == "report.pdf"
    assert update["summary"] == "Month observation"
    report_source = json.loads((tmp_path / "artifacts/reports/2026-10-12/revisions/0001/report-source.json").read_text())
    assert any("Month observation" in paragraph for item in report_source["updates"] for paragraph in item["paragraphs"])
    assert {"pdf-occ-a", "pdf-occ-month"} <= {v["source_ref"] for v in snapshot["material_versions"]}
    assert database.read_bytes() == before


@pytest.mark.parametrize("primary_change",["baseline","derived","model","pending_pdf"])
def test_verified_pdf_material_is_rendered_and_only_current_version_archived(tmp_path, monkeypatch,primary_change):
    from test_issue113_range_reports import _database
    from climate_registry.read_api import RegistryReader
    from climate_registry import information_checks
    database = _database(tmp_path,target_version=22)
    with sqlite3.connect(database) as db:
        row = db.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='pdf-occ-a'").fetchone()
        occurrence = json.loads(row[-1]);occurrence.update(occurrence_id="pdf-check-a",source_document_sha256=row[2],page=7,
            raw_url="https://example.org/a.pdf",summary_basis="verbatim_pdf_paragraph",publication_date="2026-09-20")
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES (" + ",".join("?" for _ in row) + ")", ("pdf-check-a",*row[1:-1],json.dumps(occurrence)))
    monkeypatch.setattr(information_checks, "_now", lambda: "2026-10-02T12:00:00Z")
    body = "# PDF A\nPDF observation. Published 20 September 2026. Flood insurance losses rise and capital requirements need review."
    def fetcher(identity, url):
        return {"article_id":identity,"requested_url":url,"final_url":url,"status":"ok","content":body,
            "content_ref":None,"content_hash":hashlib.sha256(body.encode()).hexdigest()}
    def verifier(kind, fields, content):
        return {"comparisons":{key:{"status":"supported"} for key in ("title","summary","publication_date")}}
    checked = information_checks.run_checks(database,kind="articles",backup_dir=tmp_path/"backups",
        occurrence_ids={"pdf-check-a"},fetcher=fetcher,verifier=verifier)
    assert checked["status"] == "complete"
    from test_registry_publication import _approve_native_packet
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    _approve_native_packet(database,tmp_path/"final-review",tmp_path/"native-final.db","cron_verified_pdf")
    if primary_change=="derived":
        from climate_registry.publication import snapshot_entity,stage_snapshot,_approve
        with sqlite3.connect(database) as db:
            current=snapshot_entity(db,"article","article-a")
            current["derived_display"]={"summary":"Native approved corrected summary"}
            sha=stage_snapshot(db,current);_approve(db,sha,{"basis":"explicit approved canonical correction fixture"}, status="accepted_legacy")
    elif primary_change=="model":
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO article_enrichments(enrichment_id,content_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('new-model','content-article-a','complete','Approved new canonical model summary','[]','[]','en','model','model-fixture','v2','2026-10-04T12:00:00Z')")
        _approve_native_packet(database,tmp_path/"model-review",tmp_path/"native-model.db","cron_verified_model")
    reader = RegistryReader(database,repository_root=tmp_path/"application")
    summary = next(o for o in reader.pdf_article("pdf-a")["occurrences"] if o["occurrence_id"] == "pdf-check-a")["verified_information"]["summary"]
    assert "Flood insurance losses" in summary
    canonical=reader.article("article-a")
    if primary_change=="derived":assert canonical["summary"]=="Native approved corrected summary"
    if primary_change=="model":assert canonical["summary"]=="Approved new canonical model summary"
    with sqlite3.connect(database) as db:
        material = dict(zip([d[0] for d in db.execute("SELECT * FROM knowledge_versions LIMIT 0").description],
            db.execute("SELECT * FROM knowledge_versions WHERE source_ref='pdf-check-a' ORDER BY rowid DESC LIMIT 1").fetchone()))
        baseline_hash = db.execute("SELECT material_sha256 FROM knowledge_versions WHERE source_ref='pdf-check-a' ORDER BY rowid LIMIT 1").fetchone()[0]
    if primary_change=="pending_pdf":
        monkeypatch.setattr(information_checks,"_now",lambda:"2026-10-05T12:00:00Z")
        body="# PDF A\nPending newer unapproved source verification summary. Published 20 September 2026."
        information_checks.run_checks(database,kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={"pdf-check-a"},fetcher=fetcher,verifier=verifier)
    state = report_review.freeze_biweekly(reader,tmp_path/"artifacts",occurrence="2026-10-12")
    frozen = tmp_path/"artifacts/reports/2026-10-12"
    snapshot = json.loads((frozen/"snapshot.json").read_text())
    article = next(a for a in snapshot["articles"] if a["article_id"] == "article-a")
    assert article["summary"]==canonical["summary"]
    selected=next(value for value in article["selected_material_summaries"] if value["provenance"]["occurrence_id"]=="pdf-check-a")
    assert selected["summary"]==summary
    provenance=selected["provenance"]
    assert provenance["basis"]=="approved_source_verification"
    assert provenance["candidate_sha256"]==canonical["published_candidate_sha256"]
    assert provenance["dto_field"]=="pdf_occurrences[occurrence_id=pdf-check-a].verified_information.summary"
    information=next(value for value in canonical["pdf_occurrences"] if value["occurrence_id"]=="pdf-check-a")["verified_information"]
    for field in ("source_url","body_sha256","generated_at"):assert provenance[field]==information[field]
    assert provenance["knowledge_id"]==material["knowledge_id"] and provenance["material_sha256"]==material["material_sha256"]
    evidence=json.loads(material["evidence_json"])
    assert provenance["packet_sha256"]==evidence["packet_sha256"] and provenance["run_id"]==evidence["run_id"]
    assert provenance["original_period_time"]=="2026-10-02T12:00:00Z"
    assert "Pending newer unapproved" not in json.dumps(snapshot)
    source = json.loads((frozen/"revisions/0001/report-source.json").read_text())
    assert summary in "\n".join(p for u in source["updates"] for p in u["paragraphs"])
    assert "Flood insurance losses" in (frozen/"revisions/0001/full-text.txt").read_text()
    assert article["publication_date"] == "2026-09-20"
    native = tmp_path/"hermes.db";_native(native,"cron_t5_verified",state["packet"])
    at = datetime(2026,10,12,12,10,tzinfo=timezone.utc)
    claim = report_review.claim_report(tmp_path/"artifacts","2026-10-12",session_id="cron_t5_verified",
        execution_id="verified",hermes_database=native,reviewer="native-reviewer",now=at)
    report_review.submit_report_review(tmp_path/"artifacts","2026-10-12",claim["token"],status="pass",reason="Verified material read in actual PDF",now=at)
    archive = json.loads((frozen/"archive.json").read_text())
    assert ["article","pdf-a",material["material_sha256"],"2026-10-02T12:00:00Z"] in archive["material_versions"]
    assert not any(v[2] == baseline_hash for v in archive["material_versions"])


@pytest.mark.parametrize("article_status", ["complete", "partial"])
def test_daily_wrapper_keeps_admitted_occurrence_after_slow_first_phase(tmp_path, monkeypatch, article_status):
    import io, sys
    from scripts import check_information
    from climate_registry import pdf_pipeline
    import climate_monitor.article_content_adapter as adapter
    clock = [datetime(2026,10,6,9,tzinfo=timezone.utc)]
    monkeypatch.setattr(check_information,"datetime",SimpleNamespace(now=lambda _tz:clock[0]))
    monkeypatch.setattr(adapter,"check_dependencies",lambda:"available")
    calls = []
    def run(database, **kwargs):
        calls.append((kwargs["kind"],kwargs["resume_run_id"],kwargs["retry_run_id"]))
        if kwargs["kind"] == "articles":clock[0] += timedelta(minutes=6)
        status = article_status if len(calls) == 1 else "complete"
        return {"run_id":kwargs["kind"]+"-run","status":status,"item_count":1,"completed_count":1}
    monkeypatch.setattr(check_information,"run_checks",run)
    from climate_registry import publication
    monkeypatch.setattr(publication,"resolve_database",lambda database:database)
    monkeypatch.setattr(publication,"prepare_review",lambda database,root:{"candidates":[]})
    monkeypatch.setenv("CLIMATE_ACQUISITION_RUN_DIR",str(tmp_path/"runs"))
    real_main = check_information.main
    def local_main(argv, **kwargs):
        argv = [str(tmp_path / Path(a).name) if a.startswith("/pipeline/") else a for a in argv]
        return real_main(argv, **kwargs)
    monkeypatch.setattr(check_information,"main",local_main)
    for name in ("CLIMATE_REGISTRY_DB","CLIMATE_REGISTRY_BACKUP_DIR","CLIMATE_PDF_INTAKE_QUEUE_DIR",
                 "CLIMATE_PDF_RUNTIME_WIKI_DIR","CLIMATE_PDF_RELOAD_URL","RELOAD_TOKEN"):
        monkeypatch.setenv(name,str(tmp_path/name))
    script = (Path(__file__).resolve().parents[1]/"scripts/hermes_job_information_check.sh").read_text()
    program = script.split("python -c '",1)[1].rsplit("\n' ",1)[0]
    def dispatch(at):
        clock[0] = at
        monkeypatch.setattr(sys,"stdin",io.StringIO("TYPESAFE_API_KEY=isolated-test\n"))
        monkeypatch.setattr(sys,"argv",["wrapper",""])
        with pytest.raises(SystemExit) as result:exec(program,{"__name__":"isolated_daily_wrapper"})
        return result.value.code
    at = datetime(2026,10,6,9,tzinfo=timezone.utc)
    assert dispatch(at) == (0 if article_status == "complete" else 2)
    assert [kind for kind,_,_ in calls] == ["articles","meetings"]
    for kind in ("articles","meetings"):
        receipt = json.loads((tmp_path/(kind+"-daily-check.json")).read_text())
        assert receipt["occurrence"] == "2026-10-06T09:00:00Z"
    assert dispatch(at+timedelta(minutes=2)) == 0
    if article_status == "partial":assert calls[-1] == ("articles",None,"articles-run")
    assert [kind for kind,_,_ in calls].count("meetings") == 1
    count = len(calls)
    assert dispatch(at+timedelta(minutes=6)) == 0
    assert len(calls) == count  # A new invocation outside the due window is blocked.
    assert dispatch(at+timedelta(hours=1)) == 0
    assert len(calls) == count  # The second UTC tick never starts fresh work.


def test_first_ingestion_in_window_survives_current_update_after_cutoff(tmp_path, monkeypatch):
    from reportlab.pdfgen.canvas import Canvas
    from test_information_checks import _record
    from climate_monitor.pdf_intake import import_pdf_reports
    from climate_monitor.meetings import EXTRACTION_FIELDS
    from climate_monitor.meeting_fields import MEETING_FIELDS
    from climate_registry import pdf_intake, information_checks as checks, pdf_pipeline
    from climate_registry.acquisition_review import knowledge_fields
    from climate_registry.pdf_pipeline import PdfIntakePipeline, enqueue_pdf_batch, load_active_projection
    from climate_registry.range_reports import load_active_range_overlay
    from climate_registry.read_api import RegistryReader
    from scripts.generate_range_report import main

    # A real import qualifies before midnight; supported T1 changes occur at NY 05:00.
    clock = [datetime(2026, 10, 11, 12, tzinfo=timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(pdf_intake, "datetime", Clock)
    monkeypatch.setattr(pdf_pipeline, "_now", lambda: clock[0].isoformat())
    monkeypatch.setattr(checks, "_now", lambda: clock[0].isoformat())
    monkeypatch.setattr(report_review, "datetime", Clock)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "source.pdf"
    pdf = Canvas(str(source))
    for lines in (("Climate Risk Intelligence Report Edition 9", "REPORTING PERIOD 1–2 September 2026",
            "DATE OF RUN 3 September 2026", "42 organisations monitored"),
        ("UPDATES", "Climate publication", "IN WINDOW 2 SEP 2026 REPORT", "Independent publication summary."),
        ("Key Dates", "DATE(S) EVENT HOST RELEVANCE", "20–23 October 2026", "EVENT",
            "Climate risk conference", "A conference relevant to insurers.")):
        for index, line in enumerate(lines):
            pdf.drawString(50, 760 - index * 20, line)
        if lines[0] == "UPDATES":
            pdf.linkURL("https://example.org/climate-publication", (48, 738, 220, 754), relative=0)
        elif lines[0] == "Key Dates":
            pdf.linkURL("https://example.org/climate-risk-conference/", (48, 676, 220, 694), relative=0)
        pdf.showPage()
    pdf.save()
    bundle = import_pdf_reports([source])
    assert len(bundle["articles"]) == len(bundle["calendar_items"]) == 1
    writer, public = tmp_path / "writer.sqlite3", tmp_path / "public.sqlite3"
    for database in (writer, public):
        with sqlite3.connect(database) as db:
            apply_migrations(db)
    queue, runtime = tmp_path / "queue", tmp_path / "runtime"
    queue.mkdir()
    batch = enqueue_pdf_batch(queue, bundle, repository_root=tmp_path / "application")
    def reload_chat(generation):
        pending = json.loads((queue / "pending.json").read_text())
        assert pending["generation_id"] == generation
        atomic_write_json(queue / "active.json", pending)
    pipeline = PdfIntakePipeline(queue, writer, tmp_path / "backups", runtime, reload_chat,
        repository_root=tmp_path / "application")
    assert pipeline.process(batch["batch_id"])["chat_ready"]
    original_generation, original_metadata = load_active_projection(runtime, queue / "active.json")
    original_snapshot = Path(original_metadata["pdf_registry_snapshot"]).read_bytes()

    article_body = ("Climate publication. New supported flood insurance loss estimates require stronger capital buffers. "
        "Independent publication summary. Published 2 September 2026.")
    meeting_body = ("Example Institute hosts Climate risk conference on 20–23 October 2026. "
        "09:00–10:30 GMT+8 at Singapore.")
    def verifier(kind, fields, body):
        result = {"comparisons": {key: {"status": "supported"} for key in fields}}
        if kind == "meetings":
            candidate = {key: None for key in EXTRACTION_FIELDS}
            candidate.update(name="Climate risk conference", event_type="conference", organizer="Example Institute",
                status="scheduled", date_precision="day", start_date="2026-10-20", end_date="2026-10-23",
                raw_time_text="09:00–10:30", timezone="GMT+8", location="Singapore", date_evidence=body)
            result["website_candidate"] = candidate
        return result
    clock[0] = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
    runs = [checks.run_checks(writer, kind=kind, backup_dir=tmp_path / "backups",
        fetcher=lambda identity, url, body=body: _record(identity, url, body), verifier=verifier)
        for kind, body in (("articles", article_body), ("meetings", meeting_body))]
    assert all(run["status"] == "complete" for run in runs)
    reader = RegistryReader(public, repository_root=tmp_path / "application")
    def freeze(root, occurrence="2026-10-12"):
        web, pdf, manifest = load_active_range_overlay(runtime, queue, repository_root=tmp_path / "application")
        return report_review.freeze_biweekly(reader, root, occurrence=occurrence,
            web_reader=web, pdf_reader=pdf, manifest=manifest)
    # Checks alone cannot expose the new version before the writer activates it.
    assert freeze(tmp_path / "before-activation")["status"] == "pending_review"
    before = json.loads((tmp_path / "before-activation/reports/2026-10-12/snapshot.json").read_text())
    original_article = (before["articles"] + before["pdf_source_updates"])[0]
    assert original_article["summary"] == json.loads(original_article["material_versions"][0]["fields_json"])["summary"]
    assert "New supported flood" not in original_article["summary"]
    assert before["pdf_calendar"]["records"][0].get("location") is None
    for run in runs:
        assert pipeline.refresh_checks(run["run_id"])["status"] == "chat_ready"
    assert Path(original_metadata["pdf_registry_snapshot"]).read_bytes() == original_snapshot
    active_generation, metadata = load_active_projection(runtime, queue / "active.json")
    assert active_generation != original_generation
    with sqlite3.connect(metadata["pdf_registry_snapshot"]) as db:
        db.row_factory = sqlite3.Row
        current = {row["source_ref"]: dict(row) for row in db.execute("SELECT * FROM knowledge_versions ORDER BY rowid")}
    clock[0] = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    root = tmp_path / "artifacts"
    assert main(["--database", str(public), "--artifact-root", str(root), "--biweekly-date", "2026-10-12",
        "--runtime-dir", str(runtime), "--queue-dir", str(queue)]) == 0
    frozen = root / "reports/2026-10-12"
    state = json.loads((frozen / "state.json").read_text())
    snapshot = json.loads((frozen / "snapshot.json").read_text())
    revision = frozen / "revisions/0001"
    rendered = json.loads((revision / "report-source.json").read_text())
    text = " ".join((revision / "full-text.txt").read_text().split())
    assert state["status"] == "pending_review" and len(snapshot["material_versions"]) == 2
    for material in snapshot["material_versions"]:
        actual = current[material["source_ref"]]
        assert material["knowledge_id"] == actual["knowledge_id"]
        assert material["material_sha256"] == digest(knowledge_fields(json.loads(actual["fields_json"])))
        assert material["selection_reason"] == "first_ingested"
        assert material["original_period_time"] == material["first_ingested_at"] == "2026-10-11T12:00:00+00:00"
        assert material["substantive_updated_at"] == "2026-10-12T09:00:00+00:00"
    article = (snapshot["articles"] + snapshot["pdf_source_updates"])[0]
    material = next(v for v in snapshot["material_versions"] if v["entity_kind"] == "article")
    fields = json.loads(material["fields_json"])
    assert article["summary"] == fields["summary"] and article["summary"] != original_article["summary"]
    assert article["title"] == (fields.get("title") or fields["anchor_text"]) == "Climate publication"
    assert article["publication_date"] == fields["publication_date"] == "2026-09-02"
    assert article["material_versions"][0]["knowledge_id"] == material["knowledge_id"]
    update = next(item for item in rendered["updates"] if item["article_id"] == material["entity_id"])
    assert update["title"] == article["title"] and update["publication_date"] == article["publication_date"]
    assert article["summary"] in "\n".join(update["paragraphs"])
    assert "New supported flood insurance loss estimates" in article["summary"]
    assert " ".join(article["summary"].split()) in text and article["title"] in text
    calendar = snapshot["pdf_calendar"]["records"][0]
    meeting = next(v for v in snapshot["material_versions"] if v["entity_kind"] == "meeting")
    meeting_fields = json.loads(meeting["fields_json"])
    assert calendar["pdf_event_id"] == meeting["entity_id"] and calendar["occurrence_id"] == meeting["source_ref"]
    assert {key: calendar.get(key) for key in MEETING_FIELDS} == meeting_fields
    assert all(value in text for value in ("Climate risk conference", "Example Institute", "Singapore", "GMT+8"))
    assert any("Singapore" in str(value) for value in rendered["key_dates"] + rendered["date_notes"])
    assert rendered["key_dates"][0][:3] == ["20–23 October 2026\n09:00–10:30\nGMT+8",
        meeting_fields["name"], meeting_fields["organizer"]]

    native = tmp_path / "hermes.db"
    _native(native, "cron_t5_current", state["packet"])
    at = datetime(2026, 10, 12, 12, 10, tzinfo=timezone.utc)
    claim = report_review.claim_report(root, "2026-10-12", session_id="cron_t5_current", execution_id="current",
        hermes_database=native, reviewer="native-reviewer", now=at)
    reviewed = report_review.submit_report_review(root, "2026-10-12", claim["token"], status="pass",
        reason="Current activated version actually inspected", now=at)
    archive = json.loads((frozen / "archive.json").read_text())
    assert {value[2] for value in archive["material_versions"]} == {v["material_sha256"] for v in snapshot["material_versions"]}

    # A later activated check snapshot cannot rewrite this occurrence or reselect it.
    frozen_bytes = {path: path.read_bytes() for path in (frozen / "snapshot.json", revision / "report-source.json", revision / "report.pdf")}
    clock[0] = datetime(2026, 10, 13, 9, tzinfo=timezone.utc)
    repeated = checks.run_checks(writer, kind="articles", backup_dir=tmp_path / "backups",
        fetcher=lambda identity, url: _record(identity, url, article_body), verifier=verifier)
    assert repeated["status"] == "complete" and pipeline.refresh_checks(repeated["run_id"])["status"] == "chat_ready"
    assert freeze(root) == reviewed
    assert all(path.read_bytes() == content for path, content in frozen_bytes.items())
    assert freeze(root, "2026-10-26")["status"] == "no_eligible_information"


@pytest.mark.parametrize("imported_at,new_location", [
    ("2026-09-15T12:00:00Z", False), ("unknown", False), ("2026-09-15T12:00:00Z", True)])
def test_unchanged_meeting_preserves_pdf_provenance_and_true_import(tmp_path, monkeypatch, imported_at, new_location):
    from test_information_checks import _record
    from climate_registry import information_checks as checks
    from climate_monitor.meetings import EXTRACTION_FIELDS
    from climate_registry.read_api import RegistryReader
    database = tmp_path/"registry.sqlite3"
    item = {"event_id":"pdf-event-1","occurrence_id":"meeting-1","name":"Climate risk conference",
        "publisher":"Example Institute","organizer":"Example Institute","relevance":"Climate risk and insurance.",
        "kind":"event","event_type":"conference","status":"scheduled","raw_date":"20–23 October 2026",
        "date_precision":"day","start_date":"2026-10-20","end_date":"2026-10-23",
        "raw_time_text":"09:00–10:30","event_timezone":"GMT+8","location":None,
        "summary":"Climate risk conference","page":1,"source_urls":["https://example.org/events"],"source_document_sha256":"a"*64}
    with sqlite3.connect(database) as db:
        apply_migrations(db,target_version=16)
        db.execute("INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at) VALUES(?,?,'report.pdf','application/pdf',0,?,'{}',?)",
            ("a"*64,str(tmp_path/"report.pdf"),"b"*64,imported_at))
        db.execute("INSERT INTO pdf_intake_calendar_items VALUES(?,?,?,1,?,'event',?,'day',?,?,?, ?,NULL,?)",
            (item["occurrence_id"],item["event_id"],"a"*64,item["name"],item["raw_date"],item["start_date"],item["end_date"],item["name"],"c"*64,json.dumps(item)))
    body = "Climate risk conference: Example Institute hosts Climate risk conference on 20–23 October 2026. 09:00–10:30 GMT+8" + (" at Singapore." if new_location else ".")
    def verifier(kind, fields, body):
        candidate = {key: None for key in EXTRACTION_FIELDS}
        candidate.update({key:item[key] for key in ("name","event_type","organizer","status","date_precision","start_date","end_date","raw_time_text")})
        candidate.update(timezone="GMT+8", location="Singapore" if new_location else None, date_evidence=body)
        return {"comparisons":{key:{"status":"supported"} for key in fields},"website_candidate":candidate}
    monkeypatch.setattr(checks,"_now",lambda:"2026-10-06T09:00:00Z")
    result = checks.run_checks(database,kind="meetings",backup_dir=tmp_path/"backups",occurrence_ids={"meeting-1"},
        fetcher=lambda identity,url:_record(identity,url,body),verifier=verifier)
    assert result["status"] == "complete"
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT fields_json,first_ingested_at,substantive_updated_at FROM knowledge_versions WHERE entity_kind='meeting' ORDER BY rowid").fetchall()
    assert rows[-1][1] == (None if imported_at == "unknown" else imported_at)
    fields = json.loads(rows[-1][0])
    assert fields["raw_date"] == item["raw_date"] and fields["source_urls"] == item["source_urls"]
    assert fields["relevance_reason"] == item["relevance"]
    assert rows[-1][2] == ("2026-10-06T09:00:00Z" if new_location else None)
    assert len(rows) == (2 if new_location else 1)
    frozen = report_review.freeze_biweekly(RegistryReader(database,repository_root=tmp_path/"application"),tmp_path/"artifacts",occurrence="2026-10-12")
    assert frozen["status"] == ("pending_review" if new_location else "no_eligible_information")


@pytest.mark.parametrize("gap", ["transient", "staging", "denied"])
def test_t10_successful_scan_recovers_real_article_gap_with_original_contract(tmp_path, monkeypatch, gap):
    from test_issue94_management_console import _definition, _store
    from test_issue112_acquisition import _batch, _item
    from climate_monitor.management import ManagementService
    from climate_monitor.request_budget import RequestBudget, ledger_path
    from climate_registry.acquisition import store_acquisition_batch, unresolved_acquisition_items
    from climate_registry import acquisition_review as review
    from scripts import run_agent_acquisition as runner
    store = _store(tmp_path); store.save(_definition(tmp_path), actor="test")
    launched = []
    service = ManagementService(store=store,runtime_root=tmp_path/"runs",launcher=lambda b:launched.append(b) or 123)
    reservation = reserve_rotation(tmp_path/"rotation","2026-10-06",["wmo"])
    service.start(trigger="scheduled",execution_mode="ingest_only",acquisition_kind="website_rotation",rotation=reservation,
        now=datetime(2026,10,6,10,tzinfo=timezone.utc))
    binding = service.binding(reservation["run_id"]); run = tmp_path/"runs"/binding["run_id"]
    item = _item(discovery_kind="site",discovery_ref="site:article",discovery_search_ref=None,source="wmo",
        selected=False,status="ok" if gap=="staging" else "failed",processing_status="pending" if gap=="staging" else "failed",
        processing_error=None if gap=="staging" else "robots.denied" if gap=="denied" else "network.timeout")
    if gap != "staging":
        item["evidence"].update(failure_reason=item["processing_error"],http_status=None)
        item["evidence"]["attempts"]=[{"engine":"web_http","status":"failed","attempted_at":"2026-09-10T08:00:00Z",
            "error":{"code":item["processing_error"],"message":"Reader refused access" if gap=="denied" else "Timed out"}}]
    candidate = {"source":"wmo","url":item["url"],"title":item["title"],"summary":item["summary"]}
    source = {"source":"wmo","status":"succeeded","coverage_status":"success","candidates":[candidate],"attempts":[],"runtime_seconds":1}
    payload = _batch([item],batch_id=binding["acquisition_batch_id"],policy=binding["date_policy"],report_date=binding["report_date"],
        completed=False,searches=[],search_decision={"status":"no_search","reason":"website-only run"})
    payload["source_outcomes"]=[source]
    assert len(unresolved_acquisition_items(payload)) == 1
    store_acquisition_batch(binding["registry_database"],payload)
    atomic_write_json(run/"attempt-1-acquisition.json",payload)
    atomic_write_json(run/"attempt-1-result.json",{"run_id":binding["run_id"],"attempt":1,"finished_at":"2026-10-06T10:01:00Z",
        "exit_code":0,"retryable":True,"execution_complete":False,"outcome":"acquisition_pending_review"})
    atomic_write_json(run/"runtime.json",{"state":"completed","attempt":1})
    budget = RequestBudget(ledger_path(binding),binding)
    budget.claim("http",item["url"]); budget.finish()
    budget_before = ledger_path(binding).read_bytes()
    root = run/"acquisition-review"; _queue_after_actual_information(run/"binding.json",binding,payload)
    native = tmp_path/"hermes.db"; _native(native,"cron_t10_article_gap")
    at = datetime(2026,10,6,12,10,tzinfo=timezone.utc)
    claim = review.claim_acquisition(root,session_id="cron_t10_article_gap",execution_id="gap",hermes_database=native,reviewer="native",now=at)
    if gap=="denied":
        with pytest.raises(ValueError,match="refusal"):review.recover_acquisition(root,claim["token"],service,now=at)
        assert len(launched)==1 and ledger_path(binding).read_bytes()==budget_before
        return
    recovered = review.recover_acquisition(root,claim["token"],service,now=at)
    assert recovered["run_id"]==binding["run_id"] and recovered["attempt"]==2
    resumed = service.binding(binding["run_id"])
    assert {k:v for k,v in resumed.items() if k not in {"attempt","created_at"}} == {k:v for k,v in binding.items() if k not in {"attempt","created_at"}}
    assert ledger_path(binding).read_bytes()==budget_before
    history = runner._resume_history(run/"attempt-2.json",resumed)
    monkeypatch.setattr(runner,"_controlled_site_context",lambda *_:pytest.fail("successful source scan repeated"))
    context = runner._resume_controlled_site_context(resumed,history)
    assert context["source_results"]==[source] and context["candidates"]==[candidate]
    assert context["attempts"]==[] and context["runtime_seconds"]==0
    assert RequestBudget(ledger_path(resumed),resumed).usage()["fetch_attempts"]==1


@pytest.mark.parametrize("last_day",[12,13])
def test_observer_recent_runs_follow_real_rotation_occurrences(tmp_path,last_day):
    from scripts.export_scheduler_status import business_status
    expected = []
    for day in range(6,last_day+1):
        occurrence = f"2026-10-{day:02d}"
        rotation = reserve_rotation(tmp_path/"rotation",occurrence,["wmo"])
        run = tmp_path/"runs"/rotation["run_id"]
        binding = {"run_id":rotation["run_id"],"acquisition_kind":"website_rotation","source_keys":["wmo"],
            "rotation":rotation,"report_date":occurrence,"created_at":occurrence+"T10:00:00Z"}
        receipt = {"finished_at":occurrence+"T10:01:00Z","outcome":"failed" if day==last_day else "acquisition_pending_review","execution_complete":day!=last_day}
        atomic_write_json(run/"binding.json",binding); atomic_write_json(run/"attempt-1-result.json",receipt)
        atomic_write_json(run/"acquisition-review/state.json",{"packet":{"run_id":rotation["run_id"],"rotation":rotation},
            "packet_sha256":digest(binding),"status":"failed" if day==last_day else "pending_review","item_reviews":{},"history":[]})
        if day>=last_day-2:expected.append(rotation["run_id"])
    status = business_status(acquisition_root=tmp_path/"runs")
    assert [row["run_id"] for row in status["T2"]["runs"]]==expected
    assert status["T2"]["runs"][-1]["outcome"]=="failed"
    assert [row["run_id"] for row in status["T10"]["reviews"]]==expected
    assert status["T10"]["reviews"][-1]["status"]=="failed"


def test_rotation_resume_retries_only_unfinished_sources_under_original_budget(tmp_path,monkeypatch):
    from test_issue94_management_console import _definition
    from climate_monitor.management import build_task_binding
    from climate_monitor.request_budget import RequestBudget, ledger_path
    from scripts import run_agent_acquisition as runner
    definition = _definition(tmp_path)
    definition["parameters"]["source_keys"] = ["wmo","unfccc"]
    binding = build_task_binding(definition,task_version=1,run_id="source-recovery",attempt=1)
    binding.update(activation_policy="acquisition_review",acquisition_kind="website_rotation",rotation=None)
    budget = RequestBudget(ledger_path(binding),binding)
    succeeded = {"source":"wmo","status":"succeeded","coverage_status":"success","candidates":[],"attempts":[{"original":"source evidence"}]}
    failed = {"source":"unfccc","status":"partial","coverage_status":"incomplete","candidates":[],"attempts":[{"new":"source evidence"}]}
    calls = []
    def scan(actual_binding,*,source_keys=None):
        calls.append(source_keys)
        assert actual_binding==binding
        assert RequestBudget(ledger_path(actual_binding),actual_binding).identity==budget.identity
        return {"source_results":[failed],"warnings":["temporary timeout"],"attempts":failed["attempts"],"runtime_seconds":2,"systemic_error":None}
    monkeypatch.setattr(runner,"_controlled_site_context",scan)
    resumed = getattr(runner,"_resume_controlled_site_context",lambda b,h:runner._controlled_site_context(b))
    context = resumed(binding,{"source_results":[succeeded,failed]})
    assert calls==[["unfccc"]]
    assert context["source_results"]==[succeeded,failed]
    assert context["attempts"]==failed["attempts"] and context["runtime_seconds"]==2
    assert context["status"]=="partial"  # Reuse does not promote incomplete sibling coverage.
    rejected = {**succeeded,"status":"failed","coverage_status":"rejected"}
    context = resumed(binding,{"source_results":[rejected,failed]})
    assert calls[-1]==["unfccc"] and context["source_results"][0]==rejected
    assert context["status"]=="partial"



def _native_read(native, session, path, body, call):
    with sqlite3.connect(native) as db:
        db.execute("INSERT INTO messages(session_id,role,tool_calls) VALUES(?,'assistant',?)",(session,json.dumps([
            {"id":call,"function":{"name":"read_file","arguments":json.dumps({"path":str(path.resolve())})}}])))
        db.execute("INSERT INTO messages(session_id,role,tool_call_id,tool_name,content) VALUES(?,'tool',?,'read_file',?)",(session,call,body))


@pytest.mark.parametrize("action",["same_token","next_cron","policy_denied","no_recovery_conclusion"])
def test_submitted_t10_recovery_stays_reachable_without_releasing_policy_guards(tmp_path,action):
    from test_issue94_management_console import _definition,_store
    from test_issue112_acquisition import _batch,_item
    from climate_monitor.management import ManagementService
    from climate_monitor.request_budget import RequestBudget,ledger_path
    from climate_registry.acquisition import store_acquisition_batch
    from climate_registry import acquisition_review as review
    from scripts.review_pipeline import pending
    store=_store(tmp_path);store.save(_definition(tmp_path),actor="test")
    launched=[]
    service=ManagementService(store=store,runtime_root=tmp_path/"runs",launcher=lambda b:launched.append(b) or 123)
    rotation=reserve_rotation(tmp_path/"rotation","2026-10-06",["wmo"])
    service.start(trigger="scheduled",execution_mode="ingest_only",acquisition_kind="website_rotation",rotation=rotation,
        now=datetime(2026,10,6,10,tzinfo=timezone.utc))
    binding=service.binding(rotation["run_id"]);run=tmp_path/"runs"/binding["run_id"]
    source={"source":"wmo","status":"failed","coverage_status":"rejected" if action=="policy_denied" else "incomplete",
        "failure_reason":"robots.denied" if action=="policy_denied" else "network.timeout","attempts":[{"status":"failed"}]}
    payload=_batch([_item(source="wmo",discovery_kind="site",discovery_ref="site:article",discovery_search_ref=None)],
        batch_id=binding["acquisition_batch_id"],report_date=binding["report_date"],policy=binding["date_policy"],completed=False,
        searches=[],search_decision={"status":"no_search","reason":"website only"})
    payload["source_outcomes"]=[source];store_acquisition_batch(binding["registry_database"],payload)
    atomic_write_json(run/"attempt-1-acquisition.json",payload)
    atomic_write_json(run/"attempt-1-result.json",{"run_id":binding["run_id"],"attempt":1,"finished_at":"2026-10-06T10:02:00Z",
        "exit_code":0,"retryable":True,"execution_complete":False,"outcome":"acquisition_pending_review"})
    atomic_write_json(run/"runtime.json",{"state":"completed","attempt":1})
    budget=RequestBudget(ledger_path(binding),binding);budget.claim("http","https://example.org/article");budget.finish()
    original_ledger=ledger_path(binding).read_bytes()
    state=_queue_after_actual_information(run/"binding.json",binding,payload);root=run/"acquisition-review"
    native=tmp_path/"hermes.db";session="cron_t10_submitted";_native(native,session)
    candidate=state["packet"]["candidates"][0];key=candidate["identity"]["acquisition_item_id"]
    _native_read(native,session,root/candidate["body_path"],candidate["identity"]["markdown_content"],"read-pass")
    if candidate.get("registry_snapshot_path"):
        full=Path(candidate["registry_snapshot_path"])
        _native_read(native,session,full,full.read_text(encoding="utf-8"),"read-registry")
    at=datetime(2026,10,6,10,5,tzinfo=timezone.utc)
    claim=review.claim_acquisition(root,session_id=session,execution_id="submit",hermes_database=native,reviewer="native",now=at)
    result=review.review_acquisition(root,claim["token"],{
        "items":{key:{"candidate_sha256":candidate["candidate_sha256"],"status":"pass","reason":"Exact body inspected"}},
        "sources":{"wmo":{"evidence_sha256":digest(state["packet"]["source_outcomes"][0]),
            "status":"restricted" if action=="no_recovery_conclusion" else "recovering","reason":source["failure_reason"]}}},now=at)
    assert json.loads((root/"claim.json").read_text())["released_at"]
    original_pass=result["item_reviews"][key]
    if action=="no_recovery_conclusion":
        assert pending("acquisition",tmp_path/"runs") is None
        with pytest.raises(ValueError,match="claim"):review.recover_acquisition(root,claim["token"],service,now=at)
        assert len(launched)==1 and ledger_path(binding).read_bytes()==original_ledger
        return
    if action=="policy_denied":
        with pytest.raises(ValueError,match="refusal"):review.recover_acquisition(root,claim["token"],service,now=at)
        assert len(launched)==1 and ledger_path(binding).read_bytes()==original_ledger
        return
    if action=="next_cron":
        with sqlite3.connect(native) as db:db.execute("UPDATE sessions SET end_reason='cron_complete' WHERE id=?",(session,))
        assert pending("acquisition",tmp_path/"runs")[0]==root
        session="cron_t10_next_recovery";_native(native,session)
        claim=review.claim_acquisition(root,session_id=session,execution_id="next",hermes_database=native,reviewer="native",now=at+timedelta(minutes=1))
        result=review.review_acquisition(root,claim["token"],{"sources":{"wmo":result["sources"]["wmo"]}},now=at+timedelta(minutes=1))
    recovered=review.recover_acquisition(root,claim["token"],service,now=at+timedelta(minutes=1))
    assert recovered["run_id"]==binding["run_id"] and recovered["attempt"]==2 and len(launched)==2
    assert ledger_path(binding).read_bytes()==original_ledger
    resumed=service.binding(binding["run_id"])
    assert {k:v for k,v in resumed.items() if k not in {"attempt","created_at"}}=={k:v for k,v in binding.items() if k not in {"attempt","created_at"}}
    assert pending("acquisition",tmp_path/"runs") is None  # Acquiring work is not a fresh review.
    # The original runner's next finished attempt creates a new review packet.
    payload["source_outcomes"]=[{**source,"status":"succeeded","coverage_status":"success","failure_reason":None}]
    store_acquisition_batch(binding["registry_database"],payload)
    atomic_write_json(run/"attempt-2-result.json",{"run_id":binding["run_id"],"attempt":2,"finished_at":"2026-10-06T10:10:00Z",
        "exit_code":0,"retryable":False,"execution_complete":True,"outcome":"acquisition_pending_review"})
    atomic_write_json(run/"runtime.json",{"state":"completed","attempt":2})
    queued=_queue_after_actual_information(run/"attempt-2.json",resumed,payload)
    assert queued["item_reviews"][key]==original_pass
    assert queued["packet"]["attempt"]==2 and queued["sources"]=={}
    assert pending("acquisition",tmp_path/"runs")[0]==root


@pytest.mark.parametrize("frozen",[False,True])
@pytest.mark.parametrize("prior_body",[False,True])
def test_new_approved_web_body_enters_biweekly_before_and_after_activation(tmp_path,monkeypatch,frozen,prior_body):
    from test_issue112_acquisition import _database,_batch,_item
    from test_issue181_ingest_only import _ack
    from climate_registry.acquisition import store_acquisition_batch,freeze_acquisition_for_report,PublicationDatePolicy
    from climate_registry import acquisition_review as review,publication
    from climate_registry.web_ingest_pipeline import WebIngestPipeline
    from climate_registry.read_api import RegistryReader
    database=_database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    if prior_body:
        monkeypatch.setattr(review,"now_stamp",lambda:"2026-09-01T12:00:00Z")
        old=_item(body="# Older unreviewed body\n\nHistorical pending source.",discovery_kind="site",discovery_ref="site:old",discovery_search_ref=None)
        old["discovered_at"]=old["evidence"]["fetched_at"]=old["evidence"]["attempts"][0]["attempted_at"]="2026-09-01T12:00:00Z"
        old_payload=_batch([old],batch_id="old-body",report_date="2026-09-01",searches=[],
            policy=PublicationDatePolicy.resolve(None,anchor_date=datetime(2026,9,1).date(),frozen_at="2026-09-01T12:00:00Z").to_dict(),
            search_decision={"status":"no_search","reason":"website only"})
        old_payload["started_at"]=old_payload["completed_at"]="2026-09-01T12:00:00Z"
        store_acquisition_batch(database,old_payload)
    monkeypatch.setattr(review,"now_stamp",lambda:"2026-09-30T12:00:00Z")
    item=_item(body="# Approved climate evidence\n\nOriginal approved body.",
        discovery_kind="site",discovery_ref="site:article",discovery_search_ref=None)
    item["discovered_at"]=item["evidence"]["fetched_at"]=item["evidence"]["attempts"][0]["attempted_at"]="2026-09-30T12:00:00Z"
    payload=_batch([item],batch_id="first-body",report_date="2026-09-30",searches=[],
        policy=PublicationDatePolicy.resolve(None,anchor_date=datetime(2026,9,30).date(),frozen_at="2026-09-30T12:00:00Z").to_dict(),
        search_decision={"status":"no_search","reason":"website only"})
    payload["started_at"]=payload["completed_at"]="2026-09-30T12:00:00Z"
    store_acquisition_batch(database,payload)
    if frozen:
        freeze_acquisition_for_report(database,"first-body",report_date="2026-09-30")
    reader=RegistryReader(database,repository_root=tmp_path/"application")
    generated=datetime(2026,10,12,12,tzinfo=timezone.utc)
    assert report_review.freeze_biweekly(reader,tmp_path/"unapproved-reports",occurrence="2026-10-12",generated_at=generated)["status"]=="no_eligible_information"
    run=tmp_path/"runs/first-body"
    binding={"run_id":"first-body","registry_database":str(database),"acquisition_batch_id":"first-body",
        "attempt":1,"task_version":3,"source_keys":["Example Institute"]}
    atomic_write_json(run/"attempt-1-result.json",{"run_id":"first-body","attempt":1,"finished_at":"2026-09-30T12:05:00Z"})
    state=_queue_after_actual_information(run/"binding.json",binding,payload)
    root=run/"acquisition-review";candidate=state["packet"]["candidates"][0]
    identity=candidate["identity"];native=tmp_path/"hermes.db"
    _native(native,"cron_t10_first_body")
    _native_read(native,"cron_t10_first_body",root/candidate["body_path"],identity["markdown_content"],"body")
    path=Path(candidate["registry_snapshot_path"])
    _native_read(native,"cron_t10_first_body",path,path.read_text(encoding="utf-8"),"snapshot")
    at=datetime(2026,10,10,12,tzinfo=timezone.utc)
    monkeypatch.setattr(review,"now_stamp",lambda:"2026-10-10T12:00:00Z")
    monkeypatch.setattr(publication,"now_stamp",lambda:"2026-10-10T12:00:00Z")
    claim=review.claim_acquisition(root,session_id="cron_t10_first_body",execution_id="first",hermes_database=native,reviewer="native-fixture",now=at)
    review.review_acquisition(root,claim["token"],{"items":{identity["acquisition_item_id"]:{"candidate_sha256":candidate["candidate_sha256"],"status":"pass","reason":"Exact body and full snapshot read"}}},now=at)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT current_content_version_id FROM articles").fetchone()[0] is None
    assert reader.article(identity["article_id"])["published_candidate_sha256"]==candidate["registry_candidate_sha256"]
    def frozen_report(directory):
        state=report_review.freeze_biweekly(reader,directory,occurrence="2026-10-12",generated_at=generated)
        assert state["status"]=="pending_review"
        snapshot=json.loads((directory/"reports/2026-10-12/snapshot.json").read_text())
        assert len(snapshot["articles"])==len(snapshot["material_versions"])==1
        article=snapshot["articles"][0];material=snapshot["material_versions"][0]
        assert article["publication_date"]=="2026-09-01"
        assert article["content_version_id"]==identity["content_version_id"]
        assert "Original approved body" in article["content"]
        assert "Pending replacement" not in article["content"] and "Older unreviewed body" not in article["content"]
        assert article["provenance"]["title"]["candidate_sha256"]==candidate["registry_candidate_sha256"]
        assert material["entity_id"]==identity["article_id"] and material["source_ref"]==identity["acquisition_item_id"]
        assert material["first_ingested_at"]==("2026-09-01T12:00:00Z" if prior_body else "2026-09-30T12:00:00Z")
        assert material["substantive_updated_at"]==("2026-09-30T12:00:00Z" if prior_body else None)
        assert material["original_period_time"]=="2026-09-30T12:00:00Z"
        assert material["material_sha256"]==digest(json.loads(material["fields_json"]))
        assert json.loads(material["evidence_json"])["content_version_id"]==identity["content_version_id"]
        assert json.loads(material["evidence_json"])["content_sha256"]==identity["content_sha256"]
        return state
    reports=tmp_path/"approved-reports";original=frozen_report(reports)
    original_bytes=(reports/"reports/2026-10-12/snapshot.json").read_bytes()
    queue=tmp_path/"queue";queue.mkdir()
    request=review.activate_approved(root,queue_dir=queue,database=database,repository_root=tmp_path/"application")
    assert WebIngestPipeline(queue,database,tmp_path/"runtime",_ack(queue),repository_root=tmp_path/"application").process(request["batch_id"])["chat_ready"]
    frozen_report(tmp_path/"activated-reports")
    monkeypatch.setattr(review,"now_stamp",lambda:"2026-10-11T12:00:00Z")
    update=_item(body="# Pending replacement\n\nUnreviewed body.",discovery_kind="site",discovery_ref="site:updated",discovery_search_ref=None)
    update["discovered_at"]=update["evidence"]["fetched_at"]=update["evidence"]["attempts"][0]["attempted_at"]="2026-10-11T12:00:00Z"
    pending=_batch([update],batch_id="pending-body",report_date="2026-10-11",searches=[],
        policy=PublicationDatePolicy.resolve(None,anchor_date=datetime(2026,10,11).date(),frozen_at="2026-10-11T12:00:00Z").to_dict(),
        search_decision={"status":"no_search","reason":"website only"})
    pending["started_at"]=pending["completed_at"]="2026-10-11T12:00:00Z"
    store_acquisition_batch(database,pending)
    frozen_report(tmp_path/"pending-reports")
    assert report_review.freeze_biweekly(reader,reports,occurrence="2026-11-09",generated_at=datetime(2026,11,9,12,tzinfo=timezone.utc))["status"]=="no_eligible_information"
    publication.set_visibility(database,"article",identity["article_id"],False)
    assert report_review.freeze_biweekly(reader,tmp_path/"hidden-reports",occurrence="2026-10-12",generated_at=generated)["status"]=="no_eligible_information"
    assert report_review.freeze_biweekly(reader,reports,occurrence="2026-10-12",generated_at=generated)==original
    assert (reports/"reports/2026-10-12/snapshot.json").read_bytes()==original_bytes


def _native_meeting_material_fixture(tmp_path,monkeypatch,*,partial=False):
    from test_issue136_meetings import _database,_candidate
    from test_registry_publication import _approve_native_packet
    from climate_monitor import meetings
    from climate_registry import publication
    body="World Climate Summit 2027 meets June 10–12, 2027 in New York. Registration deadline May 1, 2027. Register https://example.com/register. Climate risk agenda. Detailed actuarial climate risk agenda."
    database=_database(tmp_path,[body,body] if partial else [body])
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    clock=["2026-09-30T12:00:00Z"]
    monkeypatch.setattr(meetings,"_now",lambda now=None:clock[0])
    def process(version,reason="Climate risk agenda"):
        def extract(request):
            if partial and request["content_version_id"]=="content-2":raise RuntimeError("temporary extraction failure")
            return {"events":[_candidate(relevance_reason=reason)]}
        return meetings.process_batch(database,"batch",prompt_text="extract actual source "+version,prompt_version=version,provider="test",model="test",extractor=extract)
    def approve(label,at):
        monkeypatch.setattr(publication,"now_stamp",lambda:at)
        _approve_native_packet(database,tmp_path/(label+"-review"),tmp_path/(label+"-native.db"),"cron_"+label)
        with sqlite3.connect(database) as db:
            return db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='meeting'").fetchone()[0]
    return database,clock,process,approve


@pytest.mark.parametrize("partial",[False,True])
@pytest.mark.parametrize("late",[False,True])
def test_approved_native_meeting_material_enters_frozen_ledger_and_does_not_repeat(tmp_path,monkeypatch,partial,late):
    from climate_registry.read_api import RegistryReader
    from climate_registry.publication import set_visibility
    database,clock,process,approve=_native_meeting_material_fixture(tmp_path,monkeypatch,partial=partial)
    assert process("v1")["status"]==("partial" if partial else "succeeded")
    reader=RegistryReader(database,repository_root=tmp_path/"application")
    generated=datetime(2026,10,12,12,tzinfo=timezone.utc)
    assert report_review.freeze_biweekly(reader,tmp_path/"pending",occurrence="2026-10-12",generated_at=generated)["status"]=="no_eligible_information"
    sha=approve("meeting_first","2026-10-20T12:00:00Z" if late else "2026-10-10T12:00:00Z")
    occurrence="2026-10-26" if late else "2026-10-12"
    generated=datetime(2026,10,26 if late else 12,12,tzinfo=timezone.utc)
    artifacts=tmp_path/"artifacts"
    state=report_review.freeze_biweekly(reader,artifacts,occurrence=occurrence,generated_at=generated)
    assert state["status"]=="pending_review"
    path=artifacts/"reports"/occurrence/"snapshot.json";before=path.read_bytes();snapshot=json.loads(before)
    assert len(snapshot["meeting"]["records"])==len(snapshot["material_versions"])==1
    record=snapshot["meeting"]["records"][0];material=snapshot["material_versions"][0]
    assert record["material_versions"]==snapshot["material_versions"]
    assert record["material_provenance"]=={"basis":"approved_public_version","candidate_sha256":sha}
    assert record["start_date"]=="2027-06-10" and record["end_date"]=="2027-06-12"
    assert snapshot["meeting"]["coverage"]["status"]==("partial" if partial else "processed")
    assert snapshot["meeting"]["status"]==("partial" if partial else "included")
    assert material["first_ingested_at"]==material["original_period_time"]=="2026-09-30T12:00:00Z"
    assert material["substantive_updated_at"] is None
    assert material["selection_reason"]==("late_review_carryforward" if late else "first_ingested")
    source=next(source for source in record["sources"] if source["event_source_id"]==material["source_ref"])
    evidence=json.loads(material["evidence_json"])
    assert source["is_current"] and material["entity_id"]==record["event_id"]
    assert evidence["content_version_id"]==source["content_version_id"] and evidence["content_sha256"]==source["content_sha256"]
    assert evidence["source_url"]==source["source_url"] and evidence["event_source_id"]==material["source_ref"]
    assert material["material_sha256"]==digest(json.loads(material["fields_json"]))
    with sqlite3.connect(database) as db:
        assert evidence["event_version_sha256"]==db.execute("SELECT state_sha256 FROM climate_event_versions WHERE event_id=? AND record_version=?",(record["event_id"],evidence["event_version"])).fetchone()[0]
    clock[0]="2026-10-27T12:00:00Z"
    process("same-fields")
    approve("meeting_same","2026-10-28T12:00:00Z")
    assert report_review.freeze_biweekly(reader,artifacts,occurrence="2026-11-09",generated_at=datetime(2026,11,9,12,tzinfo=timezone.utc))["status"]=="no_eligible_information"
    set_visibility(database,"meeting",record["event_id"],False)
    assert report_review.freeze_biweekly(reader,tmp_path/"hidden",occurrence=occurrence,generated_at=generated)["status"]=="no_eligible_information"
    assert report_review.freeze_biweekly(reader,artifacts,occurrence=occurrence)==state and path.read_bytes()==before


def test_native_meeting_substantive_change_waits_for_exact_approval_and_keeps_first_time(tmp_path,monkeypatch):
    from climate_registry.read_api import RegistryReader
    database,clock,process,approve=_native_meeting_material_fixture(tmp_path,monkeypatch)
    process("v1");old_sha=approve("meeting_original","2026-10-10T12:00:00Z")
    reader=RegistryReader(database,repository_root=tmp_path/"application");artifacts=tmp_path/"artifacts"
    report_review.freeze_biweekly(reader,artifacts,occurrence="2026-10-12",generated_at=datetime(2026,10,12,12,tzinfo=timezone.utc))
    old=json.loads((artifacts/"reports/2026-10-12/snapshot.json").read_text())
    clock[0]="2026-10-13T12:00:00Z"
    process("improved","Detailed actuarial climate risk agenda")
    assert reader.meetings(base_date="2026-10-26")["items"][0]["relevance_reason"]=="Climate risk agenda"
    pending=tmp_path/"pending-artifacts"
    atomic_write_json(pending/"reports/2026-10-12/snapshot.json",old)
    assert report_review.freeze_biweekly(reader,pending,occurrence="2026-10-26",generated_at=datetime(2026,10,26,12,tzinfo=timezone.utc))["status"]=="no_eligible_information"
    new_sha=approve("meeting_improved","2026-10-20T12:00:00Z");assert new_sha!=old_sha
    assert report_review.freeze_biweekly(reader,artifacts,occurrence="2026-10-26",generated_at=datetime(2026,10,26,12,tzinfo=timezone.utc))["status"]=="pending_review"
    snapshot=json.loads((artifacts/"reports/2026-10-26/snapshot.json").read_text())
    assert len(snapshot["material_versions"])==1
    material=snapshot["material_versions"][0];record=snapshot["meeting"]["records"][0]
    assert material["first_ingested_at"]=="2026-09-30T12:00:00Z" and material["substantive_updated_at"]==material["original_period_time"]=="2026-10-13T12:00:00Z"
    assert material["selection_reason"]=="substantive_update"
    assert record["relevance_reason"]=="Detailed actuarial climate risk agenda" and record["material_provenance"]["candidate_sha256"]==new_sha
    assert report_review.freeze_biweekly(reader,artifacts,occurrence="2026-11-09",generated_at=datetime(2026,11,9,12,tzinfo=timezone.utc))["status"]=="no_eligible_information"


@pytest.mark.parametrize("known",[False,True])
def test_legacy_native_meeting_original_time_is_read_only_and_survives_new_improvement(tmp_path,monkeypatch,known):
    from test_issue136_meetings import _candidate
    from test_registry_publication import _approve_native_packet
    from climate_monitor import meetings
    from climate_registry import publication
    from climate_registry.read_api import RegistryReader
    original,clock,process,_=_native_meeting_material_fixture(tmp_path,monkeypatch)
    process("original")
    # Recreate a real pre-ledger archive, not a legacy business-writer bypass.
    legacy=tmp_path/"legacy.sqlite3"
    with sqlite3.connect(original) as source,sqlite3.connect(legacy) as target:
        apply_migrations(target,target_version=20)
        target.execute("PRAGMA defer_foreign_keys=ON")
        pointers=source.execute("SELECT current_version_id,current_content_version_id,article_id FROM articles").fetchall()
        tables=[row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'") if row[0] not in {"schema_migrations","knowledge_versions"}]
        for table in tables:
            columns=[row[1] for row in target.execute(f"PRAGMA table_info({table})")]
            rows=source.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
            if table=="articles":
                rows=[tuple(None if column in {"current_version_id","current_content_version_id"} else value for column,value in zip(columns,row)) for row in rows]
            if not known and table in {"climate_events","climate_event_sources","climate_event_versions"}:
                rows=[tuple("2026-09-30" if column in {"created_at","updated_at","observed_at","recorded_at"} else value for column,value in zip(columns,row)) for row in rows]
            target.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",rows)
        target.executemany("UPDATE articles SET current_version_id=?,current_content_version_id=? WHERE article_id=?",pointers)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(legacy))
    publication.migrate_publication(legacy,tmp_path/"backups",apply=True)
    reader=RegistryReader(legacy,repository_root=tmp_path/"application")
    before=legacy.read_bytes()
    state=report_review.freeze_biweekly(reader,tmp_path/"legacy-reports",occurrence="2026-10-12",generated_at=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert state["status"]==("pending_review" if known else "no_eligible_information")
    assert legacy.read_bytes()==before
    clock[0]="2026-10-13T12:00:00Z"
    result=meetings.process_batch(legacy,"batch",prompt_text="improve supported facts",prompt_version="improved",provider="test",model="test",
        extractor=lambda request:{"events":[_candidate(relevance_reason="Detailed actuarial climate risk agenda")]})
    assert result["status"]=="succeeded"
    monkeypatch.setattr(publication,"now_stamp",lambda:"2026-10-20T12:00:00Z")
    _approve_native_packet(legacy,tmp_path/"improved-review",tmp_path/"improved-native.db","cron_legacy_meeting_improved")
    assert report_review.freeze_biweekly(reader,tmp_path/"improved-reports",occurrence="2026-10-26",generated_at=datetime(2026,10,26,12,tzinfo=timezone.utc))["status"]=="pending_review"
    snapshot=json.loads((tmp_path/"improved-reports/reports/2026-10-26/snapshot.json").read_text())
    assert len(snapshot["material_versions"])==1
    material=snapshot["material_versions"][0]
    assert material["first_ingested_at"]==("2026-09-30T12:00:00Z" if known else None)
    assert material["substantive_updated_at"]==material["original_period_time"]=="2026-10-13T12:00:00Z"
    assert material["time_basis"]==("historical_native_event_created_at" if known else "legacy_time_unknown")


def test_approved_but_delayed_writer_activation_carries_exact_unfrozen_material(tmp_path,monkeypatch):
    from pypdf import PdfReader
    from test_issue112_acquisition import _database,_batch,_item
    from test_issue181_ingest_only import _ack
    from climate_registry.acquisition import store_acquisition_batch,PublicationDatePolicy
    from climate_registry import acquisition_review as review
    from climate_registry.web_ingest_pipeline import WebIngestPipeline
    from climate_registry.pdf_pipeline import load_active_projection,load_projection_manifest
    from climate_registry.read_api import RegistryReader
    writer_root=tmp_path/"writer";writer_root.mkdir();database=_database(writer_root)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    monkeypatch.setattr(review,"now_stamp",lambda:"2026-09-30T12:00:00Z")
    payload=_batch([_item(source="wmo",discovery_kind="site",discovery_ref="site:article",discovery_search_ref=None)],
        batch_id="delayed",report_date="2026-09-30",policy=PublicationDatePolicy.resolve(None,anchor_date=datetime(2026,9,30).date(),frozen_at="2026-09-30T12:00:00Z").to_dict(),searches=[],search_decision={"status":"no_search","reason":"website only"})
    store_acquisition_batch(database,payload)
    run=tmp_path/"runs/delayed";binding={"run_id":"delayed","registry_database":str(database),"acquisition_batch_id":"delayed",
        "attempt":1,"task_version":3,"source_keys":["wmo"]}
    atomic_write_json(run/"attempt-1-result.json",{"run_id":"delayed","attempt":1,"finished_at":"2026-09-30T12:05:00Z"})
    state=_queue_after_actual_information(run/"binding.json",binding,payload);root=run/"acquisition-review"
    candidate=state["packet"]["candidates"][0];key=candidate["identity"]["acquisition_item_id"]
    native=tmp_path/"hermes.db";_native(native,"cron_t10_before_cutoff")
    _native_read(native,"cron_t10_before_cutoff",root/candidate["body_path"],candidate["identity"]["markdown_content"],"approved-body")
    if candidate.get("registry_snapshot_path"):
        path=Path(candidate["registry_snapshot_path"])
        _native_read(native,"cron_t10_before_cutoff",path,path.read_text(encoding="utf-8"),"approved-registry")
    public=tmp_path/"public.sqlite3"
    # Retain the historical read-only activation contract. Current v21 reports
    # read the canonical approved database directly, regardless of writer reload.
    with sqlite3.connect(public) as db:
        apply_migrations(db,target_version=20)
        assert validate_registry_contract(db)==20
    with sqlite3.connect(database) as db:
        assert validate_registry_contract(db)==22
    public_before=public.read_bytes()
    reader=RegistryReader(public,repository_root=tmp_path/"application");artifacts=tmp_path/"artifacts"
    inactive={"web_items":[],"pdf_occurrence_ids":[],"pdf_calendar_occurrence_ids":[]}
    # Pending/unapproved material does not enter a report.
    assert report_review.freeze_biweekly(reader,tmp_path/"unapproved-artifacts",occurrence="2026-10-12",
        web_reader=RegistryReader(database,repository_root=tmp_path/"application"),manifest=inactive,
        generated_at=datetime(2026,10,12,12,tzinfo=timezone.utc))["status"]=="no_eligible_information"
    at=datetime(2026,10,10,12,tzinfo=timezone.utc)
    monkeypatch.setattr(review,"now_stamp",lambda:"2026-10-10T12:00:00Z")
    claim=review.claim_acquisition(root,session_id="cron_t10_before_cutoff",execution_id="early",hermes_database=native,reviewer="native",now=at)
    approved=review.review_acquisition(root,claim["token"],{"items":{key:{"candidate_sha256":candidate["candidate_sha256"],"status":"pass","reason":"Full candidate verified"}}},now=at)
    canonical=RegistryReader(database,repository_root=tmp_path/"application")
    assert canonical.article(candidate["identity"]["article_id"])["published_candidate_sha256"]==candidate["registry_candidate_sha256"]
    queue=tmp_path/"queue";queue.mkdir()
    request=review.activate_approved(root,queue_dir=queue,database=database,repository_root=tmp_path/"application")
    runtime=tmp_path/"runtime"
    monkeypatch.setattr("climate_registry.web_ingest_pipeline._now",lambda:"2026-10-11T12:00:00Z")
    fail=WebIngestPipeline(queue,database,runtime,
        lambda _:(_ for _ in ()).throw(RuntimeError("reload unavailable")),repository_root=tmp_path/"application").process(request["batch_id"])
    assert fail["stage"]=="failed" and fail["indexed"] and not fail["chat_ready"]
    assert load_active_projection(runtime,queue/"active.json")[0] is None
    old=report_review.freeze_biweekly(reader,artifacts,occurrence="2026-10-12",manifest=inactive,
        generated_at=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert old["status"]=="no_eligible_information"
    old_snapshot=(artifacts/"reports/2026-10-12/snapshot.json").read_bytes()
    retried=review.activate_approved(root,queue_dir=queue,database=database,repository_root=tmp_path/"application")
    assert retried["batch_id"]==request["batch_id"] and retried["registry_sha256"]==request["registry_sha256"]
    monkeypatch.setattr("climate_registry.web_ingest_pipeline._now",lambda:"2026-10-20T12:00:00Z")
    ready=WebIngestPipeline(queue,database,runtime,_ack(queue),repository_root=tmp_path/"application").process(request["batch_id"])
    assert ready["chat_ready"]
    generation,metadata=load_active_projection(runtime,queue/"active.json");manifest=load_projection_manifest(generation,metadata)
    active_reader=RegistryReader(Path(metadata["web_registry_snapshot"]),repository_root=tmp_path/"application")
    assert metadata["activated_at"]=="2026-10-20T12:00:00Z"
    active=manifest["web_items"][0]
    assert active["review"]["approved_at"]==approved["item_reviews"][key]["approved_at"]
    assert active["review"]["raw_candidate_sha256"]==digest(candidate["identity"])
    state=report_review.freeze_biweekly(reader,artifacts,occurrence="2026-10-26",web_reader=active_reader,manifest=manifest,
        generated_at=datetime(2026,10,26,12,tzinfo=timezone.utc))
    assert state["status"]=="pending_review"
    snapshot=json.loads((artifacts/"reports/2026-10-26/snapshot.json").read_text())
    assert len(snapshot["material_versions"])==1
    material=snapshot["material_versions"][0]
    assert material["selection_reason"]=="delayed_activation_carryforward"
    rendered=json.loads((artifacts/"reports/2026-10-26/revisions/0001/report-source.json").read_text())
    assert any("after delayed activation" in paragraph for paragraph in rendered["executive_summary"])
    pdf_text=" ".join(page.extract_text() or "" for page in PdfReader(artifacts/"reports/2026-10-26/revisions/0001/report.pdf").pages)
    assert "after delayed activation" in " ".join(pdf_text.split())
    assert material["first_ingested_at"]==material["original_period_time"]=="2026-09-30T12:00:00Z"
    assert material["substantive_updated_at"] is None and material["intended_report_date"]=="2026-10-12"
    assert material["approved_at"]=="2026-10-10T12:00:00+00:00"
    assert json.loads(material["evidence_json"])["content_version_id"]==active["content_version_id"]
    assert snapshot["articles"][0]["publication_date"]=="2026-09-01"
    assert report_review.freeze_biweekly(reader,artifacts,occurrence="2026-10-12",web_reader=active_reader,manifest=manifest)==old
    assert (artifacts/"reports/2026-10-12/snapshot.json").read_bytes()==old_snapshot
    # Already frozen versions do not appear in another period even before T5 PASS.
    assert report_review.freeze_biweekly(reader,artifacts,occurrence="2026-11-09",web_reader=active_reader,manifest=manifest,generated_at=datetime(2026,11,9,12,tzinfo=timezone.utc))["status"]=="no_eligible_information"
    _native(native,"cron_t5_delayed",state["packet"])
    at=datetime(2026,10,26,12,10,tzinfo=timezone.utc)
    claim=report_review.claim_report(artifacts,"2026-10-26",session_id="cron_t5_delayed",execution_id="delayed",hermes_database=native,reviewer="native",now=at)
    report_review.submit_report_review(artifacts,"2026-10-26",claim["token"],status="pass",reason="Exact active version reviewed",now=at)
    # A metadata/check retry retains exactly the same version and truthful time.
    with sqlite3.connect(database) as db:
        prior=db.execute("SELECT count(*) FROM knowledge_versions").fetchone()[0]
        record_knowledge(db,kind=material["entity_kind"],entity_id=material["entity_id"],source_kind="site",source_ref=key,
            fields=json.loads(material["fields_json"]),evidence={"retry":"check only"},recorded_at="2026-10-30T12:00:00Z")
        assert db.execute("SELECT count(*) FROM knowledge_versions").fetchone()[0]==prior
    assert report_review.freeze_biweekly(reader,artifacts,occurrence="2026-11-23",web_reader=active_reader,manifest=manifest,generated_at=datetime(2026,11,23,12,tzinfo=timezone.utc))["status"]=="no_eligible_information"
    assert public.read_bytes()==public_before


@pytest.mark.parametrize("claim_status", ["owned", "released", "expired"])
def test_public_claim_projection_and_managed_receipts(tmp_path, monkeypatch, claim_status):
    import api_server
    from fastapi.testclient import TestClient
    from climate_registry.acquisition_review import claim_review
    from scripts.export_scheduler_status import business_status, project_independent
    now = datetime.now(timezone.utc).replace(microsecond=0)
    native = tmp_path / "hermes.db"
    first, second = "cron_t10_public_first", "cron_t10_public_second"
    _native(native, first); _native(native, second)
    with sqlite3.connect(native) as db:
        db.execute("UPDATE sessions SET started_at=?", ((now - timedelta(hours=3)).isoformat(),))
    runs = tmp_path / "runs"
    root = runs / "rotation-public" / "acquisition-review"
    packet = {"run_id": "rotation-public", "batch_id": "batch", "source_keys": ["wmo"]}
    atomic_write_json(root / "state.json", {"packet": packet, "packet_sha256": digest(packet), "status": "reviewing",
        "item_reviews": {"item": {"status": "pass"}}, "sources": {"wmo": {"status": "recovering"}}, "proposals": []})
    old = claim_review(root, packet, session_id=first, execution_id="first", hermes_database=native,
        reviewer="native-reviewer", now=now - timedelta(hours=3))
    with sqlite3.connect(native) as db:
        db.execute("UPDATE sessions SET end_reason='cron_error' WHERE id=?", (first,))
    at = now - timedelta(hours=2) if claim_status == "expired" else now
    current = claim_review(root, packet, session_id=second, execution_id="second", hermes_database=native,
        reviewer="native-reviewer", now=at)
    if claim_status == "released":
        current["released_at"] = now.isoformat()
        atomic_write_json(root / "claim.json", current)
    claim_bytes = (root / "claim.json").read_bytes()
    history_bytes = next((root / "claim-history").glob("*.json")).read_bytes()
    business = business_status(acquisition_root=runs)
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE executions(job_id TEXT,status TEXT,claimed_at TEXT,started_at TEXT,finished_at TEXT)")
        ids = {role: "test-" + role for role in schedule.PIPELINE_SLOTS}
        snapshot = project_independent(db, ids, now=now, business=business,
            definitions=[{"id": value, "enabled": True, "no_agent": role not in {"T5", "T10"}, "schedule": "fixture"}
                for role, value in ids.items()])
    status_root = tmp_path / "status"
    atomic_write_json(status_root / "scheduler-status.json", snapshot)
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(status_root))
    client = TestClient(api_server.app)
    public = client.get("/api/job-status")
    assert public.status_code == 200
    for private in (old["token"], current["token"], first, second, str(native.resolve()), "native-reviewer"):
        assert private not in public.text
    review = public.json()["business"]["T10"]["reviews"][0]
    assert review["status"] == "reviewing" and review["source_statuses"] == {"wmo": "recovering"}
    assert review["claim"]["state"] == claim_status
    assert review["claim"]["created_at"] == current["created_at"] and review["claim"]["deadline"] == current["deadline"]
    assert review["claim_failures"][0]["status"] == "review_failed"
    assert review["claim_failures"][0]["end_reason"] == "cron_error"
    assert review["claim_failures"][0]["state"] == "failed"
    # A persisted observer snapshot from before this repair must use the same
    # public projection; reading it does not alter the original receipt or file.
    review["claim"] = json.loads(claim_bytes)
    review["claim_failures"] = [json.loads(history_bytes)]
    snapshot["business"]["T10"]["reviews"][0] = review
    atomic_write_json(status_root / "scheduler-status.json", snapshot)
    legacy_bytes = (status_root / "scheduler-status.json").read_bytes()
    legacy = client.get("/api/job-status")
    assert legacy.status_code == 200
    for private in (old["token"], current["token"], first, second, str(native.resolve()), "native-reviewer"):
        assert private not in legacy.text
    assert legacy.json()["business"]["T10"]["reviews"][0]["claim"]["state"] == claim_status
    assert (status_root / "scheduler-status.json").read_bytes() == legacy_bytes
    monkeypatch.setattr(api_server, "REPORT_REVIEW_DIR", None)
    monkeypatch.setattr(api_server, "_management_service", lambda: SimpleNamespace(runtime_root=runs))
    api_server.app.dependency_overrides[api_server.current_console_user] = lambda: SimpleNamespace()
    try:
        managed = client.get("/api/manage/pipeline")
    finally:
        api_server.app.dependency_overrides.pop(api_server.current_console_user, None)
    assert managed.status_code == 200
    details = managed.json()["business"]["T10"]["reviews"][0]
    for key, value in current.items():
        assert details["claim"][key] == value
    assert details["claim_failures"][0]["token"] == old["token"]
    assert (root / "claim.json").read_bytes() == claim_bytes
    assert next((root / "claim-history").glob("*.json")).read_bytes() == history_bytes


@pytest.mark.parametrize("selected", ["initial", "pipeline", "runs", "meetings", "configuration", "pdf-import"])
def test_management_pipeline_tab_has_exclusive_visibility(selected):
    import subprocess
    root = Path(__file__).resolve().parents[1]
    program = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const attribute = (attributes, name) => attributes.match(new RegExp(`(?:^|\\s)${name}="([^"]*)"`))?.[1];
const sectionNodes = [...input.html.matchAll(/<section\b([^>]*)>/g)].map(match => {
    const attributes = match[1];
    return {id: attribute(attributes, 'id'), role: attribute(attributes, 'role'), hidden: /(?:^|\s)hidden(?:\s|$)/.test(attributes)};
});
const sections = new Map(sectionNodes.map(section => [section.id, section]));
const panels = sectionNodes.filter(section => section.role === 'tabpanel');
const buttons = [...input.html.matchAll(/<button\b([^>]*)>/g)].map(match => {
    const attributes = match[1];
    const values = new Map(['id', 'role', 'data-tab', 'aria-controls', 'aria-selected'].map(name => [name, attribute(attributes, name)]));
    const classes = new Set((attribute(attributes, 'class') || '').split(/\s+/).filter(Boolean));
    return {
        dataset: {tab: values.get('data-tab')},
        classList: {toggle(name, force) { if (force) classes.add(name); else classes.delete(name); }},
        setAttribute(name, value) { values.set(name, value); },
        getAttribute(name) { return values.get(name); },
        focus() {}, onclick: null
    };
}).filter(button => button.dataset.tab);
const nodes = new Map(), callbacks = [];
const document = {querySelector(selector) { const id = selector.slice(1); if (sections.has(id)) return sections.get(id); if (!nodes.has(selector)) nodes.set(selector, {textContent: ''}); return nodes.get(selector); }, querySelectorAll(selector) { if (selector === '[role="tab"][data-tab]') return buttons; if (selector === '[role="tabpanel"]') return panels; if (selector === '.run') return []; throw Error(selector); }, addEventListener(event, callback) { if (event === 'DOMContentLoaded') callbacks.push(callback); }};
vm.runInNewContext(input.javascript, {document, window: {addEventListener() {}}, history: {pushState() {}}, Intl, Date, structuredClone, URLSearchParams, FormData: class { *[Symbol.iterator]() {} }, fetch: async () => ({status: 503, ok: false, json: async () => ({detail: 'offline test'})}), location: {pathname: '/manage', search: ''}});
callbacks.forEach(callback => callback());
const observe = () => ({visiblePanels: panels.filter(panel => !panel.hidden).map(panel => panel.id), selectedTabs: buttons.filter(button => button.getAttribute('aria-selected') === 'true').map(button => button.dataset.tab)});
const observations = {initial: observe()};
for (const id of ['pipeline', 'runs', 'meetings', 'configuration', 'pdf-import']) { buttons.find(b => b.dataset.tab === id).onclick(); observations[id] = observe(); }
console.log(JSON.stringify(observations));
"""
    result = subprocess.run(["node", "-e", program], input=json.dumps({
        "html": (root / "management_ui/index.html").read_text(),
        "javascript": (root / "management_ui/manage.js").read_text()}), text=True, capture_output=True, check=True)
    observations = json.loads(result.stdout)[selected]
    expected_tab = "configuration" if selected == "initial" else selected
    expected_panel = "pdf-import-panel" if selected == "pdf-import" else expected_tab
    assert observations["visiblePanels"] == [expected_panel]
    assert observations["selectedTabs"] == [expected_tab]
