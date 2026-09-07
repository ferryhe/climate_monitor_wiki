"""Issue #87 subprocess boundaries: preflight must not dispatch or mutate."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def invoke(tmp_path, slot, *args, **overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CLIMATE_', 'REPORT_', 'ARTICLE_', 'STATS_', 'AUTHORING_'))}
    env.update(REPO=str(ROOT), PYTHON=sys.executable, REPORT_DATE='2026-09-07')
    for key in ('STATE_DIR', 'SOURCE_DIR', 'WIKI_DIR', 'JOB_STATUS_DIR', 'DELIVERY_OUTPUT_DIR', 'DELIVERY_STATE_DIR', 'RUN_LEDGER_DIR', 'REPORTS_DIR'):
        path = tmp_path / key.lower()
        path.mkdir(exist_ok=True)
        env['CLIMATE_' + key] = str(path)
    env.update(overrides)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    result = subprocess.run(['bash', str(ROOT / f'scripts/hermes_job_{slot}.sh'), *args], cwd=tmp_path, env=env, capture_output=True, text=True)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    return result, before, after


@pytest.mark.parametrize('slot', ['monitor', 'email', 'publisher', 'registry'])
def test_f7_preflight_missing_inputs_never_writes(tmp_path, slot):
    result, before, after = invoke(tmp_path, slot, '--preflight')
    assert result.returncode == 2
    assert 'preflight_failed' in result.stdout
    assert before == after


def test_f1_monitor_missing_real_evidence_fails_before_status(tmp_path):
    result, before, after = invoke(tmp_path, 'monitor')
    assert result.returncode == 2
    assert 'missing_AUTHORING_RESPONSE' in result.stdout
    assert before == after


@pytest.mark.parametrize('slot', ['monitor', 'email', 'publisher', 'registry'])
def test_f1_fixture_dir_requires_explicit_dry_run(tmp_path, slot):
    result, before, after = invoke(tmp_path, slot, CLIMATE_DRY_RUN_FIXTURE_DIR=str(ROOT / 'tests/fixtures/issue87'))
    assert result.returncode == 2
    assert 'fixture_requires_dry_run' in result.stdout
    assert before == after


@pytest.mark.parametrize('slot', ['monitor', 'email', 'publisher', 'registry'])
def test_f11_relative_repo_rejected_from_hermes_cwd(tmp_path, slot):
    result, before, after = invoke(tmp_path, slot, '--preflight', REPO='relative')
    assert result.returncode != 0
    assert before == after


def test_f3_remote_status_documented_honestly():
    text = (ROOT / 'docs/job-status.md').read_text()
    assert 'Render has no shared source' in text
    assert '503 `not_configured`' in text
    assert 'CLIMATE_JOB_STATUS_DIR' not in (ROOT / 'render.yaml').read_text()


def test_f4_cron_mapping_is_explicit():
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo
    manifest = json.loads((ROOT / 'monitoring/jobs/weekly-climate-monitor-08h/manifest.json').read_text())
    schedules = manifest['weekly_schedule']
    for name, hour, minute in [('monitor', 8, 0), ('email', 9, 0), ('publisher', 10, 0), ('registry', 10, 30)]:
        local = datetime(2026, 9, 7, hour, minute, tzinfo=ZoneInfo('UTC')).astimezone(ZoneInfo('Asia/Shanghai'))
        assert schedules[name] == {'timezone': 'Asia/Shanghai', 'cron': f'{minute} {local.hour} * * 1', 'utc': f'{hour:02}:{minute:02}'}


def test_f2_email_plan_uses_public_cli_and_checks_identity(tmp_path, monkeypatch):
    from scripts import hermes_job as job
    from test_climate_delivery_pipeline import delivery_report, configure_env
    from test_climate_delivery_email import config_file
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    for name in ('output', 'state'):
        (tmp_path / name).mkdir()
    monkeypatch.setenv('CLIMATE_REPORT_PATH', str(report))
    monkeypatch.setenv('CLIMATE_DELIVERY_CONFIG', str(config_file(tmp_path)))
    monkeypatch.setenv('CLIMATE_DELIVERY_OUTPUT_DIR', str(tmp_path / 'output'))
    monkeypatch.setenv('CLIMATE_DELIVERY_STATE_DIR', str(tmp_path / 'state'))
    checked = []
    monkeypatch.setattr(job, 'verify_monitor', lambda report, day: checked.append((report, day)))
    command = job.email_command('2026-08-10', dry_run=True)
    assert checked == [(report, '2026-08-10')]
    assert command[:4] == [sys.executable, '-m', 'climate_delivery.cli', 'run']
    for flag in ('--report', '--output-dir', '--state-dir', '--config'):
        assert Path(command[command.index(flag) + 1]).is_absolute()
    assert command[-1] == '--dry-run'
    monkeypatch.setattr(job, 'verify_monitor', lambda *_: (_ for _ in ()).throw(ValueError('mismatch')))
    with pytest.raises(ValueError):
        job.email_command('2026-08-10', dry_run=False)


def test_email_preflight_accepts_uncreated_delivery_roots(tmp_path, monkeypatch, capsys):
    import json
    from scripts import hermes_job as job
    from test_climate_delivery_pipeline import delivery_report, configure_env
    from test_climate_delivery_email import config_file

    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    status = tmp_path / 'status'
    status.mkdir()
    monkeypatch.setenv('REPORT_DATE', '2026-08-10')
    monkeypatch.setenv('CLIMATE_REPORT_PATH', str(report))
    monkeypatch.setenv('CLIMATE_DELIVERY_CONFIG', str(config_file(tmp_path)))
    monkeypatch.setenv('CLIMATE_JOB_STATUS_DIR', str(status))
    roots = [tmp_path / 'new-output' / 'output', tmp_path / 'new-state' / 'state']
    for name, root in zip(('CLIMATE_DELIVERY_OUTPUT_DIR', 'CLIMATE_DELIVERY_STATE_DIR'), roots):
        monkeypatch.setenv(name, str(root))
    checked = []
    monkeypatch.setattr(job, 'verify_monitor', lambda report, day: checked.append((report, day)))
    assert job.main(['email', '--preflight']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'preflight_passed'
    assert checked == [(report, '2026-08-10')]
    assert all(not root.parent.exists() for root in roots)
    for name in ('CLIMATE_DELIVERY_OUTPUT_DIR', 'CLIMATE_DELIVERY_STATE_DIR'):
        with monkeypatch.context() as patch:
            patch.setenv(name, str(report))
            with pytest.raises(job.Blocked, match='unavailable_' + name):
                job.email_command('2026-08-10', dry_run=True)
    for name in ('CLIMATE_REPORT_PATH', 'CLIMATE_DELIVERY_CONFIG'):
        with monkeypatch.context() as patch:
            patch.setenv(name, str(tmp_path / 'missing-file'))
            with pytest.raises(job.Blocked, match='unavailable_' + name):
                job.email_command('2026-08-10', dry_run=True)


def test_f9_registry_runner_has_true_dry_run(tmp_path, monkeypatch, capsys):
    import scripts.weekly_registry_refresh as refresh
    from test_weekly_registry_refresh import _argv, _sync_payload
    calls = []
    def sync(args, *, dry_run, **kwargs):
        calls.append(dry_run)
        return _sync_payload(dry_run=dry_run)
    monkeypatch.setattr(refresh, '_run_sync', sync)
    assert refresh.main(_argv(tmp_path) + ['--dry-run']) == 0
    assert calls == [True]
    import json
    assert json.loads(capsys.readouterr().out)['dry_run'] is True


def test_email_changed_report_sha_fails_before_artifacts(tmp_path, monkeypatch, capsys):
    from climate_delivery.cli import main
    from test_climate_delivery_pipeline import delivery_report, configure_env
    from test_climate_delivery_email import config_file
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output, state = tmp_path / 'out', tmp_path / 'delivery-state'
    assert main(['run', '--report', str(report), '--output-dir', str(output), '--state-dir', str(state),
                 '--config', str(config_file(tmp_path)), '--expected-report-sha256', 'a' * 64, '--dry-run']) == 2
    assert 'report SHA does not match' in capsys.readouterr().out
    assert not output.exists() and not state.exists()


def test_f2_pdf_tamper_blocks_email_dispatch(tmp_path, monkeypatch):
    from scripts import hermes_job as job
    from test_climate_delivery_pipeline import delivery_report, configure_env
    from test_climate_delivery_email import config_file
    from climate_delivery.pipeline import run_delivery
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output, state = tmp_path / 'out', tmp_path / 'delivery-state'
    config = config_file(tmp_path)
    run_delivery(report, output, state, config, dry_run=True)
    monkeypatch.setenv('CLIMATE_REPORT_PATH', str(report))
    monkeypatch.setenv('CLIMATE_DELIVERY_OUTPUT_DIR', str(output))
    job.verify_delivery_artifact('2026-08-10', job.sha(report))
    next(output.rglob('*.pdf')).write_bytes(b'%PDF-tampered')
    with pytest.raises(job.Blocked, match='delivery_artifact_identity_mismatch'):
        job.verify_delivery_artifact('2026-08-10', job.sha(report))


def test_f5_f8_f6_recovery_and_continuation_remain_gated():
    report = (ROOT / 'ISSUE87_FOLLOWUP_REPORT.md').read_text()
    assert 'git revert' in report and 'merge --ff-only' in report
    assert 'hermes cron list/remove/update/create' in report
    assert 'before Monday 16:00 CST' in report
    assert 'f64c1563ff95 remains paused' in report
    assert 'fresh read-only reviewer' in report
    assert 'Issue #87 stays OPEN' in report
    assert 'separate closeout PR' in report
    assert 'git reset --hard' not in report


# Keep the acceptance scenario in the normal pytest discovery gate too.
from dryrun_isolated_pipeline_full import (  # noqa: E402,F401
    test_full_chain_all_four_consumers,
    test_full_chain_and_production_snapshots,
    test_full_chain_cli_checks_all_snapshot_domains,
    test_full_chain_failure_still_records_before_after,
)


def test_production_rejects_fixture_disguised_as_authoring_response(tmp_path):
    result, before, after = invoke(tmp_path, 'monitor', '--preflight',
        AUTHORING_RESPONSE=str(ROOT / 'tests/fixtures/issue87/57_record_fixture.json'))
    assert 'production_fixture_path_forbidden' in result.stdout
    assert before == after


def test_registry_command_requires_gate_and_dispatches_real_dry_runner(tmp_path, monkeypatch):
    from scripts import hermes_job as job
    report = tmp_path / 'climate-monitor-2026-09-07.md'; report.write_text('fixture')
    database = tmp_path / 'registry.sqlite3'; database.write_bytes(b'fixture')
    values = {'CLIMATE_SOURCE_DIR': tmp_path, 'CLIMATE_REGISTRY_DB': database,
        'CLIMATE_DELIVERY_OUTPUT_DIR': tmp_path, 'CLIMATE_REGISTRY_BACKUP_DIR': tmp_path,
        'CLIMATE_REGISTRY_LOCK': tmp_path / 'registry.sqlite3.lock', 'CLIMATE_RUN_LEDGER_DIR': tmp_path,
        'CLIMATE_EXPECTED_REPORT_SHA256': job.sha(report), 'CLIMATE_REGISTRY_ENABLE': '1'}
    for key, value in values.items(): monkeypatch.setenv(key, str(value))
    monkeypatch.delenv('CLIMATE_HUMAN_MERGE_DEPLOY_VERIFIED', raising=False)
    with pytest.raises(job.Blocked, match='awaiting_human_merge_deploy'):
        job.registry_command('2026-09-07', dry_run=True)
    monkeypatch.setenv('CLIMATE_HUMAN_MERGE_DEPLOY_VERIFIED', '1')
    command = job.registry_command('2026-09-07', dry_run=True)
    assert command[:2] == [sys.executable, str(ROOT / 'scripts/weekly_registry_refresh.py')]
    assert command[-1] == '--dry-run'
    assert command[command.index('--expected-report-sha256') + 1] == job.sha(report)
    monkeypatch.setenv('CLIMATE_EXPECTED_REPORT_SHA256', 'f' * 64)
    with pytest.raises(job.Blocked, match='deployed_report_sha_mismatch'):
        job.registry_command('2026-09-07', dry_run=True)


def test_weekly_cli_honors_explicit_offline_provider_before_driver(tmp_path, monkeypatch):
    from scripts import run_climate_monitor as cli
    calls = []
    provider = lambda *_: {'status': 'unavailable'}
    monkeypatch.setattr(cli, '_parse_loopback_provider', lambda spec: (provider,))
    def run(**kwargs):
        calls.append(kwargs)
        raise RuntimeError('driver_boundary')
    monkeypatch.setattr(cli, 'run_weekly_monitor', run)
    monkeypatch.setattr(sys, 'argv', ['run_climate_monitor.py', '--production-weekly',
        '--authoring-response', str(tmp_path / 'response.json'), '--article-evidence-loopback', 'fixture:offline'])
    with pytest.raises(RuntimeError, match='driver_boundary'):
        cli.main()
    assert calls[0]['providers'] == (provider,)


def test_f11_explicit_dry_monitor_runs_from_unrelated_cwd_without_seen_commit(tmp_path):
    import json
    from scripts.dryrun_isolated_pipeline import _build_v2_response
    fixtures = ROOT / 'tests/fixtures/issue87'
    stats = json.loads((fixtures / '57_stats.json').read_text())
    records = json.loads((fixtures / '57_article_evidence.json').read_text())['records']
    response = tmp_path / 'response.json'; response.write_text(json.dumps(_build_v2_response(stats, records)))
    run_config = tmp_path / 'run.yaml'
    run_config.write_text((ROOT / 'monitoring/run_config.yaml').read_text().replace('Daily Climate', 'Weekly Climate'))
    result, _, _ = invoke(tmp_path, 'monitor', CLIMATE_DRY_RUN='1', CLIMATE_DRY_RUN_ROOT=str(tmp_path),
        CLIMATE_DRY_RUN_FIXTURE_DIR=str(fixtures), AUTHORING_RESPONSE=str(response),
        CLIMATE_SOURCE_CONFIG=str(ROOT / 'monitoring/supranational_sources.yaml'),
        CLIMATE_RUN_CONFIG=str(run_config), CLIMATE_SITE_SCOPES=str(ROOT / 'monitoring/site_scopes.yaml'),
        ARTICLE_EVIDENCE=str(fixtures / '57_article_evidence.json'), CLIMATE_STATS_PATH=str(fixtures / '57_stats.json'))
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'dry_run_passed' in result.stdout
    assert list((tmp_path / 'source_dir').glob('climate-monitor-*.md'))
    assert not list((tmp_path / 'state_dir').rglob('*'))
    assert not list((tmp_path / 'job_status_dir').rglob('*'))


def test_f2_real_monitor_ledger_and_snapshot_sha_guard(tmp_path, monkeypatch):
    import json
    from scripts import hermes_job as job
    from climate_monitor.run_ledger import append_attempt
    from test_climate_delivery_pipeline import delivery_report
    report = delivery_report(tmp_path)
    day = '2026-08-10'
    ledger = tmp_path / 'ledger'
    status_dir = tmp_path / 'status'; status_dir.mkdir()
    append_attempt(ledger, {'schema_version': 'weekly-run-attempt.v1', 'attempt_id': 'fixture-monitor',
        'stage': 'monitor', 'report_date': day, 'scheduled_for': day + 'T08:00:00Z',
        'finished_at': day + 'T08:30:00Z', 'status': 'success', 'result_code': 'report_written',
        'report': {'report_id': 'climate-monitor-' + day, 'report_date': day, 'sha256': job.sha(report)}}, repository_root=ROOT)
    payload = {'schema_version': 'weekly-job-status.v1', 'generated_at': day + 'T11:00:00Z',
        'jobs': {name: {'scheduled_for': day + f'T{hour}:00:00Z', 'state': 'scheduled'}
                 for name, hour in [('monitor', '08'), ('email', '09'), ('publisher', '10')]}}
    payload['jobs']['monitor'].update(state='completed', claimed_at=day + 'T08:00:00Z',
        started_at=day + 'T08:00:00Z', finished_at=day + 'T08:30:00Z')
    target = status_dir / 'scheduler-status.json'; target.write_text(json.dumps(payload))
    monkeypatch.setenv('CLIMATE_RUN_LEDGER_DIR', str(ledger)); monkeypatch.setenv('CLIMATE_JOB_STATUS_DIR', str(status_dir))
    job.verify_monitor(report, day)
    payload['jobs']['monitor'] = {'scheduled_for': day + 'T08:00:00Z', 'state': 'scheduled'}
    target.write_text(json.dumps(payload))
    with pytest.raises(job.Blocked, match='monitor_identity_mismatch'): job.verify_monitor(report, day)
    payload['jobs']['monitor'].update(state='completed', claimed_at=day + 'T08:00:00Z',
        started_at=day + 'T08:00:00Z', finished_at=day + 'T08:30:00Z')
    target.write_text(json.dumps(payload)); report.write_text(report.read_text() + '\nchanged\n')
    with pytest.raises(job.Blocked, match='monitor_identity_mismatch'): job.verify_monitor(report, day)


def test_weekly_render_preserves_sidecar_lane_order_and_unresolved_count():
    from datetime import date
    from climate_monitor.models import CandidateItem
    from climate_monitor.report_writer import render_report
    from climate_monitor.semantic_bundle import rendered_article_urls, render_order
    items = [CandidateItem(title='Climate insurance', url='https://example.org/' + lane,
             lane=lane, summary='Fixture evidence.', source_name='Fixture') for lane in ('document', 'research', 'website')]
    report = render_report(report_date=date(2026, 9, 7), title='Weekly Climate Monitor', items=items,
        dedup_notes=[], sites_monitored=99, warnings=[], executive_summary='Fixture executive summary.',
        weekly_stats={'total': 4, 'updated': 1, 'unchanged': 0, 'blocked': 1, 'failed': 1, 'unresolved': 1})
    assert rendered_article_urls(report) == [item.url for item in render_order(items)]
    assert 'checked: **4**, succeeded: **1**, failed: **3**' in report
