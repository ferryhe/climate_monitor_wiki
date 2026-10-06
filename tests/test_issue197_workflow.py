"""Direct regressions for the independent acquisition/review/report contracts."""
import hashlib
import json
import sqlite3
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


def test_transaction_times_ignore_retries_and_check_metadata():
    db = sqlite3.connect(":memory:")
    apply_migrations(db)
    assert validate_registry_contract(db) == 19
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
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        record_knowledge(db, kind="article", entity_id="article-a", source_kind="site", source_ref="a-1",
            fields={"title": "old original", "publication_date": "2020-01-01"}, evidence={"content_version_id": "content-article-a"},
            recorded_at="2026-09-30T12:00:00Z")
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
    database = _database(tmp_path)
    identity = next(_manifest_item(i) for i in _batch_items(database, "batch") if i["acquisition_item_id"] == "a-1")
    _pending_version(database)
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    public_path = tmp_path / "public"
    public_path.mkdir()
    public = RegistryReader(_database(public_path), repository_root=tmp_path / "application")
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


def test_t10_partial_approval_correction_and_writer_request(tmp_path):
    from test_issue113_range_reports import _database
    from climate_registry import acquisition_review as review
    from climate_registry.web_ingest_pipeline import read_web_activation_request
    database = _database(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    binding = {"registry_database": str(database), "run_id": "run", "acquisition_batch_id": "batch",
        "attempt": 1, "task_version": 3, "source_keys": ["example"]}
    atomic_write_json(run / "attempt-1-result.json", {"run_id": "run", "attempt": 1, "finished_at": "2026-10-06T12:00:00Z"})
    state = review.queue_acquisition_review(run / "binding.json", binding, {"source_outcomes": []})
    native = tmp_path / "hermes.db"
    _native(native, "cron_t10_first")
    with sqlite3.connect(native) as db:
        for candidate in state["packet"]["candidates"]:
            key = candidate["identity"]["acquisition_item_id"]
            db.execute("INSERT INTO messages(session_id,role,tool_calls) VALUES('cron_t10_first','assistant',?)", (json.dumps([{
                "id": key, "function": {"name": "read_file", "arguments": json.dumps({"path": str((run / "acquisition-review" / candidate["body_path"]).resolve())})}}]),))
            db.execute("INSERT INTO messages(session_id,role,tool_call_id,tool_name,content) VALUES('cron_t10_first','tool',?,'read_file',?)", (key, candidate["identity"]["markdown_content"]))
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
    failed = WebIngestPipeline(queue, Path(request["registry_snapshot_path"]), runtime,
        lambda _: (_ for _ in ()).throw(RuntimeError("reload unavailable")), repository_root=tmp_path / "application").process(request["batch_id"])
    assert failed["stage"] == "failed" and failed["indexed"]
    # Preserve the same immutable request and retry activation without reacquisition.
    retried = review.activate_approved(run / "acquisition-review", queue_dir=queue, database=database, repository_root=tmp_path / "application")
    assert retried["registry_sha256"] == request["registry_sha256"]
    ready = WebIngestPipeline(queue, Path(request["registry_snapshot_path"]), runtime, _ack(queue),
        repository_root=tmp_path / "application").process(request["batch_id"])
    assert ready["chat_ready"], ready["error"]
    revised = review.correct_candidate(run / "acquisition-review", "a-1", {"summary": "Derived correction"}, reason="Actual evidence correction")
    assert set(revised["item_reviews"]) == {"b-1"}
    request2 = review.activate_approved(run / "acquisition-review", queue_dir=queue, database=database, repository_root=tmp_path / "application")
    assert {i["acquisition_item_id"] for i in request2["web_items"]} == {"b-1"}
    assert request2["batch_id"] != request["batch_id"]
    ready = WebIngestPipeline(queue, Path(request2["registry_snapshot_path"]), runtime, _ack(queue),
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
    newer = report_review.freeze_biweekly(reader, root, occurrence="2026-10-26")
    assert newer["status"] == "pending_review"
    snapshot = json.loads((root / "reports" / "2026-10-26" / "snapshot.json").read_text())
    assert any(v["selection_reason"] == "substantive_update" for v in snapshot["material_versions"])


def test_late_review_preserves_real_ingestion_and_original_publication(tmp_path):
    from test_issue113_range_reports import _database
    database = _database(tmp_path)
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
    database = _database(tmp_path)
    for entity, ref, at in (("article-a", "a-1", "2026-10-26T04:00:00Z"), ("article-b", "b-1", "2026-11-09T05:00:00Z")):
        with sqlite3.connect(database) as db:
            record_knowledge(db, kind="article", entity_id=entity, source_kind="site", source_ref=ref,
                fields={"summary": entity}, evidence={}, recorded_at=at)
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


def test_t10_recovery_reuses_binding_and_refusal_cannot_create_a_new_run(tmp_path):
    from test_issue113_range_reports import _database
    from climate_registry import acquisition_review as review
    database = _database(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    binding = {"registry_database": str(database), "run_id": "run", "acquisition_batch_id": "batch",
        "attempt": 1, "task_version": 3, "source_keys": ["example"], "budgets": {"runtime_seconds": 4500}}
    atomic_write_json(run / "attempt-1-result.json", {"run_id": "run", "attempt": 1, "finished_at": "2026-10-06T12:00:00Z"})
    root = run / "acquisition-review"
    native = tmp_path / "hermes.db"
    _native(native, "cron_t10_recovery")
    at = datetime(2026, 10, 6, 12, 10, tzinfo=timezone.utc)
    state = review.queue_acquisition_review(run / "binding.json", binding, {"source_outcomes": [
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
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE knowledge_versions")
        db.execute("DELETE FROM schema_migrations WHERE version=19")
        db.execute("PRAGMA user_version=18")
        db.execute("UPDATE pdf_intake_articles SET imported_at='unknown' WHERE article_id='pdf-month'")
        db.commit()
        assert apply_migrations(db) == [19]
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
    store_acquisition_batch(database, _batch([_item()], batch_id="legacy"))
    with sqlite3.connect(database) as db:
        assert apply_migrations(db) == [19]
    store_acquisition_batch(database, _batch([_item()], batch_id="unchanged"))
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT DISTINCT first_ingested_at,substantive_updated_at FROM knowledge_versions").fetchall() == [(None, None)]
    store_acquisition_batch(database, _batch([_item(body="# Full article\n\nSubstantive new findings.")], batch_id="changed"))
    with sqlite3.connect(database) as db:
        first, updated = db.execute("SELECT first_ingested_at,substantive_updated_at FROM knowledge_versions ORDER BY rowid DESC LIMIT 1").fetchone()
        assert first is None and datetime.fromisoformat(updated.replace("Z", "+00:00")).tzinfo is not None


def test_information_check_prepares_v18_with_exact_backup_before_v19(tmp_path):
    from climate_registry.information_checks import prepare_database
    database = tmp_path / "registry.sqlite3"
    with sqlite3.connect(database) as db:
        apply_migrations(db, target_version=18)
    backups = tmp_path / "backups"
    prepare_database(database, backups)
    with sqlite3.connect(database) as db:
        assert validate_registry_contract(db) == 19
        assert db.execute("SELECT count(*) FROM knowledge_versions").fetchone()[0] == 0
    saved = list(backups.glob("*.bak"))
    assert len(saved) == 1
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


def test_verified_pdf_material_is_rendered_and_only_current_version_archived(tmp_path, monkeypatch):
    from test_issue113_range_reports import _database
    from climate_registry.read_api import RegistryReader
    from climate_registry import information_checks
    database = _database(tmp_path)
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
    reader = RegistryReader(database,repository_root=tmp_path/"application")
    summary = next(o for o in reader.pdf_article("pdf-a")["occurrences"] if o["occurrence_id"] == "pdf-check-a")["verified_information"]["summary"]
    assert "Flood insurance losses" in summary
    with sqlite3.connect(database) as db:
        material = dict(zip([d[0] for d in db.execute("SELECT * FROM knowledge_versions LIMIT 0").description],
            db.execute("SELECT * FROM knowledge_versions WHERE source_ref='pdf-check-a' ORDER BY rowid DESC LIMIT 1").fetchone()))
        baseline_hash = db.execute("SELECT material_sha256 FROM knowledge_versions WHERE source_ref='pdf-check-a' ORDER BY rowid LIMIT 1").fetchone()[0]
    state = report_review.freeze_biweekly(reader,tmp_path/"artifacts",occurrence="2026-10-12")
    frozen = tmp_path/"artifacts/reports/2026-10-12"
    snapshot = json.loads((frozen/"snapshot.json").read_text())
    article = next(a for a in snapshot["articles"] if a["article_id"] == "article-a")
    assert summary in article["summary"]
    assert "Summary for Registry-only" not in article["summary"]
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
    monkeypatch.setattr(pdf_pipeline,"PdfIntakePipeline",lambda *args:SimpleNamespace(refresh_checks=lambda run:{"status":"chat_ready"}))
    real_main = check_information.main
    def local_main(argv, **kwargs):
        argv = [str(tmp_path / Path(a).name) if a.startswith("/pipeline/") else a for a in argv]
        return real_main(argv, **kwargs)
    monkeypatch.setattr(check_information,"main",local_main)
    for name in ("CLIMATE_REGISTRY_WRITER_DB","CLIMATE_REGISTRY_BACKUP_DIR","CLIMATE_PDF_INTAKE_QUEUE_DIR",
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
    import test_issue113_range_reports as ranges
    from climate_registry.read_api import RegistryReader
    monkeypatch.setattr(ranges,"NOW","2026-10-11T12:00:00Z")
    writer = ranges._database(tmp_path,fetched_at="2026-10-11T12:00:00Z")
    with sqlite3.connect(writer) as db:
        for stamp, summary, kind in (("2026-10-11T12:00:00Z","Original PDF summary","pdf"),
            ("2026-10-12T09:00:00Z","Current verified flood losses and capital assessment.","information_check")):
            record_knowledge(db,kind="article",entity_id="pdf-a",source_kind=kind,source_ref="pdf-occ-a",
                fields={"anchor_text":"PDF A","summary":summary,"publication_date":"2026-09-20"},
                evidence={"supported_summary":summary},recorded_at=stamp)
    public = tmp_path/"public.sqlite3"
    with sqlite3.connect(public) as db:apply_migrations(db)
    root = tmp_path/"artifacts"
    kwargs = dict(pdf_reader=RegistryReader(writer,repository_root=tmp_path/"application"),
        manifest={"web_items":[],"pdf_occurrence_ids":["pdf-occ-a"],"pdf_calendar_occurrence_ids":[]})
    reader = RegistryReader(public,repository_root=tmp_path/"application")
    state = report_review.freeze_biweekly(reader,root,occurrence="2026-10-12",**kwargs)
    assert state["status"] == "pending_review"
    frozen = root/"reports/2026-10-12"
    snapshot = json.loads((frozen/"snapshot.json").read_text())
    assert len(snapshot["material_versions"]) == 1
    material = snapshot["material_versions"][0]
    assert material["selection_reason"] == "first_ingested"
    assert material["original_period_time"] == "2026-10-11T12:00:00Z"
    assert material["first_ingested_at"] == "2026-10-11T12:00:00Z"
    assert material["substantive_updated_at"] == "2026-10-12T09:00:00Z"
    assert "Current verified flood losses" in snapshot["articles"][0]["summary"]
    assert snapshot["articles"][0]["publication_date"] == "2026-09-20"
    native = tmp_path/"hermes.db";_native(native,"cron_t5_current",state["packet"])
    at = datetime(2026,10,12,12,10,tzinfo=timezone.utc)
    claim = report_review.claim_report(root,"2026-10-12",session_id="cron_t5_current",execution_id="current",
        hermes_database=native,reviewer="native-reviewer",now=at)
    report_review.submit_report_review(root,"2026-10-12",claim["token"],status="pass",reason="Current version actually inspected",now=at)
    assert report_review.freeze_biweekly(reader,root,occurrence="2026-10-26",**kwargs)["status"] == "no_eligible_information"


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
    root = run/"acquisition-review"; review.queue_acquisition_review(run/"binding.json",binding,payload)
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
    state=review.queue_acquisition_review(run/"binding.json",binding,payload);root=run/"acquisition-review"
    native=tmp_path/"hermes.db";session="cron_t10_submitted";_native(native,session)
    candidate=state["packet"]["candidates"][0];key=candidate["identity"]["acquisition_item_id"]
    _native_read(native,session,root/candidate["body_path"],candidate["identity"]["markdown_content"],"read-pass")
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
    queued=review.queue_acquisition_review(run/"attempt-2.json",resumed,payload)
    assert queued["item_reviews"][key]==original_pass
    assert queued["packet"]["attempt"]==2 and queued["sources"]=={}
    assert pending("acquisition",tmp_path/"runs")[0]==root


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
    monkeypatch.setattr(review,"now_stamp",lambda:"2026-09-30T12:00:00Z")
    payload=_batch([_item(source="wmo",discovery_kind="site",discovery_ref="site:article",discovery_search_ref=None)],
        batch_id="delayed",report_date="2026-09-30",policy=PublicationDatePolicy.resolve(None,anchor_date=datetime(2026,9,30).date(),frozen_at="2026-09-30T12:00:00Z").to_dict(),searches=[],search_decision={"status":"no_search","reason":"website only"})
    store_acquisition_batch(database,payload)
    run=tmp_path/"runs/delayed";binding={"run_id":"delayed","registry_database":str(database),"acquisition_batch_id":"delayed",
        "attempt":1,"task_version":3,"source_keys":["wmo"]}
    atomic_write_json(run/"attempt-1-result.json",{"run_id":"delayed","attempt":1,"finished_at":"2026-09-30T12:05:00Z"})
    state=review.queue_acquisition_review(run/"binding.json",binding,payload);root=run/"acquisition-review"
    candidate=state["packet"]["candidates"][0];key=candidate["identity"]["acquisition_item_id"]
    native=tmp_path/"hermes.db";_native(native,"cron_t10_before_cutoff")
    _native_read(native,"cron_t10_before_cutoff",root/candidate["body_path"],candidate["identity"]["markdown_content"],"approved-body")
    public=tmp_path/"public.sqlite3"
    with sqlite3.connect(public) as db:apply_migrations(db)
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
    queue=tmp_path/"queue";queue.mkdir()
    request=review.activate_approved(root,queue_dir=queue,database=database,repository_root=tmp_path/"application")
    runtime=tmp_path/"runtime"
    monkeypatch.setattr("climate_registry.web_ingest_pipeline._now",lambda:"2026-10-11T12:00:00Z")
    fail=WebIngestPipeline(queue,Path(request["registry_snapshot_path"]),runtime,
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
    ready=WebIngestPipeline(queue,Path(request["registry_snapshot_path"]),runtime,_ack(queue),repository_root=tmp_path/"application").process(request["batch_id"])
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
