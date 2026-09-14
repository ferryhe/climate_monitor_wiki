"""Consume unique candidates through the public web_listening article reader.

Evidence is verified in memory before an atomic artifact write. Explicit
providers are the test/CI seam; discovery and acquisition policy remain upstream.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import stat
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ARTICLE_EVIDENCE_SCHEMA_VERSION = "article-evidence.v1"
ARTICLE_EVIDENCE_DIGEST_VERSION = "article-evidence-digest.v1"
RECORD_DIGEST_VERSION = "article-evidence-record-digest.v1"

UNAVAILABLE_REASON = (
    "web_listening_new governed URL retrieval is unavailable"
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
                    # Registry-backed evidence extends the compatible v1
                    # record with identity-bound acquisition semantics.
                    "content": {"type": ["string", "null"]},
                    "content_version_id": {"type": "string"},
                    "title": {"type": ["string", "null"]},
                    # Origin payloads predate the registry extension and have
                    # multiple producer-owned shapes. Keep those legacy shapes
                    # compatible while explicitly declaring the record field.
                    "origins": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "acquisition": {
                        "type": "object",
                        "required": [
                            "discovered_at", "publication_date",
                            "publication_date_evidence", "date_status",
                            "update_status",
                        ],
                        "properties": {
                            "discovered_at": {"type": "string"},
                            "publication_date": {"type": ["string", "null"]},
                            "publication_date_evidence": {
                                "type": ["object", "null"]
                            },
                            "date_status": {
                                "enum": [
                                    "eligible", "outside_window",
                                    "unknown_pending_review",
                                ]
                            },
                            "update_status": {
                                "enum": [
                                    "baseline", "content_changed", "unchanged",
                                    "failed",
                                ]
                            },
                        },
                        "additionalProperties": False,
                    },
                    "extra": {"type": "object"},
                },
            },
        },
        "acquisition_dispositions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "requested_url", "canonical_url", "discovered_at",
                    "publication_date", "publication_date_evidence",
                    "date_status", "update_status", "selection_status",
                    "material_status", "resolved_by_fetch_id",
                ],
                "properties": {
                    "requested_url": {"type": "string"},
                    "canonical_url": {"type": "string"},
                    "discovered_at": {"type": "string"},
                    "publication_date": {"type": ["string", "null"]},
                    "publication_date_evidence": {"type": ["object", "null"]},
                    "date_status": {
                        "enum": [
                            "eligible", "outside_window",
                            "unknown_pending_review",
                        ]
                    },
                    "update_status": {
                        "enum": [
                            "baseline", "content_changed", "unchanged", "failed",
                        ]
                    },
                    "selection_status": {"enum": ["selected", "unselected"]},
                    "resolved_by_fetch_id": {"type": ["string", "null"]},
                    "material_status": {
                        "enum": ["full_content", "snippet", "error"]
                    },
                },
                "additionalProperties": False,
            },
        },
        "artifact_digest": {"type": "string"},
        "reportability": {
            "type": "object",
            "required": [
                "schema_version", "outcome", "reportable", "full_coverage",
                "selected_record_count", "counts", "limitations",
                "acquisition_payload_sha256",
            ],
            "properties": {
                "schema_version": {"const": "climate-reportability.v1"},
                "outcome": {"enum": [
                    "completed", "completed_with_gaps",
                    "no_eligible_information", "systemic_failure",
                ]},
                "reportable": {"type": "boolean"},
                "full_coverage": {"type": "boolean"},
                "selected_record_count": {"type": "integer", "minimum": 0},
                "counts": {
                    "type": "object",
                    "required": [
                        "successful_sources", "source_gaps", "failed_searches",
                        "coverage_warnings", "unresolved_items", "blocked_tool_prechecks",
                    ],
                    "properties": {
                        key: {"type": "integer", "minimum": 0}
                        for key in (
                            "successful_sources", "source_gaps", "failed_searches",
                            "coverage_warnings", "unresolved_items", "blocked_tool_prechecks",
                        )
                    },
                    "additionalProperties": False,
                },
                "limitations": {"type": "array", "items": {"type": "string"}},
                "acquisition_payload_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
            },
            "additionalProperties": False,
        },
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


def check_dependencies() -> str:
    """Report whether the pinned public Runtime retrieval interfaces import."""
    try:
        from web_listening.request.model import Request, Scope  # noqa: F401
        from web_listening.runtime.retrieval import (  # noqa: F401
            RetrievalJobError,
            RetrievalRequestError,
        )
        from web_listening.runtime.service import RuntimeService  # noqa: F401
    except (ImportError, TypeError, ValueError):
        return "unavailable"
    else:
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


def resolve_content_ref(content_ref, content_hash, *, output_dir=None) -> bytes:
    """Read upstream evidence bytes from the portable local-file path.

    When ``output_dir`` is set, opens ``<output_dir>/<content_ref>`` with
    ``O_NOFOLLOW | O_RDONLY``, requires a regular file via ``fstat``, computes
    sha256, and compares. When ``output_dir`` is ``None`` (loopback
    providers), the caller is responsible for verifying from the inline
    ``content``; ``content_ref`` is treated as a portable opaque string
    and no disk read is attempted (this preserves the loopback test/CI
    seam without introducing a path-traversal surface).

    The adapter never calls upstream private ``_read_evidence`` /
    ``_output_path``; the in-adapter portable resolver is the only path.

    Path-safety: ``content_ref`` must be a non-empty relative path with
    no parent traversal (``..``) or absolute markers; the resolved path
    must remain inside ``output_dir``. Any violation rejects the record
    with ``content_ref_corrupt`` before opening a file descriptor.
    """
    if not content_ref or not content_hash:
        raise ArticleContentAdapterError("content_ref_unresolvable")
    if output_dir is None:
        # Loopback providers (tests) bypass the disk. The caller must have
        # already verified the inline content against the digest.
        raise ArticleContentAdapterError("content_ref_unresolvable")
    ref_name = str(content_ref)
    ref_path = Path(ref_name)
    if (
        not ref_name
        or ref_path.is_absolute()
        or ".." in ref_path.parts
        or any(sep in ref_name for sep in (chr(0),))
    ):
        raise ArticleContentAdapterError("content_ref_corrupt")
    output_root = Path(output_dir).resolve()
    target = (output_root / ref_name).resolve()
    if target != output_root and not target.is_relative_to(output_root):
        raise ArticleContentAdapterError("content_ref_corrupt")
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ArticleContentAdapterError("content_ref_corrupt") from exc
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise ArticleContentAdapterError("content_ref_corrupt")
        with os.fdopen(fd, "rb") as stream:
            body = stream.read()
    except OSError as exc:
        raise ArticleContentAdapterError("content_ref_corrupt") from exc
    if hashlib.sha256(body).hexdigest() != content_hash:
        raise ArticleContentAdapterError("content_hash_mismatch")
    return body


# ---------------------------------------------------------------------------
# Default public provider (AC-1)
# ---------------------------------------------------------------------------

def _runtime_service_type():
    from web_listening.runtime.service import RuntimeService

    return RuntimeService


_BOUNDED_RETRIEVAL_LIMITATIONS = (
    "HTML navigation is unsupported by bounded article retrieval.",
    "Cross-path redirects are unsupported by the exact-path request.",
    "Cross-origin redirects are unsupported; submit that reviewed URL separately.",
)

def _default_providers(
    *, data_root: str | Path | None = None, site_key: str | None = None,
    site_scope: Mapping[str, Any] | None = None, budget: Any | None = None,
) -> tuple[Callable[..., Any], ...]:
    """Resolve one URL through the pinned public persistent Runtime."""
    if check_dependencies() != "available":
        return ()
    default_root = Path(__file__).resolve().parent.parent / "monitoring/state/websites/.web-listening-runtime"
    root = Path(data_root or os.environ.get("CLIMATE_WEB_LISTENING_DATA_DIR") or default_root).resolve()

    def public_reader(article_id: str, url: str) -> Any:
        call_id = None
        try:
            from web_listening.request.model import Budgets, ContentType, Request, Scope
            from web_listening.request.scope import canonicalize_url
            from web_listening.runtime.retrieval import (
                RetrievalJobError,
                RetrievalRequestError,
            )

            reserved_units = 12
            attempt_limit = 4
            if budget is not None:
                remaining = budget.limits["fetch_attempts"] - budget.usage()["fetch_attempts"]
                reserved_units = min(reserved_units, remaining)
                attempt_limit = min(attempt_limit, remaining)
                if reserved_units < 1:
                    raise RuntimeError("fetch budget exhausted before URL retrieval")
                call_id = uuid.uuid4().hex
                budget.claim(
                    "web_listening_url_fetch", url, call_id=call_id, units=reserved_units,
                    retry_key=f"article:{url}",
                )
            seconds = 60 if budget is None else min(60, max(1, int(budget.remaining_seconds())))
            canonical = canonicalize_url(url)
            parsed = urlparse(canonical)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            path = parsed.path or "/"
            reviewed = copy.deepcopy(dict(site_scope or {}))
            allowed = reviewed.get("allowed_origins")
            if allowed is not None and origin not in allowed:
                raise ValueError("article origin is outside the reviewed source scope")
            effective_scope = {
                "seeds": [canonical], "allowed_origins": [origin],
                "include_paths": [path], "content_types": ["html", "file"],
            }
            request = Request(
                Scope((canonical,), (origin,), (path,), (ContentType.HTML, ContentType.FILE)),
                None, True,
                Budgets(reserved_units, 8 * 1024 * 1024, seconds, attempt_limit),
            )
            caller_id = "climate-monitor"
            runtime = _runtime_service_type().open(root)
            try:
                try:
                    retrieval = runtime.retrieve(request, caller_id=caller_id)
                except (RetrievalRequestError, RetrievalJobError) as exc:
                    job_id = exc.job_id
                else:
                    jobs = retrieval.get("jobs") if isinstance(retrieval, Mapping) else None
                    if (
                        not isinstance(jobs, list)
                        or len(jobs) != 1
                        or not isinstance(jobs[0], Mapping)
                        or not isinstance(jobs[0].get("job_id"), str)
                        or not jobs[0]["job_id"]
                    ):
                        raise RuntimeError(
                            "targeted URL retrieval did not return exactly one job identity"
                        )
                    job_id = jobs[0]["job_id"]
                completed = runtime.get_owned_job(job_id, caller_id)
                if completed.result is None:
                    raise RuntimeError("URL retrieval did not produce a terminal result")
                raw = _runtime_job_mapping(completed)
                payload = _runtime_job_payload(
                    runtime, caller_id, raw,
                    reviewed_scope=reviewed, effective_scope=effective_scope,
                )
            finally:
                runtime.close()
            if budget is not None and call_id is not None:
                result = raw["result"]
                actual = result["usage"]["requests"]
                compact = {
                    "status": result["status"], "failure_code": raw["failure_code"],
                    "usage": result["usage"], "errors": result["errors"],
                }
                budget.complete_tool(
                    call_id, compact, "ok" if payload.get("status") == "present" else "error",
                    actual_units=actual,
                )
            public_reader.site_key = site_key
            return payload
        except Exception as exc:
            if budget is not None and call_id is not None:
                event = budget.tool_event(call_id)
                if event is not None and not event.get("completed"):
                    # No measured result exists, so keep the conservative
                    # reservation spent rather than inventing a zero-request run.
                    budget.complete_tool(
                        call_id, {"error": f"{type(exc).__name__}: {exc}"}, "error",
                    )
            record = _unavailable_record(
                article_id=article_id, url=url,
                failure_reason=f"{type(exc).__name__}: {exc}").to_dict()
            return record

    public_reader.output_dir = None
    public_reader.site_key = None
    return (public_reader,)


def _runtime_job_mapping(job: Any) -> dict[str, Any]:
    """Serialize every public Job field without inventing a URL-fetch envelope."""
    payload: dict[str, Any] = {}
    for item in fields(job):
        value = getattr(job, item.name)
        if item.name == "result" and value is not None:
            payload[item.name] = value.to_dict()
        elif hasattr(value, "value"):
            payload[item.name] = value.value
        else:
            payload[item.name] = copy.deepcopy(value)
    return payload


def _runtime_job_payload(
    runtime: Any, caller_id: str, raw: Mapping[str, Any],
    *, reviewed_scope: Mapping[str, Any], effective_scope: Mapping[str, Any],
) -> dict[str, Any]:
    result = raw.get("result")
    if not isinstance(result, Mapping):
        return {
            "status": "failed", "final_url": None, "attempts": [],
            "failure_reason": raw.get("failure_code") or "runtime job has no result",
            "extraction_metadata": {
                "upstream_revision": "ac2343f89bc7939736d85f049ebe2beac571034a",
                "runtime_job": copy.deepcopy(dict(raw)),
                "reviewed_source_scope": copy.deepcopy(dict(reviewed_scope)),
                "effective_request_scope": copy.deepcopy(dict(effective_scope)),
                "coverage_limitations": list(_BOUNDED_RETRIEVAL_LIMITATIONS),
            },
        }
    attempts = [dict(attempt) for attempt in result.get("attempts", [])]
    manifest = result.get("manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    artifacts = result.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, list) else []
    source = next((item for item in artifacts if item.get("role") == "source"), None)
    derived = [item for item in artifacts if item.get("role") == "derived"]
    selected = derived[0] if derived else None
    if not isinstance(source, Mapping) or selected is None:
        errors = [item.get("code") for item in result.get("errors", []) if item.get("code")]
        return {
            "status": "failed", "final_url": manifest.get("final_url"),
            "attempts": attempts,
            "failure_reason": "; ".join(errors) or raw.get("failure_code")
            or "no cleaned content artifact",
            "extraction_metadata": {
                "upstream_revision": "ac2343f89bc7939736d85f049ebe2beac571034a",
                "runtime_job": copy.deepcopy(dict(raw)),
                "reviewed_source_scope": copy.deepcopy(dict(reviewed_scope)),
                "effective_request_scope": copy.deepcopy(dict(effective_scope)),
                "coverage_limitations": list(_BOUNDED_RETRIEVAL_LIMITATIONS),
            },
        }
    with runtime.open_owned_artifact(selected["artifact_id"], caller_id) as opened:
        content = opened.stream.read(opened.size_bytes + 1)
        if len(content) != opened.size_bytes or hashlib.sha256(content).hexdigest() != selected["sha256"]:
            raise ArticleContentAdapterError("content_hash_mismatch")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArticleContentAdapterError("cleaned_content_not_utf8") from exc
    if not text.strip():
        raise ArticleContentAdapterError("cleaned_content_empty")
    selected_attempt = next((
        attempt for attempt in reversed(attempts)
        if attempt.get("tool_id", "").startswith("acquisition.")
        and attempt.get("outcome") == "succeeded"
    ), {})
    return {
        "status": "present", "final_url": manifest.get("final_url"), "attempts": attempts,
        "selected_method": selected_attempt.get("tool_id"),
        "content_type": selected["mime_type"], "content_ref": selected["artifact_id"],
        "sha256": selected["sha256"], "content": text,
        "extraction_metadata": {
            "upstream_revision": "ac2343f89bc7939736d85f049ebe2beac571034a",
            "source_artifact": dict(source), "derived_artifact": dict(selected),
            "http_status": selected_attempt.get("http_status"),
            "runtime_job": copy.deepcopy(dict(raw)),
            "reviewed_source_scope": copy.deepcopy(dict(reviewed_scope)),
            "effective_request_scope": copy.deepcopy(dict(effective_scope)),
            "coverage_limitations": list(_BOUNDED_RETRIEVAL_LIMITATIONS),
        },
    }


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
    budget: Any | None = None,
    site_key: str | None = None,
    site_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Call provider[0] once; explicit providers override all dependency states.

    Provider exceptions become honest failed records. Invalid envelopes and
    identity mismatches reject the batch instead of becoming successful records.
    A managed budgeted read resolves verified ref-only content before its exact
    provider output directory is no longer available to the caller.
    """
    if budget is not None:
        if providers:
            raise ValueError("managed article reads require the public Runtime URL fetch")
        providers = _default_providers(
            site_key=site_key, site_scope=site_scope, budget=budget,
        )
    elif not providers:
        providers = _default_providers()
    if not url:
        return _unavailable_record(article_id=article_id, url=url, failure_reason="missing url").to_dict()
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
    record = map_tool_result_to_record(article_id, url, snippet_input, payload)
    if budget is not None and record.get("status") == "ok":
        output_dir = getattr(providers[0], "output_dir", None)
        verify_record(
            record,
            inputs_index={article_id: {"url": url}},
            output_dir=output_dir,
        )
        if record.get("content") is None:
            body = resolve_content_ref(
                record.get("content_ref"), record.get("content_hash"),
                output_dir=output_dir,
            )
            try:
                record["content"] = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ArticleContentAdapterError("content_ref_corrupt") from exc
    return record


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


