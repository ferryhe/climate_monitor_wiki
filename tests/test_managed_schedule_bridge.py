from tests.managed_runtime_fixtures import verified_cleanup
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import hermes_job as job
from climate_monitor import schedule


def service(tmp_path, monkeypatch):
    run = tmp_path / 'run-1'
    run.mkdir()
    svc = SimpleNamespace(runtime_root=tmp_path,
                          start=lambda **kw: {'accepted': True, 'run_id': 'run-1', 'attempt': 1},
                          binding=lambda run_id: {'budgets': {'runtime_seconds': 10}})
    monkeypatch.setattr(job, 'managed_monitor_preflight', lambda day, **kwargs: svc)
    records = []
    monkeypatch.setattr(job, 'record_monitor_result', lambda *args, **kwargs: records.append((args, kwargs)))
    return run, records


def test_launch_accepted_waits_for_the_real_terminal_result(tmp_path, monkeypatch):
    run, records = service(tmp_path, monkeypatch)
    def finish(_seconds):
        assert records == []
        (run / 'attempt-1-result.json').write_text(json.dumps({
            'run_id': 'run-1', 'attempt': 1, 'exit_code': 75,
        }))
    monkeypatch.setattr(job.time, 'sleep', finish)
    assert job.dispatch_managed_monitor('2026-09-14', dry_run=True) == 75
    assert records == [(('2026-09-14', None, 75), {'dry_run': True})]


def test_success_without_report_receipt_cannot_mark_completion(tmp_path, monkeypatch):
    run, records = service(tmp_path, monkeypatch)
    (run / 'attempt-1-result.json').write_text(json.dumps({
        'run_id': 'run-1', 'attempt': 1, 'exit_code': 0,
        'execution_complete': True, 'full_coverage': True,
    }))
    with pytest.raises(FileNotFoundError):
        job.dispatch_managed_monitor('2026-09-14', dry_run=False)
    assert records == []


def test_completed_with_gaps_requires_real_report_receipt(tmp_path, monkeypatch):
    run, records = service(tmp_path, monkeypatch)
    (run / 'attempt-1-result.json').write_text(json.dumps({
        'run_id': 'run-1', 'attempt': 1, 'exit_code': 0,
        'execution_complete': True, 'full_coverage': False,
        'outcome': 'completed_with_gaps',
        'reportability': {
            'schema_version': 'climate-reportability.v1',
            'outcome': 'completed_with_gaps', 'reportable': True,
            'full_coverage': False, 'selected_record_count': 1,
            'counts': {'successful_sources': 1, 'source_gaps': 0,
                       'coverage_warnings': 0, 'failed_searches': 1,
                       'unresolved_items': 0, 'blocked_tool_prechecks': 0},
            'limitations': ['Pillar B search search-1 failed: timeout'],
            'acquisition_payload_sha256': 'b' * 64,
        },
    }))
    (run / 'attempt-1-report-result.json').write_text(json.dumps({
        'report_date': '2026-09-14', 'report_path': 'climate-monitor-2026-09-14.md',
        'report_sha256': 'a' * 64, 'stats': {
            'total': 1, 'updated': 1, 'unchanged': 0,
            'blocked': 0, 'failed': 0, 'unresolved': 0,
        },
    }))
    assert job.dispatch_managed_monitor('2026-09-14', dry_run=True) == 0
    carried = records[0][0][1]
    assert records[0][0][2] == 0
    assert carried['terminal']['outcome'] == 'completed_with_gaps'
    assert carried['terminal']['reportability']['counts']['failed_searches'] == 1
    assert carried['report']['stats']['failed'] == 0


