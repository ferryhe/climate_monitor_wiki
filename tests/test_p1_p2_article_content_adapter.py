"""Regression coverage for article evidence after the public Runtime migration."""

import hashlib
from pathlib import Path
import sys

import pytest

from climate_monitor import article_content_adapter as adapter
from test_article_content_default_adapter import _install_runtime


requires_web_listening = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="pinned web-listening dependency is installed only on Python 3.12+",
)


@requires_web_listening
def test_p1_data_root_threads_into_default_reader(monkeypatch, tmp_path):
    runtime, _body, _result = _install_runtime(monkeypatch)
    artifact = adapter.build_article_evidence_artifact(
        [{"article_id": "a", "url": "https://example.test/a"}],
        report_date="2026-09-07", data_root=tmp_path / "runtime")
    assert runtime.roots == [(tmp_path / "runtime").resolve()]
    assert artifact["records"][0]["status"] == "ok"


@requires_web_listening
def test_p1_data_root_none_uses_runtime_environment(monkeypatch, tmp_path):
    runtime, _body, _result = _install_runtime(monkeypatch)
    monkeypatch.setenv("CLIMATE_WEB_LISTENING_DATA_DIR", str(tmp_path / "governed"))
    adapter.build_article_evidence_artifact(
        [{"article_id": "a", "url": "https://example.test/a"}],
        report_date="2026-09-07")
    assert runtime.roots == [(tmp_path / "governed").resolve()]


@requires_web_listening
def test_p2_collect_evidence_materializes_verified_owned_content(monkeypatch):
    _runtime, body, _result = _install_runtime(monkeypatch)
    records, output_dirs = adapter.collect_evidence(
        [{"article_id": "a", "url": "https://example.test/a"}])
    assert output_dirs == {}
    assert records[0]["content"] == body.decode()
    assert hashlib.sha256(body).hexdigest() == records[0]["content_hash"]


def test_p2_build_artifact_rechecks_materialized_content(monkeypatch):
    body = "verified evidence"
    digest = hashlib.sha256(body.encode()).hexdigest()
    provider = lambda aid, url: {"status": "present", "requested_url": url,
                                "content": body, "sha256": digest}
    calls = []
    real = adapter._verify_batch
    def verify(records, articles, **kwargs):
        calls.append(kwargs)
        return real(records, articles, **kwargs)
    monkeypatch.setattr(adapter, "_verify_batch", verify)
    artifact = adapter.build_article_evidence_artifact(
        [{"article_id": "a", "url": "https://example.test/a"}],
        providers=(provider,), report_date="2026-09-07")
    assert len(calls) == 2
    assert artifact["records"][0]["status"] == "ok"


@requires_web_listening
def test_p2_long_body_from_owned_artifact_is_hash_verified(monkeypatch):
    body = ("climate evidence " * 500).encode()
    _runtime, _, _result = _install_runtime(monkeypatch, body=body)
    artifact = adapter.build_article_evidence_artifact(
        [{"article_id": "a", "url": "https://example.test/a"}],
        report_date="2026-09-07", include_verified_content=True)
    record = artifact["records"][0]
    assert record["content"] == body.decode()
    assert record["content_hash"] == hashlib.sha256(body).hexdigest()


def test_p2_no_data_root_does_not_create_climate_artifact_cache(monkeypatch, tmp_path):
    _runtime, _, _result = _install_runtime(monkeypatch)
    monkeypatch.setenv("CLIMATE_WEB_LISTENING_DATA_DIR", str(tmp_path / "runtime"))
    _records, output_dirs = adapter.collect_evidence(
        [{"article_id": "a", "url": "https://example.test/a"}])
    assert output_dirs == {}
    assert not (tmp_path / "runtime" / ".cache" / "article_content").exists()


@requires_web_listening
def test_authoring_can_retain_verified_runtime_content(monkeypatch):
    _runtime, body, _result = _install_runtime(monkeypatch)
    artifact = adapter.build_article_evidence_artifact(
        [{"article_id": "a", "url": "https://example.test/a"}],
        report_date="2026-09-07", include_verified_content=True)
    record = artifact["records"][0]
    assert record["content"] == body.decode()
    assert record["record_hash"] == adapter._record_digest(record)


@pytest.mark.parametrize("storage", ["opaque_ref", "no_ref"])
def test_authoring_retains_hash_bound_inline_content(storage):
    body = "Verified inline climate evidence."
    def provider(aid, url):
        return {"status": "present", "requested_url": url, "content": body,
                "content_ref": "owned-artifact" if storage == "opaque_ref" else None,
                "sha256": hashlib.sha256(body.encode()).hexdigest()}
    artifact = adapter.build_article_evidence_artifact(
        [{"article_id": "inline", "url": "https://example.org/inline"}],
        providers=(provider,), report_date="2026-09-07", include_verified_content=True)
    assert artifact["records"][0]["content"] == body


@pytest.mark.parametrize("corruption", ["hash", "identity"])
def test_authoring_inline_still_requires_integrity(corruption):
    body = "Verified inline climate evidence."
    def provider(aid, url):
        return {"status": "present", "requested_url": (
                    "https://example.org/wrong" if corruption == "identity" else url),
                "content": body,
                "sha256": "0" * 64 if corruption == "hash" else hashlib.sha256(body.encode()).hexdigest()}
    with pytest.raises(adapter.ArticleContentAdapterError, match=(
            "wrong_requested_url" if corruption == "identity" else "content_hash_mismatch")):
        adapter.build_article_evidence_artifact(
            [{"article_id": "inline", "url": "https://example.org/inline"}],
            providers=(provider,), report_date="2026-09-07", include_verified_content=True)
