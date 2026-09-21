"""Regression coverage for PR #145 review: writable pipeline paths must
never be guessed from a sibling directory.

step6_render_pdf.py and step8_sync_registry.py write into
CLIMATE_ARTIFACT_ROOT / CLIMATE_REGISTRY_DB / CLIMATE_REGISTRY_BACKUP_DIR.
Before this fix, those fell back to a path derived from the script's own
checkout location (``ROOT.parent / "climate_delivery_artifacts"`` etc.).
Running the script from a plain clone, a git worktree, or any checkout
missing the conventional sibling directories would silently write into a
disconnected, brand-new location instead of failing — a rollback command
could appear to succeed while never touching the real production state.

These tests prove the fix: with the env var unset, both scripts must exit
non-zero and must not create anything on disk, regardless of the working
directory's layout.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _run(
    script: str, *args: str, cwd: Path, script_root: Path = REPO,
    env_extra: dict[str, str] | None = None,
):
    env = dict(os.environ)
    for key in (
        "CLIMATE_ARTIFACT_ROOT",
        "CLIMATE_REGISTRY_DB",
        "CLIMATE_REGISTRY_BACKUP_DIR",
        "CLIMATE_WIKI_HOME",
        "CLIMATE_REPORTS_DIR",
        "CLIMATE_WIKI_SOURCES",
    ):
        env.pop(key, None)
    env["CLIMATE_WIKI_PYTHON"] = PYTHON
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [PYTHON, str(script_root / "scripts" / script), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=60,
    )


@pytest.fixture()
def plain_clone(tmp_path):
    """An isolated git clone of this repo with no sibling directories at all.

    This is the exact scenario the review flagged: a temporary clone or
    worktree used for a manual rollback, sitting somewhere with no
    web_listening/ or climate_monitor_data/ next to it.
    """
    git = shutil.which("git")
    if not git:
        pytest.skip("git is not installed")
    clone_dir = tmp_path / "isolated-checkout"
    subprocess.run(
        [git, "clone", "-q", "--no-hardlinks", str(REPO), str(clone_dir)],
        check=True,
        timeout=60,
    )
    # git clone only includes committed files; exercise the working fixes too.
    for script in ("step6_render_pdf.py", "step8_sync_registry.py"):
        shutil.copy2(REPO / "scripts" / script, clone_dir / "scripts" / script)
    return clone_dir


def test_step6_render_pdf_rejects_missing_artifact_root_from_plain_clone(
    plain_clone, tmp_path
):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "climate-monitor-2026-09-14.md").write_text(
        "# Daily Climate & Actuarial Monitor\n", encoding="utf-8"
    )
    # Confirm the sibling directory this used to silently guess does not
    # exist anywhere near the clone.
    assert not (plain_clone.parent / "climate_delivery_artifacts").exists()

    result = _run(
        "step6_render_pdf.py",
        "--date",
        "2026-09-14",
        cwd=plain_clone,
        script_root=plain_clone,
        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)},
    )
    assert result.args[:2] == [PYTHON, str(plain_clone / "scripts" / "step6_render_pdf.py")]
    assert result.returncode != 0
    assert "CLIMATE_ARTIFACT_ROOT" in result.stdout + result.stderr
    assert not (plain_clone.parent / "climate_delivery_artifacts").exists()


def test_step8_sync_registry_rejects_missing_registry_vars_from_plain_clone(
    plain_clone, tmp_path
):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "climate-monitor-2026-09-14.md").write_text(
        "# Daily Climate & Actuarial Monitor\n", encoding="utf-8"
    )
    guessed_registry_root = plain_clone.parent / "climate_monitor_data"
    assert not guessed_registry_root.exists()

    result = _run(
        "step8_sync_registry.py",
        "--date",
        "2026-09-14",
        "--allow-offcycle",
        cwd=plain_clone,
        script_root=plain_clone,
        env_extra={"CLIMATE_REPORTS_DIR": str(reports_dir)},
    )
    assert result.args[:2] == [PYTHON, str(plain_clone / "scripts" / "step8_sync_registry.py")]
    assert result.returncode != 0
    assert "CLIMATE_REGISTRY_DB" in result.stdout + result.stderr
    # The whole point: no directory tree and no database were created at
    # the location this script used to guess.
    assert not guessed_registry_root.exists()


def test_step8_sync_registry_rejects_missing_backup_dir_even_when_db_is_set(
    plain_clone, tmp_path
):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "climate-monitor-2026-09-14.md").write_text(
        "# Daily Climate & Actuarial Monitor\n", encoding="utf-8"
    )
    db_path = tmp_path / "registry.sqlite3"

    result = _run(
        "step8_sync_registry.py",
        "--date",
        "2026-09-14",
        "--allow-offcycle",
        cwd=plain_clone,
        script_root=plain_clone,
        env_extra={
            "CLIMATE_REPORTS_DIR": str(reports_dir),
            "CLIMATE_REGISTRY_DB": str(db_path),
        },
    )
    assert result.args[:2] == [PYTHON, str(plain_clone / "scripts" / "step8_sync_registry.py")]
    assert result.returncode != 0
    assert "CLIMATE_REGISTRY_BACKUP_DIR" in result.stdout + result.stderr
    assert not db_path.exists()


def test_step6_render_pdf_still_validates_date_without_artifact_root_set(plain_clone):
    """--date validation must not require CLIMATE_ARTIFACT_ROOT.

    The required-var check has to happen after argument parsing/validation,
    not at import time, or every invocation (including --help and bad
    input) would demand the production artifact path unnecessarily.
    """
    result = _run(
        "step6_render_pdf.py", "--date", "../etc/passwd",
        cwd=plain_clone, script_root=plain_clone,
    )
    assert result.args[:2] == [PYTHON, str(plain_clone / "scripts" / "step6_render_pdf.py")]
    assert result.returncode == 1
    assert "invalid --date" in result.stdout


def test_step6_render_pdf_accepts_explicit_artifact_root_from_plain_clone(
    plain_clone, tmp_path
):
    """The fix must not break the legitimate explicit-path case."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "climate-monitor-2026-09-14.md").write_text(
        "# Daily Climate & Actuarial Monitor\n\nNo content.\n", encoding="utf-8"
    )
    artifacts_dir = tmp_path / "explicit-artifacts"

    result = _run(
        "step6_render_pdf.py",
        "--date",
        "2026-09-14",
        cwd=plain_clone,
        script_root=plain_clone,
        env_extra={
            "CLIMATE_REPORTS_DIR": str(reports_dir),
            "CLIMATE_ARTIFACT_ROOT": str(artifacts_dir),
        },
    )
    # Whatever the PDF-rendering outcome (weasyprint may be unavailable in
    # a fresh clone's env), it must get past the artifact-root gate itself:
    # a bare "CLIMATE_ARTIFACT_ROOT is required" failure must not appear.
    assert result.args[:2] == [PYTHON, str(plain_clone / "scripts" / "step6_render_pdf.py")]
    assert "CLIMATE_ARTIFACT_ROOT is required" not in result.stdout


