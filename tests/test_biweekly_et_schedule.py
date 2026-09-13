from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

from climate_monitor import schedule
from climate_monitor.job_status import JobStatusInvalidSnapshotError, validate_snapshot
from scripts.export_scheduler_status import project


@pytest.mark.parametrize('day,hour', [('2026-09-14', 12), ('2026-10-26', 12), ('2026-11-09', 13), ('2027-03-15', 12)])
def test_et_occurrence_tracks_dst_without_shifting_local_hour(day, hour):
    stamp = schedule.occurrence(day, 'monitor', eastern=True)
    assert stamp.hour == hour
    assert stamp.astimezone(schedule.ET).hour == 8


def test_anchor_skips_alternate_monday_and_wrong_dst_tick():
    assert schedule.is_run_date(date(2026, 9, 14))
    assert schedule.is_run_date(date(2026, 9, 28))
    assert not schedule.is_run_date(date(2026, 9, 7))
    assert not schedule.is_run_date(date(2026, 9, 21))
    assert schedule.due('monitor', datetime(2026, 9, 14, 12, 0, 30, tzinfo=timezone.utc))
    assert not schedule.due('monitor', datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc))
    assert not schedule.due('monitor', datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc))


def database():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE executions(job_id,status,claimed_at,started_at,finished_at)')
    return db


IDS = {k: k + '-real-id' for k in schedule.SLOTS}


def test_deployment_docs_distinguish_observed_mount_from_required_cutover():
    text = (
        Path(__file__).resolve().parents[1] / 'docs' / 'biweekly-et-deployment.md'
    ).read_text(encoding='utf-8')
    assert 'No `/pipeline` mount is currently installed' in text
    assert '**required cutover configuration**' in text
    assert 'CLIMATE_MANAGED_SOURCE_DIR=/pipeline/generated/sources' in text
    assert 'CLIMATE_MANAGED_WIKI_DIR=/pipeline/generated/wiki' in text
    assert 'CLIMATE_DELIVERY_NO_SEND=1' in text
    assert 'delivery.status=artifact-only' in text
    assert '`/var/lib/docker` is `0710 root:root`' in text
    assert 'scheduled publisher identity' in text
    assert 'No production mount, task, job, or file was changed' in text


def test_observer_before_first_occurrence_and_off_week():
    with database() as db:
        before = project(db, IDS, now=datetime(2026, 9, 13, 16, tzinfo=timezone.utc))
        assert all(x['state'] == 'scheduled' for x in before['jobs'].values())
        off_week = project(db, IDS, now=datetime(2026, 9, 21, 16, tzinfo=timezone.utc))
        assert off_week['jobs']['monitor']['scheduled_for'] == '2026-09-14T12:00:00Z'
        assert all(x['state'] == 'not_dispatched' for x in off_week['jobs'].values())


def test_observer_uses_real_terminal_result_and_ignores_wrong_offset():
    with database() as db:
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)',
                   (IDS['monitor'], 'failed', '2026-09-14T12:00:02+00:00',
                    '2026-09-14T12:00:03+00:00', '2026-09-14T12:20:00+00:00'))
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)',
                   (IDS['monitor'], 'completed', '2026-09-14T13:00:02+00:00',
                    '2026-09-14T13:00:03+00:00', '2026-09-14T13:00:04+00:00'))
        result = project(db, IDS, now=datetime(2026, 9, 14, 15, tzinfo=timezone.utc))
        assert result['jobs']['monitor']['state'] == 'failed'
        assert result['jobs']['monitor']['finished_at'] == '2026-09-14T12:20:00Z'
        result['jobs']['monitor']['scheduled_for'] = '2026-09-14T08:00:00Z'
        with pytest.raises(JobStatusInvalidSnapshotError):
            validate_snapshot(result, now=datetime(2026, 9, 14, 15, tzinfo=timezone.utc))


