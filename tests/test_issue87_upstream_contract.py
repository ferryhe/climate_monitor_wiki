"""Integration regressions against the installed public upstream contract."""
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts import run_climate_monitor as monitor


def public_inputs(tmp_path):
    contract = pytest.importorskip('web_listening.contracts.acquisition_batch')
    run = SimpleNamespace(id=2, status='completed', pages_seen=1, files_seen=0,
                          pages_changed=0, files_changed=0)
    outcome = contract.acquisition_batch_result_v2_from_scope_run(
        run, site_key='wri', requested_url='https://www.wri.org/insights',
        artifact_id='manifest-wri-2')
    manifest = {'schema_version': 'web-listening-manifest.v1',
                'manifest_id': 'manifest-wri-2',
                'run': {'run_id': 'run-2', 'parent_run_id': '2'},
                'source': {'source_id': 'wri', 'tree_seed_url': 'https://www.wri.org/insights'},
                'discovered_items': [
                    {'item_id': f'page-{i}', 'url': f'https://www.wri.org/insights/climate-{i}',
                     'title': f'Climate risk {i}', 'status': 'existing'} for i in range(3)]}
    paths = [tmp_path / name for name in ('outcome.json', 'manifest.json', 'pillar-b.json')]
    for path, payload in zip(paths, (outcome, manifest, [])):
        path.write_text(json.dumps(payload))
    return paths


def test_public_producer_scope_export_and_article_counts(tmp_path):
    op, mp, _ = public_inputs(tmp_path)
    outcome, manifest = monitor._read_outcome(op), monitor._read_manifest(mp)
    monitor._verify_same_run_identity(outcome, manifest)
    records = monitor._attach_outcome_disposition(
        monitor._collect_same_run_records(outcome, manifest), outcome)
    assert len(records) == 3
    assert {r['disposition'] for r in records} == {'unchanged'}
    assert monitor._derive_stats(records, outcome) == dict(
        total=1, updated=0, unchanged=1, blocked=0, failed=0, unresolved=0)


@pytest.mark.parametrize('mutation', ['parent', 'source', 'missing_parent', 'export', 'manifest_id'])
def test_public_identity_rejects_cross_scope_and_source(tmp_path, mutation):
    op, mp, _ = public_inputs(tmp_path)
    outcome, manifest = monitor._read_outcome(op), monitor._read_manifest(mp)
    if mutation == 'parent':
        manifest['run']['parent_run_id'] = '3'
    elif mutation == 'source':
        manifest['source']['source_id'] = 'ipcc'
    elif mutation == 'manifest_id':
        manifest['manifest_id'] = 'manifest-wri-other-export'
    elif mutation == 'export':
        manifest['run']['run_id'] = 'run-3'
    else:
        del manifest['run']['parent_run_id']
    with pytest.raises(SystemExit, match='identity'):
        monitor._verify_same_run_identity(outcome, manifest)


@pytest.mark.parametrize('seed', [None, '', '   ', 17, 'https://www.wri.org/wrong-scope'])
def test_public_identity_requires_matching_seed_before_staging(tmp_path, monkeypatch, seed):
    op, mp, bp = public_inputs(tmp_path)
    manifest = json.loads(mp.read_text())
    if seed is None:
        del manifest['source']['tree_seed_url']
    else:
        manifest['source']['tree_seed_url'] = seed
    mp.write_text(json.dumps(manifest))
    monkeypatch.setattr(monitor, 'build_article_evidence_artifact',
                        lambda *args, **kwargs: pytest.fail('evidence staging ran'))
    with pytest.raises(SystemExit, match='identity'):
        monitor._run_prepare(SimpleNamespace(
            acquisition_batch=str(op), web_listening_manifest=str(mp),
            pillar_b_artifact=str(bp), staging_dir=str(tmp_path / 'staging')), None)
    assert not (tmp_path / 'staging').exists()


