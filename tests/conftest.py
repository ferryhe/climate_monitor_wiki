"""Explicit offline gateway fixture for adapter tests that already replace Crawler."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest


@pytest.fixture
def governed_adapter_runtime(monkeypatch):
    from climate_monitor import web_listening_adapter as adapter

    gateway = SimpleNamespace(user_agent="web-listening-bot/1.0",
                              read=lambda url, **kwargs: None, close=lambda: None)
    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(adapter, "_load_gateway_builder", lambda: lambda **kwargs: gateway)
    # Tests replacing the complete source collector need only preflight; tests
    # of page collection replace this with their existing behavioral Crawler.
    monkeypatch.setattr(adapter, "_load_web_listening", lambda: (
        lambda **kwargs: nullcontext(SimpleNamespace()), {},
    ))
