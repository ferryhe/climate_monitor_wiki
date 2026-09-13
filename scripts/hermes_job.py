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
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from climate_monitor import schedule

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
    if (slot['state'] != 'completed'
        or slot['scheduled_for'] != schedule.stamp(schedule.occurrence(day, 'monitor'))
        or not attempt or attempt['status'] not in {'success', 'no_change', 'partial'}
        or attempt.get('report_date') != day
        or attempt.get('report', {}).get('sha256') != sha(report)
        or attempt.get('report', {}).get('report_date') != day):
        raise Blocked('monitor_identity_mismatch')


def email_command(day, *, dry_run, artifact_only=False):
    from climate_delivery.report import parse_weekly_report
    from climate_delivery.paths import validate_run_paths
    from climate_monitor.semantic_bundle import verify_semantic_sidecar
    report = path_env('CLIMATE_REPORT_PATH')
    output = path_env('CLIMATE_DELIVERY_OUTPUT_DIR', directory=True, exists=False)
    state = path_env('CLIMATE_DELIVERY_STATE_DIR', directory=True, exists=False)
    config = None if artifact_only else path_env('CLIMATE_DELIVERY_CONFIG')
    if artifact_only:
        from climate_delivery.paths import (
            external_directory_root, external_file_path, require_separate_trees,
        )
        external_file_path(report, 'report')
        external_directory_root(output, 'output-dir')
        external_directory_root(state, 'state-dir')
        require_separate_trees(output, state, 'output-dir', 'state-dir')
    else:
        validate_run_paths(report, output, state, config)
    if parse_weekly_report(report).report_date != day:
        raise Blocked('report_date_mismatch')
    verify_monitor(report, day)
    verify_semantic_sidecar(report)
    command = [
        sys.executable, '-m', 'climate_delivery.cli', 'run', '--report', str(report),
        '--output-dir', str(output), '--state-dir', str(state),
        '--expected-report-sha256', sha(report),
    ]
    if artifact_only:
        return command + ['--artifact-only']
    from climate_delivery.config import load_delivery_config
    if len(load_delivery_config(config).recipients) != 4:
        raise Blocked('delivery_requires_four_configured_recipients')
    return command + ['--config', str(config)] + (['--dry-run'] if dry_run else [])


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


def dispatch(
    command, slot, day, dry_run, *, resume_run_id=None, delivery_no_send=False,
):
    def run(args):
        try:
            return subprocess.run(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).returncode
        except OSError:
            return 127
    if not command:
        return 0
    if slot == 'monitor':
        if command == ['managed']:
            try:
                return dispatch_managed_monitor(
                    day, dry_run=dry_run, resume_run_id=resume_run_id,
                )
            except Exception:
                record_monitor_result(day, None, 2, dry_run=dry_run)
                return 2
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
        # Produce and verify the PDF before any optional sending invocation.
        # Artifact-only mode never loads mail configuration or calls delivery.
        validation_command = (
            command if dry_run or delivery_no_send else command + ['--dry-run']
        )
        rc = run(validation_command)
        if rc:
            return rc
        try:
            verify_delivery_artifact(day, expected)
            verify_monitor(path_env('CLIMATE_REPORT_PATH'), day)
        except Exception:
            return 2
        return 0 if dry_run or delivery_no_send else run(command)
    return run(command)