def test_writer_rolls_to_next_fortnight_without_carrying_completion(tmp_path, monkeypatch):
    from climate_monitor import scheduler_status as writer
    monkeypatch.setenv('CLIMATE_SCHEDULE', 'biweekly-et')
    current = datetime(2026, 9, 14, 13, tzinfo=timezone.utc)
    monkeypatch.setattr(writer, '_aware_now', lambda: current)
    monkeypatch.setattr(writer, '_now_utc', lambda: schedule.stamp(current))
    writer.update_slot('monitor', 'completed', scheduled_for='2026-09-14T12:00:00Z',
                       claimed_at='2026-09-14T12:00:01Z', started_at='2026-09-14T12:00:02Z',
                       finished_at='2026-09-14T12:30:00Z', status_dir=tmp_path)
    current = datetime(2026, 9, 28, 12, 0, 2, tzinfo=timezone.utc)
    writer.update_slot('monitor', 'running', scheduled_for='2026-09-28T12:00:00Z',
                       claimed_at='2026-09-28T12:00:01Z', status_dir=tmp_path)
    payload = validate_snapshot(json.loads((tmp_path / 'scheduler-status.json').read_text()), now=current)
    assert payload['jobs']['monitor']['state'] == 'running'
    assert payload['jobs']['email'] == {'state': 'scheduled', 'scheduled_for': '2026-09-28T13:00:00Z'}


def test_observer_preserves_observed_registry_merge_gate():
    with database() as db:
        now = datetime(2026, 9, 14, 15, tzinfo=timezone.utc)
        previous = project(db, IDS, now=now)
        previous['jobs']['registry']['result_code'] = 'awaiting_human_merge_deploy'
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)',
                   (IDS['registry'], 'failed', '2026-09-14T14:30:02+00:00',
                    '2026-09-14T14:30:03+00:00', '2026-09-14T14:30:04+00:00'))
        result = project(db, IDS, now=now, previous=previous)
        assert result['jobs']['registry']['state'] == 'not_dispatched'
        assert result['jobs']['registry']['result_code'] == 'awaiting_human_merge_deploy'


def test_delivery_no_send_survives_same_execution_but_not_newer_or_rollover():
    with database() as db:
        now = datetime(2026, 9, 14, 15, tzinfo=timezone.utc)
        previous = project(db, IDS, now=now)
        previous['jobs']['email'] = {
            'state': 'not_dispatched', 'scheduled_for': '2026-09-14T13:00:00Z',
            'claimed_at': '2026-09-14T13:00:02Z',
            'started_at': '2026-09-14T13:00:03Z',
            'finished_at': '2026-09-14T13:10:00Z',
            'result_code': 'delivery_no_send',
        }
        previous = validate_snapshot(previous, now=now)
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)', (
            IDS['email'], 'completed', '2026-09-14T13:00:01+00:00',
            '2026-09-14T13:00:02+00:00', '2026-09-14T13:10:01+00:00',
        ))
        retained = project(db, IDS, now=now, previous=previous)
        assert retained['jobs']['email'] == previous['jobs']['email']

        db.execute('DELETE FROM executions WHERE job_id=?', (IDS['email'],))
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)', (
            IDS['email'], 'failed', '2026-09-14T13:04:00+00:00',
            '2026-09-14T13:04:01+00:00', '2026-09-14T13:20:00+00:00',
        ))
        superseded = project(db, IDS, now=now, previous=retained)
        assert superseded['jobs']['email']['state'] == 'failed'
        assert superseded['jobs']['email']['claimed_at'] == '2026-09-14T13:04:00Z'
        rollover = project(
            db, IDS, now=datetime(2026, 9, 28, 15, tzinfo=timezone.utc),
            previous=retained,
        )
        assert rollover['jobs']['email']['scheduled_for'] == '2026-09-28T13:00:00Z'
        assert rollover['jobs']['email']['result_code'] == 'not_dispatched'


