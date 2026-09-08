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
import secrets
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
    if not exists and directory and path.exists() and not path.is_dir():
        raise Blocked('unavailable_' + name)
    if not exists and not directory and not path.parent.is_dir():
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
    output = path_env('CLIMATE_DELIVERY_OUTPUT_DIR', directory=True, exists=False)
    state = path_env('CLIMATE_DELIVERY_STATE_DIR', directory=True, exists=False)
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
    if slot == 'monitor':
        try:
            child = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
            rc = child.returncode
            result = json.loads(child.stdout) if rc == 0 else None
        except (OSError, ValueError):
            rc, result = 2, None
        try:
            # The report and its committed sidecar must match the driver's
            # result before the scheduler can mark this attempt completed.
            record_monitor_result(day, result, rc, dry_run=dry_run)
        except Exception:
            return 2
        return rc
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


def record_monitor_result(day, result, rc, *, dry_run):
    """Append the existing ledger contract after the production driver exits."""
    from climate_monitor.run_ledger import append_attempt, build_report_identity
    from climate_monitor.semantic_bundle import verify_semantic_sidecar

    finished = datetime.now(timezone.utc)
    attempt = {
        'schema_version': 'weekly-run-attempt.v1',
        'attempt_id': f'{finished:%Y%m%dt%H%M%Sz}-monitor-{secrets.token_hex(4)}',
        'stage': 'monitor', 'report_date': day, 'scheduled_for': day + 'T08:00:00Z',
        'finished_at': finished.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'status': 'failed', 'result_code': f'monitor_exit_{rc}',
    }
    if rc == 0:
        report = path_env('CLIMATE_SOURCE_DIR', directory=True) / f'climate-monitor-{day}.md'
        identity = build_report_identity(report_date=day, filename=report.name, sha256=sha(report))
        if (not isinstance(result, dict) or result.get('report_date') != day
                or result.get('report_path') != report.name
                or result.get('report_sha256') != identity.sha256):
            raise Blocked('monitor_result_identity_mismatch')
        verify_semantic_sidecar(report)
        from climate_monitor.weekly_monitor.authoring_contract import _validate_v2_stats_shape
        stats = _validate_v2_stats_shape(result.get('stats'))
        attempt.update(report=identity.as_record(), result_code='report_written',
                       status='partial' if stats['failed'] + stats['blocked'] + stats['unresolved'] else 'success')
    # Dry-run validation exercises the same identity checks but never appends
    # success to the live ledger or makes a production scheduler slot complete.
    if not dry_run:
        append_attempt(path_env('CLIMATE_RUN_LEDGER_DIR', directory=True), attempt, repository_root=ROOT)


def dry_run_unavailable_provider(article_id, url):
    # A wrapper contract rehearsal cannot resolve upstream content references.
    # Record unavailability, never fall through to a live default provider.
    return {'status': 'unavailable', 'article_id': article_id, 'requested_url': url}


def _monitor_prepare_cli_command(day: str, *, staging_dir: Path) -> list[str]:
    """Build only the prepare CLI command for the preflight path.

    Preflight validates binding and validator-consistent inputs without
    touching the staging bundle or the authoring response (those are
    consumed by the finalize phase, which the operator runs after the
    LLM step). The preflight path therefore resolves only the three
    upstream artifacts and the staging directory; the authoring
    response and bundle are not required for the preflight itself.
    """
    outcome_path = path_env("CLIMATE_OUTCOME_ARTIFACT")
    manifest_path = path_env("CLIMATE_MANIFEST_ARTIFACT")
    pillar_b_path = path_env("CLIMATE_PILLAR_B_ARTIFACT")
    from scripts.run_climate_monitor import _read_prepare_inputs
    try:
        _read_prepare_inputs(outcome_path, manifest_path, pillar_b_path, report_date=day)
    except (SystemExit, ValueError, KeyError, TypeError) as exc:
        raise Blocked("invalid_acquisition_contract") from exc
    command = [
        sys.executable,
        str(ROOT / "scripts/run_climate_monitor.py"),
        "--production-weekly",
        "--authoring-mode", "prepare",
        "--report-date", day,
        "--acquisition-batch", str(outcome_path),
        "--web-listening-manifest", str(manifest_path),
        "--pillar-b-artifact", str(pillar_b_path),
        "--staging-dir", str(staging_dir),
    ]
    for flag, env, directory in [
        ("state-dir", "CLIMATE_STATE_DIR", True),
        ("source-dir", "CLIMATE_SOURCE_DIR", True),
        ("wiki-dir", "CLIMATE_WIKI_DIR", True),
        ("source-config", "CLIMATE_SOURCE_CONFIG", False),
        ("run-config", "CLIMATE_RUN_CONFIG", False),
        ("site-scopes", "CLIMATE_SITE_SCOPES", False),
    ]:
        command += ["--" + flag, str(path_env(env, directory=directory))]
    return command