def _managed_result_parts(result):
    """Validate the terminal outcome paired with its exact report receipt."""
    if not isinstance(result, dict) or "terminal" not in result:
        return result, None
    if set(result) != {'terminal', 'report'} or not isinstance(result['terminal'], dict):
        raise Blocked('managed_result_shape_mismatch')
    terminal = result['terminal']
    reportability = terminal.get('reportability')
    outcome = terminal.get('outcome')
    reportability_fields = {
        'schema_version', 'outcome', 'reportable', 'full_coverage',
        'selected_record_count', 'counts', 'limitations',
        'acquisition_payload_sha256',
    }
    count_fields = {
        'successful_sources', 'source_gaps', 'coverage_warnings',
        'failed_searches', 'unresolved_items', 'blocked_tool_prechecks',
    }
    if (terminal.get('exit_code') != 0 or terminal.get('execution_complete') is not True
            or outcome not in {'completed', 'completed_with_gaps', 'no_eligible_information'}
            or not isinstance(reportability, dict)
            or set(reportability) != reportability_fields
            or reportability.get('schema_version') != 'climate-reportability.v1'
            or reportability.get('outcome') != outcome
            or reportability.get('full_coverage') is not terminal.get('full_coverage')
            or not isinstance(reportability.get('counts'), dict)
            or set(reportability['counts']) != count_fields
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                   for value in reportability['counts'].values())
            or not isinstance(reportability.get('limitations'), list)
            or any(not isinstance(value, str) or not value.strip()
                   for value in reportability['limitations'])
            or not isinstance(reportability.get('acquisition_payload_sha256'), str)
            or len(reportability['acquisition_payload_sha256']) != 64
            or any(char not in '0123456789abcdef'
                   for char in reportability['acquisition_payload_sha256'])):
        raise Blocked('managed_terminal_outcome_mismatch')
    reportable = reportability.get('reportable')
    selected = reportability.get('selected_record_count')
    if not isinstance(selected, int) or isinstance(selected, bool) or selected < 0:
        raise Blocked('managed_terminal_outcome_mismatch')
    if outcome == 'no_eligible_information':
        if reportable is not False or selected != 0 or result['report'] is not None:
            raise Blocked('managed_no_report_outcome_mismatch')
    elif (reportable is not True or not isinstance(selected, int)
          or isinstance(selected, bool) or selected < 1
          or not isinstance(result['report'], dict)
          or (outcome == 'completed') is not (terminal.get('full_coverage') is True)):
        raise Blocked('managed_reportable_outcome_mismatch')
    return result['report'], terminal


def record_monitor_result(day, result, rc, *, dry_run):
    """Append the existing ledger contract after the production driver exits."""
    from climate_monitor.run_ledger import append_attempt, build_report_identity
    from climate_monitor.semantic_bundle import verify_semantic_sidecar

    report_result, terminal = _managed_result_parts(result)
    finished = datetime.now(timezone.utc)
    attempt_id = f'{finished:%Y%m%dt%H%M%Sz}-monitor-{secrets.token_hex(4)}'
    if terminal:
        try:
            finished = datetime.fromisoformat(
                str(terminal['finished_at']).replace('Z', '+00:00')
            ).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError) as exc:
            raise Blocked('managed_terminal_finished_at_mismatch') from exc
        run_id = terminal.get('run_id')
        attempt_number = terminal.get('attempt')
        if (not isinstance(run_id, str) or not run_id
                or not isinstance(attempt_number, int) or isinstance(attempt_number, bool)
                or attempt_number < 1):
            raise Blocked('managed_terminal_identity_mismatch')
        attempt_id = f'managed-{run_id.lower()}-attempt-{attempt_number}'
    attempt = {
        'schema_version': 'weekly-run-attempt.v1',
        'attempt_id': attempt_id,
        'stage': 'monitor', 'report_date': day,
        'scheduled_for': schedule.stamp(schedule.occurrence(day, 'monitor')),
        'finished_at': finished.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'status': 'failed', 'result_code': f'monitor_exit_{rc}',
    }
    outcome = terminal.get('outcome') if terminal else None
    if rc == 0 and outcome == 'no_eligible_information':
        attempt.update(status='no_change', result_code='no_eligible_information')
    elif rc == 0:
        report = path_env('CLIMATE_SOURCE_DIR', directory=True) / f'climate-monitor-{day}.md'
        identity = build_report_identity(report_date=day, filename=report.name, sha256=sha(report))
        if (not isinstance(report_result, dict) or report_result.get('report_date') != day
                or report_result.get('report_path') != report.name
                or report_result.get('report_sha256') != identity.sha256):
            raise Blocked('monitor_result_identity_mismatch')
        verify_semantic_sidecar(report)
        from climate_monitor.weekly_monitor.authoring_contract import _validate_v2_stats_shape
        stats = _validate_v2_stats_shape(report_result.get('stats'))
        stats_partial = bool(stats['failed'] + stats['blocked'] + stats['unresolved'])
        if terminal and outcome == 'completed' and stats_partial:
            raise Blocked('managed_reportability_stats_mismatch')
        partial = outcome == 'completed_with_gaps' or stats_partial
        attempt.update(
            report=identity.as_record(),
            result_code='completed_with_gaps' if partial else 'report_written',
            status='partial' if partial else 'success',
        )
    # Dry-run validation exercises the same identity checks but never appends
    # success to the live ledger or makes a production scheduler slot complete.
    if not dry_run:
        append_attempt(path_env('CLIMATE_RUN_LEDGER_DIR', directory=True), attempt, repository_root=ROOT)


