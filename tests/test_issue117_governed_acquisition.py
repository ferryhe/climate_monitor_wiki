"""Public Runtime integration regressions for governed Pillar A acquisition."""

from dataclasses import asdict
import json
import os
from pathlib import Path

import pytest

from climate_monitor import web_listening_adapter as adapter
from climate_monitor.models import MonitorSource, SiteScope
from climate_monitor.request_budget import RequestBudget


def source(key="example"):
    return MonitorSource(
        key=key, abbreviation=key, full_name=key, url=f"https://{key}.test/"
    )


def rejected(url, code="robots.denied"):
    return {
        "schema_version": "web-listening-site-explore.v3",
        "status": "rejected",
        "exploration_complete": False,
        "site_state": {
            "schema_version": "web-listening-site-state.v1",
            "site_key": url.split("//", 1)[1].split("/", 1)[0],
            "generated_at": "2026-09-13T00:00:00Z",
            "site_skill_digest": None,
            "complete": False,
            "pages": [],
        },
        "site_skill_candidate": None,
        "site_skill_used": None,
        "discovery": [],
        "target_results": [],
        "attempts": [{
            "attempt_id": "attempt-1", "order": 1,
            "tool_id": "acquisition.web_http", "tool_version": "1.0.0",
            "outcome": "failed", "started_at": "2026-09-13T00:00:00Z",
            "finished_at": "2026-09-13T00:00:01Z", "requested_url": url,
            "final_url": None, "http_status": None,
            "error": {"code": code, "message": "Acquisition rejected."},
            "requests": 1, "bytes_received": 0, "runtime_ms": 1,
            "robots": [],
        }],
        "usage": {"requests": 1, "bytes_received": 0, "runtime_ms": 1,
                  "tool_attempts": 1},
        "stop_reason": "rejected",
        "errors": [{"code": code, "message": "Acquisition rejected."}],
    }


class Result:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return self.payload


class Runtime:
    opened = []

    def __init__(self, root):
        self.root = root
        self.closed = False
        self.requests = []

    @classmethod
    def open(cls, root):
        runtime = cls(root)
        cls.opened.append(runtime)
        return runtime

    def explore_site(self, request):
        self.requests.append(request)
        return Result(rejected(request.scope.seeds[0]))

    def close(self):
        self.closed = True


def install_runtime(monkeypatch):
    Runtime.opened = []
    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: Runtime)


@pytest.mark.parametrize(
    "collector",
    ["collect_source_items", "collect_website_items", "collect_website_items_with_evidence"],
)
def test_missing_runtime_fails_once_before_any_seed(tmp_path, monkeypatch, collector):
    class Missing:
        @classmethod
        def open(cls, root):
            raise RuntimeError("pinned Runtime unavailable")

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: Missing)
    with pytest.raises(RuntimeError, match="governed runtime preflight.*Runtime unavailable"):
        if collector == "collect_source_items":
            adapter.collect_source_items(source=source(), state_dir=tmp_path)
        else:
            getattr(adapter, collector)([source(), source("second")], state_dir=tmp_path)


@pytest.mark.parametrize("collector", ["collect_website_items", "collect_website_items_with_evidence"])
def test_bulk_uses_one_persistent_runtime_and_preserves_each_failure(
    tmp_path, monkeypatch, collector
):
    if os.name == "nt":
        pytest.skip("standalone RequestBudget durability uses POSIX directory fsync")
    install_runtime(monkeypatch)
    output = getattr(adapter, collector)([source(), source("second")], state_dir=tmp_path)
    runtime = Runtime.opened[0]
    assert len(Runtime.opened) == 1
    assert runtime.closed
    assert [request.scope.seeds for request in runtime.requests] == [
        ("https://example.test/",), ("https://second.test/",)
    ]
    assert len(output[1]) == (2 if collector == "collect_website_items_with_evidence" else 4)
    if collector == "collect_website_items_with_evidence":
        assert [row["coverage_status"] for row in output[2]["source_results"]] == [
            "rejected", "rejected"
        ]


def test_config_freezes_new_runtime_revision_and_budgets():
    config = adapter.gateway_configuration([source()], {})
    assert config["schema_version"] == "climate-web-listening-new.v1"
    assert config["upstream_revision"] == "ac2343f89bc7939736d85f049ebe2beac571034a"
    assert config["max_tool_attempts_per_target"] == 4
    assert config["seed_urls"] == ["https://example.test/"]
    assert len(config["authority_sha256"]) == 64


def test_scope_translation_is_bounded_and_keeps_climate_exclusions():
    scope = SiteScope(
        source_key="example", seed_urls=(),
        include_patterns=("/news/", "/reports"), exclude_patterns=("/events/",),
    )
    assert adapter._upstream_include_paths("https://example.test/news/", scope) == (
        "/news/", "/news/**", "/reports", "/reports/**"
    )
    assert adapter._url_allowed("https://example.test/news/item", scope)
    assert not adapter._url_allowed("https://example.test/events/item", scope)


