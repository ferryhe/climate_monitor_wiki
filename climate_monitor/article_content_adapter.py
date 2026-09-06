"""Consume unique candidates through the public web_listening article reader.

Evidence is verified in memory before an atomic artifact write. Explicit
providers are the test/CI seam; discovery and acquisition policy remain upstream.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)


ARTICLE_EVIDENCE_SCHEMA_VERSION = "article-evidence.v1"
ARTICLE_EVIDENCE_DIGEST_VERSION = "article-evidence-digest.v1"
RECORD_DIGEST_VERSION = "article-evidence-record-digest.v1"

UNAVAILABLE_REASON = (
    "web_listening#70 article_content fallback policy not yet available"
)


# JSON Schema describing the documented ``article-evidence.v1`` shape.
# Kept here (not split into a file) so the adapter stays self-contained and
# the schema is co-located with the code that produces the artifact.
ARTICLE_EVIDENCE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": ARTICLE_EVIDENCE_SCHEMA_VERSION,
    "type": "object",
    "required": [
        "schema_version",
        "report_date",
        "generated_at",
        "dependency_status",
        "record_count",
        "records",
        "artifact_digest",
    ],
    "properties": {
        "schema_version": {"const": ARTICLE_EVIDENCE_SCHEMA_VERSION},
        "report_date": {"type": "string"},
        "generated_at": {"type": "string"},
        "dependency_status": {
            "type": "string",
            "enum": ["available", "partial", "unavailable"],
        },
        "record_count": {"type": "integer", "minimum": 0},
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
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
                ],
                "properties": {
                    "article_id": {"type": "string"},
                    "requested_url": {"type": ["string", "null"]},
                    "final_url": {"type": ["string", "null"]},
                    "status": {
                        "type": "string",
                        "enum": [
                            "ok",
                            "no_content",
                            "failed",
                            "unavailable",
                            "deferred",
                        ],
                    },
                    "attempts": {"type": "array"},
                    "selected_method": {"type": ["string", "null"]},
                    "content_type": {"type": ["string", "null"]},
                    "content_ref": {"type": ["string", "null"]},
                    "content_hash": {"type": ["string", "null"]},
                    "summary_basis": {"type": ["string", "null"]},
                    "failure_reason": {"type": ["string", "null"]},
                    "record_hash": {"type": "string"},
                },
            },
        },
        "artifact_digest": {"type": "string"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ArticleEvidenceRecord:
    """One canonical evidence record.

    All fields are typed so that the dataclass can be reused as the contract
    surface; serialization to JSON preserves ``None`` for missing values
    rather than fabricating placeholders.
    """

    article_id: str
    requested_url: str
    final_url: str | None
    status: str
    attempts: tuple[dict[str, Any], ...]
    selected_method: str | None
    content_type: str | None
    content_ref: str | None
    content: str | None = None  # bounded body when content_type implies it
    content_hash: str | None = None
    summary_basis: str | None = None
    failure_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "article_id": self.article_id,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status": self.status,
            "attempts": [dict(attempt) for attempt in self.attempts],
            "selected_method": self.selected_method,
            "content_type": self.content_type,
            "content_ref": self.content_ref,
            "content": self.content,
            "content_hash": self.content_hash,
            "summary_basis": self.summary_basis,
            "failure_reason": self.failure_reason,
        }
        payload["extra"] = dict(self.extra)
        return payload


# ---------------------------------------------------------------------------
# Dependency probe
# ---------------------------------------------------------------------------


def _import_web_listening_contract() -> Any | None:
    """Return the ``web_listening.contracts.article_content`` module if
    importable, otherwise ``None``. Retained for PR #98 compatibility;
    the public reader import is now probed first.
    """

    try:
        return importlib.import_module("web_listening.contracts.article_content")
    except Exception:
        return None


def check_dependencies() -> str:
    """Return one of ``"available" | "partial" | "unavailable"``.

    The public blocks.article_content reader takes precedence. The older
    contracts.article_content/PROVIDERS probe remains a compatibility fallback:
    available with providers, partial with just the module, otherwise unavailable.
    Explicit provider injection is independent of this descriptive probe.
    """

    public = _import_public_reader()
    if public is not None and callable(getattr(public, "fetch_article_content", None)):
        return "available"
    module = _import_web_listening_contract()
    if module is None:
        return "unavailable"
    providers = getattr(module, "PROVIDERS", None)
    if not providers:
        return "partial"
    return "available"


# ---------------------------------------------------------------------------
# Single-URL fetch
# ---------------------------------------------------------------------------


ProviderCallable = Callable[[str, str], Any]


def _unavailable_record(
    *,
    article_id: str,
    url: str,
    failure_reason: str = UNAVAILABLE_REASON,
) -> ArticleEvidenceRecord:
    return ArticleEvidenceRecord(
        article_id=article_id,
        requested_url=url,
        final_url=None,
        status="unavailable",
        attempts=(),
        selected_method=None,
        content_type=None,
        content_ref=None,
        content=None,
        content_hash=None,
        summary_basis="none",
        failure_reason=failure_reason,
    )


class ArticleContentAdapterError(RuntimeError):
    """Reject an untrustworthy evidence batch before publication."""


def _import_public_reader():
    try:
        return importlib.import_module("web_listening.blocks.article_content")
    except ImportError:
        return None


def resolve_content_ref(content_ref, content_hash) -> bytes:
    """Read upstream evidence from its default output directory.

    Tests may replace this function or attach a ``content_resolver(ref, hash)``
    callable to an explicitly injected provider. Inline content is never a
    substitute for resolving the reference.
    """
    module = _import_public_reader()
    if module is None or not hasattr(module, "_read_evidence"):
        raise ArticleContentAdapterError("content_ref_unresolvable")
    try:
        return module._read_evidence(module._output_path(None), content_ref, content_hash)
    except (OSError, ValueError) as exc:
        raise ArticleContentAdapterError(str(exc)) from exc


def _default_providers():
    module = _import_public_reader()
    if module is None or not callable(getattr(module, "fetch_article_content", None)):
        return ()

    def public_reader(article_id, url):
        return module.fetch_article_content(url)

    return (public_reader,)


def _tool_mapping(value):
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise ArticleContentAdapterError("invalid_tool_result")


def map_tool_result_to_record(article_id, url, snippet_input, tool_result) -> dict:
    """Map the public ToolResult envelope without manufacturing body or snippet."""
    result = _tool_mapping(tool_result)
    # Retain PR #98's flat provider mapping as a compatibility input.
    data = result.get("data", result)
    if not isinstance(data, Mapping):
        raise ArticleContentAdapterError("invalid_tool_result_data")
    for payload in (result, data):
        if "article_id" in payload and payload["article_id"] != article_id:
            raise ArticleContentAdapterError("wrong_article_id")
        if "requested_url" in payload and payload["requested_url"] != url:
            raise ArticleContentAdapterError("wrong_requested_url")
    status = result.get("data_status", result.get("status"))
    if status == "ok":
        status = "present"
    failed = {"not_found", "auth_required", "permission_denied", "blocked",
              "interaction_required", "failed_quality_gate", "error", "failed"}
    if status not in failed | {"present", "no_content", "redirected", "unavailable", "deferred"}:
        raise ArticleContentAdapterError("invalid_data_status")
    attempts = data.get("attempts", [])
    if not isinstance(attempts, list) or any(not isinstance(a, Mapping) for a in attempts):
        raise ArticleContentAdapterError("invalid_attempts")
    attempts = [dict(a) for a in attempts]
    error = result.get("error") or {}
    reason = result.get("stop_reason") or error.get("code") or data.get("failure_reason")
    integrity_errors = {"content_ref_corrupt", "content_ref_hash_mismatch",
                        "capture_identity_mismatch", "capture_hash_mismatch"}
    for code in (reason, error.get("code")):
        if code in integrity_errors:
            raise ArticleContentAdapterError(code)
    final_url = data.get("final_url") or url
    extra = {"extraction_metadata": data.get("extraction_metadata") or {}}
    safety_errors = {"unsafe_redirect", "blocked_redirect"}
    if final_url != url and status not in failed and reason not in safety_errors:
        extra["redirected"] = True
        if attempts:
            attempts[-1]["redirected"] = True
    ok = status == "present"
    preview = ok and data.get("truncated") is True
    if preview:
        extra["content_status"] = "present_preview_only"
        extra["truncated_preview"] = data.get("truncated_preview")
    if ok:
        evidence_status = "ok"
    elif status in failed:
        evidence_status = "failed"
    elif status == "redirected":
        evidence_status = "no_content"
    else:
        evidence_status = status
    record = ArticleEvidenceRecord(
        article_id=article_id, requested_url=url, final_url=final_url,
        status=evidence_status,
        attempts=tuple(attempts), selected_method=data.get("selected_method"),
        content_type=data.get("content_type"),
        content_ref=data.get("content_ref") if ok else None,
        content_hash=(data.get("sha256") or data.get("content_hash")) if ok else None,
        content=None if preview or not ok else data.get("full_text", data.get("content")),
        summary_basis="preview_only" if preview else "page" if ok else "none",
        failure_reason=(reason or status) if status in failed | {"unavailable", "deferred"} else None,
        extra=extra,
    ).to_dict()
    # Only a real input search_snippet can ground a snippet-only record.
    if not ok and snippet_input:
        record["summary_basis"] = "search_snippet"
        record["extra"]["search_snippet"] = snippet_input
    return record


def fetch_article_content(
    article_id: str,
    url: str,
    *,
    providers: Sequence[ProviderCallable] = (),
    snippet_input: str | None = None,
) -> dict[str, Any]:
    """Call provider[0] once; explicit providers override all dependency states.

    Provider exceptions become honest failed records. Invalid envelopes and
    identity mismatches reject the batch instead of becoming successful records.
    """
    if not url:
        return _unavailable_record(article_id=article_id, url=url, failure_reason="missing url").to_dict()
    providers = providers or _default_providers()
    if not providers:
        record = _unavailable_record(article_id=article_id, url=url).to_dict()
        if snippet_input:
            record["summary_basis"] = "search_snippet"
            record["extra"]["search_snippet"] = snippet_input
        return record
    try:
        payload = providers[0](article_id, url)
    except ArticleContentAdapterError:
        raise
    except Exception as exc:
        record = _unavailable_record(article_id=article_id, url=url,
            failure_reason=f"{type(exc).__name__}: {exc}").to_dict()
        record["status"] = "failed"
        return record
    return map_tool_result_to_record(article_id, url, snippet_input, payload)


def _collect_unique_articles(inputs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """URL-first identity, first occurrence wins; conflicting IDs fail closed."""
    from climate_monitor.dedupe import canonical_url

    seen_urls: set[str] = set()
    seen_ids: dict[str, str] = {}
    ordered = []
    for raw in inputs:
        url = str(raw.get("url") or "").strip()
        canonical = canonical_url(url)
        article_id = str(raw.get("article_id") or canonical).strip()
        if not article_id or not url:
            raise ArticleContentAdapterError("missing_input_identity")
        if article_id in seen_ids and seen_ids[article_id] != canonical:
            raise ArticleContentAdapterError("conflicting_article_id")
        seen_ids[article_id] = canonical
        if canonical in seen_urls:
            continue
        seen_urls.add(canonical)
        ordered.append({**raw, "article_id": article_id, "url": url})
    return ordered


def verify_record(record, *, inputs_index, content_resolver=None) -> None:
    """Verify membership and complete referenced bytes, never the preview alone."""
    if not isinstance(record, Mapping):
        raise ArticleContentAdapterError("missing_output_record")
    article_id = record.get("article_id")
    if not article_id or not record.get("requested_url"):
        raise ArticleContentAdapterError("missing_output_identity")
    if article_id not in inputs_index:
        raise ArticleContentAdapterError("extra_output_identity")
    if record["requested_url"] != inputs_index[article_id]["url"]:
        raise ArticleContentAdapterError("wrong_requested_url")
    if record.get("status") == "ok":
        ref, digest = record.get("content_ref"), record.get("content_hash")
        if not ref or not digest:
            raise ArticleContentAdapterError("content_ref_unresolvable")
        try:
            body = (content_resolver or resolve_content_ref)(ref, digest)
        except ArticleContentAdapterError:
            raise
        except (OSError, ValueError, KeyError) as exc:
            raise ArticleContentAdapterError("content_ref_unresolvable") from exc
        if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != digest:
            raise ArticleContentAdapterError("content_hash_mismatch")
        inline = record.get("content")
        if inline is not None and inline.encode("utf-8") != body:
            raise ArticleContentAdapterError("inline_content_mismatch")


def _verify_batch(records, articles, *, content_resolver=None):
    inputs_index = {a["article_id"]: a for a in articles}
    seen = set()
    for record in records:
        verify_record(record, inputs_index=inputs_index, content_resolver=content_resolver)
        identity = record["article_id"]
        if identity in seen:
            raise ArticleContentAdapterError("duplicate_output_identity")
        seen.add(identity)
    if seen != set(inputs_index):
        raise ArticleContentAdapterError("missing_output_identity")


def collect_evidence(
    unique_articles: Iterable[Mapping[str, Any]],
    *,
    providers: Sequence[ProviderCallable] = (),
) -> list[dict[str, Any]]:
    """Fetch once per canonical URL, preserve input order, reject invalid batches."""
    articles = _collect_unique_articles(unique_articles)
    providers = providers or _default_providers()
    resolver = getattr(providers[0], "content_resolver", None) if providers else None
    records = []
    for article in articles:
        record = fetch_article_content(article["article_id"], article["url"],
            providers=providers, snippet_input=article.get("search_snippet"))
        if isinstance(record, dict):
            record.setdefault("extra", {}).update({key: article[key] for key in
                ("source_item_id", "source_name", "source_id", "run_id", "item_status") if key in article})
        records.append(record)
    _verify_batch(records, articles, content_resolver=resolver)
    for record in records:
        record["record_hash"] = _record_digest(record)
    return records


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_evidence_record(record: Mapping[str, Any]) -> bytes:
    """Canonical JSON bytes for one record, excluding the ``record_hash``."""

    payload = {key: value for key, value in record.items() if key != "record_hash"}
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _record_digest(record: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(RECORD_DIGEST_VERSION.encode("ascii"))
    digest.update(b"\n")
    digest.update(_serialize_evidence_record(record))
    return digest.hexdigest()


def _artifact_digest(records: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        [record.get("record_hash") for record in records],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(ARTICLE_EVIDENCE_DIGEST_VERSION.encode("ascii"))
    digest.update(b"\n")
    digest.update(payload)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Artifact assembly + path helpers
# ---------------------------------------------------------------------------


def article_evidence_artifact_path(
    source_dir: str | Path, report_date: str
) -> Path:
    """Return the canonical on-disk path for the article-evidence.v1 artifact.

    Reuses the same ``source_dir`` layout as the Step 3 aggregate so callers
    do not need a separate configuration knob.
    """

    return Path(source_dir) / f"article-evidence.v1_{report_date}.json"


def build_article_evidence_artifact(
    unique_articles: Iterable[Mapping[str, Any]],
    *,
    providers: Sequence[ProviderCallable] = (),
    report_date: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build (in memory) the versioned article-evidence.v1 artifact."""

    dependency_status = check_dependencies()
    articles = _collect_unique_articles(unique_articles)
    records = collect_evidence(articles, providers=providers)
    resolver = getattr(providers[0], "content_resolver", None) if providers else None
    _verify_batch(records, articles, content_resolver=resolver)
    artifact: dict[str, Any] = {
        "schema_version": ARTICLE_EVIDENCE_SCHEMA_VERSION,
        "report_date": report_date,
        "generated_at": generated_at or "",
        "dependency_status": dependency_status,
        "record_count": len(records),
        "records": records,
        "artifact_digest": _artifact_digest(records),
    }
    return artifact


def write_article_evidence_artifact(
    source_dir: str | Path,
    report_date: str,
    artifact: Mapping[str, Any],
    *,
    path: str | Path | None = None,
) -> Path:
    """Atomically write one already-validated article-evidence.v1 artifact."""

    payload = json.dumps(dict(artifact), indent=2, ensure_ascii=False)
    destination = (
        Path(path)
        if path is not None
        else article_evidence_artifact_path(source_dir, report_date)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(destination)
    return destination


def run_article_evidence(
    unique_articles: Iterable[Mapping[str, Any]],
    *,
    providers: Sequence[ProviderCallable] = (),
    report_date: str,
    source_dir: str | Path,
    path: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """Build + write the artifact. Convenience wrapper used by the
    ``scripts/run_climate_monitor.py`` wiring layer."""

    artifact = build_article_evidence_artifact(
        unique_articles, providers=providers, report_date=report_date
    )
    written = write_article_evidence_artifact(
        source_dir, report_date, artifact, path=path
    )
    return artifact, written
