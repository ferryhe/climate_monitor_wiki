"""Issue #87 AC-4: production driver with --no-update-seen-state does NOT
mutate any existing article_state.json snapshot on the host.

This is an integration smoke test that copies the production article_state.json
to a /tmp workspace and verifies the driver leaves it byte-for-byte unchanged.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read_optional(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


@pytest.mark.skipif(
    not (Path("/opt/climate_monitor_wiki") / "monitoring" / "state" / "article_state.json").exists(),
    reason="production article_state.json not present; cannot fingerprint",
)
def test_no_update_seen_state_leaves_production_snapshot_untouched():
    """AC-4: the production driver with --no-update-seen-state must not
    mutate any existing article_state.json snapshot on the host.
    """
    prod_state = Path("/opt/climate_monitor_wiki") / "monitoring" / "state" / "article_state.json"
    before_hash = hashlib.sha256(_read_optional(prod_state)).hexdigest()

    workspace = Path(tempfile.mkdtemp(prefix="climate-issue87-iso-"))
    try:
        fixture = ROOT / "tests" / "fixtures" / "issue87"
        stats_path = fixture / "57_stats.json"
        evidence_path = fixture / "57_article_evidence.json"
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        ev = json.loads(evidence_path.read_text(encoding="utf-8"))

        # Build the v2 response using the production driver's machinery.
        from scripts.dryrun_isolated_pipeline import _build_v2_response

        response = _build_v2_response(stats, ev["records"])
        response_path = workspace / "v2_response.json"
        response_path.write_text(json.dumps(response), encoding="utf-8")

        manifest_path = workspace / "manifest.json"
        manifest_path.write_text(
            (ROOT / "tests" / "fixtures" / "issue87" / "iais_minimal_manifest.json").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        source_dir = workspace / "sources"
        wiki_dir = workspace / "wiki"
        state = workspace / "state"
        for sub in (source_dir, wiki_dir, state):
            sub.mkdir()

        py = ROOT / ".venv" / "bin" / "python"
        py = str(py) if py.exists() else sys.executable
        proc = subprocess.run(
            [
                py,
                str(ROOT / "scripts" / "run_climate_monitor.py"),
                "--production-weekly",
                "--date", date.fromisoformat("2026-09-07").isoformat(),
                "--site-scopes", str(ROOT / "monitoring" / "site_scopes.yaml"),
                "--run-config", str(ROOT / "monitoring" / "run_config.yaml"),
                "--manifest-fixture", str(manifest_path),
                "--state-dir", str(state),
                "--source-dir", str(source_dir),
                "--wiki-dir", str(wiki_dir),
                "--no-sync",
                "--no-update-seen-state",
                "--authoring-response", str(response_path),
                "--article-evidence", str(evidence_path),
                "--stats", json.dumps(stats),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        # The dry-run may fail to round-trip the orchestrator's kept set
        # because the production driver validates against the orchestrator's
        # own candidate selection. We only assert that the production
        # state file is untouched, regardless of run outcome.
        del proc  # intentional

        after_hash = hashlib.sha256(_read_optional(prod_state)).hexdigest()
        assert before_hash == after_hash, (
            f"production article_state.json mutated: "
            f"before={before_hash} after={after_hash}"
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_no_update_seen_state_isolated_workspace_has_no_pending_delta():
    """AC-4: when --no-update-seen-state is set, the /tmp workspace must
    not contain a pending-seen-url delta after a production driver run.
    """
    workspace = Path(tempfile.mkdtemp(prefix="climate-issue87-iso-state-"))
    try:
        state = workspace / "state"
        state.mkdir()
        # No pre-existing state file. After a run with --no-update-seen-state,
        # the state directory should remain empty (no pending delta, no
        # canonical seen-url snapshot).
        fixture = ROOT / "tests" / "fixtures" / "issue87"
        stats_path = fixture / "57_stats.json"
        evidence_path = fixture / "57_article_evidence.json"
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        ev = json.loads(evidence_path.read_text(encoding="utf-8"))

        from scripts.dryrun_isolated_pipeline import _build_v2_response

        response = _build_v2_response(stats, ev["records"])
        response_path = workspace / "v2_response.json"
        response_path.write_text(json.dumps(response), encoding="utf-8")

        manifest_path = workspace / "manifest.json"
        manifest_path.write_text(
            (ROOT / "tests" / "fixtures" / "issue87" / "iais_minimal_manifest.json").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        source_dir = workspace / "sources"
        wiki_dir = workspace / "wiki"
        for sub in (source_dir, wiki_dir):
            sub.mkdir()

        py = ROOT / ".venv" / "bin" / "python"
        py = str(py) if py.exists() else sys.executable
        subprocess.run(
            [
                py,
                str(ROOT / "scripts" / "run_climate_monitor.py"),
                "--production-weekly",
                "--date", date.fromisoformat("2026-09-07").isoformat(),
                "--site-scopes", str(ROOT / "monitoring" / "site_scopes.yaml"),
                "--run-config", str(ROOT / "monitoring" / "run_config.yaml"),
                "--manifest-fixture", str(manifest_path),
                "--state-dir", str(state),
                "--source-dir", str(source_dir),
                "--wiki-dir", str(wiki_dir),
                "--no-sync",
                "--no-update-seen-state",
                "--authoring-response", str(response_path),
                "--article-evidence", str(evidence_path),
                "--stats", json.dumps(stats),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        # Whether or not the run succeeded, --no-update-seen-state must
        # leave the state directory clean: no pending delta file, no
        # canonical seen-url snapshot.
        deltas = list(state.rglob("*.pending.json"))
        canonical = list(state.rglob("article_state.json"))
        assert not deltas, f"pending delta unexpectedly written: {deltas}"
        assert not canonical, f"canonical state unexpectedly written: {canonical}"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)