def test_scope_translation_allows_only_each_declared_seed_subtree_and_exact_redirects():
    scope = SiteScope(
        source_key="example", seed_urls=(),
        include_patterns=("/current/redirect",), exclude_patterns=(),
    )
    assert adapter._upstream_include_paths("https://example.test/old", scope) == (
        "/current/redirect", "/current/redirect/**", "/old", "/old/**",
    )


def test_partial_source_keeps_successful_seed_snapshot_candidate_and_disposition(
    tmp_path, monkeypatch,
):
    if os.name == "nt":
        pytest.skip("RequestBudget durability uses POSIX directory fsync")
    install_runtime(monkeypatch)
    item = adapter.CandidateItem(
        title="Climate update", url="https://example.test/news/climate-update",
        summary="Verified update", source_name="example", lane="website",
        detected_at="2026-09-13T00:00:00Z", content_hash="a" * 64,
        source_item_id="artifact-1",
    )

    def run(_runtime, source, _scope, seed_url, *_args):
        if seed_url.endswith("/publications"):
            return {"status": "rejected", "event_kind": "policy",
                    "error": "robots.forbidden", "attempts": [],
                    "candidates": [], "candidate_urls": []}
        candidate = asdict(item)
        candidate["source_item_id"] = (
            "artifact-2" if seed_url.endswith("/climate") else "artifact-1"
        )
        return {"status": "success", "event_kind": "source", "error": None,
                "attempts": [], "candidates": [candidate],
                "candidate_urls": [item.url], "observed_at": item.detected_at,
                "checkpoint": {"schema_version": "climate-web-listening-refresh-context.v1",
                               "upstream_revision": adapter._UPSTREAM_REVISION,
                               "source_key": source.key, "seed_url": seed_url,
                               "site_skill": {}, "site_state": {}}}

    monkeypatch.setattr(adapter, "_run_site_seed", run)
    scope = SiteScope(source_key="example", include_source_url=False,
                      include_patterns=(), exclude_patterns=(),
                      seed_urls=("https://example.test/news",
                                 "https://example.test/climate",
                                 "https://example.test/publications"))
    ledger = RequestBudget(tmp_path / "budget.json", {
        "run_id": "partial", "attempt": 1,
        "budgets": {"fetch_attempts": 8, "search_attempts": 0,
                    "search_results": 0, "retries_per_item": 0,
                    "runtime_seconds": 60},
    })
    items, warnings, evidence = adapter.collect_website_items_with_evidence(
        [source()], state_dir=tmp_path / "state", site_scopes={"example": scope},
        budget=ledger,
    )
    row = evidence["source_results"][0]
    assert [value.url for value in items] == [item.url, item.url]
    assert warnings and row["coverage_status"] == "incomplete"
    assert row["status"] == "partial"
    assert row["outcome"]["status"] == "partial"
    assert row["outcome"]["counts"]["valid_snapshots"] == 1
    assert row["outcome"]["counts"]["requested"] == 1
    assert row["outcome"]["full_success"] is False
    assert row["outcome"]["dispositions"] == [{
        "task_id": "example", "site_key": "example",
        "requested_url": source().url, "disposition": "updated",
        "reason": "scope.partial", "artifact_id": row["artifact_id"],
    }]
    assert row["manifest"]["seed_outcomes"]["https://example.test/news"]["status"] == "success"
    assert row["manifest"]["seed_outcomes"]["https://example.test/climate"]["status"] == "success"
    assert row["manifest"]["seed_outcomes"]["https://example.test/publications"]["status"] == "rejected"
    assert len(row["attempts"]) == 3
    assert {entry["item_id"] for entry in row["manifest"]["discovered_items"]} == {
        "artifact-1", "artifact-2",
    }
    from scripts.run_climate_monitor import _validate_climate_acquisition_outcome
    from scripts.run_agent_acquisition import _write_report_inputs
    _validate_climate_acquisition_outcome(row["outcome"])
    paths = {name: str(tmp_path / f"{name}.json") for name in (
        "acquisition_batch", "web_listening_manifest", "pillar_b_artifact",
    )}
    _write_report_inputs(
        {"source_inventory": {"records": [{"key": "example"}]},
         "report_inputs": paths},
        {"items": [], "report_date": "2026-09-14", "date_policy": {
             "mode": "unlimited", "start": None, "end": None, "days": None,
             "anchor_date": "2026-09-14", "frozen_at": "2026-09-14T00:00:00Z",
         },
         "search_decision": {"status": "no_search", "reason": "no gap search"},
         "searches": []},
        evidence,
    )
    assert len(json.loads(Path(paths["web_listening_manifest"]).read_text())) == 1
    from scripts import run_climate_monitor as monitor
    prepared = monitor._read_prepare_inputs(
        Path(paths["acquisition_batch"]), Path(paths["web_listening_manifest"]),
        Path(paths["pillar_b_artifact"]), report_date="2026-09-14",
        allow_incomplete_pillar_b=True, bound_managed=True,
    )
    assert prepared[4] == {
        "total": 1, "updated": 1, "unchanged": 0,
        "blocked": 0, "failed": 0, "unresolved": 0,
    }
    assert len(prepared[3]) == 2


