"""Issue #139: defaults apply at new-run boundaries, never at resume."""
import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from climate_monitor.management import ManagementService
from test_issue94_management_console import _definition, _store


@pytest.mark.parametrize('trigger', ['manual', 'scheduled'])
def test_new_legacy_task_run_inherits_hermes_defaults(tmp_path, trigger):
    store = _store(tmp_path)
    definition = _definition(tmp_path)
    saved = store.save(definition, actor='legacy')
    stored_bytes = store.active_path.read_bytes()
    service = ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: 123)
    started = service.start(trigger=trigger)
    binding = service.binding(started['run_id'])
    assert store.active_path.read_bytes() == stored_bytes
    assert store.load()['hashes'] == saved['hashes']
    assert store.load()['definition']['parameters']['provider'] == 'openai-codex'
    assert store.load()['definition']['parameters']['model'] == 'gpt-5.6-sol-900k'
    for configuration in (binding, binding['definition']['parameters'], binding['meeting']):
        assert 'provider' not in configuration
        assert 'model' not in configuration


@pytest.mark.parametrize('legacy_override', [False, True])
@pytest.mark.parametrize('active_enabled', [False, True])
def test_first_manual_meeting_uses_acquisition_snapshot(tmp_path, monkeypatch, legacy_override, active_enabled):
    import climate_monitor.management as management
    import climate_monitor.meetings as meetings

    store = _store(tmp_path)
    definition = _definition(tmp_path)
    definition['meeting'] = {'enabled': True, 'prompt': {'version': 'v1', 'text': 'frozen v1'}}
    saved = store.save(definition, actor='legacy')
    launched = []
    service = ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: 123,
                                meeting_launcher=lambda b: launched.append(b) or 456)
    started = service.start()
    binding = service.binding(started['run_id'])
    # Model both already-frozen historical bindings and bindings without overrides.
    for key in ('provider', 'model'):
        if legacy_override:
            binding['meeting'][key] = definition['parameters'][key]
        else:
            binding['meeting'].pop(key, None)
    (tmp_path / 'runs' / started['run_id'] / 'binding.json').write_text(json.dumps(binding))
    (tmp_path / 'runs' / started['run_id'] / 'attempt-1.json').write_text(json.dumps(binding))
    changed = copy.deepcopy(saved['definition'])
    changed['meeting'] = {'enabled': active_enabled, 'prompt': {'version': 'v2', 'text': 'current v2'}}
    store.save(changed, expected_version=1, actor='operator')
    monkeypatch.setattr(management, 'load_acquisition_batch', lambda *a: {})
    monkeypatch.setattr(meetings, 'active_meeting_run', lambda *a: None)
    service.start_meetings(started['run_id'])
    worker = launched[0]
    assert worker['task_version'] == 1
    assert worker['prompt_version'] == 'v1'
    assert worker['prompt_text'] == 'frozen v1'
    assert worker['prompt_sha256'] == hashlib.sha256(b'frozen v1').hexdigest()
    for key in ('provider', 'model'):
        assert (key in worker) == legacy_override
        if legacy_override:
            assert worker[key] == definition['parameters'][key]


def test_frozen_disabled_meeting_cannot_be_enabled_by_active_edit(tmp_path, monkeypatch):
    store = _store(tmp_path)
    saved = store.save(_definition(tmp_path), actor='legacy')
    service = ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: 123)
    started = service.start()
    changed = copy.deepcopy(saved['definition'])
    changed['meeting']['enabled'] = True
    store.save(changed, expected_version=1, actor='operator')
    with pytest.raises(RuntimeError, match='disabled.*frozen'):
        service.start_meetings(started['run_id'])


