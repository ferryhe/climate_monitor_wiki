"""Fail-closed Hermes wrapper checks; existing public drivers own all work.

Preflight is read-only. Child output is deliberately suppressed: SMTP errors,
recipient identifiers and arbitrary upstream content must not enter cron logs.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEDULE = {'monitor': '08:00', 'email': '09:00', 'publisher': '10:00', 'registry': '10:30'}


class Blocked(ValueError):
    pass


def path_env(name, *, directory=False, exists=True):
    value = os.environ.get(name, '')
    if not value:
        raise Blocked('missing_' + name)
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or path.resolve() != path:
        raise Blocked('invalid_' + name)
    if path.is_relative_to(ROOT / 'tests' / 'fixtures') and not (os.environ.get('CLIMATE_DRY_RUN') == '1' and os.environ.get('CLIMATE_DRY_RUN_FIXTURE_DIR')):
        raise Blocked('production_fixture_path_forbidden')
    if exists and not (path.is_dir() if directory else path.is_file()):
        raise Blocked('unavailable_' + name)
    if not exists and not path.parent.is_dir():
        raise Blocked('unavailable_' + name)
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_monitor(report, day):
    from climate_monitor.job_status import validate_snapshot
    from climate_monitor.run_ledger import RunLedgerReader
    status_dir = path_env('CLIMATE_JOB_STATUS_DIR', directory=True)
    payload = json.loads((status_dir / 'scheduler-status.json').read_text())
    validate_snapshot(payload, now=datetime.now(timezone.utc))
    slot = payload['jobs']['monitor']
    ledger = RunLedgerReader(path_env('CLIMATE_RUN_LEDGER_DIR', directory=True), repository_root=ROOT).status()
    attempt = ledger['stages']['monitor']['last_attempt']
    if (slot['state'] != 'completed' or slot['scheduled_for'] != day + 'T08:00:00Z'
        or not attempt or attempt['status'] not in {'success', 'no_change', 'partial'}
        or attempt.get('report_date') != day
        or attempt.get('report', {}).get('sha256') != sha(report)
        or attempt.get('report', {}).get('report_date') != day):
        raise Blocked('monitor_identity_mismatch')


def email_command(day, *, dry_run):
    from climate_delivery.config import load_delivery_config
    from climate_delivery.report import parse_weekly_report
    from climate_delivery.paths import validate_run_paths
    from climate_monitor.semantic_bundle import verify_semantic_sidecar
    report = path_env('CLIMATE_REPORT_PATH')
    output = path_env('CLIMATE_DELIVERY_OUTPUT_DIR', directory=True)
    state = path_env('CLIMATE_DELIVERY_STATE_DIR', directory=True)
    config = path_env('CLIMATE_DELIVERY_CONFIG')
    validate_run_paths(report, output, state, config)
    if parse_weekly_report(report).report_date != day:
        raise Blocked('report_date_mismatch')
    verify_monitor(report, day)
    verify_semantic_sidecar(report)
    if len(load_delivery_config(config).recipients) != 4:
        raise Blocked('delivery_requires_four_configured_recipients')
    return [sys.executable, '-m', 'climate_delivery.cli', 'run', '--report', str(report),
            '--output-dir', str(output), '--state-dir', str(state), '--config', str(config), '--expected-report-sha256', sha(report)] + (['--dry-run'] if dry_run else [])


def verify_delivery_artifact(day, expected_sha):
    from climate_delivery.artifacts import load_report_artifact
    from climate_delivery.report import parse_weekly_report
    report = path_env('CLIMATE_REPORT_PATH')
    parsed = parse_weekly_report(report)
    artifact = load_report_artifact(path_env('CLIMATE_DELIVERY_OUTPUT_DIR', directory=True),
        report_date=day, report_filename=report.name, report_title=parsed.title,
        report_sha256=expected_sha, include_pdf_bytes=False)
    if parsed.sha256 != expected_sha or artifact is None:
        raise Blocked('delivery_artifact_identity_mismatch')


def dispatch(command, slot, day, dry_run):
    def run(args):
        try:
            return subprocess.run(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).returncode
        except OSError:
            return 127
    if not command:
        return 0
    if slot == 'email':
        expected = command[command.index('--expected-report-sha256') + 1]
        # Produce and verify the PDF with the public CLI before the sending CLI.
        rc = run(command if dry_run else command + ['--dry-run'])
        if rc:
            return rc
        try:
            verify_delivery_artifact(day, expected)
            verify_monitor(path_env('CLIMATE_REPORT_PATH'), day)
        except Exception:
            return 2
        return 0 if dry_run else run(command)
    return run(command)


def dry_run_unavailable_provider(article_id, url):
    # A wrapper contract rehearsal cannot resolve upstream content references.
    # Record unavailability, never fall through to a live default provider.
    return {'status': 'unavailable', 'article_id': article_id, 'requested_url': url}


def monitor_command(day, *, dry_run):
    # There is no executable, same-run #67 outcome -> #92 evidence -> #93
    # response producer in this wrapper. Never substitute unrelated files or
    # infer site counts from candidate URLs. Real provisioning remains blocked.
    response = path_env('AUTHORING_RESPONSE')
    evidence = path_env('ARTICLE_EVIDENCE')
    stats = path_env('CLIMATE_STATS_PATH')
    from climate_monitor.weekly_monitor.driver import _emit_authoring_request, _candidate_items_from_evidence
    from climate_monitor.weekly_monitor.authoring_contract import load_authoring_response, validate_authoring_response
    from climate_monitor.weekly_monitor.prompt_loader import load_weekly_monitor_prompt
    from climate_monitor.taxonomy import load_article_taxonomy
    evidence_data = json.loads(evidence.read_text())
    stats_data = json.loads(stats.read_text())
    request = _emit_authoring_request(article_evidence=evidence_data, stats=stats_data,
                                     report_date=date.fromisoformat(day), prompt=load_weekly_monitor_prompt())
    validate_authoring_response(_candidate_items_from_evidence(request, evidence_data),
                                load_authoring_response(response), taxonomy=load_article_taxonomy(), request=request)
    if not dry_run:
        raise Blocked('live_acquisition_contract_unavailable')
    fixture = path_env('CLIMATE_DRY_RUN_FIXTURE_DIR', directory=True)
    manifest = fixture / 'iais_minimal_manifest.json'
    if not manifest.is_file():
        raise Blocked('missing_dry_run_manifest')
    command = [sys.executable, str(ROOT / 'scripts/run_climate_monitor.py'), '--production-weekly',
               '--date', day, '--authoring-response', str(response), '--article-evidence', str(evidence),
               '--stats', json.dumps(stats_data), '--article-evidence-loopback', 'scripts.hermes_job:dry_run_unavailable_provider', '--manifest-fixture', str(manifest), '--no-update-seen-state', '--no-sync', '--json']
    for flag, env in [('state-dir', 'CLIMATE_STATE_DIR'), ('source-dir', 'CLIMATE_SOURCE_DIR'), ('wiki-dir', 'CLIMATE_WIKI_DIR')]:
        command += ['--' + flag, str(path_env(env, directory=True))]
    for name, env in [('source-config', 'CLIMATE_SOURCE_CONFIG'), ('run-config', 'CLIMATE_RUN_CONFIG'), ('site-scopes', 'CLIMATE_SITE_SCOPES')]:
        command += ['--' + name, str(path_env(env))]
    return command


def publisher_command(day, *, dry_run):
    reports = path_env('CLIMATE_REPORTS_DIR', directory=True)
    ledger = path_env('CLIMATE_RUN_LEDGER_DIR', directory=True)
    report = reports / f'climate-monitor-{day}.md'
    if not report.is_file():
        raise Blocked('missing_publisher_report')
    verify_monitor(report, day)
    from scripts.publish_weekly_reports import validate_pending_reports
    validate_pending_reports([report], source_dir=ROOT / 'sources')
    if dry_run:
        return []  # The real publisher validator passed; no clone, push or PR.
    if not all(shutil.which(name) for name in ('git', 'gh', 'flock')):
        raise Blocked('missing_publisher_command')
    lock = path_env('CLIMATE_PUBLISH_LOCK', exists=False)
    return ['flock', '--nonblock', str(lock), sys.executable, str(ROOT / 'scripts/publish_weekly_reports.py'),
            '--production-repo', str(ROOT), '--report-dir', str(reports), '--ledger-dir', str(ledger), '--date', day]


def registry_command(day, *, dry_run):
    command = [sys.executable, str(ROOT / 'scripts/weekly_registry_refresh.py'), '--date', day]
    fields = [('source-dir', 'CLIMATE_SOURCE_DIR', True, True), ('database', 'CLIMATE_REGISTRY_DB', False, True),
              ('artifact-root', 'CLIMATE_DELIVERY_OUTPUT_DIR', True, True), ('backup-dir', 'CLIMATE_REGISTRY_BACKUP_DIR', True, True),
              ('lock-file', 'CLIMATE_REGISTRY_LOCK', False, False), ('publisher-ledger-dir', 'CLIMATE_RUN_LEDGER_DIR', True, True)]
    for flag, env, directory, exists in fields:
        command += ['--' + flag, str(path_env(env, directory=directory, exists=exists))]
    report = Path(command[command.index('--source-dir') + 1]) / f'climate-monitor-{day}.md'
    if not report.is_file():
        raise Blocked('missing_deployed_report')
    expected = os.environ.get('CLIMATE_EXPECTED_REPORT_SHA256', '')
    if len(expected) != 64 or sha(report) != expected:
        raise Blocked('deployed_report_sha_mismatch')
    # Explicit human acknowledgement is necessary, but insufficient alone:
    # weekly-sync also validates the deployed tracked corpus, publisher ledger,
    # artifact identity and DB baseline before its promotion boundary.
    if os.environ.get('CLIMATE_HUMAN_MERGE_DEPLOY_VERIFIED') != '1':
        raise Blocked('awaiting_human_merge_deploy')
    if os.environ.get('CLIMATE_REGISTRY_ENABLE') != '1':
        raise Blocked('registry_disabled')
    command += ['--expected-report-sha256', expected]
    if dry_run:
        command += ['--dry-run']
    else:
        if os.environ.get('CLIMATE_REGISTRY_WRITE_ENABLE') != '1':
            raise Blocked('registry_write_not_authorized')
        for flag, env in [('base-url', 'API_BASE_URL'), ('expected-api-host', 'SITE_HOST')]:
            value = os.environ.get(env)
            if not value:
                raise Blocked('missing_' + env)
            command += ['--' + flag, value]
    return command


def record_registry_pending(day, status_dir, result_code):
    from climate_monitor.scheduler_status import update_slot
    now = datetime.now(timezone.utc)
    scheduled = datetime.fromisoformat(day + 'T10:30:00+00:00')
    if now >= scheduled and now.date() - timedelta(days=now.weekday()) == scheduled.date():
        update_slot('registry', 'not_dispatched', scheduled_for=day + 'T10:30:00Z',
                    status_dir=status_dir, result_code=result_code)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('slot', choices=SCHEDULE)
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args(argv)
    try:
        dry_run = os.environ.get('CLIMATE_DRY_RUN') == '1'
        if os.environ.get('CLIMATE_DRY_RUN_FIXTURE_DIR') and not dry_run:
            raise Blocked('fixture_requires_dry_run')
        # Legacy fixture variables are not accepted as production configuration.
        if any(value for key, value in os.environ.items() if key.startswith('CLIMATE_FIX')):
            raise Blocked('unsupported_fixture_variable')
        day = os.environ.get('REPORT_DATE', '')
        parsed = date.fromisoformat(day)
        if parsed.weekday() != 0 or parsed.isoformat() != day:
            raise Blocked('REPORT_DATE_requires_monday')
        status_dir = path_env('CLIMATE_JOB_STATUS_DIR', directory=True)
        if status_dir.is_relative_to(ROOT):
            raise Blocked('status_requires_external_directory')
        if dry_run:
            workspace = path_env('CLIMATE_DRY_RUN_ROOT', directory=True)
            if not workspace.is_relative_to(Path('/tmp')):
                raise Blocked('dry_run_requires_tmp_workspace')
            for key in ('CLIMATE_STATE_DIR', 'CLIMATE_SOURCE_DIR', 'CLIMATE_WIKI_DIR', 'CLIMATE_JOB_STATUS_DIR',
                        'CLIMATE_DELIVERY_OUTPUT_DIR', 'CLIMATE_DELIVERY_STATE_DIR', 'CLIMATE_RUN_LEDGER_DIR',
                        'CLIMATE_REPORTS_DIR', 'CLIMATE_REGISTRY_DB', 'CLIMATE_REGISTRY_BACKUP_DIR', 'CLIMATE_REGISTRY_LOCK'):
                if os.environ.get(key) and not Path(os.environ[key]).resolve().is_relative_to(workspace):
                    raise Blocked('dry_run_path_outside_workspace')
        command = globals()[args.slot + '_command'](day, dry_run=dry_run)
        if args.preflight:
            print(json.dumps({'status': 'preflight_passed', 'slot': args.slot, 'scheduled_for': day + 'T' + SCHEDULE[args.slot] + ':00Z', 'dry_run': dry_run}))
            return 0
        now = datetime.now(timezone.utc)
        scheduled = datetime.fromisoformat(day + 'T' + SCHEDULE[args.slot] + ':00+00:00')
        if not dry_run and (now < scheduled or now.date() - timedelta(days=now.weekday()) != parsed):
            raise Blocked('outside_scheduled_week_or_before_slot')
    except Exception as exc:
        code = str(exc) if isinstance(exc, Blocked) else 'invalid_contract_or_config'
        if args.slot == 'registry' and not args.preflight and not dry_run and code in {'awaiting_human_merge_deploy', 'registry_disabled', 'registry_write_not_authorized'}:
            record_registry_pending(day, status_dir, code)
        print(json.dumps({'status': 'preflight_failed', 'result_code': code}))
        return 2

    from climate_monitor.scheduler_status import update_slot
    timestamps = {'claimed_at': now, 'started_at': now}
    scheduled_for = day + 'T' + SCHEDULE[args.slot] + ':00Z'
    if not dry_run:
        update_slot(args.slot, 'running', scheduled_for=scheduled_for, status_dir=status_dir, **timestamps)
    rc = dispatch(command, args.slot, day, dry_run)
    if dry_run and args.slot == 'registry':
        record_registry_pending(day, status_dir, f'registry_dry_run_exit_{rc}')
    # Dry runs never turn a production slot into completed or alter its snapshot.
    if not dry_run:
        update_slot(args.slot, 'completed' if rc == 0 else 'failed', scheduled_for=scheduled_for,
                    status_dir=status_dir, finished_at=datetime.now(timezone.utc), **timestamps)
    print(json.dumps({'status': 'dry_run_passed' if dry_run and rc == 0 else ('completed' if rc == 0 else 'failed'),
                      'slot': args.slot, 'result_code': f'{args.slot}_exit_{rc}'}))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