def test_no_eligible_information_completes_without_report_receipt(tmp_path, monkeypatch):
    run, records = service(tmp_path, monkeypatch)
    (run / 'attempt-1-result.json').write_text(json.dumps({
        'run_id': 'run-1', 'attempt': 1, 'exit_code': 0,
        'execution_complete': True, 'full_coverage': True,
        'outcome': 'no_eligible_information',
        'reportability': {
            'schema_version': 'climate-reportability.v1',
            'outcome': 'no_eligible_information', 'reportable': False,
            'full_coverage': True, 'selected_record_count': 0,
            'counts': {'successful_sources': 1, 'source_gaps': 0,
                       'coverage_warnings': 0, 'failed_searches': 0,
                       'unresolved_items': 0, 'blocked_tool_prechecks': 0},
            'limitations': [], 'acquisition_payload_sha256': 'b' * 64,
        },
    }))
    assert job.dispatch_managed_monitor('2026-09-14', dry_run=True) == 0
    carried = records[0][0][1]
    assert carried['terminal']['outcome'] == 'no_eligible_information'
    assert carried['report'] is None


def test_managed_terminal_and_reportability_must_agree():
    terminal = {
        'exit_code': 0, 'execution_complete': True,
        'outcome': 'completed_with_gaps',
        'reportability': {
            'schema_version': 'climate-reportability.v1',
            'outcome': 'completed', 'reportable': True,
            'full_coverage': False, 'selected_record_count': 1,
            'counts': {'successful_sources': 1, 'source_gaps': 1,
                       'coverage_warnings': 0, 'failed_searches': 0,
                       'unresolved_items': 0, 'blocked_tool_prechecks': 0},
            'limitations': ['one source was unavailable'],
            'acquisition_payload_sha256': 'b' * 64,
        },
    }
    with pytest.raises(job.Blocked, match='managed_terminal_outcome_mismatch'):
        job._managed_result_parts({'terminal': terminal, 'report': {}})


def test_failed_search_only_partial_survives_ledger_and_scheduler(tmp_path, monkeypatch):
    from climate_monitor import run_ledger, scheduler_status, semantic_bundle
    from climate_monitor.job_status import validate_snapshot

    source_dir = tmp_path / 'sources'
    ledger_dir = tmp_path / 'ledger'
    status_dir = tmp_path / 'status'
    for path in (source_dir, ledger_dir, status_dir):
        path.mkdir()
    report = source_dir / 'climate-monitor-2026-09-14.md'
    report.write_text('# verified report\n')
    digest = job.sha(report)
    terminal = {
        'run_id': 'run-1', 'attempt': 1, 'exit_code': 0,
        'finished_at': '2026-09-14T12:30:00Z',
        'execution_complete': True, 'full_coverage': False,
        'outcome': 'completed_with_gaps',
        'reportability': {
            'schema_version': 'climate-reportability.v1',
            'outcome': 'completed_with_gaps', 'reportable': True,
            'full_coverage': False, 'selected_record_count': 1,
            'counts': {'successful_sources': 1, 'source_gaps': 0,
                       'coverage_warnings': 0, 'failed_searches': 1,
                       'unresolved_items': 0, 'blocked_tool_prechecks': 0},
            'limitations': ['Pillar B search search-1 failed: timeout'],
            'acquisition_payload_sha256': 'b' * 64,
        },
    }
    receipt = {
        'report_date': '2026-09-14', 'report_path': report.name,
        'report_sha256': digest,
        'stats': {'total': 1, 'updated': 1, 'unchanged': 0,
                  'blocked': 0, 'failed': 0, 'unresolved': 0},
    }
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 14, 12, 30, tzinfo=timezone.utc)

    monkeypatch.setenv('CLIMATE_SCHEDULE', 'biweekly-et')
    monkeypatch.setenv('CLIMATE_SOURCE_DIR', str(source_dir.resolve()))
    monkeypatch.setenv('CLIMATE_RUN_LEDGER_DIR', str(ledger_dir.resolve()))
    monkeypatch.setattr(job, 'datetime', FixedDateTime)
    monkeypatch.setattr(semantic_bundle, 'verify_semantic_sidecar', lambda path: None)
    job.record_monitor_result(
        '2026-09-14', {'terminal': terminal, 'report': receipt}, 0, dry_run=False,
    )
    current = datetime(2026, 9, 14, 12, 30, tzinfo=timezone.utc)
    attempt = run_ledger.RunLedgerReader(
        ledger_dir, repository_root=job.ROOT,
    ).status(now=current)['stages']['monitor']['last_attempt']
    assert attempt['status'] == 'partial'
    assert attempt['result_code'] == 'completed_with_gaps'
    assert job.monitor_scheduler_result_code(
        '2026-09-14', now=current,
    ) == 'completed_with_gaps'

    monkeypatch.setattr(scheduler_status, '_aware_now', lambda: current)
    monkeypatch.setattr(scheduler_status, '_now_utc', lambda: schedule.stamp(current))
    scheduler_status.update_slot(
        'monitor', 'completed', scheduled_for='2026-09-14T12:00:00Z',
        claimed_at='2026-09-14T12:00:01Z', started_at='2026-09-14T12:00:02Z',
        finished_at='2026-09-14T12:30:00Z',
        result_code=job.monitor_scheduler_result_code('2026-09-14', now=current),
        status_dir=status_dir,
    )
    snapshot = validate_snapshot(
        json.loads((status_dir / 'scheduler-status.json').read_text()), now=current,
    )
    assert snapshot['jobs']['monitor']['result_code'] == 'completed_with_gaps'

    job.record_monitor_result(
        '2026-09-14', {'terminal': terminal, 'report': receipt}, 0, dry_run=False,
    )
    assert len(list((ledger_dir / 'attempts' / 'monitor' / '2026-09-14').glob('*.json'))) == 1