def test_legacy_resume_preserves_override_and_original_bytes(tmp_path):
    store = _store(tmp_path)
    definition = _definition(tmp_path)
    store.save(definition, actor='legacy')
    launched = []
    service = ManagementService(store=store, runtime_root=tmp_path / 'runs',
                                launcher=lambda b: launched.append(b) or 123)
    started = service.start()
    binding = service.binding(started['run_id'])
    # A historical binding has the override in all three locations.
    for configuration in (binding, binding['definition']['parameters'], binding['meeting']):
        configuration.update({key: definition['parameters'][key] for key in ('provider', 'model')})
    from climate_monitor.management import _sha, effective_task
    binding['definition_sha256'] = _sha(binding['definition'])
    binding['effective_sha256'] = _sha(effective_task(binding['definition']))
    run_dir = tmp_path / 'runs' / started['run_id']
    original = json.dumps(binding).encode()
    (run_dir / 'binding.json').write_bytes(original)
    (run_dir / 'attempt-1.json').write_bytes(original)
    (run_dir / 'attempt-1-result.json').write_text(json.dumps({'exit_code': 75, 'retryable': True}))
    changed = store.load()['definition']
    changed['parameters'].update(provider='openai-api', model='changed')
    store.save(changed, expected_version=1, actor='operator')
    assert service.resume(started['run_id'])['attempt'] == 2
    resumed = launched[-1]
    for key in ('definition', 'meeting', 'provider', 'model', 'task_version', 'definition_sha256', 'effective_sha256'):
        assert resumed[key] == binding[key]
    assert (run_dir / 'binding.json').read_bytes() == original
    assert (run_dir / 'attempt-1.json').read_bytes() == original


@pytest.mark.parametrize('override', [False, True])
def test_automatic_meeting_worker_preserves_optional_identity(tmp_path, monkeypatch, override):
    import scripts.run_agent_acquisition as runner
    import scripts.run_meeting_extraction as worker
    import climate_monitor.meetings as meetings
    from climate_monitor.management import build_task_binding

    definition = _definition(tmp_path)
    definition['meeting']['enabled'] = True
    binding = build_task_binding(definition, task_version=1, run_id='automatic', attempt=1)
    from climate_monitor.hermes_identity import create_snapshot
    binding['hermes_snapshot'] = create_snapshot(tmp_path, source='climate-acquisition-automatic')
    (tmp_path / 'attempt-1.json').write_text(json.dumps(binding))
    if override:
        binding['meeting'].update(provider='openai-codex', model='legacy-model')
    monkeypatch.setattr(meetings, 'active_meeting_run', lambda *a: None)
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: type('Child', (), {'pid': 123, 'wait': lambda self: 0})())
    result = runner._launch_meeting_worker(tmp_path / 'attempt-1.json', binding)
    assert result['status'] == 'launched'
    frozen = json.loads(next(tmp_path.glob('meeting-auto-*.json')).read_text())
    for key in ('provider', 'model'):
        assert (key in frozen) == override
    calls = []
    monkeypatch.setattr(worker, 'process_batch', lambda *a, **k: calls.append(k) or {'status': 'succeeded'})
    worker.run(frozen)
    assert calls[0]['provider'] == ('openai-codex' if override else '')
    assert calls[0]['model'] == ('legacy-model' if override else '')


def test_defaults_reach_hermes_and_report_commands(tmp_path, monkeypatch):
    import scripts.run_agent_acquisition as runner
    from climate_monitor.management import build_task_binding
    from scripts.run_climate_monitor import _load_task_binding

    binding = build_task_binding(_definition(tmp_path), task_version=1, run_id='defaults', attempt=1)
    run_dir = tmp_path / 'runs' / 'defaults'
    run_dir.mkdir()
    path = run_dir / 'attempt-1.json'
    path.write_text(json.dumps(binding))
    assert _load_task_binding(str(path))[0] == json.loads(path.read_text())
    command = runner._hermes_command('hermes', binding, tmp_path / 'prompt')
    assert '--provider' not in command and '--model' not in command
    calls = []
    monkeypatch.setattr(runner.subprocess, 'run', lambda command, **k: calls.append(command) or type('Result', (), {'returncode': 0})())
    assert runner._run_report(path, binding) == 0
    assert '--model-provider' not in calls[0] and '--model' not in calls[0]
    monkeypatch.setenv('HERMES_INFERENCE_PROVIDER', 'openai-api')
    monkeypatch.setenv('HERMES_INFERENCE_MODEL', 'ambient-model')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-credential')
    environment = runner._minimal_environment('')
    assert environment['HERMES_INFERENCE_MODEL'] == 'ambient-model'
    assert environment['OPENAI_API_KEY'] == 'test-credential'


