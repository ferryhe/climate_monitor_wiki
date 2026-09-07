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
    """Test fixture acquisition through the four real downstream consumers."""
    from climate_monitor.candidate_aggregation import combine_current_artifacts, items_from_merged_candidates_with_carry
    from climate_monitor.article_content_adapter import build_article_evidence_artifact
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