def test_monitor_preflight_checks_consumed_contract(tmp_path, monkeypatch, capsys):
    from scripts import hermes_job
    op, mp, bp = public_inputs(tmp_path)
    partial = json.loads(op.read_text())
    del partial['summary']
    op.write_text(json.dumps(partial))
    for key, value in {
        'REPORT_DATE': '2026-09-07', 'CLIMATE_JOB_STATUS_DIR': str(tmp_path),
        'CLIMATE_STAGING_DIR': str(tmp_path / 'staging'),
        'CLIMATE_OUTCOME_ARTIFACT': str(op), 'CLIMATE_MANIFEST_ARTIFACT': str(mp),
        'CLIMATE_PILLAR_B_ARTIFACT': str(bp), 'CLIMATE_STATE_DIR': str(tmp_path),
        'CLIMATE_SOURCE_DIR': str(tmp_path), 'CLIMATE_WIKI_DIR': str(tmp_path),
        'CLIMATE_SOURCE_CONFIG': str(monitor.ROOT / 'monitoring/supranational_sources.yaml'),
        'CLIMATE_RUN_CONFIG': str(monitor.ROOT / 'monitoring/run_config.yaml'),
        'CLIMATE_SITE_SCOPES': str(monitor.ROOT / 'monitoring/site_scopes.yaml'),
    }.items():
        monkeypatch.setenv(key, value)
    assert hermes_job.main(['monitor', '--preflight']) == 2
    assert 'preflight_failed' in capsys.readouterr().out
    assert not (tmp_path / 'staging').exists()


@pytest.mark.parametrize('site_key,url', [(None, 'https://www.wri.org/insights/climate-risk'),
                                 (None, 'https://www.ipcc.ch/report/ar6/syr/'),
                                 (None, 'https://www.adb.org/news/climate-risk'),
                                 ('bis', 'https://www.bis.org/climate-risk'),
                                 ('bcbs', 'https://www.bis.org/climate-risk'),
                                 ('psi', 'https://www.unepfi.org/climate-risk'),
                                 ('fit', 'https://www.unepfi.org/climate-risk')])
@pytest.mark.parametrize('explicit_root', [False, True])
def test_default_reader_uses_real_governed_configuration(tmp_path, monkeypatch, explicit_root, site_key, url):
    article = pytest.importorskip('web_listening.blocks.article_content')
    from web_listening.contracts.tool_result import ToolResult
    from climate_monitor import article_content_adapter as adapter
    monkeypatch.setattr(article.settings, 'data_dir', tmp_path)
    # Preserve the actual public reader and compiler. Only the I/O gateway and
    # terminal transport are replaced; no provider is injected into climate.
    class Gateway:
        def close(self):
            pass
    monkeypatch.setattr('web_listening.blocks.governed_read.build_runtime_read_gateway',
                        lambda **kwargs: Gateway())
    calls = []
    def terminal(url, **kwargs):
        calls.append((url, kwargs))
        return ToolResult(ok=True, has_data=False, data_status='no_content', data_count=0,
                          tool='fetch_article_content', stop_reason='no_usable_content',
                          data={'requested_url': url}, attempts=[])
    monkeypatch.setattr(article, '_fetch_with_readers', terminal)
    result = adapter.build_article_evidence_artifact(
        [{'article_id': 'article', 'url': url, 'source_id': site_key}],
        report_date='2026-09-07', **({'data_root': tmp_path} if explicit_root else {}))
    assert len(calls) == 1, result['records']
    if site_key:
        assert calls[0][1]['profile'].site_key == site_key
    assert Path(calls[0][1]['output_dir']).is_relative_to(tmp_path)
    assert result['records'][0]['status'] != 'unavailable'


def test_prepare_threads_configured_upstream_root(tmp_path, monkeypatch):
    monkeypatch.delenv('WL_DATA_DIR', raising=False)
    from argparse import Namespace
    op, mp, bp = public_inputs(tmp_path)
    config = tmp_path / 'run.yaml'
    config.write_text('web_listening:\n  data_dir: ' + str(tmp_path / 'upstream-data') + '\n')
    captured = []
    original = monitor.build_article_evidence_artifact
    def capture(inputs, **kwargs):
        assert {item["source_id"] for item in inputs} == {"wri"}
        captured.append(kwargs)
        return original(inputs, **{**kwargs, 'providers': (lambda aid, url:
            {'status': 'unavailable', 'article_id': aid, 'requested_url': url},)})
    monkeypatch.setattr(monitor, 'build_article_evidence_artifact', capture)
    args = Namespace(acquisition_batch=str(op), web_listening_manifest=str(mp),
                     pillar_b_artifact=str(bp), staging_dir=str(tmp_path / 'staging'),
                     source_dir=str(tmp_path / 'sources'), state_dir=str(tmp_path / 'state'),
                     report_date='2026-09-07', run_config=str(config),
                     article_evidence_loopback='', json=False)
    monitor._run_prepare(args, None)
    assert captured[0]['data_root'] == tmp_path / 'upstream-data'