@pytest.mark.parametrize('outcome', ['completed_with_gaps', 'no_eligible_information'])
def test_explicit_recovery_resumes_same_run_and_carries_exact_terminal(
    tmp_path, monkeypatch, outcome,
):
    run_dir = tmp_path / 'scheduled-run'
    run_dir.mkdir()
    immutable = {
        'run_id': 'scheduled-run', 'attempt': 1,
        'budgets': {'runtime_seconds': 10, 'fetch_attempts': 20},
        'acquisition_lineage_id': 'acq-scheduled-run',
        'checkpoint_dir': str(run_dir / 'checkpoint'),
    }
    (run_dir / 'attempt-1-result.json').write_text(json.dumps({
        'run_id': 'scheduled-run', 'attempt': 1, 'exit_code': 75,
        'retryable': True, 'failure': {'schema_version': 'climate-managed-failure.v1', 'category': 'transient_service', 'cleanup': verified_cleanup()}, 'outcome': 'systemic_failure',
    }))
    current = dict(immutable)
    resume_calls = []

    def binding(run_id):
        assert run_id == 'scheduled-run'
        return dict(current)

    def resume(run_id):
        resume_calls.append(run_id)
        assert current['acquisition_lineage_id'] == immutable['acquisition_lineage_id']
        assert current['checkpoint_dir'] == immutable['checkpoint_dir']
        assert current['budgets'] == immutable['budgets']
        current['attempt'] = 2
        reportable = outcome == 'completed_with_gaps'
        terminal = {
            'run_id': run_id, 'attempt': 2, 'exit_code': 0,
            'retryable': False, 'execution_complete': True,
            'finished_at': '2026-09-14T12:50:00Z',
            'full_coverage': not reportable, 'outcome': outcome,
            'reportability': {
                'schema_version': 'climate-reportability.v1', 'outcome': outcome,
                'reportable': reportable, 'full_coverage': not reportable,
                'selected_record_count': int(reportable),
                'counts': {'successful_sources': 1, 'source_gaps': int(reportable),
                           'coverage_warnings': 0, 'failed_searches': 0,
                           'unresolved_items': 0, 'blocked_tool_prechecks': 0},
                'limitations': (['one retained source gap'] if reportable else []),
                'acquisition_payload_sha256': 'b' * 64,
            },
        }
        (run_dir / 'attempt-2-result.json').write_text(json.dumps(terminal))
        if reportable:
            (run_dir / 'attempt-2-report-result.json').write_text(json.dumps({
                'report_date': '2026-09-14',
                'report_path': 'climate-monitor-2026-09-14.md',
                'report_sha256': 'a' * 64,
                'stats': {'total': 1, 'updated': 1, 'unchanged': 0,
                          'blocked': 0, 'failed': 0, 'unresolved': 0},
            }))
        return {'accepted': True, 'run_id': run_id, 'attempt': 2}

    svc = SimpleNamespace(
        runtime_root=tmp_path, binding=binding, attach_or_resume=resume,
    )
    monkeypatch.setattr(job, 'managed_monitor_preflight', lambda *a, **k: svc)
    records = []
    monkeypatch.setattr(
        job, 'record_monitor_result',
        lambda *args, **kwargs: records.append((args, kwargs)),
    )
    assert job.dispatch_managed_monitor(
        '2026-09-14', dry_run=False, resume_run_id='scheduled-run',
    ) == 0
    assert resume_calls == ['scheduled-run']
    carried = records[0][0][1]
    assert carried['terminal']['run_id'] == 'scheduled-run'
    assert carried['terminal']['attempt'] == 2
    assert carried['terminal']['outcome'] == outcome
    assert (carried['report'] is not None) is (outcome == 'completed_with_gaps')