def verify_record(record, *, inputs_index, content_resolver=None, output_dir=None) -> None:
    """Verify membership and complete referenced bytes, never the preview alone.

    Three legal verification paths:

    1. ``content_resolver`` supplied — the caller threads an explicit
       resolver (tests/CI). Bytes are read from the resolver.
    2. ``output_dir`` supplied (default public path passed it to
       upstream) — ``resolve_content_ref`` reads
       ``<output_dir>/<content_ref>`` on disk with O_NOFOLLOW + fstat +
       sha256 verify.
    3. Both ``None`` (loopback providers in test/CI mode) — the inline
       ``content`` field is the verification path; ``content_ref`` is
       treated as a portable opaque string. The inline bytes must
       round-trip through sha256 against ``content_hash``.
    """
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
        if not digest:
            raise ArticleContentAdapterError("content_ref_unresolvable")
        inline = record.get("content")
        if ref is None and inline is not None:
            # Upstream may retain a complete inline body if writing its artifact
            # failed. It still needs the same SHA and identity verification.
            body = inline.encode("utf-8") if isinstance(inline, str) else inline
            if not isinstance(body, bytes):
                raise ArticleContentAdapterError("content_ref_unresolvable")
        elif content_resolver is not None:
            # Path 1: explicit resolver (test/CI seam).
            if not ref:
                raise ArticleContentAdapterError("content_ref_unresolvable")
            try:
                body = content_resolver(ref, digest)
            except ArticleContentAdapterError:
                raise
            except (OSError, ValueError, KeyError) as exc:
                raise ArticleContentAdapterError("content_ref_unresolvable") from exc
        elif output_dir is not None:
            # Path 2: read <output_dir>/<content_ref> from disk.
            if not ref:
                raise ArticleContentAdapterError("content_ref_unresolvable")
            try:
                body = resolve_content_ref(ref, digest, output_dir=output_dir)
            except ArticleContentAdapterError:
                raise
            except (OSError, ValueError, KeyError) as exc:
                raise ArticleContentAdapterError("content_ref_unresolvable") from exc
        elif inline is not None:
            # Path 3: loopback / test/CI provider with inline body only.
            # ``content_ref`` is allowed to be None or any opaque string.
            body = inline.encode("utf-8") if isinstance(inline, str) else inline
            if not isinstance(body, bytes):
                raise ArticleContentAdapterError("content_ref_unresolvable")
        else:
            raise ArticleContentAdapterError("content_ref_unresolvable")
        if hashlib.sha256(body).hexdigest() != digest:
            raise ArticleContentAdapterError("content_hash_mismatch")
        # Cross-check the inline field against the resolved body when
        # both are present (defends against adapter/loopback drift).
        if inline is not None and (
            not isinstance(inline, str) or inline.encode("utf-8") != body
        ):
            raise ArticleContentAdapterError("inline_content_mismatch")


