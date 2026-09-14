"""Pinned public Runtime regressions for bounded article retrieval."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from climate_monitor import article_content_adapter as adapter


pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="web_listening_new is installed only on Python 3.12+",
)


PUBLIC_IP = "93.184.216.34"
NOW = "2026-09-14T02:00:00Z"
PAGE = "https://example.test/allowed/item"
BODY = (
    b"<html><body><h1>Climate risk report</h1>"
    b"<p>Physical climate risks affect insurance exposures and adaptation policy.</p>"
    b"</body></html>"
)


class _Response:
    def __init__(self, status: int, body: bytes = b"", **headers: str) -> None:
        self.status = status
        self.body = body
        self.headers = {
            name.replace("_", "-"): value for name, value in headers.items()
        }
        self.peer_ip = PUBLIC_IP

    def read(self, max_bytes: int) -> bytes:
        return self.body[:max_bytes]

    def close(self) -> None:
        pass


class _Transport:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def send(self, url: str, *, timeout: float, addresses: tuple[str, ...]):
        del timeout, addresses
        self.requests.append(url)
        if url.endswith("/robots.txt"):
            return _Response(404)
        return self.responses[url]

    def close(self) -> None:
        pass


class _Budget:
    limits = {"fetch_attempts": 100}

    def __init__(self) -> None:
        self.claims: list[tuple[tuple, dict]] = []
        self.completions: list[tuple[tuple, dict]] = []
        self.events: dict[str, dict] = {}

    def usage(self) -> dict[str, int]:
        return {"fetch_attempts": 0}

    def remaining_seconds(self) -> int:
        return 60

    def claim(self, *args, **kwargs) -> None:
        self.claims.append((args, kwargs))
        self.events[kwargs["call_id"]] = {"completed": False}

    def complete_tool(self, *args, **kwargs) -> None:
        self.completions.append((args, kwargs))
        self.events[args[0]]["completed"] = True

    def tool_event(self, call_id):
        return self.events.get(call_id)


def _resolver(_host: str, _port: int) -> tuple[str, ...]:
    return (PUBLIC_IP,)


def _persistent_runtime(root: Path, transport: _Transport, job_ids):
    from web_listening.artifact.store import ArtifactStore
    from web_listening.runtime.jobs import JobRepository
    from web_listening.runtime.service import RuntimeService
    from web_listening.tool_registry.acquisition.builtins.web_http import (
        WEB_HTTP_MANIFEST,
        WebHttpAcquisitionTool,
    )
    from web_listening.tool_registry.registry import Registry
    from web_listening.tool_registry.transform.builtins.simple_html_markdown import (
        SIMPLE_HTML_MARKDOWN_MANIFEST,
        SimpleHtmlMarkdownTransform,
    )

    root.mkdir(parents=True, exist_ok=True)
    registry = Registry()
    registry.register(
        WEB_HTTP_MANIFEST,
        WebHttpAcquisitionTool(lambda: transport, resolver=_resolver),
    )
    registry.register(SIMPLE_HTML_MARKDOWN_MANIFEST, SimpleHtmlMarkdownTransform())
    store = ArtifactStore(root / "artifacts")
    jobs = JobRepository(root / "jobs.sqlite3")
    runtime = RuntimeService(
        registry, store, jobs, clock=lambda: NOW,
        job_id_factory=lambda: next(job_ids),
    )
    return runtime, store, jobs


def _request(url: str):
    from urllib.parse import urlsplit

    from web_listening.request.model import Budgets, ContentType, Request, Scope

    parsed = urlsplit(url)
    return Request(
        Scope(
            (url,),
            (f"{parsed.scheme}://{parsed.netloc}",),
            (parsed.path or "/",),
            (ContentType.HTML, ContentType.FILE),
        ),
        None,
        True,
        Budgets(12, 8 * 1024 * 1024, 60, 4),
    )


def _public_provider(monkeypatch, tmp_path: Path, transport: _Transport):
    from web_listening.artifact.store import ArtifactStore
    from web_listening.runtime.jobs import JobRepository
    from web_listening.runtime.service import RuntimeService
    from web_listening.tool_registry.acquisition.builtins.web_http import (
        WEB_HTTP_MANIFEST,
        WebHttpAcquisitionTool,
    )
    from web_listening.tool_registry.registry import Registry
    from web_listening.tool_registry.transform.builtins.simple_html_markdown import (
        SIMPLE_HTML_MARKDOWN_MANIFEST,
        SimpleHtmlMarkdownTransform,
    )

    tmp_path.mkdir(parents=True, exist_ok=True)
    registry = Registry()
    registry.register(
        WEB_HTTP_MANIFEST,
        WebHttpAcquisitionTool(lambda: transport, resolver=_resolver),
    )
    registry.register(SIMPLE_HTML_MARKDOWN_MANIFEST, SimpleHtmlMarkdownTransform())
    store = ArtifactStore(tmp_path / "artifacts")
    jobs = JobRepository()
    runtime = RuntimeService(
        registry, store, jobs, clock=lambda: NOW, job_id_factory=lambda: "job-1",
    )

    class Factory:
        @staticmethod
        def open(_root):
            return runtime

    monkeypatch.setattr(adapter, "check_dependencies", lambda: "available")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: Factory)
    provider = adapter._default_providers(data_root=tmp_path / "runtime")[0]
    return provider, store, jobs


def _scope(*origins: str) -> dict:
    return {
        "source_key": "example",
        "allowed_origins": list(origins or ("https://example.test",)),
        "include_patterns": ["/allowed/**"],
        "exclude_patterns": ["/events/"],
    }


def _read(monkeypatch, tmp_path, url, responses, *, scope=None):
    transport = _Transport(responses)
    provider, store, jobs = _public_provider(monkeypatch, tmp_path, transport)
    try:
        record = adapter.fetch_article_content(
            "article", url, providers=(provider,),
        ) if scope is None else adapter.fetch_article_content(
            "article", url,
            providers=(adapter._default_providers(
                data_root=tmp_path / "runtime", site_scope=scope,
            )[0],),
        )
    finally:
        jobs.close()
        store.close()
    return record, transport


def test_targeted_reader_leaves_older_submitted_job_pending_across_reopen(
    monkeypatch, tmp_path,
):
    from web_listening.runtime.jobs import JobStatus

    older = "https://example.test/allowed/older"
    current = "https://example.test/allowed/current"
    transport = _Transport({
        older: _Response(200, BODY, Content_Type="text/html"),
        current: _Response(200, BODY, Content_Type="text/html"),
    })
    root = tmp_path / "runtime"
    first, first_store, first_jobs = _persistent_runtime(
        root, transport, iter(("job-older",)),
    )
    first.submit(
        _request(older), caller_id="older-owner", idempotency_key="older-key",
    )
    first_jobs.close()
    first_store.close()

    second, second_store, second_jobs = _persistent_runtime(
        root, transport, iter(("job-current",)),
    )

    class Factory:
        @staticmethod
        def open(_root):
            return second

    monkeypatch.setattr(adapter, "check_dependencies", lambda: "available")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: Factory)
    budget = _Budget()
    record = adapter.fetch_article_content(
        "current", current, budget=budget, site_key="example",
        site_scope={"allowed_origins": ["https://example.test"]},
    )

    assert record["status"] == "ok"
    assert [url for url in transport.requests if not url.endswith("/robots.txt")] == [
        current
    ]
    assert second_jobs.get("job-older").status is JobStatus.SUBMITTED
    current_job = second_jobs.get("job-current")
    assert current_job.status is JobStatus.COMPLETED
    assert current_job.result is not None
    assert record["attempts"] == [
        attempt.to_dict() for attempt in current_job.result.attempts
    ]
    assert len(budget.claims) == len(budget.completions) == 1
    assert budget.claims[0][0][1] == current
    assert budget.completions[0][1]["actual_units"] == (
        current_job.result.usage.requests
    )
    raw = record["extra"]["extraction_metadata"]["runtime_job"]
    assert raw["caller_id"] == "climate-monitor"
    for field in ("request_json", "execution_request_json"):
        persisted = json.loads(raw[field])
        assert persisted["scope"] == {
            "allowed_origins": ["https://example.test"],
            "content_types": ["file", "html"],
            "include_paths": ["/allowed/current"],
            "seeds": [current],
        }
        assert persisted["explore_all_tools"] is True
    second_jobs.close()
    second_store.close()

    third, third_store, third_jobs = _persistent_runtime(
        root, transport, iter(("unused",)),
    )
    try:
        assert third.get_owned_job("job-older", "older-owner").status is (
            JobStatus.SUBMITTED
        )
        assert third.get_owned_job("job-current", "climate-monitor").status is (
            JobStatus.COMPLETED
        )
        with pytest.raises(ValueError, match="job.not_found"):
            third.get_owned_job("job-current", "other")
    finally:
        third_jobs.close()
        third_store.close()


def test_targeted_failure_keeps_job_evidence_and_does_not_block_next_url(
    monkeypatch, tmp_path,
):
    from web_listening.runtime.jobs import JobStatus

    older = "https://example.test/allowed/older"
    failed = "https://example.test/allowed/failed"
    excluded = "https://example.test/excluded"
    later = "https://example.test/allowed/later"
    transport = _Transport({
        older: _Response(200, BODY, Content_Type="text/html"),
        failed: _Response(302, Location=excluded),
        later: _Response(200, BODY, Content_Type="text/html"),
    })
    root = tmp_path / "runtime"
    first, first_store, first_jobs = _persistent_runtime(
        root, transport, iter(("job-older",)),
    )
    first.submit(
        _request(older), caller_id="older-owner", idempotency_key="older-key",
    )
    first_jobs.close()
    first_store.close()

    failed_runtime, failed_store, failed_jobs = _persistent_runtime(
        root, transport, iter(("job-failed",)),
    )

    class FailedFactory:
        @staticmethod
        def open(_root):
            return failed_runtime

    monkeypatch.setattr(adapter, "check_dependencies", lambda: "available")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: FailedFactory)
    failed_budget = _Budget()
    failed_record = adapter.fetch_article_content(
        "failed", failed, budget=failed_budget, site_key="example",
        site_scope={"allowed_origins": ["https://example.test"]},
    )
    assert failed_record["status"] == "failed"
    failed_job = failed_jobs.get("job-failed")
    assert failed_job.status is JobStatus.FAILED and failed_job.result is not None
    assert failed_record["attempts"] == [
        attempt.to_dict() for attempt in failed_job.result.attempts
    ]
    assert failed_record["attempts"][0]["error"]["code"] == (
        "scope.path_not_included"
    )
    assert failed_budget.completions[0][1]["actual_units"] == (
        failed_job.result.usage.requests
    )
    assert failed_jobs.get("job-older").status is JobStatus.SUBMITTED
    assert excluded not in transport.requests
    failed_jobs.close()
    failed_store.close()

    later_runtime, later_store, later_jobs = _persistent_runtime(
        root, transport, iter(("job-later",)),
    )

    class LaterFactory:
        @staticmethod
        def open(_root):
            return later_runtime

    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: LaterFactory)
    later_record = adapter.fetch_article_content(
        "later", later, budget=_Budget(), site_key="example",
        site_scope={"allowed_origins": ["https://example.test"]},
    )
    try:
        assert later_record["status"] == "ok"
        assert later_jobs.get("job-later").status is JobStatus.COMPLETED
        assert later_jobs.get("job-older").status is JobStatus.SUBMITTED
        assert [url for url in transport.requests if not url.endswith("/robots.txt")] == [
            failed, later
        ]
    finally:
        later_jobs.close()
        later_store.close()


def test_targeted_rejected_result_uses_typed_owned_job_identity(
    monkeypatch, tmp_path,
):
    from web_listening.result.model import ResultStatus
    from web_listening.runtime.jobs import JobStatus
    from web_listening.runtime.workflow import terminal_failure_result

    target = "https://example.test/allowed/rejected"
    transport = _Transport({target: _Response(200, BODY, Content_Type="text/html")})
    runtime, store, jobs = _persistent_runtime(
        tmp_path / "runtime", transport, iter(("job-rejected",)),
    )

    def rejected(job_id, request, _cancellation):
        result = terminal_failure_result(
            request, status=ResultStatus.REJECTED, run_id=job_id,
            generated_at=NOW, code="site_skill.invalid", message="Invalid skill.",
        )
        current = jobs.get(job_id)
        return jobs.transition(
            job_id, JobStatus.REJECTED, at=NOW, result=result,
            failure_code="site_skill.invalid", claim_token=current.claim_token,
        )

    runtime.execute_submitted = rejected

    class Factory:
        @staticmethod
        def open(_root):
            return runtime

    monkeypatch.setattr(adapter, "check_dependencies", lambda: "available")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: Factory)
    budget = _Budget()
    record = adapter.fetch_article_content(
        "rejected", target, budget=budget, site_key="example",
        site_scope={"allowed_origins": ["https://example.test"]},
    )
    try:
        assert record["status"] == "failed"
        raw = record["extra"]["extraction_metadata"]["runtime_job"]
        assert raw["job_id"] == "job-rejected"
        assert raw["status"] == "rejected"
        assert raw["result"]["errors"][0]["code"] == "site_skill.invalid"
        assert budget.completions[0][1]["actual_units"] == 0
    finally:
        jobs.close()
        store.close()


@pytest.mark.parametrize(
    "jobs_payload",
    [[], [{"job_id": "one"}, {"job_id": "two"}]],
)
def test_targeted_success_requires_exactly_one_job_identity(
    monkeypatch, tmp_path, jobs_payload,
):
    class Runtime:
        @classmethod
        def open(cls, _root):
            return cls()

        def retrieve(self, _request, *, caller_id):
            assert caller_id == "climate-monitor"
            return {"jobs": jobs_payload, "retrieval": {}, "provenance": []}

        def get_owned_job(self, _job_id, _caller_id):
            pytest.fail("invalid success cardinality reached owned-job read")

        def close(self):
            pass

    monkeypatch.setattr(adapter, "check_dependencies", lambda: "available")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: Runtime)
    budget = _Budget()
    record = adapter.fetch_article_content(
        "bad-envelope", "https://example.test/allowed/item", budget=budget,
        site_key="example",
        site_scope={"allowed_origins": ["https://example.test"]},
    )
    assert record["status"] == "unavailable"
    assert "exactly one job identity" in record["failure_reason"]
    assert len(budget.completions) == 1
    assert "actual_units" not in budget.completions[0][1]


def test_handle_free_runtime_error_keeps_conservative_reservation(
    monkeypatch,
):
    class Runtime:
        @classmethod
        def open(cls, _root):
            return cls()

        def retrieve(self, _request, *, caller_id):
            assert caller_id == "climate-monitor"
            raise RuntimeError("failure before a durable targeted result")

        def close(self):
            pass

    monkeypatch.setattr(adapter, "check_dependencies", lambda: "available")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: Runtime)
    budget = _Budget()
    record = adapter.fetch_article_content(
        "unknown", "https://example.test/allowed/item", budget=budget,
        site_key="example",
        site_scope={"allowed_origins": ["https://example.test"]},
    )
    assert record["status"] == "unavailable"
    assert len(budget.claims) == len(budget.completions) == 1
    assert "actual_units" not in budget.completions[0][1]


def test_old_navigation_reaches_excluded_download_but_bounded_reader_does_not(
    monkeypatch, tmp_path,
):
    from web_listening.artifact.store import ArtifactStore
    from web_listening.request.model import Budgets
    from web_listening.request.url_fetch import UrlFetchRequest
    from web_listening.runtime.url_fetch import run_url_fetch
    from web_listening.tool_registry.acquisition.builtins.web_http import (
        WEB_HTTP_MANIFEST,
        WebHttpAcquisitionTool,
    )
    from web_listening.tool_registry.discovery.builtins.html_navigation import (
        HTML_NAVIGATION_MANIFEST,
        HtmlNavigationDiscoveryTool,
    )
    from web_listening.tool_registry.registry import Registry
    from web_listening.tool_registry.transform.builtins.simple_html_markdown import (
        SIMPLE_HTML_MARKDOWN_MANIFEST,
        SimpleHtmlMarkdownTransform,
    )

    excluded = "https://example.test/events/private"
    landing = _Response(
        200,
        b'<html><body>Climate report download evidence now available '
        b'<a download href="/events/private">report</a></body></html>',
        Content_Type="text/html",
    )
    old_transport = _Transport({PAGE: landing, excluded: _Response(200, BODY, Content_Type="text/html")})
    registry = Registry()
    registry.register(WEB_HTTP_MANIFEST, WebHttpAcquisitionTool(lambda: old_transport, resolver=_resolver))
    registry.register(HTML_NAVIGATION_MANIFEST, HtmlNavigationDiscoveryTool())
    registry.register(SIMPLE_HTML_MARKDOWN_MANIFEST, SimpleHtmlMarkdownTransform())
    store = ArtifactStore(tmp_path / "old")
    try:
        run_url_fetch(
            UrlFetchRequest(PAGE, True, True, 3, Budgets(12, 8 * 1024 * 1024, 60, 4)),
            registry, store, run_id="old", clock=lambda: NOW,
        )
    finally:
        store.close()
    assert excluded in old_transport.requests

    record, new_transport = _read(
        monkeypatch, tmp_path / "new", PAGE, {PAGE: landing}, scope=_scope(),
    )
    assert record["status"] == "ok"
    assert [url for url in new_transport.requests if not url.endswith("/robots.txt")] == [PAGE]
    assert "HTML navigation" in record["extra"]["extraction_metadata"]["coverage_limitations"][0]


@pytest.mark.parametrize(
    "target",
    [
        "https://example.test/events/private",
        "https://example.test/allowed/events/private",
    ],
)
def test_exact_path_rejects_excluded_redirect_before_request(
    monkeypatch, tmp_path, target,
):
    record, transport = _read(
        monkeypatch, tmp_path, PAGE,
        {PAGE: _Response(302, Location=target)}, scope=_scope(),
    )
    assert target not in transport.requests
    assert record["status"] == "failed"
    assert any(
        (attempt.get("error") or {}).get("code") == "scope.path_not_included"
        for attempt in record["attempts"]
    )
    metadata = record["extra"]["extraction_metadata"]
    assert metadata["effective_request_scope"]["include_paths"] == ["/allowed/item"]
    assert metadata["runtime_job"]["result"]["errors"][0]["code"] == (
        "scope.path_not_included"
    )


def test_allowed_direct_content_is_owned_hash_verified_with_real_attempts(
    monkeypatch, tmp_path,
):
    record, transport = _read(
        monkeypatch, tmp_path, PAGE,
        {PAGE: _Response(200, BODY, Content_Type="text/html")}, scope=_scope(),
    )
    assert record["status"] == "ok"
    assert record["content_hash"]
    assert record["content"]
    assert [attempt["tool_id"] for attempt in record["attempts"]] == [
        "acquisition.web_http", "transform.simple_html_markdown",
    ]
    assert record["attempts"][0]["http_status"] == 200
    assert record["attempts"][1]["http_status"] is None
    raw = record["extra"]["extraction_metadata"]["runtime_job"]
    assert raw["caller_id"] == "climate-monitor"
    assert raw["result"]["artifacts"] == raw["result"]["manifest"]["artifacts"]
    assert [url for url in transport.requests if not url.endswith("/robots.txt")] == [PAGE]


def test_cross_origin_redirect_is_gap_and_secondary_candidate_works_separately(
    monkeypatch, tmp_path,
):
    secondary = "https://secondary.test/allowed/report/"
    record, transport = _read(
        monkeypatch, tmp_path / "redirect", PAGE,
        {PAGE: _Response(302, Location=secondary)},
        scope=_scope("https://example.test", "https://secondary.test"),
    )
    assert secondary not in transport.requests
    assert record["status"] == "failed"
    assert "Cross-origin redirects" in " ".join(
        record["extra"]["extraction_metadata"]["coverage_limitations"]
    )

    second, second_transport = _read(
        monkeypatch, tmp_path / "secondary", secondary,
        {secondary: _Response(200, BODY, Content_Type="text/html")},
        scope=_scope("https://example.test", "https://secondary.test"),
    )
    assert second["status"] == "ok"
    assert [url for url in second_transport.requests if not url.endswith("/robots.txt")] == [secondary]
    assert second["extra"]["extraction_metadata"]["effective_request_scope"]["include_paths"] == [
        "/allowed/report/"
    ]


@pytest.mark.parametrize(
    "url,expected_path",
    [
        (
            "https://www.ilo.org/publications/el-ni%C3%B1o-climate-report",
            "/publications/el-ni%C3%B1o-climate-report",
        ),
        ("https://tnfd.global/knowledge-hub/", "/knowledge-hub/"),
    ],
)
def test_exact_path_preserves_real_encoded_and_trailing_slash_candidates(
    monkeypatch, tmp_path, url, expected_path,
):
    record, _transport = _read(
        monkeypatch, tmp_path, url,
        {url: _Response(200, BODY, Content_Type="text/html")},
        scope={
            "source_key": "real",
            "allowed_origins": [url.split(expected_path)[0]],
            "include_patterns": ["/**"],
            "exclude_patterns": [],
        },
    )
    assert record["status"] == "ok"
    assert record["extra"]["extraction_metadata"]["effective_request_scope"] == {
        "seeds": [url],
        "allowed_origins": [url.split(expected_path)[0]],
        "include_paths": [expected_path],
        "content_types": ["html", "file"],
    }