@pytest.mark.parametrize('override', [False, True])
def test_manual_retry_reuses_persisted_failed_identity_after_active_disable(tmp_path, monkeypatch, override):
    import scripts.run_meeting_extraction as worker
    from test_issue136_meetings import _database

    store = _store(tmp_path)
    definition = _definition(tmp_path)
    definition['meeting'] = {'enabled': True, 'prompt': {'version': 'v1', 'text': 'frozen v1'}}
    store.save(definition, actor='legacy')
    launched = []
    service = ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: 123,
                                meeting_launcher=lambda b: launched.append(b) or 456)
    started = service.start()
    binding = service.binding(started['run_id'])
    meeting_root = tmp_path / 'meeting-db'
    meeting_root.mkdir()
    binding['registry_database'] = str(_database(meeting_root, ['Climate meeting evidence']))
    binding['acquisition_batch_id'] = 'batch'
    if override:
        binding['meeting'].update(provider='openai-codex', model='legacy-model')
    run_dir = tmp_path / 'runs' / started['run_id']
    for name in ('binding.json', 'attempt-1.json'):
        (run_dir / name).write_text(json.dumps(binding))
    service.start_meetings(started['run_id'])
    first = launched[-1]

    def failing_extractor(*args, **kwargs):
        def fail(request):
            raise ValueError('temporary extraction failure')
        return fail

    monkeypatch.setattr(worker, '_extractor', failing_extractor)
    failed = worker.run(first)
    assert failed['status'] == 'failed'
    changed = store.load()['definition']
    changed['meeting'] = {'enabled': False, 'prompt': {'version': 'v2', 'text': 'active v2'}}
    changed['parameters'].update(provider='openai-api', model='active-model')
    store.save(changed, expected_version=1, actor='operator')
    result = service.start_meetings(started['run_id'], retry_failed=True)
    retry = launched[-1]
    assert retry['retry_meeting_run_id'] == result['retry_meeting_run_id'] == failed['meeting_run_id']
    for key in ('task_version', 'prompt_version', 'prompt_sha256', 'prompt_text'):
        assert retry[key] == first[key]
    for key in ('provider', 'model'):
        assert (key in retry) == override
        assert retry.get(key) == first.get(key)
    identities = []
    monkeypatch.setattr(worker, '_extractor', lambda provider, model, binding_path=None, **kwargs: identities.append((provider, model)) or (lambda request: {'events': []}))
    retried = worker.run(retry)
    assert retried['status'] == 'succeeded'
    assert identities == [('openai-codex', 'legacy-model') if override else ('', '')]


