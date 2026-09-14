"""Default article adapter coverage for the pinned public URL-fetch Runtime."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import sys
from types import SimpleNamespace

import pytest

from climate_monitor import article_content_adapter as adapter


requires_web_listening = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="pinned web-listening dependency is installed only on Python 3.12+",
)


def _install_runtime(
    monkeypatch, body=b"# verified climate evidence\n", *, artifact_error=None,
):
    digest = hashlib.sha256(body).hexdigest()
    source = {"artifact_id": "source-1", "role": "source", "sha256": "1" * 64,
              "mime_type": "text/html", "source_url": "https://example.test/a"}
    derived = {"artifact_id": "derived-1", "role": "derived", "sha256": digest,
               "mime_type": "text/markdown", "source_url": "https://example.test/a"}
    result = {
        "status": "completed", "artifacts": [source, derived], "errors": [],
        "usage": {"requests": 1, "bytes_received": len(body), "runtime_ms": 1,
                  "tool_attempts": 2},
        "manifest": {"final_url": "https://example.test/a"},
        "attempts": [{"tool_id": "acquisition.web_http",
                      "outcome": "succeeded", "http_status": 200}],
    }

    class Result:
        def to_dict(self):
            return result

    @dataclass
    class Job:
        job_id: str = "job-1"
        status: str = "completed"
        result: object = None
        failure_code: str | None = None

    class Runtime:
        roots = []
        retrievals = []

        @classmethod
        def open(cls, root):
            instance = cls()
            cls.roots.append(root)
            return instance

        def retrieve(self, request, *, caller_id):
            assert request.scope.seeds == ("https://example.test/a",)
            assert request.scope.allowed_origins == ("https://example.test",)
            assert request.scope.include_paths == ("/a",)
            assert request.explore_all_tools is True
            assert caller_id == "climate-monitor"
            self.retrievals.append(request)
            return {
                "jobs": [{"job_id": "job-1"}],
                "retrieval": {},
                "provenance": [],
            }

        def get_owned_job(self, job_id, caller_id):
            assert (job_id, caller_id) == ("job-1", "climate-monitor")
            return Job(result=Result())

        @contextmanager
        def open_owned_artifact(self, artifact_id, caller_id):
            assert (artifact_id, caller_id) == ("derived-1", "climate-monitor")
            if artifact_error is not None:
                raise artifact_error
            yield SimpleNamespace(stream=io.BytesIO(body), size_bytes=len(body))

        def close(self):
            return None

    Runtime.roots = []
    Runtime.retrievals = []
    monkeypatch.setattr(adapter, "check_dependencies", lambda: "available")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: Runtime)
    return Runtime, body, result


class _Budget:
    limits = {"fetch_attempts": 100}

    def __init__(self):
        self.claims = []
        self.completions = []
        self.events = {}

    def usage(self):
        return {"fetch_attempts": 0}

    def remaining_seconds(self):
        return 60

    def claim(self, *args, **kwargs):
        self.claims.append((args, kwargs))
        self.events[kwargs["call_id"]] = {"completed": False}

    def complete_tool(self, *args, **kwargs):
        self.completions.append((args, kwargs))
        self.events[args[0]]["completed"] = True

    def tool_event(self, call_id):
        return self.events.get(call_id)


@requires_web_listening
def test_default_adapter_uses_public_runtime_and_verified_owned_artifact(monkeypatch, tmp_path):
    runtime, body, result = _install_runtime(monkeypatch)
    provider = adapter._default_providers(data_root=tmp_path)[0]
    record = adapter.fetch_article_content("a", "https://example.test/a", providers=(provider,))
    assert record["status"] == "ok"
    assert record["content"] == body.decode()
    assert record["content_ref"] == "derived-1"
    assert record["selected_method"] == "acquisition.web_http"
    metadata = record["extra"]["extraction_metadata"]
    assert metadata["runtime_job"]["result"] == result
    assert metadata["effective_request_scope"]["include_paths"] == ["/a"]
    assert "HTML navigation" in metadata["coverage_limitations"][0]
    assert runtime.roots == [tmp_path.resolve()]
    assert len(runtime.retrievals) == 1


@requires_web_listening
@pytest.mark.parametrize("error_code", ["artifact.not_found", "blob.corrupt"])
def test_default_adapter_preserves_terminal_result_when_owned_artifact_fails(
    monkeypatch, tmp_path, error_code,
):
    from web_listening.artifact.model import ArtifactStoreError

    _runtime, _body, result = _install_runtime(
        monkeypatch, artifact_error=ArtifactStoreError(error_code),
    )
    budget = _Budget()

    record = adapter.fetch_article_content(
        "a", "https://example.test/a", budget=budget, site_key="example",
        site_scope={"source_key": "example", "allowed_origins": ["https://example.test"]},
    )

    assert record["status"] == "failed"
    assert record["failure_reason"] == f"ArtifactStoreError: {error_code}"
    assert record["attempts"] == result["attempts"]
    assert record["content"] is None
    assert record["content_ref"] is None
    assert record["content_hash"] is None
    metadata = record["extra"]["extraction_metadata"]
    assert metadata["runtime_job"]["job_id"] == "job-1"
    assert metadata["runtime_job"]["result"] == result
    assert metadata["reviewed_source_scope"] == {
        "source_key": "example", "allowed_origins": ["https://example.test"],
    }
    assert metadata["effective_request_scope"]["include_paths"] == ["/a"]
    assert metadata["artifact_error"] == {
        "type": "ArtifactStoreError", "code": error_code, "message": error_code,
    }
    assert len(budget.claims) == len(budget.completions) == 1
    assert budget.claims[0][1]["units"] == 12
    assert budget.completions[0][1]["actual_units"] == 1


@requires_web_listening
def test_default_adapter_keeps_reservation_when_owned_job_has_no_result(
    monkeypatch, tmp_path,
):
    runtime, _body, _result = _install_runtime(monkeypatch)

    def no_result(_self, job_id, caller_id):
        assert (job_id, caller_id) == ("job-1", "climate-monitor")
        return SimpleNamespace(result=None)

    monkeypatch.setattr(runtime, "get_owned_job", no_result)
    budget = _Budget()

    record = adapter.fetch_article_content(
        "a", "https://example.test/a", budget=budget, site_key="example",
        site_scope={"allowed_origins": ["https://example.test"]},
    )

    assert record["status"] == "unavailable"
    assert "did not produce a terminal result" in record["failure_reason"]
    assert len(budget.completions) == 1
    assert "actual_units" not in budget.completions[0][1]


def test_default_adapter_unavailable_when_public_runtime_missing(monkeypatch):
    monkeypatch.setattr(adapter, "check_dependencies", lambda: "unavailable")
    record = adapter.fetch_article_content("a", "https://example.test/a")
    assert record["status"] == "unavailable"
    assert record["attempts"] == []
    assert "governed URL retrieval is unavailable" in record["failure_reason"]