@pytest.mark.parametrize('code', ['completed_with_gaps', 'no_eligible_information'])
def test_observer_preserves_truthful_completed_monitor_outcome(code):
    with database() as db:
        now = datetime(2026, 9, 14, 15, tzinfo=timezone.utc)
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)',
                   (IDS['monitor'], 'completed', '2026-09-14T12:00:02+00:00',
                    '2026-09-14T12:00:03+00:00', '2026-09-14T12:20:00+00:00'))
        previous = project(db, IDS, now=now)
        previous['jobs']['monitor']['result_code'] = code
        previous = validate_snapshot(previous, now=now)
        result = project(db, IDS, now=now, previous=previous)
        assert result['jobs']['monitor']['state'] == 'completed'
        assert result['jobs']['monitor']['result_code'] == code


@pytest.mark.parametrize('code', ['completed_with_gaps', 'no_eligible_information'])
def test_lagging_running_then_completed_observation_preserves_domain_outcome(code):
    with database() as db:
        now = datetime(2026, 9, 14, 15, tzinfo=timezone.utc)
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)', (
            IDS['monitor'], 'running', '2026-09-14T12:00:02+00:00',
            '2026-09-14T12:00:03+00:00', None,
        ))
        previous = project(db, IDS, now=now)
        previous['jobs']['monitor'].update(
            state='completed', finished_at='2026-09-14T12:20:00Z',
            result_code=code,
        )
        previous = validate_snapshot(previous, now=now)
        lagging = project(db, IDS, now=now, previous=previous)
        assert lagging['jobs']['monitor'] == previous['jobs']['monitor']
        db.execute(
            "UPDATE executions SET status='completed', finished_at=? WHERE job_id=?",
            ('2026-09-14T12:20:02+00:00', IDS['monitor']),
        )
        caught_up = project(db, IDS, now=now, previous=lagging)
        assert caught_up['jobs']['monitor']['state'] == 'completed'
        assert caught_up['jobs']['monitor']['result_code'] == code
        assert caught_up['jobs']['monitor']['finished_at'] == '2026-09-14T12:20:02Z'


@pytest.mark.parametrize('code', ['completed_with_gaps', 'no_eligible_information'])
def test_recovery_completion_beats_older_failure_but_newer_execution_supersedes(code):
    with database() as db:
        now = datetime(2026, 9, 14, 16, tzinfo=timezone.utc)
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)', (
            IDS['monitor'], 'failed', '2026-09-14T12:00:02+00:00',
            '2026-09-14T12:00:03+00:00', '2026-09-14T12:20:01+00:00',
        ))
        prior = project(db, IDS, now=now)
        prior['jobs']['monitor'] = {
            'state': 'completed', 'scheduled_for': '2026-09-14T12:00:00Z',
            'claimed_at': '2026-09-14T12:45:00Z',
            'started_at': '2026-09-14T12:45:01Z',
            'finished_at': '2026-09-14T12:55:00Z',
            'result_code': code,
        }
        prior = validate_snapshot(prior, now=now)
        recovered = project(db, IDS, now=now, previous=prior)
        assert recovered['jobs']['monitor'] == prior['jobs']['monitor']

        db.execute('DELETE FROM executions WHERE job_id=?', (IDS['monitor'],))
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)', (
            IDS['monitor'], 'failed', '2026-09-14T12:04:00+00:00',
            '2026-09-14T12:04:01+00:00', '2026-09-14T12:40:00+00:00',
        ))
        old = dict(prior)
        old['jobs'] = dict(prior['jobs'])
        old['jobs']['monitor'] = {
            **prior['jobs']['monitor'],
            'claimed_at': '2026-09-14T12:00:02Z',
            'started_at': '2026-09-14T12:00:03Z',
            'finished_at': '2026-09-14T12:20:00Z',
        }
        superseded = project(db, IDS, now=now, previous=old)
        assert superseded['jobs']['monitor']['state'] == 'failed'
        assert superseded['jobs']['monitor']['claimed_at'] == '2026-09-14T12:04:00Z'
        assert superseded['jobs']['monitor']['result_code'] == 'execution_failed'