@pytest.mark.parametrize(
    ("seed_status", "expected_disposition", "expected_status", "valid_snapshots"),
    [
        ("success", "unchanged", "succeeded", 1),
        ("rejected", "blocked", "failed", 0),
        ("incomplete", "failed", "failed", 0),
    ],
)
def test_source_export_unit_preserves_unchanged_and_terminal_seed_truth(
    tmp_path, monkeypatch, seed_status, expected_disposition,
    expected_status, valid_snapshots,
):
    if os.name == "nt":
        pytest.skip("RequestBudget durability uses POSIX directory fsync")
    install_runtime(monkeypatch)

    def run(_runtime, source_value, _scope, seed_url, *_args):
        if seed_status == "success":
            return {
                "status": "success", "event_kind": "source", "error": None,
                "attempts": [{"tool_id": "acquisition.web_http", "outcome": "succeeded"}],
                "candidates": [], "candidate_urls": [],
                "change_counts": {"added": 0, "changed": 0, "unchanged": 3,
                                  "missing": 0, "failed": 0, "unresolved": 0},
                "observed_at": "2026-09-13T00:00:00Z",
                "checkpoint": {
                    "schema_version": "climate-web-listening-refresh-context.v1",
                    "upstream_revision": adapter._UPSTREAM_REVISION,
                    "source_key": source_value.key, "seed_url": seed_url,
                    "site_skill": {}, "site_state": {},
                },
            }
        return {
            "status": seed_status,
            "event_kind": "policy" if seed_status == "rejected" else "source",
            "error": "robots.forbidden" if seed_status == "rejected" else "network.failed",
            "attempts": [{"tool_id": "acquisition.web_http", "outcome": "failed"}],
            "candidates": [], "candidate_urls": [],
        }

    monkeypatch.setattr(adapter, "_run_site_seed", run)
    scope = SiteScope(
        source_key="example", include_source_url=False,
        include_patterns=(), exclude_patterns=(),
        seed_urls=("https://example.test/news", "https://example.test/publications"),
    )
    ledger = RequestBudget(tmp_path / "budget.json", {
        "run_id": f"source-{seed_status}", "attempt": 1,
        "budgets": {"fetch_attempts": 8, "search_attempts": 0,
                    "search_results": 0, "retries_per_item": 0,
                    "runtime_seconds": 60},
    })
    _items, _warnings, evidence = adapter.collect_website_items_with_evidence(
        [source()], state_dir=tmp_path / "state", site_scopes={"example": scope},
        budget=ledger,
    )
    row = evidence["source_results"][0]
    outcome = row["outcome"]
    assert outcome["status"] == expected_status
    assert outcome["counts"]["requested"] == 1
    assert outcome["counts"]["valid_snapshots"] == valid_snapshots
    assert outcome["dispositions"][0]["disposition"] == expected_disposition
    assert outcome["dispositions"][0]["requested_url"] == source().url
    assert (outcome["dispositions"][0].get("artifact_id") is not None) == bool(valid_snapshots)
    assert len(row["manifest"]["seed_outcomes"]) == 2
    assert len(row["attempts"]) == 2


def test_managed_binding_freezes_scopes_and_runtime_policy(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.importorskip("tzdata")
    from climate_monitor.config import load_sources
    from climate_monitor.management import build_task_binding, default_task_definition
    from climate_registry.persistent import initialize_registry
    import scripts.run_agent_acquisition as runner

    definition = default_task_definition()
    definition["runtime"].update(
        run_root=str(tmp_path), registry_database=str(tmp_path / "registry.db")
    )
    initialize_registry(tmp_path / "registry.db")
    binding = build_task_binding(definition, task_version=1, run_id="frozen", attempt=1)
    records = binding["site_scope_inventory"]["records"]
    observed = {}

    def collect(sources, **kwargs):
        observed.update(kwargs)
        return [], [], {"status": "completed", "source_results": []}

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(adapter, "collect_website_items_with_evidence", collect)
    monkeypatch.setattr(
        "climate_monitor.config.load_site_scopes",
        lambda path: pytest.fail("must use frozen scopes"),
    )
    runner._controlled_site_context(binding)
    assert observed["gateway_config"] == binding["governed_gateway"]
    assert [asdict(scope) for scope in observed["site_scopes"].values()] == records
    assert len(binding["source_inventory"]["records"]) == len(
        load_sources("monitoring/supranational_sources.yaml")
    ) == 36


def test_real_pinned_public_runtime_opens_without_network(tmp_path, monkeypatch):
    pytest.importorskip("web_listening.runtime.service")
    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    with adapter._open_governed_runtime([source()], {}, tmp_path) as (runtime, config, _):
        assert runtime is not None
        assert config["upstream_revision"].startswith("ac2343f")
