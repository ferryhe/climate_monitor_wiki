"""Only the repository report entrypoint may bypass leaf tracing."""
import ast
from pathlib import Path
import sys

import pytest

from climate_monitor import managed_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('command', [
    [sys.executable, '-c', 'pass'],
    [sys.executable, str(ROOT / 'scripts/run_meeting_extraction.py')],
    ['/bin/sh', str(ROOT / 'scripts/run_climate_monitor.py')],
    [sys.executable, '-I', str(ROOT / 'scripts/run_climate_monitor.py')],
    [sys.executable, str(ROOT / 'scripts/run_climate_monitor.py'), '--article-evidence-loopback', 'untrusted:call'],
])
def test_host_runner_rejects_non_report_commands_before_launch(command, monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *a, **k: pytest.fail('untrusted host launch'))
    with pytest.raises(runtime.ManagedFailure):
        runtime.run_report_host(command, state_dir=tmp_path)


@pytest.mark.parametrize('source', [
    'import subprocess as sp; sp.Popen(["bad"])',
    'from subprocess import run as execute; execute(["bad"])',
    'import os; os.posix_spawn("bad", [], {})',
    'import os as system; system.fork()',
    'import multiprocessing as mp; mp.Process(target=lambda:None).start()',
    'import asyncio; asyncio.create_subprocess_exec("bad")',
    'from concurrent.futures import ProcessPoolExecutor; ProcessPoolExecutor()',
    'import ctypes; ctypes.CDLL(None)',
])
def test_host_source_gate_rejects_unmanaged_launches(source):
    with pytest.raises(ValueError):
        runtime.validate_report_worker_source(source)


def test_host_source_gate_accepts_managed_leaf():
    runtime.validate_report_worker_source(
        'from climate_monitor.managed_runtime import run_managed\nrun_managed(["hermes"])')


def test_report_execution_sources_have_no_unmanaged_launch():
    for name in [
        'scripts/run_climate_monitor.py',
        'climate_monitor/hermes_identity.py',
        'climate_monitor/hermes_auth_state.py',
        'climate_monitor/weekly_monitor/driver.py',
        'climate_monitor/orchestrator.py',
        'climate_monitor/article_content_adapter.py',
    ]:
        runtime.validate_report_worker_source((ROOT / name).read_text())


def test_acquisition_uses_host_runner_only_for_report():
    tree = ast.parse((ROOT / 'scripts/run_agent_acquisition.py').read_text())
    caller = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_run_report')
    calls = [n.func.id for n in ast.walk(caller) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert 'run_report_host' in calls
    assert 'run_managed' not in calls
