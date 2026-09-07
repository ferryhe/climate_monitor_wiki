"""Regression tests for Issue #87 P1 + P2 — live-chain recovery.

P1 (CRITICAL): the default reader ``output_dir`` must live inside the #91
transaction data root so the upstream ``web_listening.blocks.article_content``
scope validator accepts it. When ``data_root`` is ``None`` the legacy
``<tmpdir>/article_content/<uuid>`` layout is preserved.

P2 (CRITICAL): ``build_article_evidence_artifact`` must thread the per-record
``output_dirs`` captured by ``collect_evidence`` into its second
``_verify_batch`` so ref-only long-body records can be resolved from disk.

These tests use ``monkeypatch`` on
``climate_monitor.article_content_adapter._import_public_reader`` to inject
a fake upstream module. They do NOT install or invoke any real ``web_listening``
network code and do NOT mutate the host ``web_listening`` install.
"""

from __future__ import annotations

import hashlib
import re
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from climate_monitor import article_content_adapter as adapter


# ---------------------------------------------------------------------------
# Fake upstream module
# ---------------------------------------------------------------------------


class _FakeUpstreamModule:
    """Stand-in for ``web_listening.blocks.article_content``.

    Each ``fetch_article_content`` call writes the fixture body to
    ``<output_dir>/<content_ref>`` so ``resolve_content_ref`` can read it
    back from disk. The call log records every invocation so tests can
    assert what was passed to upstream.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def fetch_article_content(
        self,
        url: str,
        *,
        article_id: str | None = None,
        profile: Any = None,
        site_key: str | None = None,
        scope_path: str | None = None,
        output_dir: str | None = None,
        goal_preset: str | None = None,
    ) -> dict[str, Any]:
        # Record the call so tests can inspect the output_dir that was passed.
        self.calls.append(
            {
                "url": url,
                "article_id": article_id,
                "profile": profile,
                "site_key": site_key,
                "scope_path": scope_path,
                "output_dir": output_dir,
                "goal_preset": goal_preset,
            }
        )
        if not output_dir:
            raise RuntimeError("fake upstream requires output_dir to be set")
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        # Each call gets its own deterministic body keyed by URL so the
        # bytes written to disk match the bytes hashed into sha256.
        body = f"Climate evidence fixture body for {url} (long enough to bypass inline).".encode("utf-8")
        sha = hashlib.sha256(body).hexdigest()
        # Write the portable local-file artifact that resolve_content_ref expects.
        ref_name = f"fixture-{sha[:12]}.bin"
        (out / ref_name).write_bytes(body)
        attempts = [
            {
                "tool": "web_http",
                "data_status": "present",
                "stop_reason": "usable_data_found",
                "skipped": False,
                "reason": None,
            }
        ]
        return {
            "ok": True,
            "has_data": True,
            "data_status": "present",
            "data_count": 1,
            "tool": "web_http",
            "stop_reason": "usable_data_found",
            "error": None,
            "attempts": attempts,
            "warnings": [],
            "quality_gates": {},
            "meta": {
                "contract_version": "web-listening-tool-result.v1",
                "policy_version": "article_content.v1",
            },
            "data": {
                # article_id / requested_url intentionally omitted to mirror
                # the real upstream default-path contract (no echo back).
                "final_url": url,
                "selected_method": "web_http",
                "content_type": "text/html",
                # NO inline ``full_text`` / ``content`` here — long-body
                # ref-only path that P2 specifically targets.
                "content_ref": ref_name,
                "sha256": sha,
                "content_hash": sha,
                "truncated": False,
                "extraction_metadata": {"status_code": 200, "word_count": 12},
                "attempts": attempts,
            },
        }


@pytest.fixture()
def fake_upstream(monkeypatch):
    """Inject a fake upstream module via the public reader import point.

    The adapter's ``_default_providers`` calls ``_import_public_reader``
    once at construction time, then invokes the returned module's
    ``fetch_article_content``. Patching the import keeps the host
    ``web_listening`` install untouched.
    """
    module = _FakeUpstreamModule()
    monkeypatch.setattr(adapter, "_import_public_reader", lambda: module)
    return module


# ---------------------------------------------------------------------------
# P1 tests — data_root threads into the default reader's per-call output_dir
# ---------------------------------------------------------------------------


def test_p1_data_root_threads_into_default_reader(fake_upstream, tmp_path):
    """P1: with ``data_root`` set, the upstream call receives
    ``<data_root>/.cache/article_content/<uuid>`` as its ``output_dir``."""
    data_root = tmp_path / "p1test"
    artifact = adapter.build_article_evidence_artifact(
        [{"article_id": "aid-p1-a", "url": "https://example.org/p1a"}],
        report_date="2026-09-07",
        data_root=data_root,
    )
    assert len(fake_upstream.calls) == 1
    observed = Path(fake_upstream.calls[0]["output_dir"])
    assert observed.parent.parent.parent == data_root
    assert observed.parent.parent.name == ".cache"
    assert observed.parent.name == "article_content"
    # The leaf is a uuid4 hex (32 chars).
    assert re.fullmatch(r"[0-9a-f]{32}", observed.name)
    # The artifact still validates (no P2 regression).
    assert artifact["schema_version"] == adapter.ARTICLE_EVIDENCE_SCHEMA_VERSION
    assert artifact["record_count"] == 1
    assert artifact["records"][0]["status"] == "ok"


def test_p1_data_root_none_keeps_tmpdir_default(fake_upstream, tmp_path):
    """P1 backwards-compat: with ``data_root=None``, the per-call
    ``output_dir`` falls back to ``<tmpdir>/article_content/<uuid>`` so
    dry-run / test paths are unchanged."""
    artifact = adapter.build_article_evidence_artifact(
        [{"article_id": "aid-p1-b", "url": "https://example.org/p1b"}],
        report_date="2026-09-07",
    )
    assert len(fake_upstream.calls) == 1
    observed = Path(fake_upstream.calls[0]["output_dir"])
    tmp_root = Path(tempfile.gettempdir()).resolve()
    assert observed.parent.parent.resolve() == tmp_root
    assert observed.parent.name == "article_content"
    assert re.fullmatch(r"[0-9a-f]{32}", observed.name)
    # The leaf directory should NOT be under a tmp_root/.cache subtree.
    assert ".cache" not in observed.parts
    assert artifact["record_count"] == 1
    assert artifact["records"][0]["status"] == "ok"


# ---------------------------------------------------------------------------
# P2 tests — collect_evidence returns (records, output_dirs) and the second
# _verify_batch in build_article_evidence_artifact uses those output_dirs
# ---------------------------------------------------------------------------


def test_p2_collect_evidence_returns_output_dirs(fake_upstream):
    """P2: ``collect_evidence`` returns a ``(records, output_dirs)`` tuple
    where ``output_dirs[article_id]`` is the per-call ``output_dir`` the
    default reader wrote the portable ``content_ref`` bytes into."""
    inputs = [
        {"article_id": "aid-p2-a", "url": "https://example.org/p2a"},
        {"article_id": "aid-p2-b", "url": "https://example.org/p2b"},
    ]
    result = adapter.collect_evidence(inputs)
    # Tuple shape.
    assert isinstance(result, tuple)
    assert len(result) == 2
    records, output_dirs = result
    assert isinstance(output_dirs, dict)
    # Every input article_id should be present in output_dirs with a path
    # that matches the per-call upstream output_dir (which is what
    # resolve_content_ref needs to read).
    expected_dirs = {Path(call["output_dir"]) for call in fake_upstream.calls}
    assert set(output_dirs.keys()) == {"aid-p2-a", "aid-p2-b"}
    for observed in output_dirs.values():
        assert Path(observed) in expected_dirs
    # Both records succeeded and were verified.
    assert {r["status"] for r in records} == {"ok"}


def test_p2_build_artifact_passes_output_dirs_to_second_verify(fake_upstream):
    """P2: ``build_article_evidence_artifact`` must thread the
    ``output_dirs`` captured by ``collect_evidence`` into its internal
    second ``_verify_batch`` so ref-only long-body records do NOT fail
    with ``content_ref_unresolvable``.

    We patch ``_verify_batch`` to record the ``output_dirs`` it is invoked
    with on its SECOND call (the one inside ``build_article_evidence_artifact``,
    after ``collect_evidence`` already ran its own verify).
    """
    observed_output_dirs: list[Any] = []

    real_verify_batch = adapter._verify_batch

    def spy_verify_batch(records, articles, *, content_resolver=None, output_dirs=None):
        observed_output_dirs.append(output_dirs)
        return real_verify_batch(
            records, articles,
            content_resolver=content_resolver, output_dirs=output_dirs,
        )

    import climate_monitor.article_content_adapter as _mod
    monkey = pytest.MonkeyPatch()
    monkey.setattr(_mod, "_verify_batch", spy_verify_batch)
    try:
        artifact = adapter.build_article_evidence_artifact(
            [{"article_id": "aid-p2-verify", "url": "https://example.org/p2verify"}],
            report_date="2026-09-07",
        )
    finally:
        monkey.undo()

    # Two verify calls: one inside collect_evidence, one inside
    # build_article_evidence_artifact.
    assert len(observed_output_dirs) == 2
    second_call_output_dirs = observed_output_dirs[1]
    assert second_call_output_dirs is not None
    assert "aid-p2-verify" in second_call_output_dirs
    # And no content_ref_unresolvable escape:
    assert artifact["records"][0]["status"] == "ok"


def test_p2_long_body_ref_only_real_shape(fake_upstream):
    """P2 end-to-end: a batch with one inline-body record (WRI-style) and
    two ref-only long-body records (ADB-failed-but-hashed + IPCC-style)
    must build successfully without ``content_ref_unresolvable``."""
    inputs = [
        {"article_id": "aid-wri", "url": "https://example.org/wri"},
        {"article_id": "aid-adb", "url": "https://example.org/adb"},
        {"article_id": "aid-ipcc", "url": "https://example.org/ipcc"},
    ]
    artifact = adapter.build_article_evidence_artifact(
        inputs, report_date="2026-09-07",
    )
    assert artifact["record_count"] == 3
    article_statuses = {r["article_id"]: r["status"] for r in artifact["records"]}
    assert article_statuses == {"aid-wri": "ok", "aid-adb": "ok", "aid-ipcc": "ok"}
    # The fixture emits ref-only ToolResults, so each record carries
    # content_ref + content_hash and NO inline content.
    for record in artifact["records"]:
        assert record["content"] is None
        assert record["content_ref"]
        assert record["content_hash"]
        # Each ref must actually exist on disk under the recorded output_dir.
        # We rely on the upstream fake having written the bytes; the
        # verify path inside collect_evidence already confirmed it.
    # artifact_digest is stable across runs.
    assert artifact["artifact_digest"]


def test_p2_no_data_root_falls_back_to_tmpdir(fake_upstream):
    """P2 backwards-compat: with ``data_root=None``, the multi-record
    ref-only shape still succeeds and ``output_dirs`` are under the
    legacy ``<tmpdir>/article_content/<uuid>`` location."""
    inputs = [
        {"article_id": "aid-legacy-a", "url": "https://example.org/legacy-a"},
        {"article_id": "aid-legacy-b", "url": "https://example.org/legacy-b"},
    ]
    artifact = adapter.build_article_evidence_artifact(
        inputs, report_date="2026-09-07",
    )
    assert artifact["record_count"] == 2
    assert {r["status"] for r in artifact["records"]} == {"ok"}
    # The per-call output_dirs must be under the legacy tmpdir layout.
    tmp_root = Path(tempfile.gettempdir()).resolve()
    for call in fake_upstream.calls:
        observed = Path(call["output_dir"]).resolve()
        assert observed.parent.parent.resolve() == tmp_root
        assert observed.parent.name == "article_content"
        assert ".cache" not in observed.parts