def test_explicit_recovery_rejects_non_retryable_and_attaches_active(tmp_path):
    run_dir = tmp_path / 'run-1'
    run_dir.mkdir()
    current = {'run_id': 'run-1', 'attempt': 1}
    def reject_or_attach(_run):
        terminal = run_dir / 'attempt-1-result.json'
        if terminal.exists():
            raise RuntimeError('acquisition attempt is terminal and non-retryable')
        return {'accepted': True, 'run_id': 'run-1', 'attempt': 1, 'attached': True}

    svc = SimpleNamespace(
        runtime_root=tmp_path, binding=lambda _run: dict(current),
        attach_or_resume=reject_or_attach,
    )
    (run_dir / 'attempt-1-result.json').write_text(json.dumps({
        'exit_code': 65, 'retryable': False,
    }))
    with pytest.raises(job.Blocked, match='managed_recovery_non_retryable'):
        job._managed_recovery_launch(svc, 'run-1')
    (run_dir / 'attempt-1-result.json').unlink()
    (run_dir / 'runtime.json').write_text(json.dumps({
        'attempt': 1, 'state': 'running',
    }))
    attached = job._managed_recovery_launch(svc, 'run-1')
    assert attached == {
        'accepted': True, 'run_id': 'run-1', 'attempt': 1, 'attached': True,
    }


def test_resume_run_id_rejects_scheduled_or_non_monitor_mode(capsys):
    assert job.main([
        'monitor', '--managed', '--scheduled', '--resume-run-id', 'run-1',
    ]) == 2
    assert 'invalid_managed_recovery_mode' in capsys.readouterr().out
    assert job.main(['email', '--resume-run-id', 'run-1']) == 2
    assert 'invalid_managed_recovery_mode' in capsys.readouterr().out


