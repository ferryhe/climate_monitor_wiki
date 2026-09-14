"""Shared governed Runtime fixture for tests replacing per-source acquisition."""

import pytest


@pytest.fixture
def governed_adapter_runtime(monkeypatch):
    from climate_monitor import web_listening_adapter as adapter

    class Runtime:
        @classmethod
        def open(cls, _root):
            return cls()

        def close(self):
            return None

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: Runtime)