def test_managed_report_authoring_omits_flags_despite_cli_and_ambient_identity(tmp_path, monkeypatch):
    from datetime import date
    from pathlib import Path
    from types import SimpleNamespace
    from climate_monitor.management import build_task_binding
    from climate_monitor.models import CandidateItem
    from scripts import run_climate_monitor as monitor
    from tests.test_issue87_post_pr106 import _view_evidence, _help_with_query_file

    binding = build_task_binding(_definition(tmp_path), task_version=1, run_id='authoring', attempt=1)
    run_dir = tmp_path / 'runs' / 'authoring'
    run_dir.mkdir()
    binding_path = run_dir / 'attempt-1.json'
    from climate_monitor.hermes_identity import create_snapshot
    binding['hermes_snapshot'] = create_snapshot(run_dir)
    binding_path.write_text(json.dumps(binding))
    evidence = _view_evidence('Environmental article without an insurance connection.')
    item = CandidateItem(title='Environment', url=evidence['records'][0]['requested_url'],
                         summary='', source_name='', lane='website')
    request = monitor.build_authoring_request(
        report_date=date(2026, 9, 7), items=[item], prompt=monitor.load_weekly_monitor_prompt(),
        article_evidence=evidence,
        stats={'total': 1, 'updated': 1, 'unchanged': 0, 'blocked': 0, 'failed': 0, 'unresolved': 0})
    for name, payload in (('article_evidence.json', evidence), ('v2_authoring_request.json', request), ('bundle.json', {})):
        (tmp_path / name).write_text(json.dumps(payload))
    # This isolates invocation through the serial authoring driver; immutable
    # loading is real, while staging validation has dedicated compatibility tests.
    monkeypatch.setattr(monitor, '_verify_authoring_resume', lambda *a: None)
    monkeypatch.setattr(monitor, '_verify_candidate_selection', lambda *a: None)
    prompts = monitor._frozen_authoring_components(binding, binding_path)
    monkeypatch.setattr(monitor, '_read_staging_bundle', lambda *a: {'authoring_prompts': prompts})
    monkeypatch.setattr(monitor, '_run_finalize', lambda *a: 0)
    commands = []

    real_subprocess_run = subprocess.run
    def hermes(command, **kwargs):
        if '-c' in command:
            return real_subprocess_run(command, **kwargs)
        if '--help' in command:
            return SimpleNamespace(returncode=0, stdout=_help_with_query_file())
        from hermes_offline_runtime import successful_api_lifecycle
        successful_api_lifecycle(Path(kwargs['cwd']), kwargs['env'])
        commands.append(command)
        auth = Path(kwargs['cwd']) / 'auth.json'
        auth.write_text(json.dumps({'providers': {'test': {'refresh_token': __import__('secrets').token_hex(24)}}}))
        return SimpleNamespace(returncode=0, stderr='session_id: 20260908_120234_3b2f4b\n', stdout=json.dumps({
            'climate_related': True, 'actuarial_related': False, 'summary': '',
            'summary_basis': 'none', 'evidence_hash': None, 'categories': [], 'keywords': []}))

    monkeypatch.setattr(subprocess, 'run', hermes)
    monkeypatch.setenv('HERMES_INFERENCE_PROVIDER', 'ambient-provider')
    monkeypatch.setenv('HERMES_INFERENCE_MODEL', 'ambient-model')
    args = SimpleNamespace(staging_dir=str(tmp_path), task_binding=str(binding_path),
                           model='cli-model', model_provider='cli-provider', authoring_timeout=5)
    assert monitor._run_authoring_sequence(args, None) == 0
    assert len(list((run_dir / 'hermes-private/auth-generations').glob('*.json'))) == 2
    monitor._managed_inference_runtime(args)  # report resume verifies the sealed refresh
    assert len(commands) == 1
    assert '--provider' not in commands[0] and '--model' not in commands[0]
    assert args.model == args.model_provider == ''


def test_retry_attaches_to_persisted_worker_without_legacy_meeting_snapshot(tmp_path, monkeypatch):
    import climate_monitor.management as management
    import climate_monitor.meetings as meetings

    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor='legacy')
    service = ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: 123)
    started = service.start()
    binding = service.binding(started['run_id'])
    binding.pop('meeting')
    run_dir = tmp_path / 'runs' / started['run_id']
    for name in ('binding.json', 'attempt-1.json'):
        (run_dir / name).write_text(json.dumps(binding))
    monkeypatch.setattr(management, 'load_acquisition_batch', lambda *a: {})
    monkeypatch.setattr(meetings, 'active_meeting_run', lambda *a: {
        'meeting_run_id': 'persisted-worker', 'task_version': 7,
        'prompt_version': 'persisted-v7', 'prompt_sha256': 'a' * 64,
    })
    result = service.start_meetings(started['run_id'], retry_failed=True)
    assert result['attached'] is True
    assert result['meeting_run_id'] == 'persisted-worker'
    assert result['task_version'] == 7
    assert result['prompt_version'] == 'persisted-v7'
    assert result['prompt_sha256'] == 'a' * 64