def _monitor_cli_command(
    day: str,
    *,
    mode: str,
    staging_dir: Path,
    response: Path | None = None,
    dry_run: bool,
) -> list[str]:
    """Build the ``run_climate_monitor.py --production-weekly`` command for
    the ``prepare`` (build staging bundle + emit v2 authoring request) and
    ``finalize`` (consume prepared bundle + one authoring response, run
    the #91 transaction) phases of the same-run chain.

    The production monitor is the only authoritative entrypoint; this
    wrapper is a thin Hermes adapter that resolves required paths and
    threads them through the same CLI. The operator never assembles the
    AUTHORING_RESPONSE / ARTICLE_EVIDENCE / CLIMATE_STATS_PATH triple;
    the prepare phase builds the bundle from #67 outcome + manifest +
    Pillar B and the finalize phase consumes only the prepared bundle
    plus exactly one authoring response.
    """
    if mode not in {"prepare", "finalize", "run"}:
        raise Blocked(f"unknown monitor authoring mode: {mode!r}")
    outcome_path = path_env("CLIMATE_OUTCOME_ARTIFACT")
    manifest_path = path_env("CLIMATE_MANIFEST_ARTIFACT")
    pillar_b_path = path_env("CLIMATE_PILLAR_B_ARTIFACT")
    command = [
        sys.executable,
        str(ROOT / "scripts/run_climate_monitor.py"),
        "--production-weekly",
        "--authoring-mode", mode,
        "--report-date", day,
        "--acquisition-batch", str(outcome_path),
        "--web-listening-manifest", str(manifest_path),
        "--pillar-b-artifact", str(pillar_b_path),
        "--staging-dir", str(staging_dir),
    ]
    if mode in {"run", "finalize"}:
        from scripts.run_climate_monitor import _resolve_authoring_identity
        try:
            model, provider = _resolve_authoring_identity()
        except SystemExit as exc:
            raise Blocked("missing_authoring_model_provider") from exc
        command += ["--model", model, "--model-provider", provider]
    if mode == "finalize":
        if response is None:
            response = path_env("CLIMATE_AUTHORING_RESPONSE")
        command += ["--authoring-response", str(response)]
    for flag, env, directory in [
        ("state-dir", "CLIMATE_STATE_DIR", True),
        ("source-dir", "CLIMATE_SOURCE_DIR", True),
        ("wiki-dir", "CLIMATE_WIKI_DIR", True),
        ("source-config", "CLIMATE_SOURCE_CONFIG", False),
        ("run-config", "CLIMATE_RUN_CONFIG", False),
        ("site-scopes", "CLIMATE_SITE_SCOPES", False),
    ]:
        command += ["--" + flag, str(path_env(env, directory=directory))]
    return command


def monitor_command(day, *, dry_run):
    """Delegate one complete authoring sequence to the production CLI."""
    staging_dir = path_env("CLIMATE_STAGING_DIR", directory=True, exists=False)
    ledger = path_env('CLIMATE_RUN_LEDGER_DIR', directory=True)
    if ledger.is_relative_to(ROOT):
        raise Blocked('ledger_requires_external_directory')
    return _monitor_cli_command(day, mode="run", staging_dir=staging_dir, dry_run=dry_run) + ['--json']


def finalize_command(day, *, dry_run):
    """Build the finalize CLI command for the same-run chain.

    The finalize slot consumes the staging bundle produced by the
    prepare slot plus exactly one authoring response and commits the
    #91 atomic transaction (final report + seen-state commit). This
    wrapper delegates to the same production CLI in
    ``--authoring-mode finalize`` so there is exactly one
    authoring-validation path in the codebase.
    """
    staging_dir = path_env("CLIMATE_STAGING_DIR", directory=True)
    if not (staging_dir / "bundle.json").is_file():
        raise Blocked("missing_prepared_bundle")
    return [_monitor_cli_command(
        day, mode="finalize", staging_dir=staging_dir, dry_run=dry_run
    )]


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
        if args.slot == "monitor":
            # Read-only preflight consumes the same upstream contract as
            # prepare. The production command owns the later authoring turn.
            staging_for_preflight = path_env(
                "CLIMATE_STAGING_DIR", directory=True, exists=False
            )
            _monitor_prepare_cli_command(day, staging_dir=staging_for_preflight)
            command = monitor_command(day, dry_run=dry_run)
        else:
            command = globals()[args.slot + "_command"](day, dry_run=dry_run)
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