@pytest.mark.parametrize('state', ['running', 'failed'])
@pytest.mark.parametrize('observation', ['absent', 'older'])
def test_newer_recovery_claim_survives_absent_or_older_hermes(state, observation):
    with database() as db:
        now = datetime(2026, 9, 14, 16, tzinfo=timezone.utc)
        previous = project(db, IDS, now=now)
        recovery = {
            'state': state, 'scheduled_for': '2026-09-14T12:00:00Z',
            'claimed_at': '2026-09-14T12:03:00Z',
            'started_at': '2026-09-14T12:03:01Z',
        }
        if state == 'failed':
            recovery.update(
                finished_at='2026-09-14T12:40:00Z',
                result_code='execution_failed',
            )
        previous['jobs']['monitor'] = recovery
        previous = validate_snapshot(previous, now=now)
        if observation == 'older':
            db.execute('INSERT INTO executions VALUES(?,?,?,?,?)', (
                IDS['monitor'], 'failed', '2026-09-14T12:00:02+00:00',
                '2026-09-14T12:00:03+00:00', '2026-09-14T12:20:00+00:00',
            ))
        merged = project(db, IDS, now=now, previous=previous)
        assert merged['jobs']['monitor'] == recovery


@pytest.mark.parametrize('prior_state', ['running', 'failed'])
def test_genuinely_newer_hermes_claim_supersedes_recovery(prior_state):
    with database() as db:
        now = datetime(2026, 9, 14, 16, tzinfo=timezone.utc)
        previous = project(db, IDS, now=now)
        recovery = {
            'state': prior_state, 'scheduled_for': '2026-09-14T12:00:00Z',
            'claimed_at': '2026-09-14T12:02:00Z',
            'started_at': '2026-09-14T12:02:01Z',
        }
        if prior_state == 'failed':
            recovery.update(
                finished_at='2026-09-14T12:30:00Z',
                result_code='execution_failed',
            )
        previous['jobs']['monitor'] = recovery
        previous = validate_snapshot(previous, now=now)
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)', (
            IDS['monitor'], 'running', '2026-09-14T12:04:00+00:00',
            '2026-09-14T12:04:01+00:00', None,
        ))
        merged = project(db, IDS, now=now, previous=previous)
        assert merged['jobs']['monitor']['state'] == 'running'
        assert merged['jobs']['monitor']['claimed_at'] == '2026-09-14T12:04:00Z'


def test_same_claim_progresses_to_terminal_without_regressing_terminal():
    with database() as db:
        now = datetime(2026, 9, 14, 16, tzinfo=timezone.utc)
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)', (
            IDS['monitor'], 'completed', '2026-09-14T12:00:02+00:00',
            '2026-09-14T12:00:03+00:00', '2026-09-14T12:20:00+00:00',
        ))
        previous = project(db, IDS, now=now)
        previous['jobs']['monitor'] = {
            'state': 'running', 'scheduled_for': '2026-09-14T12:00:00Z',
            'claimed_at': '2026-09-14T12:00:02Z',
            'started_at': '2026-09-14T12:00:03Z',
        }
        advanced = project(db, IDS, now=now, previous=previous)
        assert advanced['jobs']['monitor']['state'] == 'completed'

        db.execute("UPDATE executions SET status='running', finished_at=NULL")
        preserved = project(db, IDS, now=now, previous=advanced)
        assert preserved['jobs']['monitor'] == advanced['jobs']['monitor']

        failed = dict(advanced)
        failed['jobs'] = dict(advanced['jobs'])
        failed['jobs']['monitor'] = {
            **advanced['jobs']['monitor'],
            'state': 'failed', 'result_code': 'execution_failed',
        }
        failed = validate_snapshot(failed, now=now)
        still_failed = project(db, IDS, now=now, previous=failed)
        assert still_failed['jobs']['monitor'] == failed['jobs']['monitor']