def test_step8_help_without_production_env(tmp_path):
    result = _run("step8_sync_registry.py", "--help", cwd=tmp_path)
    assert result.returncode == 0
    assert "--date" in result.stdout
    assert "is required" not in result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("report_date, error", [
    ("../invalid", "invalid --date"),
    ("20260914", "invalid --date"),
    ("2020-01-07", "not a Monday"),
    ("9999-01-04", "in the future"),
])
def test_step8_date_policy_precedes_env_validation(tmp_path, report_date, error):
    result = _run("step8_sync_registry.py", "--date", report_date, cwd=tmp_path)
    assert result.returncode == 1
    assert error in result.stdout
    assert "CLIMATE_REGISTRY" not in result.stdout + result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", ["", " \t "])
def test_step6_rejects_blank_artifact_root_without_output(tmp_path, value):
    report = tmp_path / "climate-monitor-2026-09-14.md"
    report.write_text("# Test report\n", encoding="utf-8")
    result = _run(
        "step6_render_pdf.py", "--date", "2026-09-14", cwd=tmp_path,
        env_extra={"CLIMATE_REPORTS_DIR": str(tmp_path), "CLIMATE_ARTIFACT_ROOT": value},
    )
    assert result.returncode == 1
    assert "CLIMATE_ARTIFACT_ROOT is required" in result.stdout + result.stderr
    assert list(tmp_path.iterdir()) == [report]


@pytest.mark.parametrize("variable", ["CLIMATE_REGISTRY_DB", "CLIMATE_REGISTRY_BACKUP_DIR"])
@pytest.mark.parametrize("value", ["", " \t "])
def test_step8_rejects_blank_paths_without_state(tmp_path, variable, value):
    report = tmp_path / "climate-monitor-2026-09-14.md"
    report.write_text("# Test report\n", encoding="utf-8")
    environment = {
        "CLIMATE_WIKI_HOME": str(tmp_path),
        "CLIMATE_WIKI_SOURCES": str(tmp_path),
        "CLIMATE_REPORTS_DIR": str(tmp_path),
        "CLIMATE_REGISTRY_DB": str(tmp_path / "registry.sqlite3"),
        "CLIMATE_REGISTRY_BACKUP_DIR": str(tmp_path / "backups"),
    }
    environment[variable] = value
    result = _run(
        "step8_sync_registry.py", "--date", "2026-09-14", cwd=tmp_path,
        env_extra=environment,
    )
    assert result.returncode == 1
    assert f"{variable} is required" in result.stdout + result.stderr
    assert list(tmp_path.iterdir()) == [report]


@pytest.mark.parametrize("script, paths, downstream_error", [
    ("step6_render_pdf.py", ("CLIMATE_ARTIFACT_ROOT",), "markdown not found"),
    ("step8_sync_registry.py", ("CLIMATE_REGISTRY_DB", "CLIMATE_REGISTRY_BACKUP_DIR"),
     "report not found in deployed sources/"),
])
def test_explicit_paths_pass_gates(tmp_path, script, paths, downstream_error):
    environment = {key: str(tmp_path / key) for key in paths}
    environment.update(CLIMATE_REPORTS_DIR=str(tmp_path), CLIMATE_WIKI_SOURCES=str(tmp_path))
    result = _run(script, "--date", "2026-09-14", cwd=tmp_path, env_extra=environment)
    assert result.returncode == 1
    assert downstream_error in result.stdout + result.stderr
    assert "is required" not in result.stdout + result.stderr
    assert not list(tmp_path.iterdir())


def test_step8_initializes_explicit_first_deploy_database(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "climate-monitor-2026-09-14.md").write_text("# Test report\n", encoding="utf-8")
    database = tmp_path / "registry" / "registry.sqlite3"
    result = _run(
        "step8_sync_registry.py", "--date", "2026-09-14", cwd=tmp_path,
        env_extra={
            "CLIMATE_WIKI_SOURCES": str(sources),
            "CLIMATE_REPORTS_DIR": str(sources),
            "CLIMATE_REGISTRY_DB": str(database),
            "CLIMATE_REGISTRY_BACKUP_DIR": str(tmp_path / "backups"),
        },
    )
    # Initialization precedes report-content validation by the Registry CLI.
    assert f"Initialized registry database: {database}" in result.stdout
    assert database.is_file()