@pytest.mark.parametrize('missing', ['provider', 'model'])
def test_identity_pair_task_definition_rejects_one_field(tmp_path, missing):
    from climate_monitor.management import validate_task_definition

    definition = _definition(tmp_path)
    definition['parameters'].pop(missing)
    with pytest.raises(ValueError, match='provider and model'):
        validate_task_definition(definition)


@pytest.mark.parametrize('identity', [
    {'provider': 'legacy-provider'}, {'model': 'legacy-model'},
    {'provider': 'legacy-provider', 'model': ''},
    {'provider': None, 'model': 'legacy-model'},
])
def test_identity_pair_worker_rejects_invalid_identity_before_extraction(tmp_path, monkeypatch, identity):
    import scripts.run_meeting_extraction as worker

    binding = {
        'schema_version': 'climate-meeting-worker-binding.v1',
        'acquisition_run_id': 'run', 'acquisition_batch_id': 'batch',
        'registry_database': str(tmp_path / 'unused.sqlite3'), 'meeting_attempt': 1,
        'retry_failed': False, 'retry_meeting_run_id': None, 'task_version': 1,
        'prompt_version': 'v1', 'prompt_text': 'prompt',
        'prompt_sha256': hashlib.sha256(b'prompt').hexdigest(), **identity,
    }
    monkeypatch.setattr(worker, '_extractor', lambda *a: pytest.fail('invalid identity reached extractor'))
    monkeypatch.setattr(worker, 'process_batch', lambda *a, **k: pytest.fail('invalid identity reached persistence'))
    with pytest.raises(ValueError, match='provider and model'):
        worker.run(binding)


@pytest.mark.parametrize('provider,model', [('legacy-provider', ''), ('', 'legacy-model')])
def test_identity_pair_process_rejects_partial_identity_before_database(tmp_path, monkeypatch, provider, model):
    import climate_monitor.meetings as meetings

    monkeypatch.setattr(meetings, '_meeting_batch_lock', lambda *a, **k: pytest.fail('invalid identity reached database'))
    with pytest.raises(ValueError, match='provider and model'):
        meetings.process_batch(tmp_path / 'unused.sqlite3', 'batch', prompt_text='prompt',
                               prompt_version='v1', provider=provider, model=model,
                               extractor=lambda request: pytest.fail('invalid identity reached Hermes'))


@pytest.mark.parametrize('missing', ['provider', 'model'])
@pytest.mark.parametrize('retry', [False, True])
def test_identity_pair_manual_projection_rejects_partial_frozen_or_persisted_identity(tmp_path, monkeypatch, missing, retry):
    import climate_monitor.management as management
    import climate_monitor.meetings as meetings

    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor='legacy')
    launched = []
    service = ManagementService(store=store, runtime_root=tmp_path / 'runs',
                                meeting_launcher=lambda b: launched.append(b) or 123)
    frozen = {'enabled': True, 'prompt_text': 'prompt', 'prompt_version': 'v1',
              'prompt_sha256': hashlib.sha256(b'prompt').hexdigest(),
              'provider': 'legacy-provider', 'model': 'legacy-model'}
    if retry:
        frozen[missing] = ''  # SQLite represents absent overrides with empty strings.
    else:
        frozen.pop(missing)
    monkeypatch.setattr(service, 'binding', lambda run_id: {
        'registry_database': 'unused.sqlite3', 'acquisition_batch_id': 'batch',
        'task_version': 1, 'meeting': frozen,
    })
    monkeypatch.setattr(management, 'load_acquisition_batch', lambda *a: {})
    monkeypatch.setattr(meetings, 'active_meeting_run', lambda *a: None)
    monkeypatch.setattr(meetings, 'meeting_retry_run', lambda *a, **k: {
        **frozen, 'task_version': 1, 'meeting_run_id': 'failed-run',
    })
    with pytest.raises(ValueError, match='provider and model'):
        service.start_meetings('run', retry_failed=retry)
    assert launched == []