def monitor_scheduler_result_code(
    day: str, *, now: datetime | None = None,
) -> str | None:
    """Project the last truthful monitor outcome into the scheduler snapshot."""
    from climate_monitor.run_ledger import RunLedgerReader
    status = RunLedgerReader(
        path_env('CLIMATE_RUN_LEDGER_DIR', directory=True), repository_root=ROOT
    ).status(now=now)
    attempt = status['stages']['monitor']['last_attempt']
    if not attempt or attempt.get('report_date') != day:
        return None
    if attempt.get('result_code') == 'no_eligible_information':
        return 'no_eligible_information'
    if (attempt.get('result_code') == 'completed_with_gaps'
            or attempt.get('status') == 'partial'):
        return 'completed_with_gaps'
    return None


def downstream_no_report_outcome(
    day: str, *, now: datetime | None = None,
) -> str | None:
    """Return the exact completed monitor outcome that makes later slots no-ops."""
    from climate_monitor.job_status import validate_snapshot
    from climate_monitor.run_ledger import RunLedgerReader

    status_dir = path_env('CLIMATE_JOB_STATUS_DIR', directory=True)
    current = now or datetime.now(timezone.utc)
    try:
        snapshot = validate_snapshot(
            json.loads((status_dir / 'scheduler-status.json').read_text()),
            now=current,
        )
    except FileNotFoundError:
        return None
    monitor = snapshot['jobs']['monitor']
    if not (
        monitor['state'] == 'completed'
        and monitor.get('result_code') == 'no_eligible_information'
        and monitor['scheduled_for'] == schedule.stamp(schedule.occurrence(day, 'monitor'))
    ):
        return None
    attempt = RunLedgerReader(
        path_env('CLIMATE_RUN_LEDGER_DIR', directory=True), repository_root=ROOT
    ).status()['stages']['monitor']['last_attempt']
    if (attempt and attempt.get('report_date') == day
            and attempt.get('status') == 'no_change'
            and attempt.get('result_code') == 'no_eligible_information'):
        return 'no_eligible_information'
    return None


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
    scheduled = schedule.occurrence(day, 'registry')
    if now >= scheduled and schedule.period_date(now) == date.fromisoformat(day):
        update_slot('registry', 'not_dispatched', scheduled_for=schedule.stamp(scheduled),
                    status_dir=status_dir, result_code=result_code)


def _validate_managed_recovery_binding(service, definition, binding, run_id, day):
    from climate_monitor.management import managed_report_inputs

    if (binding.get('run_id') != run_id
            or binding.get('trigger') != 'scheduled'):
        raise Blocked('managed_recovery_requires_scheduled_run')
    if binding.get('report_date') != day:
        raise Blocked('managed_recovery_report_date_mismatch')
    if (binding.get('task_id') != definition.get('task_id')
            or binding.get('definition', {}).get('runtime') != definition.get('runtime')):
        raise Blocked('managed_recovery_task_runtime_mismatch')
    expected_paths = managed_report_inputs(binding['definition'], run_id)
    if (binding.get('report_inputs') != expected_paths
            or Path(binding.get('checkpoint_dir', '')).resolve()
            != (service.runtime_root / run_id / 'checkpoint').resolve()
            or Path(binding.get('frozen_report_input', '')).resolve()
            != (service.runtime_root / run_id / 'frozen-report-input.json').resolve()
            or Path(binding.get('registry_database', '')).resolve()
            != Path(definition['runtime']['registry_database']).resolve()):
        raise Blocked('managed_recovery_bound_path_mismatch')
    return expected_paths