def test_production_wrapper_plans_without_preexisting_response(tmp_path, monkeypatch):
    from scripts import hermes_job
    op, mp, bp = public_inputs(tmp_path)
    for key, value in {
        'CLIMATE_STAGING_DIR': tmp_path / 'staging', 'CLIMATE_OUTCOME_ARTIFACT': op,
        'CLIMATE_MANIFEST_ARTIFACT': mp, 'CLIMATE_PILLAR_B_ARTIFACT': bp,
        'CLIMATE_STATE_DIR': tmp_path, 'CLIMATE_SOURCE_DIR': tmp_path, 'CLIMATE_WIKI_DIR': tmp_path,
        'CLIMATE_SOURCE_CONFIG': monitor.ROOT / 'monitoring/supranational_sources.yaml',
        'CLIMATE_RUN_CONFIG': monitor.ROOT / 'monitoring/run_config.yaml',
        'CLIMATE_SITE_SCOPES': monitor.ROOT / 'monitoring/site_scopes.yaml',
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv('CLIMATE_AUTHORING_RESPONSE', raising=False)
    monkeypatch.setenv('HERMES_INFERENCE_MODEL', 'gpt-6-astra')
    monkeypatch.setenv('HERMES_INFERENCE_PROVIDER', 'openai-codex')
    command = hermes_job.monitor_command('2026-09-07', dry_run=False)
    assert '--production-weekly' in command
    assert command[command.index('--authoring-mode') + 1] == 'run'
    assert '--article-evidence-loopback' not in command
    assert '--authoring-response' not in command
    assert command[command.index('--model') + 1] == 'gpt-6-astra'
    assert command[command.index('--model-provider') + 1] == 'openai-codex'


@pytest.mark.parametrize('reply', ['valid', 'absent', 'invalid'])
def test_production_cli_prepares_authors_once_then_finalizes(tmp_path, monkeypatch, reply):
    import sys
    from argparse import Namespace
    from scripts import hermes_job
    from climate_monitor.weekly_monitor.authoring_contract import (
        AUTHORING_RESPONSE_SCHEMA_VERSION_V2, AUTHORING_CONTRACT_VERSION_V2)
    monkeypatch.setenv('HERMES_INFERENCE_MODEL', 'ambient-model')
    monkeypatch.setenv('HERMES_INFERENCE_PROVIDER', 'ambient-provider')
    op, mp, bp = public_inputs(tmp_path)
    payload = json.loads(mp.read_text())
    for item in payload['discovered_items']:
        item['summary'] = 'Climate insurance supervision risk evidence.'
        item['title'] = 'Climate insurance supervision risk update'
    mp.write_text(json.dumps(payload))
    bp.write_text(json.dumps([{'url': 'https://www.iais.org/pillar-b-climate-risk',
        'title': 'Climate insurance supervision research', 'source': 'web',
        'summary': 'Climate insurance supervision risk evidence.'}]))
    staging = tmp_path / 'staging'
    events = []
    driver = monitor.run_weekly_monitor
    def tracked_driver(**kwargs):
        assert kwargs['model'] == 'gpt-6-astra'
        assert kwargs['model_provider'] == 'openai-codex'
        assert kwargs.get('temperature') is None
        assert kwargs.get('max_output_tokens') is None
        return driver(**kwargs)
    monkeypatch.setattr(monitor, 'run_weekly_monitor', tracked_driver)
    prepare = monitor._run_prepare
    finalize = monitor._run_finalize
    def tracked_prepare(args, parser):
        events.append('prepare')
        return prepare(args, parser)
    def tracked_finalize(args, parser):
        events.append('finalize')
        assert args.model == 'gpt-6-astra'
        assert args.model_provider == 'openai-codex'
        return finalize(args, parser)
    def hermes(command, **kwargs):
        assert (staging / 'v2_authoring_request.json').is_file()
        assert not (staging / 'authoring_response.json').exists()
        events.append('author')
        assert command[command.index('--model') + 1] == 'gpt-6-astra'
        assert command[command.index('--provider') + 1] == 'openai-codex'
        assert command[command.index('--query-file') + 1] == '-'
        assert command[command.index('--toolsets') + 1] == 'none'
        assert kwargs['input'] not in command
        request = json.loads((staging / 'v2_authoring_request.json').read_text())
        assert request['request_sha256'] in kwargs['input']
        assert len(request['articles']) == 4
        assert any(a['url'].endswith('/pillar-b-climate-risk') for a in request['articles'])
        response = dict(schema_version=AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
            contract_version=AUTHORING_CONTRACT_VERSION_V2, request_sha256=request['request_sha256'],
            stats=request['stats'], executive_summary='Climate supervision developments.',
            article_count=len(request['articles']), articles=[{**item, 'relevant': True,
                'summary': 'Climate insurance supervision risk evidence.' if item['evidence']['search_snippet'] else '',
                'summary_basis': 'search_snippet' if item['evidence']['search_snippet'] else 'none',
                'evidence_hash': None, 'categories': ['Supervision & Disclosure'],
                'keywords': ['climate', 'insurance', 'supervision']} for item in request['articles']])
        return Namespace(returncode=0, stdout=('Warning: Unknown toolsets: none\n' + json.dumps(response) + '\n') if reply == 'valid' else
                         ('' if reply == 'absent' else '{}'), stderr='\nsession_id: 20260907_204029_4993a5\n')
    monkeypatch.setattr(monitor, '_run_prepare', tracked_prepare)
    monkeypatch.setattr(monitor, '_run_finalize', tracked_finalize)
    # Only the Hermes process is substituted. Prepare, request validation,
    # response validation and finalize execute the production implementation.
    import subprocess
    original_run = subprocess.run
    monkeypatch.setattr(subprocess, 'run', lambda cmd, **kw:
                        hermes(cmd, **kw) if cmd[0] == 'hermes' else original_run(cmd, **kw))
    monkeypatch.setattr(sys, 'argv', ['run_climate_monitor.py', '--production-weekly',
        '--authoring-mode', 'run', '--model', 'gpt-6-astra', '--model-provider', 'openai-codex', '--report-date', '2026-09-07',
        '--acquisition-batch', str(op), '--web-listening-manifest', str(mp),
        '--pillar-b-artifact', str(bp), '--staging-dir', str(staging),
        '--source-dir', str(tmp_path / 'sources'), '--state-dir', str(tmp_path / 'state'),
        '--wiki-dir', str(tmp_path / 'wiki'), '--no-sync', '--no-update-seen-state',
        '--article-evidence-loopback', 'scripts.hermes_job:dry_run_unavailable_provider'])
    if reply == 'valid':
        monitor.main()
        assert events == ['prepare', 'author', 'finalize']
        assert list((tmp_path / 'sources').glob('climate-monitor-*.md'))
    else:
        with pytest.raises(SystemExit):
            monitor.main()
        assert events == ['prepare', 'author']
        assert not list((tmp_path / 'sources').glob('*.md'))


def test_saved_batch_fixture_is_the_public_v2_contract():
    contract = pytest.importorskip('web_listening.contracts.acquisition_batch')
    payload = json.loads((monitor.ROOT / 'tests/fixtures/issue87/acquisition_batch_result.v2.57.json').read_text())
    validated = contract.AcquisitionBatchResultV2.model_validate_json(json.dumps(payload))
    assert validated.counts.requested == 57
    assert validated.counts.succeeded == 42
    assert validated.summary.failed == 15


def test_existing_response_stops_before_prepare_or_author(tmp_path, monkeypatch):
    staging = tmp_path / 'staging'
    staging.mkdir()
    response = staging / 'authoring_response.json'
    response.write_text('{}')
    monkeypatch.setattr(monitor, '_run_prepare', lambda *args: pytest.fail('prepare ran'))
    with pytest.raises(SystemExit, match='already exists'):
        monitor._run_authoring_sequence(SimpleNamespace(staging_dir=str(staging)), None)
    assert response.read_text() == '{}'


def test_partial_public_outcome_is_rejected_by_shared_boundary(tmp_path):
    op, mp, bp = public_inputs(tmp_path)
    payload = json.loads(op.read_text())
    for key in ('authoritative_status', 'status', 'full_success', 'summary'):
        payload.pop(key)
    op.write_text(json.dumps(payload))
    with pytest.raises(SystemExit, match='public acquisition'):
        monitor._read_prepare_inputs(op, mp, bp)


@pytest.mark.parametrize('stderr', [
    '', 'session_id: ', 'session_id: bad id', 'session_id: bogus',
    'session_id: 20260907_204029_4993a5\nsession_id: 20260907_204029_4993a5',
    'session_id: 20260907_204029_4993a5\nwarning',
    'warning\nsession_id: 20260907_204029_4993a5',
])
def test_quiet_stdout_rejects_invalid_envelope(stderr):
    with pytest.raises(ValueError):
        monitor._parse_hermes_quiet_response('{"ok":true}', stderr)


@pytest.mark.parametrize('stdout', [
    '', 'Warning: Unknown toolsets: none\n',
    'Warning: Unknown toolsets: other\n{"ok":true}',
    'Warning: Unknown toolsets: none\nWarning: Unknown toolsets: none\n{"ok":true}',
    'session_id: 20260907_204029_4993a5\n{"ok":true}',
    '{"ok":true}\nwarning', '{"ok":true}\n{"extra":true}',
    'unexpected chrome\n{"ok":true}',
])
def test_quiet_stdout_rejects_unknown_chrome_or_extra_payload(stdout):
    with pytest.raises(ValueError):
        monitor._parse_hermes_quiet_response(stdout, '\nsession_id: 20260907_204029_4993a5\n')


def test_quiet_stdout_accepts_manager_observed_stream_split():
    assert monitor._parse_hermes_quiet_response(
        'Warning: Unknown toolsets: none\n{"ok":true}\n',
        '\nsession_id: 20260907_204029_4993a5\n') == {'ok': True}


def test_quiet_stdout_accepts_session_and_full_json():
    assert monitor._parse_hermes_quiet_response(
        '{\n"ok":true\n}\n',
        ' \nsession_id: 20260907_204029_4993a5\n\t') == {'ok': True}


def test_missing_authoring_identity_stops_before_prepare(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_INFERENCE_MODEL', raising=False)
    monkeypatch.delenv('HERMES_INFERENCE_PROVIDER', raising=False)
    monkeypatch.setattr(monitor, '_run_prepare', lambda *args: pytest.fail('prepare ran'))
    with pytest.raises(SystemExit, match='model.*provider'):
        monitor._run_authoring_sequence(SimpleNamespace(
            staging_dir=str(tmp_path), model='', model_provider=''), None)


@pytest.mark.parametrize('fixture_index', [0, 1])
def test_dependency_free_saved_fixtures_match_public_model(fixture_index):
    from issue87_outcome_fixture import FIXTURES, validate_fixture
    contract = pytest.importorskip('web_listening.contracts.acquisition_batch')
    raw = FIXTURES[fixture_index].read_text()
    assert validate_fixture(raw) == contract.AcquisitionBatchResultV2.model_validate_json(
        raw).model_dump(mode='json', exclude_none=True)


@pytest.mark.parametrize('mutation', ['missing_summary', 'wrong_count', 'extra', 'boolean_count'])
def test_dependency_free_fixture_rejects_changed_payload(mutation):
    from issue87_outcome_fixture import FIXTURES, validate_fixture
    payload = json.loads(FIXTURES[0].read_text())
    if mutation == 'missing_summary':
        del payload['summary']
    elif mutation == 'wrong_count':
        payload['counts']['requested'] = 56
    elif mutation == 'boolean_count':
        payload['counts']['unresolved'] = False
    else:
        payload['extra'] = 'unapproved'
    with pytest.raises(ValueError):
        validate_fixture(json.dumps(payload))


@pytest.fixture
def dependency_free_outcome(tmp_path, monkeypatch):
    import builtins
    from issue87_outcome_fixture import FIXTURES
    original_import = builtins.__import__
    def without_upstream(name, *args, **kwargs):
        if name == 'web_listening.contracts.acquisition_batch':
            raise ModuleNotFoundError("No module named 'web_listening'", name='web_listening')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_upstream)
    monkeypatch.setenv('CLIMATE_DRY_RUN_OUTCOME_FIXTURE', '1')
    monkeypatch.setenv('CLIMATE_DRY_RUN', '1')
    monkeypatch.setenv('CLIMATE_DRY_RUN_ROOT', str(tmp_path))
    path = tmp_path / 'outcome.json'
    path.write_text(FIXTURES[0].read_text())
    return path


def test_dependency_free_outcome_is_explicit_and_strict(dependency_free_outcome, monkeypatch):
    path = dependency_free_outcome
    assert monitor._read_outcome(path)['counts']['requested'] == 57
    payload = json.loads(path.read_text())
    del payload['summary']
    path.write_text(json.dumps(payload))
    with pytest.raises(SystemExit, match='invalid public'):
        monitor._read_outcome(path)
    monkeypatch.delenv('CLIMATE_DRY_RUN_OUTCOME_FIXTURE')
    with pytest.raises(ModuleNotFoundError):
        monitor._read_outcome(path)


def test_dependency_free_fixture_is_forbidden_in_production(dependency_free_outcome, monkeypatch):
    monkeypatch.delenv('CLIMATE_DRY_RUN')
    with pytest.raises(SystemExit, match='isolated temporary dry run'):
        monitor._read_outcome(dependency_free_outcome)


def test_dependency_free_fixture_cannot_read_outside_workspace(dependency_free_outcome, monkeypatch):
    monkeypatch.setenv('CLIMATE_DRY_RUN_ROOT', str(dependency_free_outcome.parent / 'other'))
    with pytest.raises(SystemExit, match='isolated temporary dry run'):
        monitor._read_outcome(dependency_free_outcome)


def test_installed_public_model_wins_over_fixture_seam(tmp_path, monkeypatch):
    import runpy
    op, _, _ = public_inputs(tmp_path)  # real producer output outside the allowlist
    monkeypatch.setenv('CLIMATE_DRY_RUN_OUTCOME_FIXTURE', '1')
    monkeypatch.setenv('CLIMATE_DRY_RUN', '1')
    monkeypatch.setenv('CLIMATE_DRY_RUN_ROOT', str(tmp_path))
    monkeypatch.setattr(runpy, 'run_path', lambda *args: pytest.fail('fixture fallback used'))
    assert monitor._read_outcome(op)['run_id'] == 'scope-run-2'


@pytest.mark.parametrize('escape', ['run', 'sync', 'provider', 'output'])
def test_dependency_free_cli_cannot_author_or_escape(dependency_free_outcome, monkeypatch, escape):
    import sys
    root = dependency_free_outcome.parent
    argv = ['run_climate_monitor.py', '--production-weekly', '--authoring-mode', 'prepare',
            '--report-date', '2026-09-07', '--acquisition-batch', str(dependency_free_outcome),
            '--web-listening-manifest', str(root / 'manifest.json'),
            '--pillar-b-artifact', str(root / 'pillar-b.json'), '--staging-dir', str(root / 'staging'),
            '--source-dir', str(root / 'sources'), '--state-dir', str(root / 'state'),
            '--wiki-dir', str(root / 'wiki'), '--no-sync',
            '--article-evidence-loopback', 'scripts.hermes_job:dry_run_unavailable_provider']
    if escape == 'run':
        argv[argv.index('--authoring-mode') + 1] = 'run'
    elif escape == 'sync':
        argv.remove('--no-sync')
    elif escape == 'provider':
        argv[-1] = ''
    else:
        argv[argv.index('--source-dir') + 1] = str(monitor.ROOT / 'sources')
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setattr(monitor, '_run_prepare', lambda *args: pytest.fail('prepare ran'))
    monkeypatch.setattr(monitor, '_run_authoring_sequence', lambda *args: pytest.fail('author ran'))
    with pytest.raises(SystemExit) as error:
        monitor.main()
    assert error.value.code == 2
    assert not (root / 'staging').exists()
