"""Gateway integration regressions: fakes use the actual adapter injection seams."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from climate_monitor import web_listening_adapter as adapter
from climate_monitor.models import MonitorSource, SiteScope


def source(key="example"):
    return MonitorSource(key=key, abbreviation=key, full_name=key,
                         url=f"https://{key}.test/")


def install_runtime(monkeypatch, *, builder_error=None, page_error=None):
    calls = {"build": [], "fetch": [], "close": 0}
    gateway = SimpleNamespace(user_agent="web-listening-bot/1.0", read=lambda url, **kwargs: None)

    def close():
        calls["close"] += 1
    gateway.close = close

    def builder(**kwargs):
        calls["build"].append(kwargs)
        if builder_error:
            raise builder_error
        return gateway

    class Crawler:
        def __init__(self, *, fetch_mode, read_gateway=None):
            assert read_gateway.gateway is gateway, "actual Crawler must receive governed gateway"
            assert fetch_mode == "http"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def fetch_page(self, url, *, fetch_mode, fetch_config_json):
            calls["fetch"].append((url, fetch_mode, fetch_config_json))
            if page_error is not None:
                raise page_error
            return SimpleNamespace(final_url=url, status_code=200,
                                   fit_markdown="Climate insurance research", markdown="",
                                   content_text="", raw_html="", metadata_json={"links": []})

    diff = dict(compute_hash=lambda text: "a" * 64,
                extract_links=lambda html, url: [],
                find_document_links=lambda links: [],
                find_new_links=lambda old, new: [],
                select_compare_text=lambda **kwargs: kwargs["fit_markdown"])
    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(adapter, "_load_web_listening", lambda: (Crawler, diff))
    # raising=False allows the unchanged baseline to demonstrate the missing seam.
    monkeypatch.setattr(adapter, "_load_gateway_builder", lambda: builder, raising=False)
    return calls


@pytest.mark.parametrize("collector", ["collect_source_items", "collect_website_items",
                                      "collect_website_items_with_evidence"])
def test_missing_gateway_fails_once_before_any_seed(tmp_path, monkeypatch, collector):
    root = RuntimeError("pinned gateway transport unavailable")
    calls = install_runtime(monkeypatch, builder_error=root)
    kwargs = dict(state_dir=tmp_path)
    with pytest.raises(RuntimeError, match="governed gateway preflight.*transport unavailable") as error:
        if collector == "collect_source_items":
            adapter.collect_source_items(source=source(), **kwargs)
        else:
            getattr(adapter, collector)([source(), source("second")], **kwargs)
    assert error.value.__cause__ is root
    assert len(calls["build"]) == 1
    assert calls["fetch"] == []


@pytest.mark.parametrize("collector", ["collect_website_items", "collect_website_items_with_evidence"])
def test_bulk_injects_one_gateway_with_matching_identity(tmp_path, monkeypatch, collector):
    calls = install_runtime(monkeypatch)
    getattr(adapter, collector)([source(), source("second")], state_dir=tmp_path)
    assert len(calls["build"]) == 1
    assert len(calls["fetch"]) == 2
    assert calls["close"] == 1
    config = calls["build"][0]
    assert set(config["seed_urls"]) == {source().url, source("second").url}
    assert all(row[1:] == ("http", {"user_agent": config["user_agent"]})
               for row in calls["fetch"])


def test_browser_classification_is_not_browser_execution(tmp_path, monkeypatch):
    calls = install_runtime(monkeypatch)
    scope = SiteScope(source_key="example", seed_urls=(), include_patterns=(),
                      exclude_patterns=(), fetch_mode="browser",
                      fetch_config_json={"user_agent_profile": "browser", "extra_wait_ms": 2500})
    _, warnings, evidence = adapter.collect_website_items_with_evidence(
        [source()], state_dir=tmp_path, site_scopes=[scope])
    assert not warnings
    result = evidence["source_results"][0]
    assert result["status"] == "succeeded"
    attempt = result["attempts"][0]
    assert attempt["requested_engine"] == "browser"
    assert attempt["effective_engine"] == "governed_http"
    assert calls["fetch"][0][1] == "http"


def test_managed_binding_freezes_scopes_and_gateway(tmp_path, monkeypatch):
    from climate_monitor.config import load_sources
    from climate_monitor.management import default_task_definition, build_task_binding
    import scripts.run_agent_acquisition as runner

    definition = default_task_definition()
    definition["runtime"].update(run_root=str(tmp_path), registry_database=str(tmp_path / "registry.db"))
    from climate_registry.persistent import initialize_registry
    initialize_registry(tmp_path / "registry.db")
    binding = build_task_binding(definition, task_version=1, run_id="frozen", attempt=1)
    records = binding["site_scope_inventory"]["records"]
    assert records
    observed = {}
    def collect(sources, **kwargs):
        observed.update(kwargs)
        return [], [], {"status": "completed", "source_results": []}
    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(adapter, "collect_website_items_with_evidence", collect)
    monkeypatch.setattr("climate_monitor.config.load_site_scopes", lambda path: pytest.fail("must use frozen scopes"))
    runner._controlled_site_context(binding)
    assert observed["gateway_config"] == binding["governed_gateway"]
    assert [asdict(scope) for scope in observed["site_scopes"].values()] == records
    assert len(binding["source_inventory"]["records"]) == 36
    assert len(load_sources("monitoring/supranational_sources.yaml")) == 36


def test_gateway_root_cause_survives_managed_result_and_progress(tmp_path, monkeypatch):
    import json
    from climate_monitor.management import default_task_definition, build_task_binding
    from climate_registry.persistent import initialize_registry
    import scripts.run_agent_acquisition as runner

    calls = install_runtime(monkeypatch, builder_error=RuntimeError("transport version mismatch"))
    definition = default_task_definition()
    definition["runtime"].update(run_root=str(tmp_path), registry_database=str(tmp_path / "registry.db"))
    initialize_registry(tmp_path / "registry.db")
    binding = build_task_binding(definition, task_version=1, run_id="root-cause", attempt=1)
    binding_path = tmp_path / "attempt-1.json"
    binding_path.write_text(json.dumps(binding))
    monkeypatch.setenv("HERMES_EXECUTABLE", "/bin/true")
    monkeypatch.setattr(runner, "_invoke_hermes", lambda *args, **kwargs: pytest.fail("preflight must stop before Hermes"))
    assert runner._execute_locked(binding_path) == 65
    result = json.loads((tmp_path / "attempt-1-result.json").read_text())
    progress = json.loads((tmp_path / "progress.json").read_text())
    for value in (result, progress):
        assert "[tool] governed gateway preflight" in value["error"]
        assert "transport version mismatch" in value["error"]
        assert "budget exhausted" not in value["error"]
    assert len(calls["build"]) == 1
    assert calls["fetch"] == []


def test_real_pinned_public_constructor_and_crawler_without_network(monkeypatch):
    pytest.importorskip("web_listening.blocks.governed_read")
    from web_listening.blocks.governed_read import GovernedReadGateway

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    with adapter._open_governed_runtime([source()], {}) as (crawler, _, config):
        gateway = crawler.http_crawler.read_gateway
        assert isinstance(gateway.gateway, GovernedReadGateway)
        assert gateway.user_agent == config["user_agent"]


def test_real_crawler_reads_through_injected_public_gateway(tmp_path, monkeypatch):
    governed = pytest.importorskip("web_listening.blocks.governed_read")
    import httpx

    sends = []
    def transport(request):
        sends.append(str(request.url))
        return httpx.Response(200, text="<html><body>Climate insurance research</body></html>",
                              headers={"Content-Type": "text/html"})
    client = httpx.Client(transport=httpx.MockTransport(transport))
    gateway = governed.MockClientReadGateway(client, user_agent="web-listening-bot/1.0",
                                             max_body_bytes=4 * 1024 * 1024)
    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(adapter, "_load_gateway_builder", lambda: lambda **kwargs: gateway)
    _, warnings, evidence = adapter.collect_website_items_with_evidence([source()], state_dir=tmp_path)
    assert not warnings
    assert sends == [source().url]
    assert evidence["source_results"][0]["status"] == "succeeded"


def test_browser_source_http_rejection_does_not_claim_success(tmp_path, monkeypatch):
    calls = install_runtime(monkeypatch, page_error=RuntimeError("governed read rejected by policy"))
    scope = SiteScope(source_key="example", seed_urls=(), include_patterns=(),
                      exclude_patterns=(), fetch_mode="browser")
    _, warnings, evidence = adapter.collect_website_items_with_evidence(
        [source()], state_dir=tmp_path, site_scopes=[scope])
    row = evidence["source_results"][0]
    assert len(calls["fetch"]) == 1
    assert row["status"] == "failed"
    assert row["outcome"]["full_success"] is False
    assert row["attempts"][0]["requested_engine"] == "browser"
    assert row["attempts"][0]["effective_engine"] == "governed_http"
    assert "rejected by policy" in row["attempts"][0]["error"]
    assert warnings