def managed_monitor_preflight(
    day, *, planning=False, dry_run=False, resume_run_id=None,
):
    from climate_monitor.management import ManagementService, _resolved_report_date, managed_report_inputs
    from scripts.run_agent_acquisition import _validate_agent_prompt_protocol
    from climate_monitor.request_budget import (
        AGENT_PROTOCOL_VERSION, CANDIDATE_RECEIPT_POLICY,
        PROVIDER_NATIVE_SEARCH_POLICY,
    )

    service = ManagementService.from_environment()
    definition = service.store.load()['definition']
    binding = None
    if resume_run_id is not None:
        try:
            binding = service.binding(resume_run_id)
        except (KeyError, OSError, ValueError) as exc:
            raise Blocked('managed_recovery_run_not_found') from exc
    if (resume_run_id is None
            and _resolved_report_date(definition['parameters']).isoformat() != day
            and not (planning and definition['parameters']['report_date'] == 'auto')):
        raise Blocked('managed_report_date_mismatch')
    _validate_agent_prompt_protocol(binding or {
        'definition': definition,
        'prompt_versions': {
            k: v['version'] for k, v in definition['prompts'].items()
        },
        'agent_protocol': {
            'version': AGENT_PROTOCOL_VERSION,
            'search_policy': PROVIDER_NATIVE_SEARCH_POLICY,
            'candidate_policy': CANDIDATE_RECEIPT_POLICY,
        },
    })
    if resume_run_id is not None:
        paths = _validate_managed_recovery_binding(
            service, definition, binding, resume_run_id, day,
        )
    else:
        paths = managed_report_inputs(definition, 'preflight')
    if dry_run:
        workspace = path_env('CLIMATE_DRY_RUN_ROOT', directory=True)
        for value in [*definition['runtime'].values(), str(service.store.active_path),
                      *(paths[key] for key in ('source_dir', 'wiki_dir', 'state_dir'))]:
            if not Path(value).resolve().is_relative_to(workspace):
                raise Blocked('managed_dry_run_path_outside_workspace')
    for key in ('source_dir', 'wiki_dir', 'state_dir'):
        path = Path(paths[key])
        if not path.is_absolute() or path.resolve().is_relative_to(ROOT):
            raise Blocked('managed_generation_requires_external_directory')
    if Path(paths['source_dir']).resolve() != path_env('CLIMATE_SOURCE_DIR', directory=True):
        raise Blocked('managed_source_directory_mismatch')
    path_env('CLIMATE_RUN_LEDGER_DIR', directory=True)
    return service


def _managed_recovery_launch(service, run_id):
    try:
        return service.attach_or_resume(run_id)
    except RuntimeError as exc:
        if 'terminal and non-retryable' in str(exc):
            raise Blocked('managed_recovery_non_retryable') from exc
        raise