def test_recovery_binding_rejects_manual_wrong_date_and_changed_paths(
    tmp_path, monkeypatch,
):
    from climate_monitor import management

    run_id = 'scheduled-run'
    run_root = tmp_path / 'runs'
    database = tmp_path / 'registry.sqlite3'
    definition = {
        'task_id': 'weekly-climate-monitor-acquisition',
        'runtime': {'run_root': str(run_root), 'registry_database': str(database)},
    }
    report_inputs = {'source_dir': str(tmp_path / 'sources')}
    binding = {
        'run_id': run_id, 'trigger': 'scheduled', 'report_date': '2026-09-14',
        'task_id': definition['task_id'], 'definition': definition,
        'report_inputs': report_inputs,
        'checkpoint_dir': str(run_root / run_id / 'checkpoint'),
        'frozen_report_input': str(run_root / run_id / 'frozen-report-input.json'),
        'registry_database': str(database),
    }
    service = SimpleNamespace(runtime_root=run_root)
    monkeypatch.setattr(
        management, 'managed_report_inputs', lambda _definition, _run: report_inputs,
    )
    assert job._validate_managed_recovery_binding(
        service, definition, binding, run_id, '2026-09-14',
    ) == report_inputs
    manual = dict(binding, trigger='manual')
    with pytest.raises(job.Blocked, match='requires_scheduled_run'):
        job._validate_managed_recovery_binding(
            service, definition, manual, run_id, '2026-09-14',
        )
    with pytest.raises(job.Blocked, match='report_date_mismatch'):
        job._validate_managed_recovery_binding(
            service, definition, binding, run_id, '2026-09-28',
        )
    moved = dict(binding, checkpoint_dir=str(tmp_path / 'other-checkpoint'))
    with pytest.raises(job.Blocked, match='bound_path_mismatch'):
        job._validate_managed_recovery_binding(
            service, definition, moved, run_id, '2026-09-14',
        )


def test_terminal_record_from_another_run_is_rejected(tmp_path, monkeypatch):
    run, records = service(tmp_path, monkeypatch)
    (run / 'attempt-1-result.json').write_text(json.dumps({
        'run_id': 'other-run', 'attempt': 1, 'exit_code': 0,
    }))
    with pytest.raises(job.Blocked, match='managed_result_identity_mismatch'):
        job.dispatch_managed_monitor('2026-09-14', dry_run=False)
    assert records == []


def test_downstream_no_report_requires_matching_snapshot_and_ledger(tmp_path, monkeypatch):
    from climate_monitor import run_ledger

    now = datetime(2026, 9, 14, 15, tzinfo=timezone.utc)
    status_dir = tmp_path / 'status'
    ledger_dir = tmp_path / 'ledger'
    status_dir.mkdir()
    ledger_dir.mkdir()
    jobs = {
        alias: {'state': 'scheduled', 'scheduled_for': schedule.stamp(
            schedule.occurrence('2026-09-14', alias, eastern=True)
        )}
        for alias in schedule.SLOTS
    }
    jobs['monitor'] = {
        'state': 'completed', 'scheduled_for': '2026-09-14T12:00:00Z',
        'claimed_at': '2026-09-14T12:00:01Z', 'started_at': '2026-09-14T12:00:02Z',
        'finished_at': '2026-09-14T12:20:00Z',
        'result_code': 'no_eligible_information',
    }
    (status_dir / 'scheduler-status.json').write_text(json.dumps({
        'schema_version': schedule.SCHEMA, 'generated_at': schedule.stamp(now),
        'jobs': jobs,
    }))
    attempt = {
        'status': 'no_change', 'result_code': 'no_eligible_information',
        'report_date': '2026-09-14',
    }
    monkeypatch.setenv('CLIMATE_JOB_STATUS_DIR', str(status_dir.resolve()))
    monkeypatch.setenv('CLIMATE_RUN_LEDGER_DIR', str(ledger_dir.resolve()))
    monkeypatch.setenv('CLIMATE_SCHEDULE', 'biweekly-et')
    monkeypatch.setattr(
        run_ledger, 'RunLedgerReader',
        lambda *args, **kwargs: SimpleNamespace(status=lambda: {
            'stages': {'monitor': {'last_attempt': attempt}},
        }),
    )
    assert job.downstream_no_report_outcome('2026-09-14', now=now) == 'no_eligible_information'
    attempt['report_date'] = '2026-09-01'
    assert job.downstream_no_report_outcome('2026-09-14', now=now) is None