@pytest.mark.parametrize('missing', ['provider', 'model'])
def test_identity_pair_automatic_projection_rejects_partial_frozen_identity(tmp_path, monkeypatch, missing):
    import scripts.run_agent_acquisition as runner
    import climate_monitor.meetings as meetings

    frozen = {'enabled': True, 'prompt_text': 'prompt', 'prompt_version': 'v1',
              'prompt_sha256': hashlib.sha256(b'prompt').hexdigest(),
              'provider': 'legacy-provider', 'model': 'legacy-model'}
    frozen.pop(missing)
    binding = {'run_id': 'run', 'acquisition_batch_id': 'batch', 'registry_database': 'unused.sqlite3',
               'task_version': 1, 'meeting': frozen}
    monkeypatch.setattr(meetings, 'active_meeting_run', lambda *a: None)
    launched = []
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: launched.append(a) or type('Child', (), {'pid': 123, 'wait': lambda self: 0})())
    result = runner._launch_meeting_worker(tmp_path / 'attempt-1.json', binding)
    assert result['status'] == 'launch_failed'
    assert 'provider and model' in result['error']
    assert launched == []
    assert not [path for path in tmp_path.glob('meeting-auto-*.json') if not path.name.endswith('-result.json')]


@pytest.mark.parametrize('override', [False, True])
def test_identity_pair_task_definition_accepts_complete_or_absent_pair(tmp_path, override):
    from climate_monitor.management import validate_task_definition

    definition = _definition(tmp_path)
    if not override:
        for key in ('provider', 'model'):
            definition['parameters'].pop(key)
    normalized = validate_task_definition(definition)
    for key in ('provider', 'model'):
        assert (key in normalized['parameters']) == override
        if override:
            assert normalized['parameters'][key] == definition['parameters'][key]


@pytest.mark.parametrize('field', ['provider', 'model'])
@pytest.mark.parametrize('value', [None, 17, ' '])
def test_identity_pair_task_definition_requires_nonempty_strings(tmp_path, field, value):
    from climate_monitor.management import validate_task_definition

    definition = _definition(tmp_path)
    definition['parameters'][field] = value
    with pytest.raises(ValueError, match='provider and model'):
        validate_task_definition(definition)


@pytest.mark.parametrize('missing', ['provider', 'model'])
@pytest.mark.parametrize('retry', [False, True])
def test_identity_pair_process_rejects_partial_persisted_retry_or_recovery(tmp_path, missing, retry):
    import sqlite3
    from climate_monitor.meetings import process_batch
    from test_issue136_meetings import _database

    database = _database(tmp_path, ['Meeting evidence'])

    def fail(request):
        raise ValueError('temporary extraction failure')

    failed = process_batch(database, 'batch', prompt_text='prompt', prompt_version='v1',
                           provider='legacy-provider', model='legacy-model', extractor=fail)
    assert failed['status'] == 'failed'
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE meeting_runs SET {missing}='', status=?",
                           ('failed' if retry else 'running',))
    with pytest.raises(ValueError, match='provider and model'):
        process_batch(database, 'batch', prompt_text='prompt', prompt_version='v1',
                      provider='', model='', retry_failed=retry,
                      retry_meeting_run_id=failed['meeting_run_id'] if retry else None,
                      extractor=lambda request: pytest.fail('invalid persisted identity reached Hermes'))
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT count(*) FROM meeting_runs').fetchone()[0] == 1