@pytest.mark.parametrize('state', ['running', 'failed'])
def test_recovery_preterminal_or_failure_does_not_cross_fortnight(state):
    with database() as db:
        previous_now = datetime(2026, 9, 14, 16, tzinfo=timezone.utc)
        previous = project(db, IDS, now=previous_now)
        block = {
            'state': state, 'scheduled_for': '2026-09-14T12:00:00Z',
            'claimed_at': '2026-09-14T12:03:00Z',
            'started_at': '2026-09-14T12:03:01Z',
        }
        if state == 'failed':
            block.update(
                finished_at='2026-09-14T12:30:00Z',
                result_code='execution_failed',
            )
        previous['jobs']['monitor'] = block
        previous = validate_snapshot(previous, now=previous_now)

        rollover = project(
            db, IDS, now=datetime(2026, 9, 28, 16, tzinfo=timezone.utc),
            previous=previous,
        )
        assert rollover['jobs']['monitor'] == {
            'state': 'not_dispatched',
            'scheduled_for': '2026-09-28T12:00:00Z',
            'result_code': 'not_dispatched',
        }


def test_bridge_recovery_failure_survives_lagging_exporter(
    tmp_path, monkeypatch, capsys,
):
    from scripts import export_scheduler_status as exporter
    from scripts import hermes_job as job
    from climate_monitor import scheduler_status as writer

    now = datetime(2026, 9, 14, 12, 45, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    status_dir = tmp_path / 'status'
    status_dir.mkdir()
    monkeypatch.setenv('CLIMATE_SCHEDULE', 'biweekly-et')
    monkeypatch.setenv('CLIMATE_JOB_STATUS_DIR', str(status_dir.resolve()))
    monkeypatch.setenv('REPORT_DATE', '2026-09-14')
    monkeypatch.delenv('CLIMATE_DRY_RUN', raising=False)
    monkeypatch.setattr(job, 'datetime', Clock)
    monkeypatch.setattr(writer, '_aware_now', lambda: now)
    monkeypatch.setattr(writer, '_now_utc', lambda: schedule.stamp(now))
    monkeypatch.setattr(job, 'managed_monitor_preflight', lambda *_a, **_k: object())
    monkeypatch.setattr(job, 'dispatch', lambda *_a, **_k: 75)

    assert job.main(['monitor', '--managed', '--resume-run-id', 'run-1']) == 75
    application = validate_snapshot(
        json.loads((status_dir / 'scheduler-status.json').read_text()), now=now,
    )
    assert application['jobs']['monitor']['state'] == 'failed'

    db_path = tmp_path / 'executions.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE executions(job_id,status,claimed_at,started_at,finished_at)')
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)', (
            IDS['monitor'], 'failed', '2026-09-14T12:00:02+00:00',
            '2026-09-14T12:00:03+00:00', '2026-09-14T12:20:00+00:00',
        ))
    jobs_path = tmp_path / 'jobs.json'
    jobs_path.write_text(json.dumps(IDS))
    exporter.export_snapshot(db_path, jobs_path, status_dir, clock=lambda: now)
    merged = validate_snapshot(
        json.loads((status_dir / 'scheduler-status.json').read_text()), now=now,
    )
    assert merged['jobs']['monitor'] == application['jobs']['monitor']
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'


def test_observer_never_carries_completion_across_fortnight():
    with database() as db:
        first = datetime(2026, 9, 14, 15, tzinfo=timezone.utc)
        previous = project(db, IDS, now=first)
        previous['jobs']['monitor'] = {
            'state': 'completed', 'scheduled_for': '2026-09-14T12:00:00Z',
            'claimed_at': '2026-09-14T12:00:01Z',
            'started_at': '2026-09-14T12:00:02Z',
            'finished_at': '2026-09-14T12:30:00Z',
            'result_code': 'no_eligible_information',
        }
        result = project(
            db, IDS, now=datetime(2026, 9, 28, 15, tzinfo=timezone.utc),
            previous=previous,
        )
        assert result['jobs']['monitor']['scheduled_for'] == '2026-09-28T12:00:00Z'
        assert result['jobs']['monitor']['state'] == 'not_dispatched'
        assert result['jobs']['monitor']['result_code'] == 'not_dispatched'


