"""Single isolated chain acceptance scenario, also invoked by the dry-run CLI."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_full_chain_and_production_snapshots(tmp_path):
    script = ROOT / 'scripts/dryrun_full_pipeline.py'
    config = tmp_path / 'snapshots.json'
    watched = {name: str(tmp_path / name) for name in ('seen', 'delivery', 'registry', 'scheduler')}
    config.write_text(json.dumps(watched))
    result = subprocess.run([sys.executable, str(script), '--snapshot-config', str(config)], capture_output=True, text=True)
    assert result.returncode != 0  # Missing production inputs must never count as PASS.
    assert 'snapshot_configuration_incomplete' in result.stdout


def test_full_chain_all_four_consumers(tmp_path):
    from scripts.dryrun_full_pipeline import run_chain
    result = run_chain(tmp_path)
    assert result['stages'] == ['A/B', 'evidence', 'authoring', 'MD/sidecar',
        'PDF', 'delivery-dry-run', 'publisher-no-push-plan',
        'monitor-ledger', 'CLI prepare+finalize', 'registry-dry-run']
    assert result['unique_urls'] == 3
    assert result['database_sha256_before'] == result['database_sha256_after']
    assert result['tamper_rejected'] is True


def test_full_chain_cli_checks_all_snapshot_domains(tmp_path, monkeypatch, capsys):
    from scripts.dryrun_full_pipeline import run_chain, main, git
    run_chain(tmp_path)
    config = tmp_path / 'snapshots.json'
    config.write_text(json.dumps({'checkout': str(tmp_path / 'deployed'), 'seen': str(tmp_path / 'seen'),
        'delivery': str(tmp_path / 'delivery-state'), 'registry': str(tmp_path / 'registry.sqlite3'),
        'scheduler': str(tmp_path / 'scheduler'), 'rolling_repository': str(tmp_path / 'deployed'),
        'rolling_ref': git(tmp_path / 'deployed', 'symbolic-ref', 'HEAD')}))
    monkeypatch.setattr(sys, 'argv', ['dryrun_full_pipeline.py', '--snapshot-config', str(config)])
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'pipeline_pass'
    assert set(result['before']) == {'checkout', 'seen', 'delivery', 'registry', 'scheduler', 'rolling'}
    assert result['before'] == result['after']


def test_full_chain_failure_still_records_before_after(tmp_path, monkeypatch, capsys):
    import scripts.dryrun_full_pipeline as dry
    config = tmp_path / 'config.json'; config.write_text('{}')
    snapshots = []
    def snapshot(_config):
        snapshots.append(1)
        return {'fixture_hash': 'same'}
    monkeypatch.setattr(dry, 'snapshot', snapshot)
    monkeypatch.setattr(dry, 'run_chain', lambda *_: (_ for _ in ()).throw(RuntimeError('fixture failure')))
    monkeypatch.setattr(sys, 'argv', ['dryrun_full_pipeline.py', '--snapshot-config', str(config)])
    assert dry.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert len(snapshots) == 2
    assert result['before'] == result['after'] == {'fixture_hash': 'same'}
    assert result['status'] == 'failed'
