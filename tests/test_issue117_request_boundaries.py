"""Request-boundary and durable-resume requirements, independent of live services."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def binding(tmp_path, *, fetch=2, search=1, runtime=60, attempt=1):
    return {"run_id": "boundary", "attempt": attempt, "effective_sha256": "a" * 64,
            "checkpoint_dir": str(tmp_path / "checkpoint"),
            "budgets": {"fetch_attempts": fetch, "search_attempts": search,
                        "search_results": 5, "retries_per_item": 2, "runtime_seconds": runtime}}


def budget(tmp_path, **kwargs):
    from climate_monitor.request_budget import RequestBudget
    return RequestBudget(tmp_path / "request-budget.json", binding(tmp_path, **kwargs))


def test_default_covers_all_seed_and_bounded_article_work():
    from climate_monitor.management import default_task_definition
    value = default_task_definition()["parameters"]["budgets"]
    # Four sends per seed/article operation, 40 articles with two retries,
    # plus 120 native fetch-tool units. Larger redirect chains stop honestly.
    assert value["fetch_attempts"] >= 116 * 4 + 40 * 3 * 4 + 120


def test_target_redirect_and_failure_reservations_are_durable(tmp_path):
    from climate_monitor.request_budget import GuardedGateway, RequestBudgetError
    ledger = budget(tmp_path, fetch=1)
    sends = []
    class Gateway:
        user_agent = "test"
        def read(self, url, *, before_target_request, **kwargs):
            for target in (url, url + "redirect"):
                release = before_target_request(target, SimpleNamespace(decision_id="decision"))
                sends.append(target)
                if release:
                    release()
    with pytest.raises(RequestBudgetError):
        GuardedGateway(Gateway(), ledger, "seed").read("https://example.test/")
    assert sends == ["https://example.test/"]
    assert budget(tmp_path, fetch=1).usage()["fetch_attempts"] == 1
    assert any(event["event_kind"] == "precheck" for event in ledger.events())


def test_policy_rejection_has_no_target_charge(tmp_path):
    from climate_monitor.request_budget import GuardedGateway
    ledger = budget(tmp_path)
    class Gateway:
        def read(self, url, **kwargs):
            raise RuntimeError("policy rejection before callback")
    with pytest.raises(RuntimeError, match="policy"):
        GuardedGateway(Gateway(), ledger, "seed").read("https://example.test/")
    assert ledger.usage()["fetch_attempts"] == 0


def test_deadline_blocks_before_send(tmp_path, monkeypatch):
    import climate_monitor.request_budget as module
    monkeypatch.setattr(module.time, "time", lambda: 100.0)
    ledger = budget(tmp_path, runtime=10)
    monkeypatch.setattr(module.time, "time", lambda: 110.0)
    with pytest.raises(module.RequestBudgetError, match="runtime"):
        ledger.claim("http", "https://example.test/")
    assert ledger.usage()["fetch_attempts"] == 0


def test_ledger_cannot_reset_or_change_limits_on_resume(tmp_path):
    from climate_monitor.request_budget import RequestBudget, RequestBudgetError
    ledger = budget(tmp_path, fetch=1)
    ledger.claim("http", "https://example.test/")
    ledger.finish()
    resumed = budget(tmp_path, fetch=1, attempt=2)
    with pytest.raises(RequestBudgetError):
        resumed.claim("http", "https://example.test/next")
    with pytest.raises(ValueError, match="identity|limits"):
        budget(tmp_path, fetch=2, attempt=2)
    with pytest.raises(ValueError, match="missing"):
        RequestBudget(tmp_path / "missing.json", binding(tmp_path, attempt=2))


@pytest.mark.parametrize("tool", ["web_search", "web_extract", "browser_exec"])
def test_hermes_pre_hook_blocks_handler_at_exact_boundary(tmp_path, tool):
    from climate_monitor.request_budget import hook_decision
    ledger = budget(tmp_path, fetch=1, search=1)
    args = {"query": "climate", "num_results": 5} if tool == "web_search" else {"url": "https://example.test/"}
    payload = {"hook_event_name": "pre_tool_call", "tool_name": tool,
               "tool_input": args, "session_id": "s", "extra": {"tool_call_id": "1"}}
    invoked = []
    for call_id in ("1", "2"):
        payload["extra"]["tool_call_id"] = call_id
        directive = hook_decision(ledger, payload)
        if directive.get("action") != "block":
            invoked.append(call_id)
    assert invoked == ["1"]
    assert len([e for e in ledger.events() if e["event_kind"] == "tool"]) == 1


def test_unsupported_article_never_invokes_unguarded_reader(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter as article
    called = []
    monkeypatch.setattr(article, "_import_public_reader", lambda: SimpleNamespace(
        fetch_article_content=lambda url: called.append(url)))
    record = article.fetch_article_content("a", "https://example.test/", budget=budget(tmp_path))
    assert called == []
    assert record["status"] == "unavailable"
    assert "before_target_request" in record["failure_reason"]
    assert record["attempts"][0]["event_kind"] == "unsupported"


def test_receipt_is_hash_verified_and_shared_across_attempts(tmp_path):
    ledger = budget(tmp_path)
    ledger.save_receipt("seed", {"checkpoint": {"content_hash": "a", "links": []}})
    ledger.finish()
    resumed = budget(tmp_path, attempt=2)
    assert resumed.receipt("seed")["checkpoint"]["content_hash"] == "a"
    value = json.loads((tmp_path / "request-budget.json").read_text())
    value["receipts"]["seed"]["payload"]["checkpoint"]["content_hash"] = "tampered"
    (tmp_path / "request-budget.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="hash|digest"):
        resumed.receipt("seed")


def test_pinned_shell_hook_runs_before_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    hooks = pytest.importorskip("agent.shell_hooks")
    import shlex
    import sys
    from climate_monitor.request_budget import ledger_path
    from climate_monitor.hermes_acquisition_hooks import install_hooks
    b = binding(tmp_path, fetch=1)
    path = tmp_path / "attempt-1.json"
    path.write_text(json.dumps(b))
    ledger = budget(tmp_path, fetch=1)
    # Use the actual installed Python runtime, with a CLI-shaped entrypoint.
    executable = tmp_path / "hermes"
    executable.write_text(f"#!{sys.executable}\n")
    executable.chmod(0o700)
    env, home = install_hooks([str(executable)], path, b, {"PATH": __import__('os').environ['PATH']})
    config = json.loads((home / "config.yaml").read_text())
    spec = next(s for s in hooks.iter_configured_hooks(config) if s.event == "pre_tool_call")
    invoked = []
    for call_id in ("1", "2"):
        outcome = hooks.run_once(spec, {"tool_name": "web_extract", "args": {"url": "https://example.test/"},
                                       "session_id": "test", "tool_call_id": call_id})
        if (outcome.get("parsed") or {}).get("action") != "block":
            invoked.append(call_id)
    assert invoked == ["1"]
    assert env["HERMES_HOME"] == str(home)
    assert spec.fail_closed


def test_failed_source_projection_keeps_artifact_and_no_full_success(tmp_path):
    import hashlib
    import scripts.run_agent_acquisition as runner
    manifest = {"manifest_id": "failed-evidence", "source": {"source_id": "example"}}
    path = tmp_path / "failed.json"
    path.write_text(json.dumps(manifest))
    outcome = {"full_success": False, "counts": {"valid_snapshots": 0},
               "dispositions": [{"disposition": "blocked", "artifact_id": None}]}
    row = {"source": "example", "status": "failed", "artifact_id": "failed-evidence",
           "artifact_path": str(path), "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
           "manifest": manifest, "outcome": outcome}
    b = {"source_inventory": {"records": [{"key": "example"}]},
         "report_inputs": {key: str(tmp_path / key) for key in ("acquisition_batch", "web_listening_manifest", "pillar_b_artifact")}}
    payload = {"items": [], "report_date": "2026-09-07", "date_policy": {},
               "search_decision": "no_search", "searches": []}
    runner._write_report_inputs(b, payload, {"status": "completed", "source_results": [row]})
    assert json.loads(Path(b["report_inputs"]["acquisition_batch"]).read_text())[0]["full_success"] is False
    manifest_path = Path(b["report_inputs"]["web_listening_manifest"])
    assert json.loads(manifest_path.read_text()) == []
    assert json.loads(manifest_path.with_suffix(".diagnostics.json").read_text()) == [manifest]
    assert json.loads(path.read_text()) == manifest


def seed_runtime(monkeypatch, sends, interrupt_at=None, outcomes=None):
    from climate_monitor import web_listening_adapter as adapter
    class Gateway:
        user_agent = "web-listening-bot/1.0"
        def close(self): pass
        def read(self, url, *, before_target_request, **kwargs):
            kind = outcomes(url) if outcomes else "success"
            if kind == "rejected":
                error = RuntimeError("governed policy refusal")
                error.envelope = SimpleNamespace(model_dump=lambda **kwargs: {"reason_code": "policy.refused"})
                raise error
            before_target_request(url, SimpleNamespace(decision_id="authorized"))
            sends.append(url)
            if kind == "incomplete":
                raise OSError("network temporarily unavailable")
            return SimpleNamespace(final_url=url, status_code=200, fit_markdown="Climate risk evidence",
                                   markdown="", content_text="", raw_html="", metadata_json={"links": []})
    class Crawler:
        def __init__(self, *, fetch_mode, read_gateway): self.gateway = read_gateway
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def fetch_page(self, url, **kwargs):
            if interrupt_at is not None and len(sends) == interrupt_at:
                raise KeyboardInterrupt("simulated worker crash after completed receipts")
            return self.gateway.read(url)
    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(adapter, "_load_gateway_builder", lambda: lambda **kwargs: Gateway())
    monkeypatch.setattr(adapter, "_load_web_listening", lambda: (Crawler, {
        "compute_hash": lambda text: "a" * 64, "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
        "find_new_links": lambda old, new: [], "find_document_links": lambda links: [],
    }))


def test_116_seed_crash_resume_has_no_duplicate_completed_sends(tmp_path, monkeypatch):
    from climate_monitor import web_listening_adapter as adapter
    from climate_monitor.config import load_sources, load_site_scopes
    sources = load_sources("monitoring/supranational_sources.yaml")
    scopes = load_site_scopes("monitoring/site_scopes.yaml")
    sends = []
    first = budget(tmp_path, fetch=1200)
    seed_runtime(monkeypatch, sends, interrupt_at=40)
    with pytest.raises(KeyboardInterrupt):
        adapter.collect_website_items_with_evidence(sources, state_dir=tmp_path / "seeds",
                                                   site_scopes=scopes, budget=first)
    assert len(sends) == 40
    # No graceful finish: attempt 2 reconciles the interrupted attempt's time.
    resumed = budget(tmp_path, fetch=1200, attempt=2)
    seed_runtime(monkeypatch, sends)
    _, warnings, evidence = adapter.collect_website_items_with_evidence(sources,
        state_dir=tmp_path / "seeds", site_scopes=scopes, budget=resumed)
    assert len(sends) == 116
    from collections import Counter
    scopes_by_key = {scope.source_key: scope for scope in scopes}
    expected = [url for source in sources for url in adapter._seed_urls(source, scopes_by_key.get(source.key))]
    # There are 116 selected source/seed slots but 114 distinct URLs: two URLs
    # belong to multiple institutions. Preserve those slots, without retries.
    assert Counter(sends) == Counter(expected)
    assert resumed.usage()["fetch_attempts"] == 116
    assert len(evidence["source_results"]) == 36
    assert evidence["full_success"] and not warnings


def test_completed_with_gaps_status_never_says_report_completed(tmp_path):
    from test_issue94_management_console import _store, _definition
    from climate_monitor.management import ManagementService
    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor="operator")
    service = ManagementService(store=store, runtime_root=tmp_path / "runs", launcher=lambda b: 4321)
    started = service.start(trigger="manual")
    root = service._run_dir(started["run_id"])
    (root / "attempt-1-result.json").write_text(json.dumps({"exit_code": 0, "retryable": False,
        "execution_complete": True, "full_coverage": False, "error": "unsupported article guard"}))
    value = service.progress(started["run_id"])
    assert value["stage"] == "completed_with_gaps"
    assert value["report_phase"] == "not_started"
    assert value["coverage"]["full_success"] is False


def test_mixed_run_round_trips_registry_status_and_blocks_report(tmp_path, monkeypatch):
    from test_issue94_management_console import _store, _definition
    from climate_monitor.management import ManagementService
    from climate_registry.acquisition import load_acquisition_batch, readback_source_outcomes, freeze_acquisition_for_report, AcquisitionIncompleteError
    import scripts.run_agent_acquisition as runner
    store = _store(tmp_path)
    definition = _definition(tmp_path)
    definition["parameters"]["source_keys"] = ["iais", "wmo", "ipcc"]
    store.save(definition, actor="operator")
    service = ManagementService(store=store, runtime_root=tmp_path / "runs", launcher=lambda b: 4321)
    started = service.start(trigger="manual")
    b = service.binding(started["run_id"])
    root = service._run_dir(started["run_id"])
    path = root / "attempt-1.json"
    sends = []
    # This binding owns a test-only checkpoint root; no shared repository state.
    b['report_inputs']['state_dir'] = str(tmp_path / 'source-state')
    path.write_text(json.dumps(b))
    seed_runtime(monkeypatch, sends, outcomes=lambda url:
                 "rejected" if "wmo.int" in url else "incomplete" if "ipcc.ch" in url else "success")
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/true")
    monkeypatch.setattr(runner, "_trusted_tool_events", lambda *args, **kwargs: [])
    def invoke(command, response_path, binding_path, binding, deadline):
        payload = {"schema_version": "pre-report-acquisition-batch.v1", "batch_id": b["acquisition_batch_id"],
                   "report_date": b["report_date"], "date_policy": b["date_policy"],
                   "started_at": b["created_at"], "completed_at": b["created_at"],
                   "search_decision": {"status": "no_search", "reason": "Fixture requests source diagnostics only; no supplemental query was executed"},
                   "searches": [], "items": []}
        response_path.write_text(json.dumps({"acquisition_batch": payload}))
        return 0
    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    monkeypatch.setattr(runner, "_run_report", lambda *args: pytest.fail("gaps must not dispatch report"))
    assert runner._execute_locked(path) == 0
    payload = json.loads((root / "attempt-1-acquisition.json").read_text())
    stored = load_acquisition_batch(b["registry_database"], b["acquisition_batch_id"])
    assert stored["completed_at"] is None
    rows = readback_source_outcomes(b["registry_database"], payload)
    assert {row["coverage_status"] for row in rows} == {"success", "rejected", "incomplete"}
    with pytest.raises(AcquisitionIncompleteError):
        freeze_acquisition_for_report(b["registry_database"], b["acquisition_batch_id"], report_date=b["report_date"])
    status = service.progress(started["run_id"])
    assert status["stage"] == "completed_with_gaps"
    assert status["coverage"]["execution_complete"] is True
    assert status["coverage"]["full_success"] is False
    assert status["coverage"]["rejected_sources"] == 1
    assert status["coverage"]["incomplete_sources"] == 1
    assert status["budget"]["used"]["fetch_attempts"] == len(sends)
    assert status["budget"]["used"]["search_attempts"] == 0
    assert not Path(b["frozen_report_input"]).exists()


def test_supported_article_redirect_is_guarded_at_public_provider_seam(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter as article
    sends = []
    def reader(url, *, before_target_request, timeout_seconds, **kwargs):
        for target in (url, url + 'redirect'):
            before_target_request(target, None)
            sends.append(target)
        pytest.fail('redirect over the boundary must not complete')
    monkeypatch.setattr(article, '_import_public_reader', lambda: SimpleNamespace(
        fetch_article_content=reader, runtime_data_dir=lambda: tmp_path))
    monkeypatch.setattr(article, '_load_site_scopes', lambda: {
        'example': SimpleNamespace(seed_urls=['https://example.test/'])})
    monkeypatch.setattr(article, '_prepare_public_configuration', lambda *args: (
        SimpleNamespace(site_key='example', model_dump=lambda **kwargs: {}), tmp_path / 'scope.yaml'))
    ledger = budget(tmp_path, fetch=1)
    record = article.fetch_article_content('a', 'https://example.test/', budget=ledger)
    assert sends == ['https://example.test/']
    assert record['status'] != 'ok'
    assert record['attempts'][0]['event_kind'] == 'precheck'
    assert ledger.usage()['fetch_attempts'] == 1


def test_retries_remain_spent_after_failure_and_resume(tmp_path):
    from climate_monitor.request_budget import GuardedGateway, RequestBudgetError
    sends = []
    class Gateway:
        def read(self, url, *, before_target_request, **kwargs):
            before_target_request(url, None)
            sends.append(url)
            raise OSError('transport failed')
    ledger = budget(tmp_path, fetch=20)
    for _ in range(2):
        with pytest.raises(OSError):
            GuardedGateway(Gateway(), ledger, 'seed').read('https://example.test/')
    ledger.finish()
    resumed = budget(tmp_path, fetch=20, attempt=2)
    with pytest.raises(OSError):
        GuardedGateway(Gateway(), resumed, 'seed').read('https://example.test/')
    with pytest.raises(RequestBudgetError, match='retry'):
        GuardedGateway(Gateway(), resumed, 'seed').read('https://example.test/')
    assert len(sends) == 3
    assert resumed.usage()['retries'] == 2
    assert resumed.usage(attempt=2)['retries'] == 1


def test_concurrent_claims_cannot_overdraw(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from climate_monitor.request_budget import RequestBudgetError
    ledger = budget(tmp_path, fetch=2)
    def claim(i):
        try:
            ledger.claim('http', f'https://example.test/{i}')
            return True
        except RequestBudgetError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, range(12))) == 2
    assert ledger.usage()['fetch_attempts'] == 2


def test_failed_search_releases_results_but_spends_attempt(tmp_path):
    from climate_monitor.request_budget import hook_decision
    ledger = budget(tmp_path, search=2)
    payload = {'hook_event_name': 'pre_tool_call', 'tool_name': 'web_search',
               'tool_input': {'query': 'climate', 'num_results': 5},
               'session_id': 's', 'extra': {'tool_call_id': '1'}}
    assert hook_decision(ledger, payload) == {}
    payload.update(hook_event_name='post_tool_call', extra={'tool_call_id': '1', 'status': 'error', 'result': 'failed'})
    assert hook_decision(ledger, payload) == {}
    assert ledger.usage()['search_attempts'] == 1
    assert ledger.usage()['search_results'] == 0
    assert ledger.usage()['search_results_reserved'] == 0
    payload.update(hook_event_name='pre_tool_call', extra={'tool_call_id': '2'})
    assert hook_decision(ledger, payload) == {}


def test_deleted_ledger_cannot_restart_same_attempt(tmp_path):
    ledger = budget(tmp_path)
    ledger.claim('http', 'https://example.test/')
    ledger.path.unlink()
    with pytest.raises(ValueError, match='missing'):
        budget(tmp_path)


def test_replayed_search_completion_cannot_release_spent_results(tmp_path):
    ledger = budget(tmp_path)
    ledger.claim('web_search', 'climate', call_id='call', results=5)
    ledger.complete_tool('call', {'results': [{'url': 'https://example.test/'}]}, 'ok')
    before = ledger.usage()['search_results']
    assert before == 1
    with pytest.raises(ValueError, match='completion'):
        ledger.complete_tool('call', None, 'error')
    assert ledger.usage()['search_results'] == before


def test_incompatible_gateway_hook_fails_once_before_bulk(tmp_path, monkeypatch):
    from climate_monitor import web_listening_adapter as adapter
    from test_issue117_governed_acquisition import install_runtime, source
    calls = install_runtime(monkeypatch)
    class Crawler:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def fetch_page(self, url, **kwargs):
            calls['fetch'].append(url)
            raise RuntimeError('reader cannot guard targets')
    monkeypatch.setattr(adapter, '_load_web_listening', lambda: (Crawler, {}))
    # The old public shape can read but cannot expose target sends.
    monkeypatch.setattr(adapter, '_load_gateway_builder', lambda: lambda **kwargs: SimpleNamespace(
        read=lambda url: None, close=lambda: None, user_agent='web-listening-bot/1.0'))
    with pytest.raises(RuntimeError, match='preflight.*before_target_request'):
        adapter.collect_website_items_with_evidence([source(), source('second')], state_dir=tmp_path)
    assert calls['fetch'] == []


def test_article_dependency_gap_round_trips_registry_and_status(tmp_path, monkeypatch):
    from test_issue94_management_console import _store, _definition
    from test_issue112_acquisition import _item, _batch
    from climate_monitor import article_content_adapter as article
    from climate_monitor.management import ManagementService
    from climate_registry.acquisition import load_acquisition_batch
    import scripts.run_agent_acquisition as runner
    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor='operator')
    service = ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: 4321)
    started = service.start(trigger='manual')
    b = service.binding(started['run_id'])
    root = service._run_dir(started['run_id'])
    path = root / 'attempt-1.json'
    monkeypatch.setattr(article, '_import_public_reader', lambda: SimpleNamespace(
        fetch_article_content=lambda url: pytest.fail('unguarded article provider invoked')))
    payload = _batch([_item(discovery_kind='site', discovery_ref='site:wmo', discovery_search_ref=None)],
        batch_id=b['acquisition_batch_id'], report_date=b['report_date'], searches=[],
        search_decision={'status': 'no_search', 'reason': 'No supplemental query was executed; article reader is unsupported'})
    payload['date_policy'] = b['date_policy']
    payload = runner._controlled_fetch_payload(path, b, payload)
    payload['completed_at'] = None
    runner._store_readback_and_freeze(b, payload, cumulative_actual=runner._empty_tool_usage(), allow_unresolved=True)
    runner._write_result(path, exit_code=0, retryable=False, error='Article dependency unsupported',
                         execution_complete=True, full_coverage=False)
    stored = load_acquisition_batch(b['registry_database'], b['acquisition_batch_id'])
    assert stored['items'][0]['attempts'][0]['event_kind'] == 'unsupported'
    status = service.progress(started['run_id'])
    assert status['stage'] == 'completed_with_gaps'
    assert 'before_target_request' in status['items'][0]['error']
    assert status['search_decision']['status'] == 'no_search'
    assert 'No supplemental query' in status['search_decision']['reason']
    assert status['budget']['used']['fetch_attempts'] == 0


def test_expired_article_work_retains_precheck_gap_without_reader(tmp_path, monkeypatch):
    from test_issue112_acquisition import _item
    from climate_monitor import article_content_adapter as article
    import scripts.run_agent_acquisition as runner
    monkeypatch.setattr(article, 'fetch_article_content', lambda *args, **kwargs: pytest.fail('expired handler invoked'))
    b = binding(tmp_path)
    payload = runner._controlled_fetch_payload(tmp_path / 'attempt-1.json', b,
        {'items': [_item()]}, deadline=runner.time.monotonic() - 1)
    assert payload['items'][0]['processing_status'] == 'failed'
    assert payload['items'][0]['evidence']['attempts'][0]['event_kind'] == 'precheck'


@pytest.mark.parametrize('tool', ['web_search', 'web_extract', 'browser_exec'])
def test_hermes_deadline_blocks_each_tool_without_dispatch(tmp_path, monkeypatch, tool):
    import climate_monitor.request_budget as module
    monkeypatch.setattr(module.time, 'time', lambda: 100.0)
    ledger = budget(tmp_path, runtime=10)
    monkeypatch.setattr(module.time, 'time', lambda: 110.0)
    directive = module.hook_decision(ledger, {'hook_event_name': 'pre_tool_call',
        'tool_name': tool, 'tool_input': {'query': 'climate', 'url': 'https://example.test/'},
        'session_id': 'test', 'extra': {'tool_call_id': '1'}})
    assert directive['action'] == 'block'
    assert ledger.usage()['fetch_attempts'] == ledger.usage()['search_attempts'] == 0
    assert ledger.events()[-1]['event_kind'] == 'precheck'


def test_incompatible_hermes_installation_never_launches_attempt(tmp_path, monkeypatch):
    import scripts.run_agent_acquisition as runner
    b = binding(tmp_path)
    b['provider'] = 'openai-codex'
    path = tmp_path / 'attempt-1.json'
    path.write_text(json.dumps(b))
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('incompatible attempt launched'))
    with pytest.raises(ValueError, match='Hermes Python'):
        runner._invoke_hermes(['/bin/true'], tmp_path/'response.txt', path, b, runner.time.monotonic()+60)


def test_lower_fetch_override_is_frozen(tmp_path):
    from climate_monitor.management import default_task_definition, build_task_binding
    from climate_registry.persistent import initialize_registry
    definition = default_task_definition()
    definition['parameters']['budgets']['fetch_attempts'] = 1
    definition['runtime'].update(run_root=str(tmp_path), registry_database=str(tmp_path/'registry.db'))
    initialize_registry(tmp_path/'registry.db')
    value = build_task_binding(definition, task_version=1, run_id='small', attempt=1)
    assert value['budgets']['fetch_attempts'] == 1
    assert value['governed_gateway']['budget_limit'] == 1
    assert len(value['source_inventory']['records']) == 36


def test_shared_seed_url_is_not_another_sources_retry(tmp_path, monkeypatch):
    from climate_monitor import web_listening_adapter as adapter
    from climate_monitor.models import MonitorSource
    from climate_monitor.request_budget import RequestBudget
    b = binding(tmp_path)
    b['budgets']['retries_per_item'] = 0
    ledger = RequestBudget(tmp_path/'request-budget.json', b)
    sources = [MonitorSource(key=key, abbreviation=key, full_name=key,
                             url='https://example.test/') for key in ('first', 'second')]
    sends = []
    seed_runtime(monkeypatch, sends)
    _, warnings, evidence = adapter.collect_website_items_with_evidence(
        sources, state_dir=tmp_path/'seeds', budget=ledger)
    assert sends == ['https://example.test/'] * 2
    assert evidence['full_success'] and not warnings
    assert ledger.usage()['retries'] == 0


@pytest.mark.parametrize('event', ['pre_tool_call', 'post_tool_call'])
@pytest.mark.parametrize('field', ['session_id', 'tool_call_id'])
@pytest.mark.parametrize('value', [None, '', '   ', 123])
def test_review_hook_requires_both_durable_ids(event, field, value):
    from climate_monitor.request_budget import hook_decision
    invoked = []
    ledger = SimpleNamespace(attempt=1, claim=lambda *a, **kw: invoked.append('claim'),
                             complete_tool=lambda *a: invoked.append('complete'))
    payload = {'hook_event_name': event, 'tool_name': 'web_search',
               'session_id': 'session', 'extra': {'tool_call_id': 'call'},
               'tool_input': {'query': 'climate'}}
    if field == 'session_id':
        payload[field] = value
    else:
        payload['extra'][field] = value
    assert hook_decision(ledger, payload).get('action') == 'block'
    assert not invoked


@pytest.mark.parametrize('tool', ['web_search', 'web_extract', 'browser_exec'])
def test_review_transcript_requires_completed_admission(tmp_path, tool):
    from test_issue94_management_console import _write_hermes_tool_events
    from climate_monitor.hermes_acquisition_hooks import attempt_home
    import scripts.run_agent_acquisition as runner
    b = binding(tmp_path)
    b['created_at'] = '2026-09-11T00:00:00Z'
    ledger = budget(tmp_path)
    result = {'results': [{'url': 'https://example.test/'}]}
    _write_hermes_tool_events(attempt_home(b), b, [
        {'tool': tool, 'tool_call_id': 'call', 'arguments': {}, 'result': result}])
    ledger.claim(tool, 'https://example.test/', call_id='1:session-1:call', results=1)
    with pytest.raises(ValueError, match='durable completion'):
        runner._trusted_tool_events(b)
    assert ledger.usage()['search_results'] == 0
    ledger.complete_tool('1:session-1:call', result, 'ok')
    assert len(runner._trusted_tool_events(b)) == 1
    if tool == 'web_search':
        assert ledger.usage()['search_results'] == 1
        assert ledger.usage()['search_results_reserved'] == 0


def test_review_failed_completion_is_not_marked_complete(tmp_path):
    ledger = budget(tmp_path)
    ledger.claim('web_search', 'climate', call_id='call', results=1)
    with pytest.raises(ValueError, match='exceeded'):
        ledger.complete_tool('call', {'results': [{'url': 'https://a.test/'}, {'url': 'https://b.test/'}]}, 'ok')
    assert not ledger.events()[0].get('completed')
    assert ledger.usage()['search_results_reserved'] == 1


def test_review_mixed_projection_matches_downstream_exports(tmp_path, monkeypatch):
    from climate_monitor import web_listening_adapter as adapter
    from climate_monitor.models import MonitorSource
    from scripts import run_agent_acquisition as runner, run_climate_monitor as monitor
    sources = [MonitorSource(key=key, abbreviation=key, full_name=key, url=f'https://{key}.test/')
               for key in ('success', 'rejected', 'incomplete')]
    seed_runtime(monkeypatch, [], outcomes=lambda url: url.split('//')[1].split('.')[0])
    _, _, context = adapter.collect_website_items_with_evidence(sources, state_dir=tmp_path/'seeds', budget=budget(tmp_path))
    b = {'source_inventory': {'records': [{'key': source.key} for source in sources]},
         'report_inputs': {key: str(tmp_path/(key+'.json')) for key in
                          ('acquisition_batch', 'web_listening_manifest', 'pillar_b_artifact')}}
    payload = {'items': [], 'report_date': '2026-09-07', 'date_policy': {},
               'search_decision': 'no_search', 'searches': []}
    runner._write_report_inputs(b, payload, context)
    outcomes = json.loads(Path(b['report_inputs']['acquisition_batch']).read_text())
    manifest_path = Path(b['report_inputs']['web_listening_manifest'])
    exports = json.loads(manifest_path.read_text())
    monitor._verify_collection_identity(outcomes, exports)
    assert len(exports) == 1 and len(outcomes) == 3
    diagnostics = json.loads(manifest_path.with_suffix('.diagnostics.json').read_text())
    assert {row['source']['source_id'] for row in diagnostics} == {'rejected', 'incomplete'}
    assert all(Path(row['artifact_path']).is_file() for row in context['source_results'])