def test_exporter_and_other_slot_writer_serialize_complete_transactions(
    tmp_path, monkeypatch,
):
    from climate_monitor import scheduler_status as writer
    from scripts import export_scheduler_status as exporter

    now = datetime(2026, 9, 14, 15, tzinfo=timezone.utc)
    monkeypatch.setenv('CLIMATE_SCHEDULE', 'biweekly-et')
    monkeypatch.setattr(writer, '_aware_now', lambda: now)
    monkeypatch.setattr(writer, '_now_utc', lambda: '2026-09-14T15:00:00Z')
    status_dir = tmp_path / 'status'
    transaction_lock = threading.Lock()

    @contextmanager
    def thread_snapshot_transaction(value=None):
        directory = Path(value or status_dir)
        directory.mkdir(parents=True, exist_ok=True)
        with transaction_lock:
            yield directory / 'scheduler-status.json'

    monkeypatch.setattr(writer, 'snapshot_transaction', thread_snapshot_transaction)
    monkeypatch.setattr(exporter, 'snapshot_transaction', thread_snapshot_transaction)
    writer.update_slot(
        'monitor', 'completed', scheduled_for='2026-09-14T12:00:00Z',
        claimed_at='2026-09-14T12:00:01Z', started_at='2026-09-14T12:00:02Z',
        finished_at='2026-09-14T12:30:00Z', result_code='completed_with_gaps',
        status_dir=status_dir,
    )
    db_path = tmp_path / 'executions.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE executions(job_id,status,claimed_at,started_at,finished_at)')
    jobs_path = tmp_path / 'jobs.json'
    jobs_path.write_text(json.dumps(IDS))
    entered = threading.Event()
    release = threading.Event()
    writer_done = threading.Event()
    errors = []
    real_project = exporter.project

    def paused_project(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_project(*args, **kwargs)

    monkeypatch.setattr(exporter, 'project', paused_project)
    observer = threading.Thread(target=lambda: exporter.export_snapshot(
        db_path, jobs_path, status_dir, clock=lambda: now,
    ))

    def write_email():
        try:
            writer.update_slot(
                'email', 'completed', scheduled_for='2026-09-14T13:00:00Z',
                claimed_at='2026-09-14T13:00:01Z', started_at='2026-09-14T13:00:02Z',
                finished_at='2026-09-14T13:10:00Z', status_dir=status_dir,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            writer_done.set()

    observer.start()
    assert entered.wait(5)
    updater = threading.Thread(target=write_email)
    updater.start()
    time.sleep(0.1)
    assert not writer_done.is_set()
    release.set()
    observer.join(5)
    updater.join(5)
    assert not errors
    snapshot = validate_snapshot(
        json.loads((status_dir / 'scheduler-status.json').read_text()), now=now,
    )
    assert snapshot['jobs']['monitor']['result_code'] == 'completed_with_gaps'
    assert snapshot['jobs']['email']['state'] == 'completed'


def test_delivery_no_send_main_uses_persistent_paths_and_truthful_snapshot(
    tmp_path, monkeypatch, capsys,
):
    from scripts import hermes_job as job
    from climate_monitor import scheduler_status as writer

    now = datetime(2026, 9, 14, 13, 20, tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    status_dir = tmp_path / 'persistent-status'
    status_dir.mkdir()
    monkeypatch.setenv('CLIMATE_SCHEDULE', 'biweekly-et')
    monkeypatch.setenv('CLIMATE_JOB_STATUS_DIR', str(status_dir.resolve()))
    monkeypatch.setenv('CLIMATE_DELIVERY_NO_SEND', '1')
    monkeypatch.setenv('REPORT_DATE', '2026-09-14')
    monkeypatch.delenv('CLIMATE_DRY_RUN', raising=False)
    monkeypatch.delenv('CLIMATE_DRY_RUN_ROOT', raising=False)
    monkeypatch.setattr(job, 'datetime', Clock)
    monkeypatch.setattr(writer, '_aware_now', lambda: now)
    monkeypatch.setattr(writer, '_now_utc', lambda: schedule.stamp(now))
    monkeypatch.setattr(job, 'downstream_no_report_outcome', lambda *_a, **_k: None)
    monkeypatch.setattr(
        job, 'email_command',
        lambda day, **kwargs: ['artifact-only', day, str(kwargs['artifact_only'])],
    )
    dispatches = []
    monkeypatch.setattr(
        job, 'dispatch',
        lambda command, slot, day, dry_run, **kwargs:
            dispatches.append((command, slot, day, dry_run, kwargs)) or 0,
    )
    assert job.main(['email']) == 0
    assert dispatches[0][4]['delivery_no_send'] is True
    payload = validate_snapshot(
        json.loads((status_dir / 'scheduler-status.json').read_text()), now=now,
    )
    assert payload['jobs']['email']['state'] == 'not_dispatched'
    assert payload['jobs']['email']['result_code'] == 'delivery_no_send'
    assert json.loads(capsys.readouterr().out)['result_code'] == 'delivery_no_send'

    monkeypatch.setattr(
        job, 'downstream_no_report_outcome',
        lambda *_a, **_k: 'no_eligible_information',
    )
    assert job.main(['email']) == 0
    payload = validate_snapshot(
        json.loads((status_dir / 'scheduler-status.json').read_text()), now=now,
    )
    assert payload['jobs']['email']['state'] == 'completed'
    assert payload['jobs']['email']['result_code'] == 'no_eligible_information'


@pytest.mark.skipif(os.name != 'posix', reason='real fcntl interprocess proof is Linux-only')
def test_snapshot_lock_serializes_real_linux_processes(tmp_path):
    from climate_monitor.scheduler_status import snapshot_transaction

    marker = tmp_path / 'child-acquired'
    code = (
        "import pathlib,sys; from climate_monitor.scheduler_status import snapshot_transaction; "
        "exec(\"with snapshot_transaction(sys.argv[1]):\\n "
        "pathlib.Path(sys.argv[2]).write_text('acquired')\")"
    )
    with snapshot_transaction(tmp_path):
        child = subprocess.Popen(
            [sys.executable, '-c', code, str(tmp_path), str(marker)],
            cwd=Path(__file__).resolve().parents[1],
        )
        time.sleep(0.2)
        assert not marker.exists()
    assert child.wait(timeout=5) == 0
    assert marker.read_text() == 'acquired'


@pytest.mark.parametrize('alias', ['email', 'publisher', 'registry'])
def test_no_eligible_monitor_outcome_is_a_truthful_downstream_completion(alias):
    with database() as db:
        now = datetime(2026, 9, 14, 15, tzinfo=timezone.utc)
        occurrence = schedule.occurrence('2026-09-14', alias, eastern=True)
        db.execute('INSERT INTO executions VALUES(?,?,?,?,?)', (
            IDS[alias], 'completed', occurrence.isoformat(), occurrence.isoformat(),
            (occurrence.replace(second=30)).isoformat(),
        ))
        previous = project(db, IDS, now=now)
        previous['jobs'][alias]['result_code'] = 'no_eligible_information'
        previous = validate_snapshot(previous, now=now)
        result = project(db, IDS, now=now, previous=previous)
        assert result['jobs'][alias]['state'] == 'completed'
        assert result['jobs'][alias]['result_code'] == 'no_eligible_information'