@pytest.fixture
def ambient_route_installation(tmp_path, monkeypatch):
    import os
    import sys
    from types import SimpleNamespace
    import yaml
    import climate_monitor.hermes_acquisition_hooks as hooks
    from scripts.run_agent_acquisition import _hermes_command
    from climate_monitor.management import build_task_binding
    from test_issue94_management_console import _historical_binding

    ambient = tmp_path / 'ambient-hermes'
    ambient.mkdir(mode=0o700)
    route = {'default': 'sentinel-default-model', 'provider': 'sentinel-provider'}
    ambient_config = {
        'model': {**route, 'api_key': 'raw-model-credential',
                  'base_url': 'https://user:raw-url-credential@example.test',
                  'max_tokens': 999, 'extra_headers': {'Authorization': 'raw-header-credential'}},
        'api_key': 'raw-root-credential',
        'hooks_auto_accept': False, 'hooks': {'ambient-hook': 'untrusted-command'},
        'mcp_servers': {'ambient-mcp': {}},
        'memory': {'memory_enabled': True, 'user_profile_enabled': True},
        'plugins': {'enabled': ['ambient-plugin']}, 'terminal': {'backend': 'ambient-terminal'},
        'tools': {'tool_search': {'enabled': 'on'}},
        'providers': {'ambient-provider': {'api_key': 'raw-provider-credential'}},
        'custom_providers': [{'name': 'ambient-custom', 'api_key': 'raw-custom-credential'}],
        'unrelated': 'ambient-setting',
    }
    (ambient / 'config.yaml').write_text(yaml.safe_dump(ambient_config))
    (ambient / 'auth.json').write_text('{"credential": "raw-auth-credential"}')
    monkeypatch.delenv('HERMES_INFERENCE_MODEL', raising=False)
    monkeypatch.delenv('HERMES_INFERENCE_PROVIDER', raising=False)
    from pathlib import Path
    executable = Path(os.environ['HERMES_EXECUTABLE'])

    def install(*, legacy=False, encoding='default'):
        expected_route = dict(route)
        if encoding == 'scalar':
            ambient_config['model'] = route['default']
            expected_route.pop('provider')
        elif encoding == 'alias':
            ambient_config['model']['model'] = ambient_config['model'].pop('default')
        elif encoding == 'nested':
            ambient_config['model']['default'] = {
                'model': route['default'], 'provider': route['provider'],
                'api_key': 'raw-nested-credential',
                'base_url': 'https://user:raw-nested-url-credential@example.test',
                'extra_headers': {'Authorization': 'raw-nested-header-credential'},
            }
            ambient_config['model']['provider'] = 'outer-provider-must-not-win'
        (ambient / 'config.yaml').write_text(yaml.safe_dump(ambient_config))
        definition = _definition(tmp_path)
        builder = _historical_binding if legacy else build_task_binding
        binding = builder(definition, task_version=1, run_id='route', attempt=1)
        run_dir = Path(binding['checkpoint_dir']).parent
        run_dir.mkdir(parents=True, mode=0o700)
        binding_path = run_dir / 'attempt-1.json'
        command = _hermes_command(str(executable), binding, tmp_path / 'prompt.txt')
        environment = {'PATH': os.environ['PATH'], 'HERMES_HOME': str(ambient),
                       'OPENAI_API_KEY': 'raw-env-credential'}
        from climate_monitor.hermes_identity import create_snapshot
        for name in ('config.yaml', 'auth.json'):
            (ambient / name).chmod(0o600)
        binding['hermes_snapshot'] = create_snapshot(run_dir, environ={**environment, 'HERMES_EXECUTABLE': str(executable)})
        binding_path.write_text(json.dumps(binding))
        expected_route = ambient_config['model']
        if encoding in ('alias', 'nested'):
            expected_route = dict(expected_route)
            expected_route.pop('model', None)
            expected_route['default'] = 'sentinel-default-model'
        # Hermes normalizes aliases before inference. An explicitly configured
        # outer provider wins over a nested provider in both supported releases.
        # Only the external Hermes hook-runtime verifier is stubbed.
        # The installer, generated files, argv and child environment are real.
        with monkeypatch.context() as verifier:
            verifier.setattr(hooks.subprocess, 'run', lambda *a, **k: SimpleNamespace(
                returncode=0, stdout='climate acquisition hooks verified\n', stderr=''))
            child_env, home = hooks.install_hooks(command, binding_path, binding, environment)
        return command, child_env, home, expected_route

    return install


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('encoding', ['default', 'scalar', 'alias', 'nested'])
def test_frozen_route_is_preserved_with_optional_legacy_cli_override(ambient_route_installation, legacy, encoding):
    import sys
    command, environment, home, route = ambient_route_installation(legacy=legacy, encoding=encoding)
    assert 'HERMES_INFERENCE_MODEL' not in environment
    assert 'HERMES_INFERENCE_PROVIDER' not in environment
    # Read the actual isolated config in a fresh child with the returned HERMES_HOME.
    result = subprocess.run([sys.executable, '-c',
        'import json,os,pathlib,yaml; '
        'config=yaml.safe_load((pathlib.Path(os.environ["HERMES_HOME"])/"config.yaml").read_text()); '
        'print(json.dumps(config.get("model", {})))'],
        env=environment, capture_output=True, text=True, check=True)
    if legacy:
        assert json.loads(result.stdout) == route
        assert command[command.index('--provider') + 1] == 'openai-codex'
        assert command[command.index('--model') + 1] == 'gpt-5.6-sol-900k'
    else:
        assert json.loads(result.stdout) == route
        assert '--provider' not in command and '--model' not in command


