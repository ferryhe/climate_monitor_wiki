"""The actual wrapper producer must satisfy the email consumer's ledger gate."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import hermes_job as job
from climate_monitor.run_ledger import RunLedgerReader
from climate_monitor.scheduler_status import update_slot
from test_climate_delivery_pipeline import delivery_report, config_file, configure_env

DAY = '2026-08-10'


def setup_run(tmp_path, monkeypatch):
    configure_env(monkeypatch)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 10, 8, 30, tzinfo=timezone.utc)
    from climate_monitor import scheduler_status
    monkeypatch.setattr(job, 'datetime', Clock)
    monkeypatch.setattr(scheduler_status, 'datetime', Clock)
    sources = tmp_path / 'reports'; sources.mkdir()
    report = delivery_report(sources)
    ledger = tmp_path / 'ledger'; ledger.mkdir()
    status = tmp_path / 'status'; status.mkdir()
    config = config_file(tmp_path)
    for key, value in {
        'CLIMATE_SOURCE_DIR': sources, 'CLIMATE_RUN_LEDGER_DIR': ledger,
        'CLIMATE_JOB_STATUS_DIR': status, 'CLIMATE_REPORT_PATH': report,
        'CLIMATE_DELIVERY_CONFIG': config, 'CLIMATE_DELIVERY_OUTPUT_DIR': tmp_path / 'delivery',
        'CLIMATE_DELIVERY_STATE_DIR': tmp_path / 'delivery-state',
    }.items(): monkeypatch.setenv(key, str(value))
    result = dict(report_date=DAY, report_path=report.name, report_sha256=job.sha(report),
                  stats=dict(total=2, updated=1, unchanged=0, failed=1, blocked=0, unresolved=0))
    return report, ledger, status, result


def test_producer_creates_matching_ledger_then_email_preflight_passes(tmp_path, monkeypatch):
    report, ledger, status, result = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(job.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(result)))
    assert job.dispatch(['production-driver', '--json'], 'monitor', DAY, False) == 0
    attempt = RunLedgerReader(ledger, repository_root=job.ROOT).status()['stages']['monitor']['last_attempt']
    assert attempt['status'] == 'partial'
    assert attempt['report']['sha256'] == job.sha(report)
    update_slot('monitor', 'completed', status_dir=status, scheduled_for=DAY+'T08:00:00Z',
                claimed_at=DAY+'T08:00:00Z', started_at=DAY+'T08:00:00Z', finished_at=DAY+'T08:30:00Z')
    job.verify_monitor(report, DAY)
    assert '--dry-run' in job.email_command(DAY, dry_run=True)


@pytest.mark.parametrize('fault', ['wrong_sha', 'missing_sidecar', 'wrong_date', 'malformed_output', 'driver_failure'])
def test_invalid_driver_result_never_produces_success(tmp_path, monkeypatch, fault):
    report, ledger, _, result = setup_run(tmp_path, monkeypatch)
    rc = 0
    if fault == 'wrong_sha': result['report_sha256'] = '0' * 64
    elif fault == 'missing_sidecar': report.with_suffix('.semantics.json').unlink()
    elif fault == 'wrong_date': result['report_date'] = '2026-08-03'
    elif fault == 'driver_failure': rc = 1
    stdout = 'incomplete' if fault == 'malformed_output' else json.dumps(result)
    monkeypatch.setattr(job.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=rc, stdout=stdout))
    assert job.dispatch(['production-driver', '--json'], 'monitor', DAY, False) != 0
    attempt = RunLedgerReader(ledger, repository_root=job.ROOT).status()['stages']['monitor']['last_attempt']
    assert attempt is None or (attempt['status'] == 'failed' and 'report' not in attempt)


def test_dry_run_validates_without_writing_success(tmp_path, monkeypatch):
    _, ledger, _, result = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(job.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(result)))
    assert job.dispatch(['production-driver', '--json'], 'monitor', DAY, True) == 0
    assert list(ledger.iterdir()) == []
