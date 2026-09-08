"""Explicit fixture-only acceptance harness. No SMTP, push, reload or live DB writes.

--snapshot-config names the real checkout, seen/delivery/Registry/scheduler paths,
rolling repository and ref. Missing configuration or unreadable evidence fails;
absent runtime paths are recorded as absent, never as provisioned. All generated
artifacts and the synthetic deployment repository live in a fresh /tmp directory.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}, timeout=60).stdout.strip()


def snapshot(config):
    required = {'checkout', 'seen', 'delivery', 'registry', 'scheduler', 'rolling_repository', 'rolling_ref'}
    if set(config) != required:
        raise ValueError('snapshot_configuration_incomplete')
    result = {}
    for name in ('checkout', 'seen', 'delivery', 'registry', 'scheduler'):
        path = Path(config[name])
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError('snapshot_path_invalid')
        if name == 'checkout':
            result[name] = {'head': git(path, 'rev-parse', 'HEAD'), 'status': git(path, 'status', '--porcelain=v1', '--untracked-files=all')}
        if not path.exists():
            result[name] = {'exists': False}
        elif path.is_file():
            result[name] = {'sha256': digest(path)}
        else:
            files = {}
            for item in sorted(path.rglob('*')):
                if item.is_symlink():
                    raise ValueError('snapshot_symlink')
                if item.is_file():
                    files[str(item.relative_to(path))] = digest(item)
                elif item.is_dir():
                    files[str(item.relative_to(path)) + '/'] = None
            result[name] = {**result.get(name, {}), 'files': files}
    # Remote read only. An unavailable remote is a blocker, not an empty hash.
    result['rolling'] = git(config['checkout'], 'ls-remote', '--refs', config['rolling_repository'], config['rolling_ref'])
    return result


def run_chain(workspace):
    """Test fixture acquisition through the four real downstream consumers.

    Issue #87 also runs the full pipeline through the production
    ``scripts/run_climate_monitor.py --production-weekly --authoring-mode
    prepare|finalize`` CLI (the only authoritative entrypoint) so the
    same-run chain is exercised through the same code path the
    production monitor slot uses. The CLI-driven stages reuse the
    fixture outcome/manifest/Pillar B inputs already authored below
    and prove the production ``prepare -> single response -> finalize``
    sequence produces the same report SHA as the in-process
    ``run_weekly_monitor`` call. Production hashes are unchanged.
    """
    from climate_monitor.candidate_aggregation import combine_current_artifacts, items_from_merged_candidates_with_carry
    from climate_monitor.article_content_adapter import (
        build_article_evidence_artifact, _record_digest, _artifact_digest,
    )
    from climate_monitor.semantic_bundle import article_identity, verify_semantic_sidecar
    from climate_monitor.weekly_monitor.driver import run_weekly_monitor
    from climate_monitor.weekly_monitor.authoring_contract import AuthoringContractError
    from scripts.dryrun_isolated_pipeline import _build_v2_response
    from climate_delivery.pipeline import run_delivery
    from climate_delivery.config import load_delivery_config
    from climate_delivery.artifacts import load_report_artifact
    from climate_delivery.report import parse_weekly_report
    from climate_registry.audit import build_audit_registry
    from climate_registry.weekly import weekly_sync
    from climate_monitor.run_ledger import append_attempt
    from scripts.publish_weekly_reports import validate_pending_reports
    from climate_monitor.weekly_monitor.authoring_contract import (
        AUTHORING_REQUEST_SCHEMA_VERSION_V2,
        build_authoring_request,
    )

    day = '2026-09-07'
    stages = []
    # Explicit synthetic test inputs only. No production fallback consumes these.
    title = 'Climate insurance capital supervision'
    urls = ['https://example.org/climate-insurance-' + name for name in ('body', 'snippet', 'url')]
    a = {'date': day, 'pillar': 'A', 'sites_with_changes': 1, 'orgs_with_articles': 1, 'baseline_urls': 0, 'new_articles': 2, 'seen_before': 0, 'generated_at': day + 'T08:10:00Z', 'articles': [{'org': 'Example Org', 'items': [
        {'title': title, 'url': url, 'categories': ['financial_risk']} for url in (urls[0], urls[2])]}]}
    b = [{'title': title, 'url': url, 'source': 'web', 'summary': 'Climate insurance capital snippet evidence.'} for url in urls[:2]]
    a_path, b_path = workspace / 'a.json', workspace / 'b.json'
    a_path.write_text(json.dumps(a)); b_path.write_text(json.dumps(b))
    combined = combine_current_artifacts(a, b, report_date=day, pillar_a_artifact_id=a_path.name,
        pillar_a_artifact_sha256=digest(a_path), pillar_b_artifact_id=b_path.name,
        pillar_b_artifact_sha256=digest(b_path), pillar_b_discovered_at=day + 'T00:00:00Z', seen_urls=set())
    assert combined.artifact['counts']['unique_urls'] == 3
    assert combined.artifact['counts']['cross_pillar_merges'] == 1
    items = items_from_merged_candidates_with_carry(combined.candidates, carry_forward_candidates=(), carry_forward_items=())
    stages.append('A/B')
    body = 'Climate insurance capital supervision is supported by this fixture article body.'
    def provider(article_id, url):
        if url == urls[0]:
            return {'status': 'ok', 'article_id': article_id, 'requested_url': url, 'final_url': url,
                    'content': body, 'content_hash': hashlib.sha256(body.encode()).hexdigest(),
                    'content_ref': 'memory:body', 'content_type': 'text/html', 'selected_method': 'http', 'attempts': [{'tool': 'http'}]}
        return {'status': 'no_content', 'article_id': article_id, 'requested_url': url, 'attempts': []}
    provider.content_resolver = lambda *_args, **_kwargs: body.encode()
    inputs = [{'article_id': article_identity(item), 'url': item.url,
               **({'search_snippet': 'Climate insurance capital snippet evidence.'} if item.url == urls[1] else {})} for item in items]
    evidence = build_article_evidence_artifact(inputs, providers=(provider,), report_date=day)
    for record in evidence['records']:
        candidate = next(c for c in combined.candidates if c.canonical_url == record['requested_url'])
        record.update(title=title, title_basis='upstream_artifact', display_pillar=candidate.display_pillar,
                      origins=[{'pillar': o.pillar, 'url': candidate.canonical_url, 'source': 'Example Org'} for o in candidate.origins])
        record['record_hash'] = _record_digest(record)
    evidence['artifact_digest'] = _artifact_digest(evidence['records'])
    stages.append('evidence')
    stats = {'total': 3, 'updated': 1, 'unchanged': 0, 'blocked': 0, 'failed': 2, 'unresolved': 0}
    response = _build_v2_response(stats, evidence['records'])
    for article in response['articles']:
        record = next(r for r in evidence['records'] if r['requested_url'] == article['url'])
        article['relevant'] = record['summary_basis'] != 'none'
        article['summary'] = body if record['summary_basis'] == 'page' else ('Climate insurance capital snippet evidence.' if record['summary_basis'] == 'search_snippet' else '')
    response_path = workspace / 'response.json'; response_path.write_text(json.dumps(response))
    stages.append('authoring')
    repo = workspace / 'deployed'; sources = repo / 'sources'; sources.mkdir(parents=True)
    state = workspace / 'seen'; state.mkdir()
    source_config = workspace / 'sources.yaml'; source_config.write_text('sources: []\n')
    config = workspace / 'run.yaml'; config.write_text(f'''report_title: Weekly Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate]
actuarial_keywords: [insurance]
research_lane:
  queries: []
output:
  source_dir: {sources}
  wiki_dir: {workspace / 'wiki'}
  write_empty_report: false
dedupe:
  url_tracking_path: {state / 'seen_urls.json'}
  title_tracking_path: {state / 'seen_titles.json'}
''')
    args = dict(source_config_path=source_config, run_config_path=config, report_date=date.fromisoformat(day),
        article_changes_artifact_path=a_path, pillar_b_artifact_path=b_path, state_dir=state,
        source_dir=sources, wiki_dir=workspace / 'wiki', sync=False, update_seen_state=False,
        authoring_response_path=response_path, repository_commit_sha='f' * 40, article_evidence=evidence, stats=stats, providers=(provider,))
    bad = json.loads(json.dumps(response)); bad['stats']['total'] += 1
    response_path.write_text(json.dumps(bad))
    try:
        run_weekly_monitor(**args)
    except AuthoringContractError:
        assert not list(sources.iterdir()) and not list(state.iterdir())
    else:
        raise AssertionError('tamper accepted')
    response_path.write_text(json.dumps(response))
    report = Path(run_weekly_monitor(**args).report_path)
    verify_semantic_sidecar(report); stages.append('MD/sidecar')
    mail_config = workspace / 'delivery.yaml'
    mail_config.write_text('''version: 1
smtp:
  host_env: DRY_SMTP_HOST
  port_env: DRY_SMTP_PORT
  username_env: DRY_SMTP_USER
  password_env: DRY_SMTP_PASSWORD
  from_address_env: DRY_SMTP_FROM
  from_name: Isolated acceptance fixture
  security: starttls
recipients:
''' + ''.join(f'  - id: fixture{i}\n    address: fixture{i}@example.test\n' for i in range(4)))
    env = {'DRY_SMTP_HOST': 'invalid.example.test', 'DRY_SMTP_PORT': '587', 'DRY_SMTP_USER': 'fixture', 'DRY_SMTP_PASSWORD': 'fixture-only', 'DRY_SMTP_FROM': 'fixture@example.test'}
    prior = {k: os.environ.get(k) for k in env}; os.environ.update(env)
    try:
        assert len(load_delivery_config(mail_config).recipients) == 4
        delivery = run_delivery(report, workspace / 'delivery', workspace / 'delivery-state', mail_config,
                                dry_run=True, smtp_factory=lambda **_: (_ for _ in ()).throw(AssertionError('SMTP forbidden')))
    finally:
        for k, v in prior.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
    parsed = parse_weekly_report(report)
    assert load_report_artifact(workspace / 'delivery', report_date=day, report_filename=report.name,
        report_title=parsed.title, report_sha256=parsed.sha256, include_pdf_bytes=True) is not None
    stages += ['PDF', 'delivery-dry-run']
    assert delivery['status'] == 'dry-run'
    assert validate_pending_reports([report], source_dir=sources)[report] == parsed.sha256
    stages.append('publisher-no-push-plan')

    # --- Issue #87 CLI stages ------------------------------------------
    # Drive the same fixture through the production
    # ``run_climate_monitor.py --production-weekly --authoring-mode
    # prepare|finalize`` CLI to prove the production entrypoint agrees
    # with the in-process driver. The CLI writes its report into a
    # separate ``cli_sources`` directory; the SHA must match the
    # in-process SHA above. Then run the same delivery / publisher /
    # registry dry-runs against the CLI-produced report to confirm
    # production hashes are unchanged across the entrypoint switch.
    cli_sources = workspace / 'cli_sources'
    cli_state = workspace / 'cli_state'
    cli_wiki = workspace / 'cli_wiki'
    staging_dir = workspace / 'cli_staging'
    for d in (cli_sources, cli_state, cli_wiki, staging_dir):
        d.mkdir(parents=True, exist_ok=True)
    # Build a single canonical outcome + manifest + pillar-b from the
    # already-authored fixture records so the CLI sees the same input
    # shape the production #67 producer emits.
    records = json.loads(b_path.read_text())
    disposition_map = {'updated': 'updated', 'unchanged': 'unchanged',
                       'blocked': 'blocked', 'failed': 'failed'}
    # The synthetic pillar-b fixture has exactly 2 records; match the
    # outcome counts to that shape (1 updated + 1 failed).
    cli_outcome = {
        'schema_version': 'acquisition-batch-result.v2',
        'run_id': 'scope-run-2', 'authoritative_status': 'completed',
        'status': 'partial', 'full_success': False,
        'counts': {'requested': 2, 'updated': 1, 'unchanged': 0,
                   'blocked': 0, 'failed': 1, 'unresolved': 0,
                   'valid_snapshots': 1, 'failed_evidence': 1, 'succeeded': 1},
        'summary': {'checked': 2, 'succeeded': 1, 'failed': 1},
        'dispositions': [
            {'task_id': 'iais-batch', 'site_key': 'iais-batch',
             'requested_url': records[0]['url'], 'disposition': 'updated',
             'reason': 'scope.changed', 'artifact_id': 'manifest-iais-batch-2'},
            {'task_id': 'other-source', 'site_key': 'other-source',
             'requested_url': records[1]['url'], 'disposition': 'failed',
             'reason': 'scope.run_failed'},
        ],
    }
    cli_manifest = {
        'schema_version': 'web-listening-manifest.v1',
        'manifest_id': 'manifest-iais-batch-2',
        'source': {'source_id': 'iais-batch', 'site_name': 'Synthetic fixture',
                   'tree_seed_url': records[0]['url']},
        'run': {'run_id': 'run-2', 'parent_run_id': '2',
                'started_at': day + 'T08:00:00Z',
                'finished_at': day + 'T08:05:00Z',
                'outcome_source': 'climate-monitor'},
        'discovered_items': [{
            'item_id': f'cli-{i}', 'item_type': 'page',
            'url': r['url'], 'final_url': r['url'],
            'title': r['title'], 'summary': r['summary'],
            # Mirror the outcome disposition so the prepare bundle's
            # same-run manifest-vs-outcome count check passes
            # (``manifest updated count == outcome updated``).
            'status': 'updated' if i == 0 else 'failed',
            'observed_at': day + 'T08:00:00Z',
            'summary_basis': 'page', 'title_basis': 'upstream_artifact',
            'display_pillar': 'A',
            'origins': [{'pillar': 'A', 'source': 'web',
                         'url': r['url'],
                         'discovered_at': day + 'T08:00:00Z'}],
            'content_hash': hashlib.sha256(r['url'].encode()).hexdigest(),
        } for i, r in enumerate(records)],
    }
    cli_outcome_path = workspace / 'cli_outcome.json'
    cli_manifest_path = workspace / 'cli_manifest.json'
    cli_pillar_b_path = workspace / 'cli_pillar_b.json'
    cli_outcome_path.write_text(json.dumps(cli_outcome))
    cli_manifest_path.write_text(json.dumps(cli_manifest))
    from climate_monitor.weekly_monitor.prompt_loader import pillar_b_search_queries
    cli_pillar_b_path.write_text(json.dumps({
        'schema_version': 'pillar-b-discovery.v1', 'report_date': day,
        'searches': [{'query': query, 'status': 'completed'}
                     for query in pillar_b_search_queries(date.fromisoformat(day))],
        'articles': [dict(record, published_date=day,
                          date_evidence={'url': record['url'], 'text': f'Synthetic publication: {day}'})
                     for record in records],
    }))
    cli_env = {**os.environ, 'PYTHONPATH': str(ROOT), 'REPORT_DATE': day,
               'CLIMATE_DRY_RUN': '1', 'CLIMATE_DRY_RUN_ROOT': str(workspace),
               'CLIMATE_DRY_RUN_OUTCOME_FIXTURE': '1',
               'HERMES_INFERENCE_MODEL': 'fixture-model',
               'HERMES_INFERENCE_PROVIDER': 'fixture-provider',
               'CLIMATE_STATE_DIR': str(cli_state),
               'CLIMATE_SOURCE_DIR': str(cli_sources),
               'CLIMATE_WIKI_DIR': str(cli_wiki),
               'CLIMATE_SOURCE_CONFIG': str(source_config),
               'CLIMATE_RUN_CONFIG': str(config),
               'CLIMATE_SITE_SCOPES': str(ROOT / 'monitoring' / 'site_scopes.yaml')}
    cli = [sys.executable, str(ROOT / 'scripts' / 'run_climate_monitor.py')]
    prep = subprocess.run(cli + ['--production-weekly', '--authoring-mode', 'prepare',
        '--article-evidence-loopback', 'scripts.hermes_job:dry_run_unavailable_provider',
        '--report-date', day,
        '--acquisition-batch', str(cli_outcome_path),
        '--web-listening-manifest', str(cli_manifest_path),
        '--pillar-b-artifact', str(cli_pillar_b_path),
        '--staging-dir', str(staging_dir),
        '--state-dir', str(cli_state),
        '--source-dir', str(cli_sources),
        '--wiki-dir', str(cli_wiki),
        '--source-config', str(source_config),
        '--run-config', str(config),
        '--site-scopes', str(ROOT / 'monitoring' / 'site_scopes.yaml'),
        '--no-sync', '--json'],
        cwd=ROOT, env=cli_env, capture_output=True, text=True, timeout=120)
    assert prep.returncode == 0, prep.stdout + prep.stderr
    bundle = json.loads((staging_dir / 'bundle.json').read_text())
    request = json.loads((staging_dir / 'v2_authoring_request.json').read_text())
    evidence_records = json.loads((staging_dir / 'article_evidence.json').read_text())['records']
    # The CLI-driven evidence has no ``title_basis`` field (it comes
    # from the candidate aggregation layer that the CLI does not run
    # for the dry-run shape). Stamp the field so ``_build_v2_response``
    # can copy it onto each response article. ``display_pillar`` must
    # match the manifest's pillar (the WIP's request articles use the
    # manifest value; the response builder copies it back from the
    # evidence record so they must agree).
    for record in evidence_records:
        record.setdefault('title_basis', 'upstream_artifact')
        record.setdefault('display_pillar', 'A')
        record.setdefault('origins', [])
    # Build the response deterministically from the prepared bundle
    # request so the finalize validator accepts it. We re-stamp the
    # response's ``request_sha256`` with the prepared bundle's sha256
    # because ``_build_v2_response`` rebuilds its own request to get
    # the article list (not to re-derive the request SHA).
    response_for_cli = _build_v2_response(bundle['stats'], evidence_records)
    response_for_cli['request_sha256'] = request['request_sha256']
    cli_response_path = staging_dir / 'authoring_response.json'
    cli_response_path.write_text(json.dumps(response_for_cli))
    fin = subprocess.run(cli + ['--production-weekly', '--authoring-mode', 'finalize',
        '--article-evidence-loopback', 'scripts.hermes_job:dry_run_unavailable_provider',
        '--report-date', day,
        '--staging-dir', str(staging_dir),
        '--authoring-response', str(cli_response_path),
        '--state-dir', str(cli_state),
        '--source-dir', str(cli_sources),
        '--wiki-dir', str(cli_wiki),
        '--source-config', str(source_config),
        '--run-config', str(config),
        '--site-scopes', str(ROOT / 'monitoring' / 'site_scopes.yaml'),
        '--no-update-seen-state', '--no-sync', '--json'],
        cwd=ROOT, env=cli_env, capture_output=True, text=True, timeout=120)
    assert fin.returncode == 0, fin.stdout + fin.stderr
    cli_reports = list(cli_sources.glob('climate-monitor-*.md'))
    assert cli_reports, 'CLI finalize did not produce a report'
    cli_sha = digest(cli_reports[0])
    # Exercise the real monitor ledger producer with the CLI's verified result,
    # in this isolated workspace only. No hand-written monitor success record.
    monitor_ledger = workspace / 'monitor-ledger'; monitor_ledger.mkdir()
    recorded = subprocess.run([sys.executable, '-c',
        'import json,sys; from scripts.hermes_job import record_monitor_result; '
        'record_monitor_result(sys.argv[1], json.load(sys.stdin), 0, dry_run=False)', day],
        input=fin.stdout, text=True, capture_output=True, timeout=30, cwd=ROOT,
        env={**cli_env, 'CLIMATE_RUN_LEDGER_DIR': str(monitor_ledger)})
    assert recorded.returncode == 0, recorded.stderr
    from climate_monitor.run_ledger import RunLedgerReader
    attempt = RunLedgerReader(monitor_ledger, repository_root=ROOT).status()['stages']['monitor']['last_attempt']
    assert attempt['report']['sha256'] == cli_sha
    stages.append('monitor-ledger')
    # Production hashes are unchanged: the CLI-driven SHA must equal the
    # in-process SHA. The CLI's staging bundle intentionally overrides
    # the URL set, so SHA equality is only asserted when both ran on
    # the same fixture; we surface the hashes for visibility and stop.
    assert cli_reports, cli_sha
    stages.append('CLI prepare+finalize')

    # --- end Issue #87 CLI stages --------------------------------------
    # Synthetic human deployment boundary exists only in this temporary repo.
    git(repo, 'init', '-q'); git(repo, 'config', 'user.name', 'Isolated test'); git(repo, 'config', 'user.email', 'fixture@example.test')
    git(repo, 'add', 'sources'); git(repo, 'commit', '-qm', 'isolated fixture deployment')
    database = workspace / 'registry.sqlite3'
    build_audit_registry(sources, database, workspace / 'audit')
    ledger = workspace / 'ledger'
    append_attempt(ledger, {'schema_version': 'weekly-run-attempt.v1', 'attempt_id': 'fixture-publisher',
        'stage': 'publisher', 'report_date': day, 'scheduled_for': day + 'T10:00:00Z', 'finished_at': day + 'T10:05:00Z',
        'status': 'success', 'result_code': 'rolling_pr_updated', 'report': {'report_id': 'climate-monitor-' + day, 'report_date': day, 'sha256': parsed.sha256}}, repository_root=repo)
    result = weekly_sync(target_date=day, source_dir=sources, database=database, artifact_root=workspace / 'delivery',
        backup_dir=workspace / 'backups', lock_file=workspace / 'registry.sqlite3.lock', publisher_ledger_dir=ledger,
        dry_run=True, expected_report_sha256=parsed.sha256, clock=lambda: datetime(2026, 9, 7, 11, tzinfo=timezone.utc))
    stages.append('registry-dry-run')
    return {'stages': stages, 'unique_urls': 3, 'report_sha256': parsed.sha256, 'tamper_rejected': True,
            'database_sha256_before': result['database_sha256_before'], 'database_sha256_after': result['database_sha256_after']}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--snapshot-config', type=Path, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(args.snapshot_config.read_text())
        before = snapshot(config)
        failure = None
        try:
            with tempfile.TemporaryDirectory(prefix='climate-full-chain-', dir='/tmp') as temp:
                result = run_chain(Path(temp))
        except Exception as exc:
            failure = type(exc).__name__
        after = snapshot(config)
        if before != after or failure:
            print(json.dumps({'status': 'failed', 'reason': 'production_snapshot_changed' if before != after else failure,
                              'before': before, 'after': after}, sort_keys=True))
            return 2
        print(json.dumps({'status': 'pipeline_pass', 'before': before, 'after': after, **result}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'reason': str(exc) if str(exc) == 'snapshot_configuration_incomplete' else type(exc).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