@pytest.mark.parametrize('encoding', ['default', 'scalar', 'alias', 'nested'])
def test_frozen_route_retains_private_credentials_but_excludes_unrelated_settings(ambient_route_installation, encoding):
    import yaml
    from climate_monitor.hermes_acquisition_hooks import SEARCH_IDENTITY_PLUGIN_ID

    command, environment, home, route = ambient_route_installation(encoding=encoding)
    raw = (home / 'config.yaml').read_text()
    config = yaml.safe_load(raw)
    assert 'raw-root-credential' not in raw
    assert 'ambient-setting' not in raw
    assert 'ambient-hook' not in raw
    assert 'ambient-mcp' not in raw
    assert 'ambient-plugin' not in raw
    assert set(config) <= {'model', 'providers', 'custom_providers', 'hooks_auto_accept', 'hooks', 'mcp_servers', 'memory', 'plugins', 'tools'}
    assert config['hooks_auto_accept'] is True
    assert config['mcp_servers'] == {}
    assert config['memory'] == {'memory_enabled': False, 'user_profile_enabled': False}
    assert config['plugins'] == {'enabled': ['climate-frozen-identity', SEARCH_IDENTITY_PLUGIN_ID]}
    assert config['tools'] == {'tool_search': {'enabled': 'off'}}
    assert set(config['hooks']) == {'pre_tool_call', 'post_tool_call'}
    assert config['hooks']['pre_tool_call'][0]['fail_closed'] is True
    import shlex
    hook = shlex.split(config['hooks']['pre_tool_call'][0]['command'])
    assert hook[1:3] == ['-I', '-S']
    assert Path(hook[3]) == home.parent / 'bootstrap/launcher.py'
    assert hook[4] == '--budget-hook'
    assert (home.parent / 'acquisition/scripts/acquisition_budget_hook.py').is_file()
    assert environment['OPENAI_API_KEY'] == 'raw-env-credential'
    assert json.loads((home / 'auth.json').read_text()) == {'credential': 'raw-auth-credential'}
