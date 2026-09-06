"""Focused tests for climate_monitor/article_content_adapter.py (Issue #92 AC-5..7).

These tests use fake providers and do NOT touch the network, the production
``web_listening`` package, or the production Registry. The adapter MUST be
honest about dependency status: without an importable public reader or explicit
provider, every record is URL-only with ``status="unavailable"`` and a populated
``failure_reason``. Explicit loopbacks always override dependency status.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from climate_monitor import article_content_adapter as adapter
from tests.fixtures.article_content import providers as loopbacks


# ---------------------------------------------------------------------------
# Test helpers / fixtures
# ---------------------------------------------------------------------------


class _FakeProvider:
    """In-memory single-URL provider used as a fake ``web_listening`` adapter.

    Each call to ``fetch(article_id, url)`` returns the next entry from
    ``self.records`` keyed by ``article_id``. The ``call_log`` records
    every invocation so tests can assert that the adapter only invokes the
    underlying provider once per ``article_id`` per run.
    """

    def __init__(
        self,
        *,
        records: dict[str, dict[str, Any]] | None = None,
        raises: type[BaseException] | None = None,
        final_url: str = "",
    ):
        self.records = dict(records or {})
        self.raises = raises
        self.final_url = final_url
        self.call_log: list[tuple[str, str]] = []

    def __call__(self, article_id: str, url: str) -> dict[str, Any]:
        self.call_log.append((article_id, url))
        if self.raises is not None:
            raise self.raises("simulated provider failure")
        record = self.records.get(article_id)
        if record is None:
            return {
                "status": "no_content",
                "final_url": self.final_url or url,
                "selected_method": None,
                "content_type": None,
                "content_ref": None,
                "content": None,
                "content_hash": None,
                "attempts": [],
                "summary_basis": None,
            }
        return dict(record)


@pytest.fixture()
def ensure_unavailable(monkeypatch):
    """Force the dependency probe to report ``"unavailable"``.

    The current production state has no installed ``web_listening`` package,
    so this is the real-world default. We make it explicit anyway.
    """

    import climate_monitor.article_content_adapter as adapter

    monkeypatch.setattr(adapter, "_import_web_listening_contract", lambda: None)


@pytest.fixture()
def force_available(monkeypatch):
    """Inject a fake ``web_listening`` contract import surface."""

    import climate_monitor.article_content_adapter as adapter

    sentinel_module = type(sys)("web_listening_fake")
    sentinel_module.__path__ = []  # mark as package
    contracts = type(sys)("web_listening_fake.contracts")
    contracts.__path__ = []
    article_content = type(sys)("web_listening_fake.contracts.article_content")
    article_content.PROVIDERS = ("http", "browser", "stealth")

    def _importer():
        return article_content

    monkeypatch.setattr(adapter, "_import_web_listening_contract", _importer)
    return article_content


@pytest.fixture()
def force_partial(monkeypatch):
    """Contract present but no providers attached (e.g. still unconfigured)."""

    import climate_monitor.article_content_adapter as adapter

    sentinel_module = type(sys)("web_listening_fake_partial")
    sentinel_module.__path__ = []
    contracts = type(sys)("web_listening_fake_partial.contracts")
    contracts.__path__ = []
    article_content = type(sys)("web_listening_fake_partial.contracts.article_content")
    # No PROVIDERS attribute — partial.

    def _importer():
        return article_content

    monkeypatch.setattr(adapter, "_import_web_listening_contract", _importer)
    return article_content


# ---------------------------------------------------------------------------
# check_dependencies
# ---------------------------------------------------------------------------


def test_check_dependencies_returns_unavailable_when_contract_missing(
    ensure_unavailable,
):
    from climate_monitor.article_content_adapter import check_dependencies

    assert check_dependencies() == "unavailable"


def test_check_dependencies_returns_partial_when_contract_present_without_providers(
    force_partial,
):
    from climate_monitor.article_content_adapter import check_dependencies

    assert check_dependencies() == "partial"


def test_check_dependencies_returns_available_when_contract_and_providers_present(
    force_available,
):
    from climate_monitor.article_content_adapter import check_dependencies

    assert check_dependencies() == "available"


# ---------------------------------------------------------------------------
# fetch_article_content
# ---------------------------------------------------------------------------


def test_fetch_returns_unavailable_record_when_dependency_missing(ensure_unavailable):
    from climate_monitor.article_content_adapter import fetch_article_content

    record = fetch_article_content("aid-1", "https://example.org/a")
    assert record["article_id"] == "aid-1"
    assert record["requested_url"] == "https://example.org/a"
    assert record["status"] == "unavailable"
    assert record["selected_method"] is None
    assert record["content"] is None
    assert record["content_hash"] is None
    assert record["summary_basis"] == "none"
    assert (
        record["failure_reason"]
        == "web_listening#70 article_content fallback policy not yet available"
    )
    assert record["attempts"] == []


def test_explicit_providers_override_partial_dependency(force_partial):
    from climate_monitor.article_content_adapter import fetch_article_content

    provider = _FakeProvider()
    record = fetch_article_content("aid-2", "https://example.org/partial", providers=(provider,))
    assert provider.call_log == [("aid-2", "https://example.org/partial")]
    assert record["status"] == "no_content"


def test_no_providers_with_partial_dependency_returns_unavailable_record(force_partial):
    from climate_monitor.article_content_adapter import fetch_article_content

    record = fetch_article_content("aid-2", "https://example.org/partial")
    assert record["status"] == "unavailable"
    assert record["content"] is None
    assert record["content_hash"] is None


def test_fetch_dispatches_to_provider_when_available(force_available):
    from climate_monitor.article_content_adapter import fetch_article_content

    provider = _FakeProvider(
        records={
            "aid-1": {
                "status": "ok",
                "final_url": "https://example.org/a",
                "selected_method": "http",
                "content_type": "text/html",
                "content_ref": None,
                "content": "<html>...</html>",
                "content_hash": hashlib.sha256(b"<html>...</html>").hexdigest(),
                "attempts": [{"provider": "http", "status": "ok"}],
                "summary_basis": "page",
            }
        }
    )
    record = fetch_article_content(
        "aid-1", "https://example.org/a", providers=(provider,)
    )
    assert record["status"] == "ok"
    assert record["selected_method"] == "http"
    assert record["content_hash"] == hashlib.sha256(b"<html>...</html>").hexdigest()
    assert provider.call_log == [("aid-1", "https://example.org/a")]


def test_fetch_swallows_runtime_error_and_marks_failed(force_available):
    from climate_monitor.article_content_adapter import fetch_article_content

    provider = _FakeProvider(raises=RuntimeError)
    record = fetch_article_content(
        "aid-x", "https://example.org/x", providers=(provider,)
    )
    assert record["status"] == "failed"
    assert record["selected_method"] is None
    assert record["content"] is None
    assert record["content_hash"] is None
    assert "RuntimeError" in (record["failure_reason"] or "")


# ---------------------------------------------------------------------------
# collect_evidence
# ---------------------------------------------------------------------------


def test_collect_evidence_emits_exactly_one_record_per_unique_article_id(force_available):
    from climate_monitor.article_content_adapter import collect_evidence

    inputs = [
        {"article_id": "aid-1", "url": "https://example.org/a", "title": "A"},
        {"article_id": "aid-1", "url": "https://example.org/a", "title": "A again"},
        {"article_id": "aid-2", "url": "https://example.org/b", "title": "B"},
    ]
    provider = _FakeProvider()
    records = collect_evidence(inputs, providers=(provider,))
    assert len(records) == 2
    article_ids = [r["article_id"] for r in records]
    assert article_ids == ["aid-1", "aid-2"]
    # aid-1 only called once even though it appears twice in inputs.
    call_for_aid_1 = [c for c in provider.call_log if c[0] == "aid-1"]
    assert len(call_for_aid_1) == 1


def test_collect_evidence_distinct_urls_with_identical_title_remain_distinct(
    force_available,
):
    from climate_monitor.article_content_adapter import collect_evidence

    inputs = [
        {"article_id": "aid-1", "url": "https://example.org/a", "title": "Same"},
        {"article_id": "aid-2", "url": "https://example.org/b", "title": "Same"},
    ]
    records = collect_evidence(inputs, providers=(_FakeProvider(),))
    assert [r["article_id"] for r in records] == ["aid-1", "aid-2"]
    assert records[0]["article_id"] != records[1]["article_id"]


def test_collect_evidence_three_inputs_produce_three_records_in_input_order(
    force_available,
):
    from climate_monitor.article_content_adapter import collect_evidence

    inputs = [
        {"article_id": f"aid-{i}", "url": f"https://example.org/{i}", "title": f"T{i}"}
        for i in range(3)
    ]
    records = collect_evidence(inputs, providers=(_FakeProvider(),))
    assert [r["article_id"] for r in records] == ["aid-0", "aid-1", "aid-2"]
    # Every record has a deterministic content_hash (even when None content).
    for record in records:
        # Re-derive the hash from the canonical record bytes and confirm
        # equality. The hash is version-prefixed so consumers can recompute
        # it from the documented digest version + canonical JSON bytes.
        from climate_monitor.article_content_adapter import (
            RECORD_DIGEST_VERSION,
            _serialize_evidence_record,
        )
        canonical = _serialize_evidence_record(record)
        digest_input = RECORD_DIGEST_VERSION.encode("ascii") + b"\n" + canonical
        expected = hashlib.sha256(digest_input).hexdigest()
        assert record["record_hash"] == expected


def test_collect_evidence_unavailable_path_emits_honest_records(ensure_unavailable):
    from climate_monitor.article_content_adapter import collect_evidence

    inputs = [
        {"article_id": "aid-1", "url": "https://example.org/a"},
        {"article_id": "aid-2", "url": "https://example.org/b"},
    ]
    records = collect_evidence(inputs, providers=())
    assert len(records) == 2
    for record in records:
        assert record["status"] == "unavailable"
        assert record["selected_method"] is None
        assert record["content"] is None
        assert record["content_hash"] is None
        assert record["summary_basis"] == "none"
        assert (
            record["failure_reason"]
            == "web_listening#70 article_content fallback policy not yet available"
        )


def test_collect_evidence_provider_runtime_error_still_emits_record(force_available):
    from climate_monitor.article_content_adapter import collect_evidence

    inputs = [{"article_id": "aid-x", "url": "https://example.org/x"}]
    provider = _FakeProvider(raises=RuntimeError("boom"))
    records = collect_evidence(inputs, providers=(provider,))
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert "RuntimeError" in (records[0]["failure_reason"] or "")


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------


def test_artifact_path_uses_versioned_filename(tmp_path):
    from climate_monitor.article_content_adapter import article_evidence_artifact_path

    path = article_evidence_artifact_path(tmp_path, "2026-09-14")
    assert path.name == "article-evidence.v1_2026-09-14.json"


def test_wired_artifact_matches_documented_schema(tmp_path, force_available, monkeypatch):
    """The wired entrypoint writes an artifact that satisfies the documented
    ``article-evidence.v1`` shape. We validate with ``jsonschema`` when
    available, and fall back to field-presence assertions otherwise."""

    from climate_monitor.article_content_adapter import (
        ARTICLE_EVIDENCE_SCHEMA,
        build_article_evidence_artifact,
    )

    inputs = [
        {"article_id": "aid-1", "url": "https://example.org/a", "title": "A"},
        {"article_id": "aid-2", "url": "https://example.org/b", "title": "B"},
    ]
    artifact = build_article_evidence_artifact(
        inputs, providers=(_FakeProvider(),), report_date="2026-09-14"
    )

    assert artifact["schema_version"] == "article-evidence.v1"
    assert artifact["report_date"] == "2026-09-14"
    assert artifact["record_count"] == 2
    assert artifact["dependency_status"] == "available"
    assert isinstance(artifact["records"], list)
    assert len(artifact["records"]) == 2

    # Field-presence check on every record.
    required = {
        "article_id",
        "requested_url",
        "final_url",
        "status",
        "attempts",
        "selected_method",
        "content_type",
        "content_ref",
        "content_hash",
        "summary_basis",
        "record_hash",
        "failure_reason",
    }
    for record in artifact["records"]:
        missing = required - set(record.keys())
        assert not missing, f"missing fields: {missing}"

    # Optional: jsonschema validation (skipped if jsonschema import fails).
    try:
        import jsonschema  # type: ignore

        jsonschema.validate(artifact, ARTICLE_EVIDENCE_SCHEMA)
    except ImportError:
        pytest.skip("jsonschema not installed; field-presence check is sufficient")


def test_artifact_digest_is_deterministic(force_available):
    """Two builds with the same inputs must produce the same digest."""
    from climate_monitor.article_content_adapter import build_article_evidence_artifact

    inputs = [
        {"article_id": "aid-1", "url": "https://example.org/a", "title": "A"},
        {"article_id": "aid-2", "url": "https://example.org/b", "title": "B"},
    ]
    a = build_article_evidence_artifact(
        inputs, providers=(_FakeProvider(),), report_date="2026-09-14"
    )
    b = build_article_evidence_artifact(
        inputs, providers=(_FakeProvider(),), report_date="2026-09-14"
    )
    assert a["artifact_digest"] == b["artifact_digest"]
    assert a["records"][0]["record_hash"] == b["records"][0]["record_hash"]


def test_artifact_unavailable_path_is_honest(tmp_path, ensure_unavailable):
    from climate_monitor.article_content_adapter import build_article_evidence_artifact

    inputs = [{"article_id": "aid-1", "url": "https://example.org/a"}]
    artifact = build_article_evidence_artifact(
        inputs, providers=(), report_date="2026-09-14"
    )
    assert artifact["dependency_status"] == "unavailable"
    assert artifact["records"][0]["status"] == "unavailable"
    assert (
        artifact["records"][0]["failure_reason"]
        == "web_listening#70 article_content fallback policy not yet available"
    )


# ---------------------------------------------------------------------------
# Wiring into scripts/run_climate_monitor.py
# ---------------------------------------------------------------------------


def test_run_climate_monitor_wires_article_evidence_artifact(tmp_path, monkeypatch):
    """The wired entrypoint must produce an article-evidence.v1 artifact.

    This exercises the post-#91 unique-candidate set → adapter path without
    running the full Step 1-5 pipeline. We patch ``run_monitor`` to return
    a synthetic ``MonitorRunResult`` carrying a unique-candidate list and
    then call ``scripts.run_climate_monitor.main`` with a writable
    ``--state-dir`` so the adapter output is materialised on disk.
    """

    from climate_monitor.models import CandidateItem, MonitorRunResult
    from scripts import run_climate_monitor

    state_dir = tmp_path / "monitoring" / "state"
    state_dir.mkdir(parents=True)
    source_dir = tmp_path / "sources"
    source_dir.mkdir(parents=True)
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True)
    seen_urls = state_dir / "seen_urls.json"
    seen_urls.write_text("[]")

    fake_items = (
        CandidateItem(
            title="Climate insurance report",
            url="https://example.org/climate",
            summary="",
            source_name="Example",
            lane="website",
            content_hash="",
        ),
    )

    def _fake_run_monitor(**kwargs):
        return MonitorRunResult(
            report_date=kwargs["report_date"],
            report_path=None,
            items=fake_items,
            synced=False,
        )

    monkeypatch.setattr(run_climate_monitor, "run_monitor", _fake_run_monitor)

    from climate_monitor import article_content_adapter as adapter

    captured: dict[str, Path] = {}

    def _fake_collect(unique_articles, *, providers=(), report_date=""):
        artifact = adapter.build_article_evidence_artifact(
            unique_articles,
            providers=providers,
            report_date=report_date or "2026-09-14",
        )
        out_path = source_dir / f"article-evidence.v1_{report_date or '2026-09-14'}.json"
        out_path.write_text(json.dumps(artifact, indent=2))
        captured["path"] = out_path
        return artifact

    monkeypatch.setattr(
        run_climate_monitor, "build_article_evidence_artifact", _fake_collect
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_climate_monitor.py",
            "--date",
            "2026-09-14",
            "--state-dir",
            str(state_dir),
            "--source-dir",
            str(source_dir),
            "--wiki-dir",
            str(wiki_dir),
            "--no-sync",
            "--no-update-seen-state",
        ],
    )
    run_climate_monitor.main()

    assert "path" in captured, "adapter output path was not captured"
    artifact = json.loads(captured["path"].read_text())
    assert artifact["schema_version"] == "article-evidence.v1"
    assert artifact["record_count"] == 1
    assert artifact["records"][0]["article_id"]  # non-empty
    assert artifact["records"][0]["requested_url"] == "https://example.org/climate"


def test_url_only_summary_basis_is_none(ensure_unavailable):
    from climate_monitor.article_content_adapter import fetch_article_content
    assert fetch_article_content("a", "https://example.org/a")["summary_basis"] == "none"


@pytest.mark.parametrize("status,expected", [
    ("present", "ok"), ("no_content", "no_content"), ("not_found", "failed"),
    ("auth_required", "failed"), ("permission_denied", "failed"),
    ("blocked", "failed"), ("interaction_required", "failed"),
    ("failed_quality_gate", "failed"), ("error", "failed"), ("redirected", "no_content"),
])
def test_tool_result_status_mapping(status, expected):
    payload = loopbacks.loopback_success_provider("a", "https://example.org/a")
    payload.update(data_status=status, stop_reason="reader_runtime_error" if status == "error" else status)
    record = adapter.map_tool_result_to_record("a", "https://example.org/a", None, payload)
    assert record["status"] == expected
    assert record["attempts"] == payload["data"]["attempts"]
    assert record["selected_method"] == "web_http"
    assert record["content_type"] == "text/html"
    assert record["extra"]["extraction_metadata"]["status_code"] == 200
    if expected == "ok":
        assert record["content_ref"] == payload["data"]["content_ref"]
        assert record["content_hash"] == payload["data"]["sha256"]
        assert record["summary_basis"] == "page"
    else:
        assert record["content_ref"] is record["content_hash"] is record["content"] is None
        assert record["summary_basis"] == "none"
    if expected == "failed":
        assert record["failure_reason"] == payload["stop_reason"]


def test_preview_is_never_canonical_body():
    records = adapter.collect_evidence([{"article_id": "a", "url": "https://example.org/a"}],
        providers=(loopbacks.loopback_preview_only_provider,))
    record = records[0]
    assert record["content"] is None
    assert record["summary_basis"] == "preview_only"
    assert record["extra"]["content_status"] == "present_preview_only"
    assert len(record["extra"]["truncated_preview"]) == 2000
    full = loopbacks.in_process_resolver(record["content_ref"], record["content_hash"])
    assert len(full) > 2000
    assert hashlib.sha256(full).hexdigest() == record["content_hash"]


def test_redirect_preserves_requested_and_final_urls():
    record = adapter.fetch_article_content("a", "https://example.org/a",
        providers=(loopbacks.loopback_redirect_provider,))
    assert record["requested_url"] == "https://example.org/a"
    assert record["final_url"] == "https://example.org/a/redirected"
    assert record["extra"]["redirected"] is True
    assert record["attempts"][-1]["redirected"] is True


def test_safety_error_does_not_become_successful_redirect():
    payload = loopbacks.loopback_redirect_provider("a", "https://example.org/a")
    payload.update(data_status="error", stop_reason="unsafe_redirect")
    record = adapter.map_tool_result_to_record("a", "https://example.org/a", None, payload)
    assert record["status"] == "failed"
    assert "redirected" not in record["extra"]


@pytest.mark.parametrize("snippet", [None, "Input search evidence"])
def test_snippet_only_comes_from_input(snippet):
    def provider(a, u):
        payload = loopbacks.loopback_no_content_provider(a, u)
        payload["data"]["search_snippet"] = "Invented upstream text"
        payload["data"]["snippet"] = "Invented upstream text"
        return payload
    record = adapter.collect_evidence([{"article_id": "a", "url": "https://example.org/a",
        "search_snippet": snippet}], providers=(provider,))[0]
    assert record["summary_basis"] == ("search_snippet" if snippet else "none")
    assert record["extra"].get("search_snippet") == snippet
    assert "Invented" not in json.dumps(record)
    assert record["content"] is None


@pytest.mark.parametrize("provider,reason", [
    (loopbacks.loopback_damaged_content_ref_provider, "content_ref_unresolvable"),
    (loopbacks.loopback_hash_mismatch_provider, "content_hash_mismatch"),
    (loopbacks.loopback_wrong_identity_provider, "wrong_article_id"),
])
def test_corrupt_batch_has_no_partial_write(tmp_path, provider, reason):
    def mixed(a, u):
        return loopbacks.loopback_success_provider(a, u) if a == "good" else provider(a, u)
    mixed.content_resolver = loopbacks.in_process_resolver
    with pytest.raises(adapter.ArticleContentAdapterError, match=reason):
        adapter.run_article_evidence([
            {"article_id": "good", "url": "https://example.org/good"},
            {"article_id": "bad", "url": "https://example.org/bad"}],
            providers=(mixed,), report_date="2026-09-07", source_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_wrong_requested_url_rejects_batch(tmp_path):
    def provider(a, u):
        payload = loopbacks.loopback_success_provider(a, u)
        payload["data"]["requested_url"] = "https://wrong.example/"
        return payload
    with pytest.raises(adapter.ArticleContentAdapterError, match="wrong_requested_url"):
        adapter.run_article_evidence([{"article_id": "a", "url": "https://example.org/a"}],
            providers=(provider,), report_date="2026-09-07", source_dir=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind,reason", [
    ("missing", "missing_output_identity"), ("extra", "extra_output_identity"),
    ("duplicate", "duplicate_output_identity"), ("empty", "missing_output_identity"),
])
def test_output_set_mismatch_rejects_entire_artifact(tmp_path, monkeypatch, kind, reason):
    inputs = [{"article_id": "a", "url": "https://example.org/a"},
              {"article_id": "b", "url": "https://example.org/b"}]
    original = adapter.collect_evidence
    def corrupt(articles, **kw):
        records = original(articles, providers=(loopbacks.loopback_no_content_provider,))
        if kind == "missing":
            records.pop()
        elif kind == "extra":
            records.append({**records[0], "article_id": "extra"})
        elif kind == "duplicate":
            records.append(dict(records[0]))
        else:
            records[0].update(article_id="", requested_url="")
        return records
    monkeypatch.setattr(adapter, "collect_evidence", corrupt)
    with pytest.raises(adapter.ArticleContentAdapterError, match=reason):
        adapter.run_article_evidence(inputs, report_date="2026-09-07", source_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_missing_input_identity_rejects_before_fetch(tmp_path):
    provider = _FakeProvider()
    with pytest.raises(adapter.ArticleContentAdapterError, match="missing_input_identity"):
        adapter.run_article_evidence([{}], providers=(provider,),
            report_date="2026-09-07", source_dir=tmp_path)
    assert provider.call_log == []
    assert not list(tmp_path.iterdir())


def test_same_url_across_a_b_fetches_once():
    provider = _FakeProvider()
    records = adapter.collect_evidence([
        {"article_id": "pillar-a", "url": "https://example.org/a?utm_source=mail"},
        {"article_id": "pillar-b", "url": "https://example.org/a"}], providers=(provider,))
    assert len(provider.call_log) == len(records) == 1
    assert records[0]["article_id"] == "pillar-a"


def test_one_id_cannot_fetch_two_urls():
    provider = _FakeProvider()
    with pytest.raises(adapter.ArticleContentAdapterError, match="conflicting_article_id"):
        adapter.collect_evidence([{"article_id": "a", "url": "https://example.org/a"},
            {"article_id": "a", "url": "https://example.org/b"}], providers=(provider,))
    assert not provider.call_log


def test_artifact_digest_survives_reserialization(tmp_path):
    _, path = adapter.run_article_evidence([{"article_id": "a", "url": "https://example.org/a"}],
        providers=(loopbacks.loopback_success_provider,), report_date="2026-09-07", source_dir=tmp_path)
    artifact = json.loads(path.read_text())
    for record in artifact["records"]:
        payload = {k: v for k, v in record.items() if k != "record_hash"}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        assert hashlib.sha256((adapter.RECORD_DIGEST_VERSION + "\n" + canonical).encode()).hexdigest() == record["record_hash"]
    hashes = json.dumps([r["record_hash"] for r in artifact["records"]], separators=(",", ":"))
    assert hashlib.sha256((adapter.ARTICLE_EVIDENCE_DIGEST_VERSION + "\n" + hashes).encode()).hexdigest() == artifact["artifact_digest"]


def test_default_public_provider_wraps_url_signature_and_tool_result(monkeypatch):
    from types import SimpleNamespace
    calls = []
    class ToolResult:
        def model_dump(self):
            payload = loopbacks.loopback_success_provider("a", "https://example.org/a")
            payload["data"].pop("article_id")  # Not required by the upstream envelope.
            return payload
    def upstream(url):
        calls.append(url)
        return ToolResult()
    module = SimpleNamespace(fetch_article_content=upstream,
        _output_path=lambda output: "/upstream/default/article_content",
        _read_evidence=lambda root, ref, digest: loopbacks.in_process_resolver(ref, digest))
    original = adapter.importlib.import_module
    monkeypatch.setattr(adapter.importlib, "import_module", lambda name:
        module if name == "web_listening.blocks.article_content" else original(name))
    assert adapter.check_dependencies() == "available"
    artifact = adapter.build_article_evidence_artifact([
        {"article_id": "a", "url": "https://example.org/a"}], report_date="2026-09-07")
    assert calls == ["https://example.org/a"]
    assert artifact["records"][0]["status"] == "ok"


def test_explicit_provider_overrides_unavailable_and_default(monkeypatch):
    monkeypatch.setattr(adapter, "check_dependencies", lambda: "unavailable")
    monkeypatch.setattr(adapter, "_default_providers", lambda: pytest.fail("default provider invoked"))
    record = adapter.collect_evidence([{"article_id": "a", "url": "https://example.org/a"}],
        providers=(loopbacks.loopback_success_provider,))[0]
    assert record["status"] == "ok"


def test_unresolvable_default_ref_fails_closed(monkeypatch):
    monkeypatch.setattr(adapter, "_import_public_reader", lambda: None)
    with pytest.raises(adapter.ArticleContentAdapterError, match="content_ref_unresolvable"):
        adapter.resolve_content_ref("missing", "0" * 64)


def test_stealth_skip_attempt_stays_ordered():
    record = adapter.fetch_article_content("a", "https://example.org/a",
        providers=(loopbacks.loopback_stealth_skip_provider,))
    assert [a["tool"] for a in record["attempts"]] == ["web_http", "cloakbrowser"]
    assert record["attempts"][1]["skipped"] is True
    assert record["status"] == "no_content"


def test_failed_batch_preserves_previous_artifact(tmp_path):
    path = tmp_path / "article-evidence.v1_2026-09-07.json"
    path.write_text("previous artifact")
    with pytest.raises(adapter.ArticleContentAdapterError, match="content_hash_mismatch"):
        adapter.run_article_evidence([{"article_id": "a", "url": "https://example.org/a"}],
            providers=(loopbacks.loopback_hash_mismatch_provider,), report_date="2026-09-07", source_dir=tmp_path)
    assert path.read_text() == "previous artifact"
    assert list(tmp_path.iterdir()) == [path]


def test_missing_provider_output_is_rejected(tmp_path):
    with pytest.raises(adapter.ArticleContentAdapterError, match="invalid_tool_result"):
        adapter.run_article_evidence([{"article_id": "a", "url": "https://example.org/a"}],
            providers=(lambda a, u: None,), report_date="2026-09-07", source_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_first_provider_is_the_only_provider_called():
    def unexpected(a, u):
        pytest.fail("consumer must not reimplement the upstream fallback chain")
    record = adapter.fetch_article_content("a", "https://example.org/a",
        providers=(loopbacks.loopback_failure_provider, unexpected))
    assert record["status"] == "failed"
    assert record["failure_reason"] == "reader_runtime_error"


@pytest.mark.parametrize("code", ["content_ref_corrupt", "content_ref_hash_mismatch",
                                  "capture_identity_mismatch", "capture_hash_mismatch"])
def test_upstream_integrity_error_rejects_batch(tmp_path, code):
    def provider(a, u):
        payload = loopbacks.loopback_failure_provider(a, u)
        payload["stop_reason"] = code
        payload["error"]["code"] = code
        return payload
    with pytest.raises(adapter.ArticleContentAdapterError, match=code):
        adapter.run_article_evidence([{"article_id": "a", "url": "https://example.org/a"}],
            providers=(provider,), report_date="2026-09-07", source_dir=tmp_path)
    assert not list(tmp_path.iterdir())
