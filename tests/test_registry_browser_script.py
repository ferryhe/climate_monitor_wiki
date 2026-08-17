from __future__ import annotations

import builtins
import runpy
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_registry_browser.py"


def _simulate_missing_playwright(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    real_import = builtins.__import__
    attempted: list[str] = []

    def import_without_playwright(name, *args, **kwargs):
        if name == "playwright.sync_api":
            attempted.append(name)
            raise ImportError("simulated missing optional dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_playwright)
    return attempted


def test_import_does_not_require_playwright(monkeypatch):
    attempted = _simulate_missing_playwright(monkeypatch)

    namespace = runpy.run_path(str(SCRIPT))

    assert callable(namespace["main"])
    assert attempted == []


def test_direct_execution_exits_two_when_playwright_is_missing(monkeypatch, capsys):
    _simulate_missing_playwright(monkeypatch)

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(SCRIPT), run_name="__main__")

    assert raised.value.code == 2
    assert "pip install playwright==1.62.1" in capsys.readouterr().err