def _verify_batch(records, articles, *, content_resolver=None, output_dirs=None):
    inputs_index = {a["article_id"]: a for a in articles}
    seen = set()
    for record in records:
        # When the caller threads per-record output_dirs (default public path),
        # look up by article_id; otherwise fall back to a single output_dir.
        record_output_dir = None
        if output_dirs is not None:
            record_output_dir = output_dirs.get(record.get("article_id"))
        verify_record(
            record,
            inputs_index=inputs_index,
            content_resolver=content_resolver,
            output_dir=record_output_dir,
        )
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
    data_root: str | Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Fetch once per canonical URL, preserve input order, reject invalid batches.

    The provider may carry ``.output_dir`` (set by the default public
    callable after each upstream call) or ``.content_resolver`` (set by
    loopback test fixtures). When neither is present, ``resolve_content_ref``
    falls back to inline ``content`` verification only — no second artifact
    store is created in climate.

    Returns a ``(records, output_dirs)`` tuple so callers (notably
    ``build_article_evidence_artifact``) can re-run ``_verify_batch`` with
    the exact per-record ``output_dirs`` captured here (P2: the second
    verify must see the same on-disk path the first verify used; otherwise
    ``resolve_content_ref`` falls back to inline-only verification and
    silently loses the portability guarantee that ``content_ref`` was
    written to disk).
    """
    articles = _collect_unique_articles(unique_articles)
    # When ``data_root`` is supplied, the per-call ``output_dir`` parent is
    # anchored inside ``<data_root>/.cache/article_content/`` (P1). The
    # default providers here already build that anchor — we forward the
    # kwarg so this function stays the single source of truth.
    explicit_providers = providers
    resolver = getattr(providers[0], "content_resolver", None) if providers else None
    records = []
    output_dirs: dict[str, str] = {}
    for article in articles:
        providers = explicit_providers or _default_providers(
            data_root=data_root, site_key=article.get("site_key") or article.get("source_id"),
            site_scope=article.get("site_scope"),
        )
        # The default public reader updates ``provider.output_dir`` to the
        # per-call ``<data_root>/.cache/article_content/<uuid>`` (or the
        # upstream runtime data root when ``data_root`` is None)
        # directory before invoking upstream. Snapshot it AFTER the call so
        # verification reads from the exact same on-disk path that produced
        # ``content_ref``.
        record = fetch_article_content(article["article_id"], article["url"],
            providers=providers, snippet_input=article.get("search_snippet"))
        per_record_output_dir = (
            getattr(providers[0], "output_dir", None) if providers else None
        )
        if isinstance(record, dict):
            record.update({key: article[key] for key in
                           ("title", "title_basis", "origins", "display_pillar") if key in article})
            record.setdefault("extra", {}).update({key: article[key] for key in
                ("source_item_id", "source_name", "source_id", "run_id", "item_status") if key in article})
        if per_record_output_dir and isinstance(record, dict) and record.get("status") == "ok":
            aid = record.get("article_id")
            if isinstance(aid, str):
                output_dirs[aid] = per_record_output_dir
        records.append(record)
    _verify_batch(records, articles, content_resolver=resolver, output_dirs=output_dirs or None)
    for record in records:
        record["record_hash"] = _record_digest(record)
    return records, output_dirs


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
    data_root: str | Path | None = None,
    include_verified_content: bool = False,
    title_extractor: Callable[[str], tuple[str, str] | None] | None = None,
) -> dict[str, Any]:
    """Build (in memory) the versioned article-evidence.v1 artifact.

    When ``data_root`` is provided, the per-call ``output_dir`` for any
    default public provider is anchored inside ``<data_root>/.cache/``
    (P1). ``collect_evidence`` is responsible for the first
    ``_verify_batch``; this function re-runs ``_verify_batch`` with the
    same per-record ``output_dirs`` it captured, so any default-public
    record that was verified on disk in the first pass is verified the
    same way in the second pass (P2).

    With ``include_verified_content``, callers may plug in a pure
    ``title_extractor``. It reads only the verified HTML and records the title
    source before record/artifact hashing; discovery origins remain unchanged.
    """

    dependency_status = check_dependencies()
    articles = _collect_unique_articles(unique_articles)
    records, output_dirs = collect_evidence(
        articles, providers=providers, data_root=data_root
    )
    resolver = getattr(providers[0], "content_resolver", None) if providers else None
    _verify_batch(
        records,
        articles,
        content_resolver=resolver,
        output_dirs=output_dirs or None,
    )
    if include_verified_content:
        for record in records:
            if record.get("status") != "ok":
                continue
            output_dir = output_dirs.get(record["article_id"])
            if output_dir is not None and record.get("content_ref") is not None:
                body = resolve_content_ref(record["content_ref"], record["content_hash"],
                                           output_dir=output_dir)
                record["content"] = body.decode("utf-8")
            elif resolver is not None and record.get("content_ref") is not None:
                body = resolver(record["content_ref"], record["content_hash"])
                if hashlib.sha256(body).hexdigest() != record["content_hash"]:
                    raise ArticleContentAdapterError("content_ref_hash_mismatch")
                record["content"] = body.decode("utf-8")
            if title_extractor is not None and "html" in (record.get("content_type") or "").lower():
                title = title_extractor(record.get("content") or "")
                if title is not None:
                    extra = record.setdefault("extra", {})
                    extra["page_title_evidence"] = {
                        "source": title[1], "content_hash": record["content_hash"],
                        "discovery_title": record.get("title"),
                        "discovery_title_basis": record.get("title_basis"),
                    }
                    record["title"], record["title_basis"] = title[0], "page"
            record["record_hash"] = _record_digest(record)
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


def validate_retained_article_evidence(
    artifact: Mapping[str, Any], *, report_date: str, urls: Iterable[str]
) -> None:
    """Validate the prepared artifact for reuse at commit, without acquisition."""
    from jsonschema import Draft202012Validator, ValidationError
    from .dedupe import canonical_url

    try:
        Draft202012Validator(ARTICLE_EVIDENCE_SCHEMA).validate(artifact)
    except ValidationError as exc:
        raise ArticleContentAdapterError("retained evidence schema mismatch") from exc
    records = artifact["records"]
    expected = {canonical_url(url) for url in urls}
    actual = {canonical_url(record["requested_url"] or "") for record in records}
    if (artifact["report_date"] != report_date or artifact["record_count"] != len(records)
            or len(records) != len(expected) or actual != expected):
        raise ArticleContentAdapterError("retained evidence date or candidate mismatch")
    for record in records:
        if record["record_hash"] != _record_digest(record):
            raise ArticleContentAdapterError("retained evidence record hash mismatch")
        body = record.get("content")
        if isinstance(body, str) and body and hashlib.sha256(body.encode("utf-8")).hexdigest() != record["content_hash"]:
            raise ArticleContentAdapterError("retained evidence content hash mismatch")
    if artifact["artifact_digest"] != _artifact_digest(records):
        raise ArticleContentAdapterError("retained evidence artifact digest mismatch")


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