def dispatch_managed_monitor(day, *, dry_run, resume_run_id=None):
    service = managed_monitor_preflight(
        day, dry_run=dry_run, resume_run_id=resume_run_id,
    )
    try:
        launched = (
            _managed_recovery_launch(service, resume_run_id)
            if resume_run_id is not None else service.start(trigger='scheduled')
        )
    except RuntimeError as exc:
        raise Blocked(str(exc)) from exc
    binding = service.binding(launched['run_id'])
    run_dir = service.runtime_root / launched['run_id']
    terminal = run_dir / f"attempt-{launched['attempt']}-result.json"
    deadline = time.monotonic() + binding['budgets']['runtime_seconds'] + 300
    rc, result = 124, None
    while time.monotonic() < deadline:
        if terminal.is_file():
            finished = json.loads(terminal.read_text())
            if (finished.get('run_id') != launched['run_id']
                    or finished.get('attempt') != launched['attempt']):
                raise Blocked('managed_result_identity_mismatch')
            rc = finished['exit_code']
            if rc == 0 and finished.get('execution_complete') is not True:
                rc = 75
            if rc == 0:
                if finished.get('outcome') == 'no_eligible_information':
                    result = {'terminal': finished, 'report': None}
                else:
                    result = {
                        'terminal': finished,
                        'report': json.loads(
                            (run_dir / f"attempt-{launched['attempt']}-report-result.json").read_text()
                        ),
                    }
            break
        time.sleep(2)
    record_monitor_result(day, result, rc, dry_run=dry_run)
    return rc


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('slot', choices=SCHEDULE)
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--managed', action='store_true', help='Use the saved management task for monitor')
    parser.add_argument('--scheduled', action='store_true', help='Guard a Hermes tick by the anchored ET fortnight')
    parser.add_argument('--resume-run-id', help='Explicitly resume/reconcile one scheduled managed run')
    args = parser.parse_args(argv)
    reportless_outcome = None
    if args.resume_run_id and (
        args.slot != 'monitor' or not args.managed or args.scheduled or args.preflight
    ):
        print(json.dumps({'status': 'preflight_failed',
                          'result_code': 'invalid_managed_recovery_mode'}))
        return 2
    if args.scheduled and not args.preflight:
        now = datetime.now(timezone.utc)
        if not schedule.biweekly():
            raise ValueError('--scheduled requires CLIMATE_SCHEDULE=biweekly-et')
        if not schedule.due(args.slot, now):
            return 0
        os.environ['REPORT_DATE'] = now.astimezone(schedule.ET).date().isoformat()
    try:
        dry_run = os.environ.get('CLIMATE_DRY_RUN') == '1'
        delivery_no_send_value = os.environ.get('CLIMATE_DELIVERY_NO_SEND', '')
        if args.slot == 'email' and delivery_no_send_value not in {'', '0', '1'}:
            raise Blocked('invalid_CLIMATE_DELIVERY_NO_SEND')
        delivery_no_send = (
            args.slot == 'email' and delivery_no_send_value == '1'
        )
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
        if args.slot != 'monitor':
            reportless_outcome = downstream_no_report_outcome(day)
        if reportless_outcome:
            command = []
        elif args.slot == "monitor" and args.managed:
            managed_monitor_preflight(
                day, planning=args.preflight, dry_run=dry_run,
                resume_run_id=args.resume_run_id,
            )
            command = ['managed']
        elif args.slot == "monitor":
            # Read-only preflight consumes the same upstream contract as
            # prepare. The production command owns the later authoring turn.
            staging_for_preflight = path_env(
                "CLIMATE_STAGING_DIR", directory=True, exists=False
            )
            _monitor_prepare_cli_command(day, staging_dir=staging_for_preflight)
            command = monitor_command(day, dry_run=dry_run)
        elif args.slot == 'email':
            command = email_command(
                day, dry_run=dry_run, artifact_only=delivery_no_send,
            )
        else:
            command = globals()[args.slot + "_command"](day, dry_run=dry_run)
        if args.preflight:
            print(json.dumps({'status': 'preflight_passed', 'slot': args.slot,
                              'scheduled_for': schedule.stamp(schedule.occurrence(day, args.slot)),
                              'dry_run': dry_run}))
            return 0
        now = datetime.now(timezone.utc)
        scheduled = schedule.occurrence(day, args.slot)
        if not dry_run and (now < scheduled or schedule.period_date(now) != parsed):
            raise Blocked('outside_scheduled_week_or_before_slot')
    except Exception as exc:
        code = str(exc) if isinstance(exc, Blocked) else 'invalid_contract_or_config'
        if args.slot == 'registry' and not args.preflight and not dry_run and code in {'awaiting_human_merge_deploy', 'registry_disabled', 'registry_write_not_authorized'}:
            record_registry_pending(day, status_dir, code)
        print(json.dumps({'status': 'preflight_failed', 'result_code': code}))
        return 2

    from climate_monitor.scheduler_status import update_slot
    timestamps = {'claimed_at': now, 'started_at': now}
    scheduled_for = schedule.stamp(schedule.occurrence(day, args.slot))
    if not dry_run:
        update_slot(args.slot, 'running', scheduled_for=scheduled_for, status_dir=status_dir, **timestamps)
    rc = dispatch(
        command, args.slot, day, dry_run, resume_run_id=args.resume_run_id,
        delivery_no_send=delivery_no_send,
    )
    if dry_run and args.slot == 'registry':
        record_registry_pending(day, status_dir, f'registry_dry_run_exit_{rc}')
    # Dry runs never turn a production slot into completed or alter its snapshot.
    if not dry_run:
        completion_code = (
            reportless_outcome or (
                monitor_scheduler_result_code(day)
                if args.slot == 'monitor' and rc == 0 else None
            )
        )
        if delivery_no_send and rc == 0 and not reportless_outcome:
            update_slot(
                args.slot, 'not_dispatched', scheduled_for=scheduled_for,
                status_dir=status_dir, finished_at=datetime.now(timezone.utc),
                result_code='delivery_no_send', **timestamps,
            )
        else:
            update_slot(args.slot, 'completed' if rc == 0 else 'failed', scheduled_for=scheduled_for,
                        status_dir=status_dir, finished_at=datetime.now(timezone.utc),
                        result_code=completion_code, **timestamps)
    output_status = (
        'not_dispatched' if delivery_no_send and rc == 0 and not reportless_outcome else
        ('dry_run_passed' if dry_run and rc == 0 else ('completed' if rc == 0 else 'failed'))
    )
    print(json.dumps({'status': output_status, 'slot': args.slot,
                      'result_code': ('delivery_no_send' if delivery_no_send and rc == 0 and not reportless_outcome
                                      else reportless_outcome or f'{args.slot}_exit_{rc}')}))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
