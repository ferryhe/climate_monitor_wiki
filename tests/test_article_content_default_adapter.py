"""Default article adapter coverage for the pinned public URL-fetch Runtime."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
from types import SimpleNamespace

from climate_monitor import article_content_adapter as adapter


def _install_runtime(monkeypatch, body=b"# verified climate evidence\n"):
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
            yield SimpleNamespace(stream=io.BytesIO(body), size_bytes=len(body))

        def close(self):
            return None

    Runtime.roots = []
    Runtime.retrievals = []
    monkeypatch.setattr(adapter, "check_dependencies", lambda: "available")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: Runtime)
    return Runtime, body, result


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


def test_default_adapter_unavailable_when_public_runtime_missing(monkeypatch):
    monkeypatch.setattr(adapter, "check_dependencies", lambda: "unavailable")
    record = adapter.fetch_article_content("a", "https://example.test/a")
    assert record["status"] == "unavailable"
    assert record["attempts"] == []
    assert "governed URL retrieval is unavailable" in record["failure_reason"]
