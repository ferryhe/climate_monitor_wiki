"""Local PDF adapter contracts; these fixtures are not production author evidence."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
from reportlab.pdfgen.canvas import Canvas

from climate_delivery.io import atomic_write_json, transaction_lock
from climate_registry import pdf_author as author
from climate_registry.pdf_pipeline import PdfIntakePipeline, enqueue_pdf_batch, read_pdf_batch
from climate_registry.pdf_intake import persist_pdf_intake
from climate_monitor.pdf_intake import import_pdf_reports, reconcile_source_coverage
from climate_registry.persistent import initialize_registry
from climate_registry.publication import migrate_publication


def _fixture(tmp_path, monkeypatch, database=None, *, extra=False):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "original.pdf"
    canvas = Canvas(str(path))
    canvas.drawString(50, 760, "UPDATES")
    canvas.drawString(50, 740, "Climate risk study")
    canvas.drawString(50, 720, "IN WINDOW 14 SEP 2026 REPORT")
    canvas.drawString(50, 700, "Flood losses reached 10 billion.")
    canvas.linkURL("https://example.org/good", (48, 738, 250, 754), relative=0)
    if extra:
        canvas.showPage()
        canvas.drawString(50, 740, "Another climate study")
        canvas.drawString(50, 720, "IN WINDOW 14 SEP 2026 REPORT")
        canvas.drawString(50, 700, "A distinct source observation requiring identity resolution.")
        canvas.linkURL("https://example.org/other", (48, 738, 250, 754), relative=0)
    canvas.save()
    bundle = import_pdf_reports([path])
    database = database or tmp_path / "registry.sqlite3"
    if not database.exists():
        initialize_registry(database)
        migrate_publication(database, tmp_path / "backups", apply=True)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    queue = tmp_path / "queue"; queue.mkdir()
    pipeline = PdfIntakePipeline(queue, database, tmp_path / "backups", tmp_path / "wiki",
        lambda *_: pytest.fail("G9 cannot activate Chat"), repository_root=tmp_path / "application")
    status = enqueue_pdf_batch(queue, bundle, repository_root=tmp_path / "application", authorization={
        "actor_id": "authenticated-console-user-id", "actor": "operator", "confirmed_at": "2026-10-26T12:00:00Z",
        "preview_digest": "validated-preview-digest", "scope": author.SCOPE})
    return pipeline, bundle, status["batch_id"]


def _decisions(bundle, actor):
    source = bundle["source_coverage"][0]
    decisions, used = [], set()
    for index, (observation, ids) in enumerate(source["observation_bindings"].items()):
        used.update(ids)
        decisions.append({"source_document_sha256": source["source_document_sha256"], "entry_key": str(index),
            "binding_ids": ids, "observation_ids": [observation], "disposition": "mapped",
            "reason": "The complete exact material has a real retained occurrence.", "agent": actor})
    for index, binding in enumerate(source["bindings"]):
        if binding["binding_id"] not in used:
            decisions.append({"source_document_sha256": source["source_document_sha256"], "entry_key": "header-" + str(index),
                "binding_ids": [binding["binding_id"]], "observation_ids": [], "disposition": "excluded",
                "reason": "A report section heading.", "agent": actor})
    return decisions


def _coverage_task(pipeline):
    author.advance(pipeline, spawn=lambda task: None)
    return next(pipeline.queue_dir.glob("*/author/*/binding.json")).parent


def _fake_runtime(task, monkeypatch):
    from climate_monitor import hermes_identity
    manifest = task / "hermes-private" / "manifest.json"
    manifest.parent.mkdir(exist_ok=True)
    manifest.write_text("{}")
    runtime = {"run_dir": str(task), "source": "climate-pdf-" + task.name,
        "hermes_snapshot": {"sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}}
    identity = {"provider": "offline-fixture", "model": "local-process-fixture", "source": runtime["source"]}
    monkeypatch.setattr(hermes_identity, "load_snapshot", lambda *args: {})
    monkeypatch.setattr(hermes_identity, "require_effective_identity", lambda *args: identity)
    atomic_write_json(task / "runtime.json", runtime)
    return runtime, identity


def test_confirmed_bundle_and_registry_are_separate_readiness(tmp_path, monkeypatch):
    pipeline, bundle, batch = _fixture(tmp_path, monkeypatch)
    before = pipeline.database.read_bytes()
    task = _coverage_task(pipeline)
    status = author.advance(pipeline, spawn=lambda task: None)[0]
    assert status["bundle_ready"] and not status["source_persisted"] and not status["author_started"]
    assert pipeline.database.read_bytes() == before
    assert author._read(task / "binding.json")["authorization"]["actor"] == "operator"
    # The status flag is not an identity or evidence substitute.
    raw = author._read(pipeline.queue_dir / batch / "status.json")
    atomic_write_json(pipeline.queue_dir / batch / "status.json", {**raw, "imported": True})
    assert not author.advance(pipeline, spawn=lambda task: None)[0]["source_persisted"]
    persist_pdf_intake(pipeline.database, pipeline.backup_dir, bundle)
    assert author.advance(pipeline, spawn=lambda task: None)[0]["source_persisted"]
    assert not read_pdf_batch(pipeline.queue_dir, batch)["author"]["author_started"]


def test_missing_original_and_missing_authorization_are_technical_holds(tmp_path, monkeypatch):
    pipeline, bundle, batch = _fixture(tmp_path, monkeypatch)
    path = pipeline.queue_dir / batch / "bundle.json"
    path.unlink()
    result = author.advance(pipeline, spawn=lambda _: pytest.fail("missing original cannot dispatch"))[0]
    assert not result["bundle_ready"] and result["held"]
    atomic_write_json(path, bundle)
    status_path = path.with_name("status.json")
    status = author._read(status_path); status.pop("author_authorization")
    atomic_write_json(status_path, status)
    result = author.advance(pipeline, spawn=lambda _: pytest.fail("historical imports cannot gain authorization"))[0]
    assert result["bundle_ready"] and result["held"][0]["reason"] == "confirmed_console_authorization_unavailable"


def test_corrupt_batch_does_not_stop_independent_ready_batch(tmp_path, monkeypatch):
    pipeline, _, batch = _fixture(tmp_path, monkeypatch)
    bad = pipeline.queue_dir / ("0" * 64); bad.mkdir()
    (bad / "status.json").write_text("broken interrupted local file")
    admitted = []
    results = author.advance(pipeline, spawn=lambda task: admitted.append(task))
    assert any(value["batch_id"] == bad.name and value["held"] for value in results)
    assert any(value["batch_id"] == batch and value["bundle_ready"] for value in results)
    assert admitted and admitted[0].parent.parent.name == batch


def test_preview_never_dispatches_and_confirmation_retains_console_principal(tmp_path, monkeypatch):
    from test_issue170_pdf_import import _client, _pdf_bytes, _confirmed_preview
    from climate_monitor.console_auth import _configured_user
    client, database = _client(monkeypatch, tmp_path)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    original = database.read_bytes()
    payload = {"files": ("original.pdf", _pdf_bytes(), "application/pdf")}
    preview = client.post("/api/manage/pdf-intake/preview", files=payload)
    assert preview.status_code == 200
    assert list((tmp_path / "queue").iterdir()) == []
    confirmed = client.post("/api/manage/pdf-intake/import", params=_confirmed_preview(preview.json()), files=payload)
    assert confirmed.status_code == 200
    authorization = confirmed.json()["author_authorization"]
    assert authorization["actor_id"] == str(_configured_user().id)
    assert authorization["actor"] == _configured_user().email
    assert authorization["preview_digest"] == preview.json()["preview_digest"]
    assert database.read_bytes() == original
    batch = confirmed.json()["batch_id"]
    response = client.post(f"/api/manage/pdf-intake/batches/{batch}/author/resume")
    assert response.status_code == 200 and response.json()["status"] == "resume_requested"
    assert not list((tmp_path / "queue" / batch / "author").glob("*/binding.json"))


def test_coverage_callback_reuses_original_and_only_existing_writer_persists(tmp_path, monkeypatch):
    pipeline, bundle, batch = _fixture(tmp_path, monkeypatch)
    task = _coverage_task(pipeline)
    runtime, _ = _fake_runtime(task, monkeypatch)
    before = pipeline.database.read_bytes()
    def invoke(task, binding, runtime, request, claim):
        # No PDF-wide writer lock is held while inference runs.
        with transaction_lock(pipeline.queue_dir, "intake-writer"):
            pass
        output = {"request_sha256": request["request_sha256"], "decisions": [
            {key: value for key, value in decision.items() if key != "agent"} for decision in _decisions(bundle, None)]}
        _fixture_receipt(task, runtime, request, output)
        return output
    monkeypatch.setattr(author, "_invoke", invoke)
    assert author.work(task)["state"] == "complete"
    assert pipeline.database.read_bytes() == before
    author.advance(pipeline, spawn=lambda _: None)
    with sqlite3.connect(pipeline.database) as connection:
        counts = [connection.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in
            ("pdf_intake_documents", "pdf_intake_article_occurrences", "pdf_source_coverage")]
    author.advance(pipeline, spawn=lambda _: None)
    with sqlite3.connect(pipeline.database) as connection:
        assert counts == [connection.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in
            ("pdf_intake_documents", "pdf_intake_article_occurrences", "pdf_source_coverage")]
    assert author._read(pipeline.queue_dir / batch / "bundle.json") == bundle
    assert read_pdf_batch(pipeline.queue_dir, batch)["author_coverage_tasks"] == [task.name]
    assert not read_pdf_batch(pipeline.queue_dir, batch)["chat_ready"]
    with sqlite3.connect(pipeline.database) as connection:
        coverage = json.loads(connection.execute("SELECT coverage_json FROM pdf_source_coverage").fetchone()[0])
        agents = {revision["agent"] for entry in coverage["entries"] for revision in entry["revisions"]}
        assert agents == {"hermes:offline-fixture/local-process-fixture/session:20261026_120000_abcdef"}
        assert not any(task.name in agent for agent in agents)
    # A changed prompt preserves valid historical proof without attaching it to the new scope.
    monkeypatch.setattr(author, "COVERAGE_PROMPT", author.COVERAGE_PROMPT + "\nA new prompt revision.")
    historical = author._task_status(task, batch)
    assert historical["author_started"] and not historical["current_binding"]
    receipt = next(task.glob("calls/*/receipt.json"))
    (receipt.parent / "stdout.txt").write_text('{"different": "locally replaced output"}')
    assert not author.read_status(pipeline.queue_dir, batch)["author_started"]


def test_changed_prompt_retains_old_handoff_lineage(tmp_path, monkeypatch):
    pipeline, _, _ = _fixture(tmp_path, monkeypatch)
    first = _coverage_task(pipeline)
    monkeypatch.setattr(author, "COVERAGE_PROMPT", author.COVERAGE_PROMPT + "\nNew version.")
    author.advance(pipeline, spawn=lambda _: None)
    tasks = list(first.parent.glob("*/binding.json"))
    assert len(tasks) == 2
    second = next(path for path in tasks if path.parent != first)
    assert first.name in author._read(second)["lineage"]
    assert not author._task_status(first, first.parent.parent.name)["current_binding"]


def test_real_claim_survives_lost_dispatch_response(tmp_path, monkeypatch):
    pipeline, _, _ = _fixture(tmp_path, monkeypatch)
    task = _coverage_task(pipeline)
    with author._execution_lock(task) as fd:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], pass_fds=(fd,))
    try:
        assert author._admit(task, spawn=lambda _: pytest.fail("live inherited claim cannot redispatch")) is None
    finally:
        child.kill(); child.wait()
    called = []
    assert author._admit(task, spawn=lambda _: called.append(True))
    assert called == [True]


def test_actual_detached_worker_receives_claim_and_reports_missing_runtime(tmp_path, monkeypatch):
    pipeline, _, _ = _fixture(tmp_path, monkeypatch)
    task = _coverage_task(pipeline)
    monkeypatch.setenv("HERMES_EXECUTABLE", "/nonexistent/offline-fixture-hermes")
    assert author._admit(task)
    dispatch = author._read(task / "dispatch.json")
    assert dispatch["worker_process"]["pid"] > 0
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and author._read(task / "state.json")["state"] not in {"held", "complete"}:
        time.sleep(.05)
    state = author._read(task / "state.json")
    assert state["state"] == "held" and state["next_owner"] == "PDF runtime/G7"
    assert not state["author_started"] and not list(task.glob("calls/*/receipt.json"))


def _local_invocation(task, monkeypatch):
    from climate_monitor import hermes_identity, hermes_auth_state
    from scripts import run_climate_monitor
    runtime, identity = _fake_runtime(task, monkeypatch)
    home = task / "home"; home.mkdir(exist_ok=True)
    executable = task / "fixture-runtime.py"
    executable.write_text("import sys\nif sys.argv[1:] == ['chat', '--help']:\n print('--query-file --max-turns --reasoning --ignore-rules')\nelse:\n sys.stdin.read()\n print('{\"answer\": 42}')\n print('session_id: 20261026_120000_abcdef', file=sys.stderr)\n")
    monkeypatch.setattr(hermes_identity, "inference_runtime", lambda *args, **kwargs: ([sys.executable, str(executable)], os.environ.copy(), home))
    monkeypatch.setattr(hermes_auth_state, "verify_auth", lambda *_: None)
    @contextmanager
    def execution(*args, **kwargs):
        yield {}
    monkeypatch.setattr(hermes_identity, "auth_execution", execution)
    monkeypatch.setattr(run_climate_monitor, "_hermes_authoring_invocation", lambda *args, **kwargs:
        (["hermes", "-c", "import sys; sys.stdin.read(); print('{\"answer\": 42}'); print('session_id: 20261026_120000_abcdef', file=sys.stderr)"], "fixture request"))
    request = {"prompt": author.COVERAGE_PROMPT, "request_sha256": "fixture-bound-request"}
    return runtime, request


def _fixture_receipt(task, runtime, request, output):
    """Mocked model evidence for writer tests; real local process coverage is separate."""
    path = task / "calls" / (author.digest(request) + "-0001"); path.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path / "request.json", request)
    stdout = json.dumps(output)
    (path / "stdout.txt").write_text(stdout)
    (path / "stderr.txt").write_text("session_id: 20261026_120000_abcdef\n")
    atomic_write_json(path / "receipt.json", {"request_sha256": author.digest(request),
        "session_id": "20261026_120000_abcdef", "actor": {"provider": "offline-fixture",
            "model": "local-process-fixture", "source": runtime["source"]},
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(), "output_sha256": author.digest(output)})


def test_actual_process_session_and_output_are_required_and_reused(tmp_path, monkeypatch):
    pipeline, _, _ = _fixture(tmp_path, monkeypatch)
    task = _coverage_task(pipeline)
    runtime, request = _local_invocation(task, monkeypatch)
    with author._execution_lock(task) as fd:
        first = author._invoke(task, author._read(task / "binding.json"), runtime, request, fd)
        assert first == {"answer": 42}
        receipt = next(task.glob("calls/*/receipt.json"))
        saved = receipt.read_bytes()
        assert author._read(receipt)["session_id"] == "20261026_120000_abcdef"
        assert author._read(receipt)["process"]["pid"] != os.getpid()
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("exact response must replay"))
        assert author._invoke(task, {}, runtime, request, fd) == first
        assert receipt.read_bytes() == saved
        receipt.unlink()  # The result callback was lost after the real process finished.
        assert author._invoke(task, {}, runtime, request, fd) == first
        assert author._read(receipt)["reconciled"]
        (receipt.parent / "stderr.txt").write_text("no session")
        with pytest.raises(ValueError, match="session_id"):
            author._invoke(task, {}, runtime, request, fd)


def test_intent_without_started_or_result_preserves_attempt_and_can_retry(tmp_path, monkeypatch):
    pipeline, _, _ = _fixture(tmp_path, monkeypatch)
    task = _coverage_task(pipeline)
    runtime, request = _local_invocation(task, monkeypatch)
    call = task / "calls" / (author.digest(request) + "-0001"); call.mkdir(parents=True)
    atomic_write_json(call / "intent.json", {"request_sha256": author.digest(request)})
    with author._execution_lock(task) as fd:
        with pytest.raises(ValueError, match="interrupted_without_valid_result"):
            author._invoke(task, {}, runtime, request, fd)
        assert (call / "failed.json").exists()
        assert author._invoke(task, {}, runtime, request, fd) == {"answer": 42}
    assert len(list(task.glob("calls/*/intent.json"))) == 2


def test_tampered_task_cannot_dispatch(tmp_path, monkeypatch):
    pipeline, _, _ = _fixture(tmp_path, monkeypatch)
    task = _coverage_task(pipeline)
    binding = author._read(task / "binding.json")
    binding["document_sha256"] = "b" * 64
    atomic_write_json(task / "binding.json", binding)
    assert author._admit(task, spawn=lambda _: pytest.fail("tampered binding cannot dispatch")) is None
    assert author._read(task / "state.json")["state"] == "held"
    assert "binding_integrity" in author._read(task / "state.json")["reason"]


def test_locally_filled_completion_cannot_substitute_for_actual_author(tmp_path, monkeypatch):
    pipeline, bundle, batch = _fixture(tmp_path, monkeypatch)
    task = _coverage_task(pipeline)
    binding = author._read(task / "binding.json")
    request = author._coverage_request(bundle, binding)
    output = {"request_sha256": request["request_sha256"], "decisions": [
        {key: value for key, value in decision.items() if key != "agent"} for decision in _decisions(bundle, None)]}
    atomic_write_json(task / "output.json", output)
    atomic_write_json(task / "state.json", {"state": "complete", "author_started": True, "output_sha256": author.digest(output)})
    before = pipeline.database.read_bytes()
    result = author.advance(pipeline, spawn=lambda _: pytest.fail("unverified completion cannot dispatch"))[0]
    assert not result["author_started"] and pipeline.database.read_bytes() == before
    assert any("no_actual_author_output" in item["reason"] for item in result["held"])


def _canonical_sources(tmp_path, monkeypatch, *, extra=False):
    from test_content_enrichment import _setup
    database, published = _setup(tmp_path)
    pipeline, bundle, batch = _fixture(tmp_path, monkeypatch, database, extra=extra)
    source = bundle["source_coverage"][0]
    reconcile_source_coverage(bundle, _decisions(bundle, "source-coverage-fixture"))
    persist_pdf_intake(database, pipeline.backup_dir, bundle)
    with sqlite3.connect(database) as connection:
        # Existing G4 confirmation fixture; the adapter must never write this link.
        connection.execute("UPDATE pdf_intake_articles SET core_article_id='good',confirmation_basis='exact_url_eligible_detail' WHERE canonical_url='https://example.org/good'")
    return pipeline, bundle, batch, published


def test_ready_article_is_independent_of_other_material_identity_hold(tmp_path, monkeypatch):
    pipeline, bundle, _, _ = _canonical_sources(tmp_path, monkeypatch, extra=True)
    ready, held = author._sources(pipeline.database, bundle)
    assert len(ready) == 1 and len(held) == 1
    assert ready[0]["canonical"] == ["article", "good"]
    assert held[0]["next_owner"] == "G4"
    assert held[0]["target"]["occurrence_id"] != ready[0]["target"]["occurrence_id"]


def test_existing_article_source_handoff_keeps_baseline_and_manual_dates_without_redispatch(tmp_path, monkeypatch):
    from test_article_content_dates import _manual
    from test_owner_checks import _ingest
    pipeline, bundle, batch, published = _canonical_sources(tmp_path, monkeypatch)
    ready, held = author._sources(pipeline.database, bundle)
    assert len(ready) == 1 and not held
    assert ready[0]["canonical"] == ["article", "good"]
    assert ready[0]["published_observation"]["candidate_sha256"] == published
    assert ready[0]["published_observation"]["read_at"]
    with sqlite3.connect(pipeline.database) as connection:
        publication = connection.execute("SELECT published_candidate_sha256,latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()
    observation = ready[0]["published_observation"]
    assert (observation["published_candidate_sha256"], observation["latest_candidate_sha256"]) == publication
    assert observation["read_at"] == ready[0]["read_at"] and observation["requires_downstream_recheck"]
    assert ready[0]["content"]["content_version_id"] == "body-b"
    # Use the same real source key even if an owner instruction moves the published snapshot.
    task = _coverage_task(pipeline)
    base = author._read(task / "binding.json")
    base = {key: value for key, value in base.items() if key not in
        {"handoff_id", "item", "runtime", "created_at", "lineage", "predecessors", "stage", "prompt_sha256"}}
    content = author._task(task.parent, base, "content", ready[0], {"source": "fixture"})
    frozen_bytes = (content / "binding.json").read_bytes()
    full_binding = author._read(content / "binding.json")
    assert full_binding["binding_integrity_sha256"] == author.digest({key: value for key, value in full_binding.items()
        if key != "binding_integrity_sha256"})
    _ingest(pipeline.database, tmp_path, _manual(pipeline.database))
    after, _ = author._sources(pipeline.database, bundle)
    assert after[0]["manual_date_overrides"]["publication_date"]["new_value"] == "2026-10-02"
    assert after[0]["published_observation"]["candidate_sha256"] != published
    assert author._task(task.parent, base, "content", after[0], {"source": "fixture"}) == content
    assert (content / "binding.json").read_bytes() == frozen_bytes
    frozen = author._read(content / "binding.json")["item"]
    assert frozen["published_observation"]["candidate_sha256"] == published
    assert frozen["published_observation"] == observation
    assert frozen["published_observation"]["requires_downstream_recheck"]
    from test_enrichment_contract import _insert_body
    with sqlite3.connect(pipeline.database) as connection:
        _insert_body(connection, "body-c", "A real newer complete body.", "2026-10-25T00:00:00Z")
        assert connection.execute("SELECT count(*) FROM articles WHERE article_id='good'").fetchone()[0] == 1
    newest, _ = author._sources(pipeline.database, bundle)
    changed = author._task(task.parent, base, "content", newest[0], {"source": "fixture"})
    assert changed != content and content.name in author._read(changed / "binding.json")["lineage"]


def test_new_pdf_same_confirmed_article_keeps_cross_batch_source_lineage(tmp_path, monkeypatch):
    pipeline, bundle, batch, _ = _canonical_sources(tmp_path, monkeypatch)
    with sqlite3.connect(pipeline.database) as connection:
        old_document_count = connection.execute("SELECT count(*) FROM pdf_intake_documents").fetchone()[0]
    ready, _ = author._sources(pipeline.database, bundle)
    coverage = _coverage_task(pipeline)
    base = author._read(coverage / "binding.json")
    base = {key: value for key, value in base.items() if key not in
        {"handoff_id", "item", "runtime", "created_at", "lineage", "predecessors", "stage", "prompt_sha256"}}
    first = author._task(coverage.parent, base, "content", ready[0], {"source": "fixture"})
    path = tmp_path / "another-original.pdf"
    canvas = Canvas(str(path))
    canvas.drawString(50, 760, "Another PDF edition with the same existing article")
    canvas.drawString(50, 740, "Climate risk study")
    canvas.drawString(50, 720, "IN WINDOW 15 SEP 2026 REPORT")
    canvas.drawString(50, 700, "Flood losses reached 10 billion.")
    canvas.linkURL("https://example.org/good", (48, 738, 250, 754), relative=0)
    canvas.save()
    new = import_pdf_reports([path])
    status = enqueue_pdf_batch(pipeline.queue_dir, new, repository_root=tmp_path / "application",
        authorization=base["authorization"])
    reconcile_source_coverage(new, _decisions(new, "actual-existing-coverage-fixture"))
    persist_pdf_intake(pipeline.database, pipeline.backup_dir, new)
    with sqlite3.connect(pipeline.database) as connection:
        assert connection.execute("SELECT core_article_id FROM pdf_intake_articles WHERE canonical_url='https://example.org/good'").fetchone()[0] == "good"
        assert connection.execute("SELECT count(*) FROM articles WHERE canonical_url='https://example.org/good'").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM pdf_intake_documents").fetchone()[0] == old_document_count + 1
    newest, _ = author._sources(pipeline.database, new)
    assert newest[0]["canonical"] == ready[0]["canonical"]
    assert newest[0]["content"] == ready[0]["content"]
    next_base = {**base, "batch_id": status["batch_id"], "document_sha256": new["documents"][0]["source"]["sha256"],
        "bundle_path": str(pipeline.queue_dir / status["batch_id"] / "bundle.json")}
    second = author._task(pipeline.queue_dir / status["batch_id"] / "author", next_base, "content", newest[0], {"source": "fixture"})
    lineage = author._read(second / "binding.json")
    assert second != first and first.name in lineage["lineage"]
    reference = next(value for value in lineage["predecessors"] if value["handoff_id"] == first.name)
    assert reference["batch_id"] == batch and reference["canonical"] == ["article", "good"]
    assert reference["content"]["content_version_id"] == "body-b"


@pytest.mark.parametrize("manual_during_author", [False, True])
def test_g7_uses_real_pdf_target_and_reconciles_committed_check_run(tmp_path, monkeypatch, manual_during_author):
    from climate_monitor import hermes_identity
    from climate_registry import information_checks
    from test_content_enrichment import _author, BODY
    pipeline, bundle, batch, published = _canonical_sources(tmp_path, monkeypatch)
    ready, _ = author._sources(pipeline.database, bundle)
    coverage = _coverage_task(pipeline)
    base = author._read(coverage / "binding.json")
    base = {key: value for key, value in base.items() if key not in
        {"handoff_id", "item", "runtime", "created_at", "lineage", "predecessors", "stage", "prompt_sha256"}}
    runtime, identity = _fake_runtime(coverage, monkeypatch)
    task = author._task(coverage.parent, base, "content", ready[0], runtime)
    calls = []
    def invoke(task, binding, runtime, request, claim):
        calls.append(request)
        if manual_during_author:
            from test_article_content_dates import _manual
            from test_owner_checks import _ingest
            _ingest(pipeline.database, tmp_path, _manual(pipeline.database))
        return _author(request)
    monkeypatch.setattr(author, "_invoke", invoke)
    actual = information_checks.run_checks
    def checks(*args, **kwargs):
        def fetch(occurrence, url):
            with transaction_lock(pipeline.queue_dir, "intake-writer"):
                pass
            return {"article_id": occurrence, "requested_url": url, "final_url": url, "status": "ok",
                "content": BODY, "content_ref": None, "content_hash": hashlib.sha256(BODY.encode()).hexdigest()}
        return actual(*args, **kwargs, fetcher=fetch, verifier=lambda kind, fields, body: {"comparisons": {
            key: {"status": "supported"} for key in fields}})
    monkeypatch.setattr(information_checks, "run_checks", checks)
    binding = author._read(task / "binding.json")
    output = author._content(task, binding, runtime, -1, bundle)
    assert output["content_enrichment"][0]["status"] == "pending_review", output
    assert calls[0]["previous"]["candidate_sha256"] == published
    assert "30 percent" in calls[0]["article_body"]
    # Simulate losing the adapter output/callback after the T1 run actually committed.
    calls.clear()
    again = author._content(task, binding, runtime, -1, bundle)
    assert again["run_id"] == output["run_id"] and not calls
    with sqlite3.connect(pipeline.database) as connection:
        assert connection.execute("SELECT count(*) FROM article_check_runs WHERE run_id=?", (output["run_id"],)).fetchone()[0] == 1
        current_public = connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0]
        assert (current_public != published) == manual_during_author
        candidate = output["content_enrichment"][0]["candidate_sha256"]
        assert connection.execute("SELECT count(*) FROM registry_reviews WHERE candidate_sha256=?", (candidate,)).fetchone()[0] == 0
        if manual_during_author:
            snapshot = json.loads(connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (candidate,)).fetchone()[0])
            assert snapshot["derived_display"]["manual_date_overrides"]["publication_date"]["new_value"] == "2026-10-02"
            assert snapshot["enrichment_scope"]["content_comparison_baseline"]["candidate_sha256"] == published


def _content_worker_fixture(tmp_path, monkeypatch, *, verifier=None):
    from climate_registry import information_checks
    from test_content_enrichment import BODY
    pipeline, bundle, batch, _ = _canonical_sources(tmp_path, monkeypatch)
    ready, _ = author._sources(pipeline.database, bundle)
    coverage = _coverage_task(pipeline)
    base = author._read(coverage / "binding.json")
    base = {key: value for key, value in base.items() if key not in
        {"handoff_id", "item", "runtime", "created_at", "lineage", "predecessors", "stage", "prompt_sha256"}}
    runtime, _ = _fake_runtime(coverage, monkeypatch)
    task = author._task(coverage.parent, base, "content", ready[0], runtime)
    actual = information_checks.run_checks
    def checks(*args, **kwargs):
        return actual(*args, **kwargs, fetcher=lambda occurrence, url: {
            "article_id": occurrence, "requested_url": url, "final_url": url, "status": "ok",
            "content": BODY, "content_ref": None, "content_hash": hashlib.sha256(BODY.encode()).hexdigest()},
            verifier=verifier or (lambda kind, fields, body: {"comparisons": {key: {"status": "supported"} for key in fields}}))
    monkeypatch.setattr(information_checks, "run_checks", checks)
    return pipeline, bundle, task, runtime


def test_transient_content_author_error_resumes_same_frozen_t1_run(tmp_path, monkeypatch):
    from test_content_enrichment import _author
    pipeline, bundle, task, runtime = _content_worker_fixture(tmp_path, monkeypatch)
    frozen_binding = (task / "binding.json").read_bytes()
    calls = []
    def invoke(task, binding, runtime, request, claim):
        calls.append(request)
        if len(calls) == 1:
            raise OSError("temporary author connection failure")
        output = _author(request)
        _fixture_receipt(task, runtime, request, output)
        return output
    monkeypatch.setattr(author, "_invoke", invoke)
    first = author.work(task)
    assert first["state"] == "held", first
    run_id = first["check_run_id"]
    with sqlite3.connect(pipeline.database) as connection:
        assert connection.execute("SELECT count(*) FROM article_check_attempts WHERE run_id=?", (run_id,)).fetchone()[0] == 0
    # A real short-lived worker has now exited; the queue resumes the same exact handoff.
    atomic_write_json(task / "state.json", {**first, "worker_process": None})
    assert author._admit(task, spawn=lambda _: None, force=True)
    assert author.work(task)["state"] == "complete"
    assert author._read(task / "state.json")["check_run_id"] == run_id
    assert (task / "binding.json").read_bytes() == frozen_binding
    with sqlite3.connect(pipeline.database) as connection:
        attempts = connection.execute("SELECT attempt_id FROM article_check_attempts WHERE run_id=?", (run_id,)).fetchall()
        assert len(attempts) == 1
        assert len([row for row in connection.execute("SELECT input_json FROM article_check_runs")
            if json.loads(row[0]).get("content_author", {}).get("pdf_binding_path") == str(task / "binding.json")]) == 1
    assert author.work(task)["state"] == "complete" and len(calls) == 2
    with sqlite3.connect(pipeline.database) as connection:
        assert connection.execute("SELECT attempt_id FROM article_check_attempts WHERE run_id=?", (run_id,)).fetchall() == attempts


def test_coverage_prompt_successor_freezes_and_appends_persisted_revision_chain(tmp_path, monkeypatch):
    pipeline, bundle, batch = _fixture(tmp_path, monkeypatch)
    original_upload = (pipeline.queue_dir / batch / "bundle.json").read_bytes()
    first = _coverage_task(pipeline)
    _fake_runtime(first, monkeypatch)
    def invoke(task, binding, runtime, request, claim):
        source = request["source_coverage"][0]
        revisions = {entry["revisions"][-1]["entry_key"]: entry["revisions"][-1]["revision_id"]
            for entry in source["entries"] if entry["revisions"]}
        decisions = [{**{key: value for key, value in decision.items() if key != "agent"},
            "previous_revision_id": revisions.get(decision["entry_key"]), "reason": binding["prompt_sha256"]}
            for decision in _decisions(bundle, None)]
        output = {"request_sha256": request["request_sha256"], "decisions": decisions}
        _fixture_receipt(task, runtime, request, output)
        return output
    monkeypatch.setattr(author, "_invoke", invoke)
    assert author.work(first)["state"] == "complete"
    author.advance(pipeline, spawn=lambda _: None)
    from climate_registry.read_api import RegistryReader
    reader = RegistryReader(pipeline.database, public=False, repository_root=tmp_path / "application")
    before = reader.pdf_source_coverage(bundle["documents"][0]["source"]["sha256"])["current"]
    assert any(entry["revisions"] for entry in before["entries"])
    monkeypatch.setattr(author, "COVERAGE_PROMPT", author.COVERAGE_PROMPT + "\nRecheck the same source judgments.")
    author.advance(pipeline, spawn=lambda _: None)
    successor = next(path.parent for path in first.parent.glob("*/binding.json") if path.parent != first)
    binding = author._read(successor / "binding.json")
    request = author._coverage_request(bundle, binding)
    assert request["source_coverage"] == [before]
    _fake_runtime(successor, monkeypatch)
    assert author.work(successor)["state"] == "complete"
    author.advance(pipeline, spawn=lambda _: None)
    after = reader.pdf_source_coverage(bundle["documents"][0]["source"]["sha256"])["current"]
    old_entries = {entry["entry_id"]: entry for entry in before["entries"] if entry["revisions"]}
    for entry in after["entries"]:
        if entry["entry_id"] in old_entries:
            previous = old_entries[entry["entry_id"]]["revisions"]
            assert entry["revisions"][:-1] == previous
            assert entry["revisions"][-1]["previous_revision_id"] == previous[-1]["revision_id"]
    assert successor.name in read_pdf_batch(pipeline.queue_dir, batch)["author_coverage_tasks"]
    assert (pipeline.queue_dir / batch / "bundle.json").read_bytes() == original_upload


def test_terminal_partial_content_uses_existing_t1_retry_and_preserves_attempts(tmp_path, monkeypatch):
    from test_content_enrichment import _author
    supported = False
    def verify(kind, fields, body):
        return {"comparisons": {key: {"status": "supported" if supported or key != "title" else "missing"} for key in fields}}
    pipeline, bundle, task, runtime = _content_worker_fixture(tmp_path, monkeypatch, verifier=verify)
    frozen_binding = (task / "binding.json").read_bytes()
    responses, actual_author_calls = {}, []
    def invoke(task, binding, runtime, request, claim):
        key = author.digest(request)
        if key not in responses:
            actual_author_calls.append(key)
            responses[key] = _author(request)
            _fixture_receipt(task, runtime, request, responses[key])
        return responses[key]
    monkeypatch.setattr(author, "_invoke", invoke)
    first = author.work(task)
    assert first["state"] == "held" and first["t1_status"] == "partial"
    old_run = first["check_run_id"]
    with sqlite3.connect(pipeline.database) as connection:
        old_attempts = connection.execute("SELECT attempt_id FROM article_check_attempts WHERE run_id=?", (old_run,)).fetchall()
        assert len(old_attempts) == 1
    supported = True
    result = author.work(task)
    assert result["state"] == "complete", result
    new_run = result["check_run_id"]
    assert new_run != old_run and old_run in result["check_run_predecessors"]
    assert (task / "binding.json").read_bytes() == frozen_binding
    with sqlite3.connect(pipeline.database) as connection:
        assert connection.execute("SELECT attempt_id FROM article_check_attempts WHERE run_id=?", (old_run,)).fetchall() == old_attempts
        frozen = json.loads(connection.execute("SELECT input_json FROM article_check_runs WHERE run_id=?", (new_run,)).fetchone()[0])
        assert frozen["retry_of"] == old_run
        assert connection.execute("SELECT count(*) FROM article_check_attempts WHERE run_id=?", (new_run,)).fetchone()[0] == 1
    assert len(actual_author_calls) == 1


def test_coverage_append_race_is_rejected_then_successor_rereads_actual_ledger(tmp_path, monkeypatch):
    from climate_registry.read_api import RegistryReader
    pipeline, bundle, batch = _fixture(tmp_path, monkeypatch)
    original_upload = (pipeline.queue_dir / batch / "bundle.json").read_bytes()
    reconcile_source_coverage(bundle, _decisions(bundle, "persisted-original-coverage-fixture"))
    persist_pdf_intake(pipeline.database, pipeline.backup_dir, bundle)
    task = _coverage_task(pipeline)
    _fake_runtime(task, monkeypatch)
    def decisions(source, reason, *, agent=None):
        heads = {entry["revisions"][-1]["entry_key"]: entry["revisions"][-1]["revision_id"]
            for entry in source["entries"] if entry["revisions"]}
        return [{**{key: value for key, value in decision.items() if key != "agent"},
            "reason": reason, "previous_revision_id": heads.get(decision["entry_key"]),
            **({"agent": agent} if agent else {})} for decision in _decisions(bundle, None)]
    def invoke(task, binding, runtime, request, claim):
        output = {"request_sha256": request["request_sha256"], "decisions": decisions(request["source_coverage"][0], "author recheck")}
        _fixture_receipt(task, runtime, request, output)
        return output
    monkeypatch.setattr(author, "_invoke", invoke)
    assert author.work(task)["state"] == "complete"
    # Another valid writer appends after the task froze its input, before this writer's apply.
    reconcile_source_coverage(bundle, decisions(bundle["source_coverage"][0], "concurrent reviewed judgment", agent="concurrent-coverage-fixture"))
    persist_pdf_intake(pipeline.database, pipeline.backup_dir, bundle)
    reader = RegistryReader(pipeline.database, public=False, repository_root=tmp_path / "application")
    current = reader.pdf_source_coverage(bundle["documents"][0]["source"]["sha256"])["current"]
    result = author.advance(pipeline, spawn=lambda _: None)[0]
    assert any("stored PDF coverage judgment changed" in item["reason"] for item in result["held"])
    assert author._read(task / "state.json")["coverage_rebase_required"]
    assert task.name not in read_pdf_batch(pipeline.queue_dir, batch).get("author_coverage_tasks", [])
    assert reader.pdf_source_coverage(bundle["documents"][0]["source"]["sha256"])["current"] == current
    author.advance(pipeline, spawn=lambda _: None)
    successor_id = author._read(task / "state.json")["coverage_successor"]
    successor = task.parent / successor_id
    binding = author._read(successor / "binding.json")
    assert binding["coverage_input"]["source_coverage"] == [current]
    assert binding["coverage_predecessor"] == task.name and task.name in binding["lineage"]
    _fake_runtime(successor, monkeypatch)
    assert author.work(successor)["state"] == "complete"
    author.advance(pipeline, spawn=lambda _: None)
    assert successor_id in read_pdf_batch(pipeline.queue_dir, batch)["author_coverage_tasks"]
    assert author._task(task.parent, {key: value for key, value in author._read(task / "binding.json").items()
        if key not in {"handoff_id", "item", "runtime", "created_at", "lineage", "predecessors", "stage", "prompt_text", "prompt_sha256"}}, "coverage") == successor
    assert (pipeline.queue_dir / batch / "bundle.json").read_bytes() == original_upload


@pytest.mark.parametrize("field", ["published_observation", "runtime", "predecessors"])
def test_full_frozen_binding_tamper_is_held_without_dispatch(tmp_path, monkeypatch, field):
    pipeline, _, task, _ = _content_worker_fixture(tmp_path, monkeypatch)
    binding = author._read(task / "binding.json")
    if field == "published_observation":
        binding["item"]["published_observation"]["published_candidate_sha256"] = "not-the-observed-version"
    elif field == "runtime":
        binding["runtime"]["hermes_snapshot"]["sha256"] = "d" * 64
    else:
        binding["predecessors"].append({"handoff_id": "unverified-predecessor"})
    atomic_write_json(task / "binding.json", binding)
    batch = binding["batch_id"]
    result = author.read_status(pipeline.queue_dir, batch)
    assert any(item.get("handoff_id") == task.name and "binding_integrity" in item["reason"] for item in result["held"])
    admitted = []
    assert author._admit(task, spawn=lambda task: admitted.append(task), force=True) is None
    assert not admitted and author._read(task / "state.json")["state"] == "held"


def test_authenticated_legacy_confirmation_survives_writer_persist_interleave(tmp_path, monkeypatch):
    import asyncio
    from io import BytesIO
    from fastapi import UploadFile
    import api_server
    from climate_registry import pdf_pipeline
    from climate_monitor.console_auth import _configured_user
    from test_issue170_pdf_import import _client, _pdf_bytes, _confirmed_preview
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    client, database = _client(monkeypatch, tmp_path)
    migrate_publication(database, tmp_path / "backups", apply=True)
    raw = _pdf_bytes()
    bundle = asyncio.run(api_server._uploaded_pdf_bundle([UploadFile(filename="legacy.pdf", file=BytesIO(raw))]))
    queue = tmp_path / "queue"
    pipeline = PdfIntakePipeline(queue, database, tmp_path / "backups", tmp_path / "wiki",
        lambda *_: pytest.fail("pending intake cannot activate Chat"), repository_root=tmp_path / "application")
    legacy = enqueue_pdf_batch(queue, bundle, repository_root=pipeline.repository_root)
    batch = legacy["batch_id"]
    assert "author_authorization" not in read_pdf_batch(queue, batch)
    payload = {"files": ("legacy.pdf", raw, "application/pdf")}
    preview = client.post("/api/manage/pdf-intake/preview", files=payload)
    assert preview.status_code == 200
    confirmations = []
    actual_persist = pdf_pipeline.persist_pdf_intake
    def persist(database, backup, value):
        # The writer already owns its old status snapshot and intake-writer lock.
        response = client.post("/api/manage/pdf-intake/import", params=_confirmed_preview(preview.json()), files=payload)
        assert response.status_code == 200, response.text
        confirmations.append(response.json())
        assert confirmations[0]["batch_id"] == batch
        assert confirmations[0]["author_authorization"]["actor_id"] == str(_configured_user().id)
        return actual_persist(database, backup, value)
    monkeypatch.setattr(pdf_pipeline, "persist_pdf_intake", persist)
    result = pipeline.process(batch)
    assert result["stage"] == "pending_review" and result["imported"]
    saved = read_pdf_batch(queue, batch)
    assert saved["author_authorization"] == confirmations[0]["author_authorization"]
    assert saved["stage"] == "pending_review" and saved["attempts"] == 1
    admitted = []
    next_tick = author.advance(pipeline, spawn=lambda task: admitted.append(task))[0]
    assert admitted and next_tick["source_persisted"]
    assert not any("confirmed_console_authorization_unavailable" in item["reason"] for item in next_tick["held"])
    assert author._read_binding(admitted[0])["authorization"] == saved["author_authorization"]
    with sqlite3.connect(database) as connection:
        counts = [connection.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in
            ("pdf_intake_documents", "pdf_intake_article_occurrences", "pdf_intake_calendar_items")]
    pipeline.process(batch)
    assert len(confirmations) == 1  # Reprocessing needs neither another import nor another confirmation.
    assert read_pdf_batch(queue, batch)["author_authorization"] == saved["author_authorization"]
    with sqlite3.connect(database) as connection:
        assert counts == [connection.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in
            ("pdf_intake_documents", "pdf_intake_article_occurrences", "pdf_intake_calendar_items")]


@pytest.mark.parametrize("first_operation", ["confirmation", "writer"])
def test_confirmation_and_stale_writer_status_are_serialized(tmp_path, monkeypatch, first_operation):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
    from threading import Event
    from climate_delivery.errors import LockStateError
    from climate_registry import pdf_pipeline
    pipeline, bundle, batch = _fixture(tmp_path, monkeypatch)
    path = pipeline.queue_dir / batch / "status.json"
    stale = author._read(path)
    authorization = stale.pop("author_authorization")
    atomic_write_json(path, stale)
    entered, release, second_started = Event(), Event(), Event()
    actual_write = pdf_pipeline.atomic_write_json
    def write(target, value):
        if Path(target) == path and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return actual_write(target, value)
    monkeypatch.setattr(pdf_pipeline, "atomic_write_json", write)
    def confirm():
        return enqueue_pdf_batch(pipeline.queue_dir, bundle, repository_root=pipeline.repository_root, authorization=authorization)
    def save():
        return pipeline._save(batch, dict(stale), stage="processing", attempts=1, registry_database=str(pipeline.database))
    first, second = (confirm, save) if first_operation == "confirmation" else (save, confirm)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(first)
        try:
            assert entered.wait(5)
            def run_second():
                second_started.set()
                return second()
            second_result = executor.submit(run_second)
            assert second_started.wait(5)
            with pytest.raises(FutureTimeoutError):
                second_result.result(timeout=.05)
        finally:
            release.set()
        first_result.result(timeout=5)
        second_result.result(timeout=5)
    saved = read_pdf_batch(pipeline.queue_dir, batch)
    assert saved["author_authorization"] == authorization
    assert saved["stage"] == "processing" and saved["attempts"] == 1
    # Existing writer transactions remain fail-fast unless this short status path opts in.
    with transaction_lock(pipeline.queue_dir, "default-writer"):
        with pytest.raises(LockStateError):
            with transaction_lock(pipeline.queue_dir, "default-writer"):
                pytest.fail("default lock semantics changed")


def test_retry_preserves_confirmation_appended_after_its_status_read(tmp_path, monkeypatch):
    from climate_registry import pdf_pipeline
    pipeline, bundle, batch = _fixture(tmp_path, monkeypatch)
    path = pipeline.queue_dir / batch / "status.json"
    legacy = author._read(path)
    authorization = legacy.pop("author_authorization")
    legacy.update(stage="failed", registry_database=str(pipeline.database), attempts=1, error="prior fixture failure")
    atomic_write_json(path, legacy)
    actual_validate = pdf_pipeline._validate_pdf_binding
    confirmations = []
    def validate(status, database=None):
        actual_validate(status, database)
        confirmed = enqueue_pdf_batch(pipeline.queue_dir, bundle, repository_root=pipeline.repository_root, authorization=authorization)
        confirmations.append(confirmed["author_authorization"])
    monkeypatch.setattr(pdf_pipeline, "_validate_pdf_binding", validate)
    retried = pdf_pipeline.retry_pdf_batch(pipeline.queue_dir, batch)
    assert confirmations == [authorization]
    assert retried["stage"] == "queued" and retried["author_authorization"] == authorization
    assert retried["failure_history"][-1]["reason"] == "prior fixture failure"
    assert read_pdf_batch(pipeline.queue_dir, batch)["author_authorization"] == authorization
