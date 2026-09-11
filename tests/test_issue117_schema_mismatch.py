import copy
import json
from pathlib import Path
import pytest

from test_issue112_acquisition import _batch, _item
from scripts import run_agent_acquisition as runner
from climate_registry.acquisition import AcquisitionIncompleteError


def example(items=None):
    payload = _batch(items or [])
    binding = {'acquisition_batch_id': payload['batch_id'], 'report_date': payload['report_date'],
               'date_policy': copy.deepcopy(payload['date_policy']),
               'source_inventory': {'records': [{'key': 'Example Institute'}]},
               'budgets': {'fetch_attempts': 10, 'search_attempts': 2, 'search_results': 5, 'retries_per_item': 2}}
    alias = copy.deepcopy(payload)
    alias['articles'] = alias.pop('items')
    alias['fetch_attempts'] = alias.pop('searches')
    return binding, payload, alias


def test_equivalent_aliases_are_canonical_before_storage(tmp_path):
    from climate_registry.persistent import initialize_registry
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    b, canonical, alias = example([_item()])
    original = copy.deepcopy(alias)
    validated = runner._validate_agent_payload(b, alias)
    assert validated == canonical
    assert alias == original
    database = tmp_path/'registry.db'
    initialize_registry(database)
    store_acquisition_batch(database, validated)
    loaded = load_acquisition_batch(database, canonical['batch_id'])
    assert loaded['payload_sha256'] == runner._canonical_digest(canonical)
    assert len(loaded['items']) == len(loaded['searches']) == 1


@pytest.mark.parametrize('mutation', ['missing', 'not_list', 'http_attempt', 'incomplete_item', 'selected_type', 'content_hash', 'search_budget_type'])
def test_unproved_aliases_require_correction(mutation):
    b, _, alias = example([_item()])
    if mutation == 'missing': del alias['fetch_attempts']
    elif mutation == 'not_list': alias['articles'] = {}
    elif mutation == 'http_attempt': alias['fetch_attempts'] = [{'url': 'https://example.org/', 'status': 200}]
    elif mutation == 'incomplete_item': alias['articles'] = [{'url': 'https://example.org/'}]
    elif mutation == 'selected_type': alias['articles'][0]['selected'] = 1
    elif mutation == 'content_hash': alias['articles'][0]['evidence']['content_hash'] = 'a'*64
    else: alias['fetch_attempts'][0]['budget']['max_results'] = True
    with pytest.raises(AcquisitionIncompleteError, match='Response contract correction required'):
        runner._validate_agent_payload(b, alias)


@pytest.mark.parametrize('key,alias_key', [('items', 'articles'), ('searches', 'fetch_attempts')])
def test_canonical_alias_conflicts_are_rejected(key, alias_key):
    b, canonical, _ = example()
    canonical[alias_key] = [{}]
    with pytest.raises(AcquisitionIncompleteError, match='conflicting'):
        runner._validate_agent_payload(b, canonical)


def test_alias_search_provenance_is_not_bypassed():
    b, canonical, alias = example()
    event = {'tool': 'web_search', 'arguments': {'query': 'agent chosen query'},
             'result': {'results': [{'url': 'result-1'}]}}
    assert runner._validate_agent_payload(b, alias, [event]) == canonical
    with pytest.raises(ValueError, match='one-to-one'):
        runner._validate_agent_payload(b, alias, [])
    b['budgets']['search_attempts'] = 0
    with pytest.raises(ValueError, match='search-attempt budget'):
        runner._validate_agent_payload(b, alias, [event])


def test_response_shape_visible_even_with_old_bound_task(tmp_path):
    from test_issue94_management_console import _definition
    from climate_monitor.management import build_task_binding
    definition = _definition(tmp_path)
    definition['prompts']['acquisition_task']['text'] += '\nLegacy task text without field names.'
    b = build_task_binding(definition, task_version=1, run_id='contract', attempt=1)
    prompt = runner._prompt(tmp_path/'attempt-1.json', b)
    for name in ('"items"', '"searches"', '"search_decision"', '"discovery_search_ref"', '"processing_status"', '"evidence"', '"result_refs"'):
        assert name in prompt
    assert 'fetch_attempts is NOT a list of HTTP requests' in prompt
    assert 'Legacy task text without field names.' in prompt


def test_resume_prompt_includes_contract_correction(tmp_path):
    from test_issue94_management_console import _definition
    from climate_monitor.management import build_task_binding
    b = build_task_binding(_definition(tmp_path), task_version=1, run_id='contract', attempt=2)
    error = 'Response contract correction required: searches must be a list'
    (tmp_path/'attempt-1-result.json').write_text(json.dumps({'error': error}))
    assert error in runner._prompt(tmp_path/'attempt-2.json', b)


def test_equal_duplicate_aliases_are_stripped():
    b, canonical, alias = example([_item()])
    assert runner._validate_agent_payload(b, {**canonical, **alias}) == canonical


def test_malformed_alias_is_retryable_without_resetting_seed_accounting(tmp_path, monkeypatch):
    from test_issue94_management_console import _store, _definition
    from test_issue117_request_boundaries import seed_runtime
    from climate_monitor.management import ManagementService
    from climate_monitor.request_budget import ledger_path
    store = _store(tmp_path)
    definition = _definition(tmp_path)
    definition["parameters"]["source_keys"] = ["iais"]
    store.save(definition, actor="operator")
    service = ManagementService(store=store, runtime_root=tmp_path / "runs", launcher=lambda b: 4321)
    started = service.start(trigger="manual")
    b = service.binding(started["run_id"])
    root = service._run_dir(started["run_id"])
    path = root / "attempt-1.json"
    b['report_inputs']['state_dir'] = str(tmp_path / 'source-state')
    path.write_text(json.dumps(b))
    sends = []
    seed_runtime(monkeypatch, sends)
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/true")
    monkeypatch.setattr(runner, "_trusted_tool_events", lambda *args, **kwargs: [])
    def invoke(command, response_path, binding_path, binding, deadline):
        payload = {"batch_id": b["acquisition_batch_id"], "report_date": b["report_date"],
                   "date_policy": b["date_policy"], "articles": [],
                   "fetch_attempts": [{"url": "https://example.org/", "status": 200}]}
        response_path.write_text(json.dumps({"acquisition_batch": payload}))
        return 0
    monkeypatch.setattr(runner, "_invoke_hermes", invoke)
    assert runner._execute_locked(path) == 75
    result = json.loads((root / "attempt-1-result.json").read_text())
    assert result['retryable'] is True
    assert 'Response contract correction required' in result['error']
    assert sends
    assert not (root / "attempt-1-acquisition.json").exists()
    b['attempt'] = 2
    path = root / 'attempt-2.json'
    path.write_text(json.dumps(b))
    prior_sends = list(sends)
    assert runner._execute_locked(path) == 75
    assert sends == prior_sends
    assert 'Response contract correction required' in (root / 'attempt-2.prompt.md').read_text()
    assert Path(ledger_path(b)).exists()


def test_unhashable_nested_alias_status_requires_correction():
    b, _, alias = example([_item()])
    alias['articles'][0]['evidence']['attempts'][0]['status'] = {}
    with pytest.raises(AcquisitionIncompleteError, match='Response contract correction required'):
        runner._validate_agent_payload(b, alias)
