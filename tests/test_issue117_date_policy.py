"""The agent may annotate a policy, but only the frozen policy is authoritative."""
import copy
from datetime import date

import pytest

from climate_registry.acquisition import PublicationDatePolicy
from scripts.run_agent_acquisition import _validate_agent_payload


@pytest.fixture(params=[{'mode': 'unlimited'}, {'mode': 'recent', 'days': 1},
                        {'mode': 'custom', 'start': '2026-09-01', 'end': '2026-09-07'}])
def bound_payload(request):
    now = '2026-09-07T08:00:00Z'
    policy = PublicationDatePolicy.resolve(request.param, anchor_date=date(2026, 9, 7), frozen_at=now).to_dict()
    binding = {'acquisition_batch_id': 'policy-canary', 'report_date': '2026-09-07',
               'date_policy': policy, 'source_inventory': {'records': []},
               'budgets': {'search_attempts': 2, 'search_results': 5, 'fetch_attempts': 30, 'retries_per_item': 2}}
    payload = {'schema_version': 'pre-report-acquisition-batch.v1',
               'batch_id': binding['acquisition_batch_id'], 'report_date': binding['report_date'],
               'started_at': now, 'completed_at': now, 'date_policy': copy.deepcopy(policy),
               'items': [], 'searches': [], 'search_decision': {
                   'status': 'no_search', 'reason': 'No supplemental search executed in this local fixture'}}
    return binding, payload


def test_additive_window_flag_is_stripped_before_registry(bound_payload, tmp_path):
    from climate_registry.persistent import initialize_registry
    from climate_registry.acquisition import store_acquisition_batch, load_acquisition_batch
    binding, payload = bound_payload
    payload['date_policy']['window_enabled'] = False
    original = copy.deepcopy(payload)
    validated = _validate_agent_payload(binding, payload, [])
    assert validated['date_policy'] == binding['date_policy']
    assert 'window_enabled' not in validated['date_policy']
    assert validated['date_policy'] is not binding['date_policy']
    assert payload == original
    assert PublicationDatePolicy.from_dict(validated['date_policy']).to_dict() == binding['date_policy']
    database = tmp_path/'registry.db'
    initialize_registry(database)
    store_acquisition_batch(database, validated)
    assert load_acquisition_batch(database, binding['acquisition_batch_id'])['date_policy'] == binding['date_policy']


@pytest.mark.parametrize('field', ['mode', 'anchor_date', 'frozen_at', 'start', 'end', 'days'])
def test_changed_bound_policy_field_is_rejected(bound_payload, field):
    binding, payload = bound_payload
    payload['date_policy'][field] = 'changed'
    with pytest.raises(ValueError, match='bound publication-date policy'):
        _validate_agent_payload(binding, payload)


@pytest.mark.parametrize('field', ['mode', 'anchor_date', 'frozen_at', 'start', 'end', 'days'])
def test_missing_bound_policy_field_is_rejected(bound_payload, field):
    binding, payload = bound_payload
    del payload['date_policy'][field]
    with pytest.raises(ValueError, match='bound publication-date policy'):
        _validate_agent_payload(binding, payload)


@pytest.mark.parametrize('corruption', [True, 1.0, '1', [], {}, None])
def test_bound_integer_type_cannot_be_coerced(corruption):
    policy = PublicationDatePolicy.resolve({'mode': 'recent', 'days': 1},
        anchor_date=date(2026, 9, 7), frozen_at='2026-09-07T08:00:00Z').to_dict()
    binding = {'acquisition_batch_id': 'b', 'report_date': '2026-09-07', 'date_policy': policy,
               'source_inventory': {'records': []},
               'budgets': {'search_attempts': 2, 'search_results': 5, 'fetch_attempts': 30, 'retries_per_item': 2}}
    payload = {'batch_id': 'b', 'report_date': binding['report_date'], 'date_policy': {**policy, 'days': corruption},
               'items': [], 'searches': []}
    with pytest.raises(ValueError, match='bound publication-date policy'):
        _validate_agent_payload(binding, payload)


@pytest.mark.parametrize('corruption', [None, [], 'unlimited', False])
def test_non_object_policy_is_rejected(bound_payload, corruption):
    binding, payload = bound_payload
    payload['date_policy'] = corruption
    with pytest.raises(ValueError, match='bound publication-date policy'):
        _validate_agent_payload(binding, payload)
