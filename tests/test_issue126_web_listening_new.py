"""Issue #126 contracts that keep one pinned public web_listening_new runtime."""

import os

from pathlib import Path

import pytest

from climate_monitor.request_budget import RequestBudget
from climate_monitor.web_listening_adapter import _load_refresh_checkpoint
from climate_monitor.models import MonitorSource


ROOT = Path(__file__).resolve().parent.parent
PIN = "ac2343f89bc7939736d85f049ebe2beac571034a"


def binding(tmp_path):
    return {
        "run_id": "issue126", "attempt": 1,
        "budgets": {"fetch_attempts": 8, "search_attempts": 0,
                    "search_results": 0, "retries_per_item": 0,
                    "runtime_seconds": 60},
        "source_inventory": {}, "site_scope_inventory": {},
        "governed_gateway": {}, "date_policy": {}, "report_date": "2026-09-14",
    }


def test_one_new_upstream_pin_and_no_legacy_imports():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert requirements.count("web-listening @") == 1
    assert f"web_listening_new.git@{PIN}" in requirements
    affected = [
        ROOT / "climate_monitor/web_listening_adapter.py",
        ROOT / "climate_monitor/article_content_adapter.py",
    ]
    forbidden = (
        "web_listening.blocks", "web_listening.contracts",
        "web_listening.site_skill_registry", "web_listening.executors",
        "WEB_LISTENING_PROJECT_PATH",
    )
    for path in affected:
        text = path.read_text(encoding="utf-8")
        assert all(value not in text for value in forbidden)


def test_browser_runtime_is_built_in_final_python_image_and_request_is_serializable():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    builder, final = dockerfile.split("FROM python:3.12-slim", 1)
    assert "/opt/web-listening-source" not in builder
    assert "python -m venv /opt/web-listening-data/browser-runtimes/playwright" in final
    assert "ENV CLIMATE_WEB_LISTENING_DATA_DIR=/opt/web-listening-data" in final
    assert final.index("python -m venv /opt/web-listening-data") < final.index(
        "pip install --no-cache-dir -r requirements.txt"
    ) < final.index("host_runtime(root")
    assert final.index("host_runtime(root") < final.index(
        "ARG CLIMATE_REPOSITORY_COMMIT_SHA"
    ) < final.index("COPY api_server.py")
    qualifier = (ROOT / "scripts/qualify_web_listening_playwright.py").read_text(encoding="utf-8")
    assert "json.dumps(asdict(request))" in qualifier
    assert "request.to_dict()" not in qualifier


def test_image_fetches_only_exact_pinned_source_revisions():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG HERMES_REVISION=5538bd1f933be2e94aca9755deca5cc59cccc553" in dockerfile
    assert "ARG WEB_LISTENING_REVISION=ac2343f89bc7939736d85f049ebe2beac571034a" in dockerfile
    for variable, directory in (
        ("HERMES_REVISION", "/opt/hermes-agent"),
        ("WEB_LISTENING_REVISION", "/opt/web-listening-source"),
    ):
        assert (
            f'git -C {directory} fetch --depth 1 --filter=blob:none origin "${variable}"'
            in dockerfile
        )
        assert f"git -C {directory} checkout --detach FETCH_HEAD" in dockerfile
        assert f'git -C {directory} rev-parse HEAD)" = "${variable}"' in dockerfile
    assert "git clone" not in dockerfile


def test_reserved_alternative_budget_reconciles_to_actual_attempts(tmp_path):
    if os.name == "nt":
        pytest.skip("RequestBudget durability uses POSIX directory fsync")
    ledger = RequestBudget(tmp_path / "budget.json", binding(tmp_path))
    ledger.claim("web_listening_site", "https://example.test/", call_id="call", units=4)
    ledger.complete_tool("call", {"status": "completed"}, "ok", actual_units=2)
    assert ledger.usage()["fetch_attempts"] == 2
    assert ledger.tool_event("call")["fetch_units"] == 2
    ledger.complete_tool("call", {"status": "completed"}, "ok", actual_units=2)


def test_actual_attempts_cannot_exceed_reserved_budget(tmp_path):
    if os.name == "nt":
        pytest.skip("RequestBudget durability uses POSIX directory fsync")
    ledger = RequestBudget(tmp_path / "budget.json", binding(tmp_path))
    ledger.claim("web_listening_site", "https://example.test/", call_id="call", units=2)
    with pytest.raises(ValueError, match="more fetch units than it reserved"):
        ledger.complete_tool("call", {}, "error", actual_units=3)
    assert ledger.usage()["fetch_attempts"] == 2


def test_legacy_checkpoint_is_an_honest_new_runtime_baseline(tmp_path):
    source = MonitorSource("example", "EX", "Example", "https://example.test/")
    path = tmp_path / "example.json"
    path.write_text('{"content_hash":"abc","links":[]}', encoding="utf-8")
    assert _load_refresh_checkpoint(path, source, source.url) is None


def test_first_exploration_projects_non_seed_pages_as_new_discoveries(tmp_path):
    class Budget:
        limits = {"fetch_attempts": 8}
        def usage(self): return {"fetch_attempts": 0}
        def remaining_seconds(self): return 60
        def claim(self, *args, **kwargs): return None
        def complete_tool(self, *args, **kwargs): return None

    class Result:
        def to_dict(self):
            return {
                "status": "completed", "stop_reason": "source_exhausted",
                "usage": {"requests": 2}, "errors": [], "attempts": [],
                "site_skill_candidate": {"site_key": "example.test"},
                "site_state": {
                    "generated_at": "2026-09-13T00:00:00Z",
                    "pages": [
                        {"canonical_url": "https://example.test/", "artifact_id": "seed",
                         "content_digest": "sha256:" + "1" * 64},
                        {"canonical_url": "https://example.test/news/climate", "artifact_id": "item",
                         "content_digest": "sha256:" + "2" * 64},
                    ],
                },
            }

    class Runtime:
        def explore_site(self, request):
            assert request.scope.include_paths == ("/",)
            return Result()

    from climate_monitor import web_listening_adapter as adapter
    receipt = adapter._run_site_seed(
        Runtime(), MonitorSource("example", "EX", "Example", "https://example.test/"),
        None, "https://example.test/", tmp_path,
        adapter.gateway_configuration(
            [MonitorSource("example", "EX", "Example", "https://example.test/")], {}
        ), Budget(),
    )
    assert receipt["status"] == "success"
    assert receipt["candidate_urls"] == ["https://example.test/news/climate"]
    assert receipt["candidates"][0]["content_hash"] == "2" * 64
