"""Thin article-content adapter for Issue #92 AC-5..7.

This module is a *thin* content-adapter layer: it consumes the post-#91
unique-candidate set, calls the (currently unavailable) ferryhe/web_listening#70
public contract for one URL → one record, and emits exactly N evidence records
per N unique inputs. It NEVER fabricates content: when the dependency is not
satisfied every record is URL-only with explicit ``status="unavailable"`` and
a populated ``failure_reason``.

The adapter's only network touchpoint is the upstream
``web_listening.contracts.article_content`` provider; the production state of
that contract today is "not yet available", so :func:`check_dependencies`
returns ``"unavailable"`` until web_listening#70 lands. The wired
``scripts/run_climate_monitor.py`` entrypoint always materialises a versioned
``article-evidence.v1`` artifact regardless of dependency status so issue
#93 can consume a stable shape.
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
        return payload


# ---------------------------------------------------------------------------
# Dependency probe
# ---------------------------------------------------------------------------


def _import_web_listening_contract() -> Any | None:
    """Return the ``web_listening.contracts.article_content`` module if
    importable, otherwise ``None``. This is the only place that probes the
    upstream contract; tests can monkeypatch it to force a specific status.
    """

    try:
        return importlib.import_module("web_listening.contracts.article_content")
    except Exception:
        return None


def check_dependencies() -> str:
    """Return one of ``"available" | "partial" | "unavailable"``.

    * ``"available"``  — ``web_listening.contracts.article_content`` importable
      AND exposes a non-empty ``PROVIDERS`` iterable.
    * ``"partial"``    — contract module importable but providers not declared.
    * ``"unavailable"`` — contract module not importable.
    """

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


ProviderCallable = Callable[[str, str], Mapping[str, Any]]


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
        summary_basis=None,
        failure_reason=failure_reason,
    )


def _record_from_provider_payload(
    *, article_id: str, url: str, payload: Mapping[str, Any]
) -> ArticleEvidenceRecord:
    """Translate one provider payload into the canonical record shape."""

    attempts_raw = payload.get("attempts") or []
    attempts: tuple[dict[str, Any], ...] = tuple(
        dict(item) if isinstance(item, Mapping) else {"raw": item}
        for item in attempts_raw
    )
    return ArticleEvidenceRecord(
        article_id=article_id,
        requested_url=url,
        final_url=str(payload.get("final_url") or url),
        status=str(payload.get("status") or "ok"),
        attempts=attempts,
        selected_method=payload.get("selected_method"),
        content_type=payload.get("content_type"),
        content_ref=payload.get("content_ref"),
        content=payload.get("content"),
        content_hash=payload.get("content_hash"),
        summary_basis=payload.get("summary_basis"),
        failure_reason=payload.get("failure_reason"),
    )


def fetch_article_content(
    article_id: str,
    url: str,
    *,
    providers: Sequence[ProviderCallable] = (),
) -> dict[str, Any]:
    """Fetch one URL → one evidence record.

    ``providers`` is an ordered list of zero-arg-prepared callables that
    accept ``(article_id, url)`` and return a mapping. The first provider
    is consulted first; if it raises, the record is marked ``"failed"``
    with the exception class name in ``failure_reason``. When ``providers``
    is empty OR :func:`check_dependencies` returns ``"unavailable"``, the
    call returns an URL-only ``"unavailable"`` record and never invokes
    anything.
    """

    if not url:
        record = _unavailable_record(
            article_id=article_id,
            url=url,
            failure_reason="missing url",
        )
        return record.to_dict()

    if check_dependencies() in ("unavailable", "partial") or not providers:
        return _unavailable_record(article_id=article_id, url=url).to_dict()

    provider = providers[0]
    try:
        payload = provider(article_id, url)
    except BaseException as exc:  # noqa: BLE001 - honest error surfacing
        record = ArticleEvidenceRecord(
            article_id=article_id,
            requested_url=url,
            final_url=None,
            status="failed",
            attempts=(),
            selected_method=None,
            content_type=None,
            content_ref=None,
            content=None,
            content_hash=None,
            summary_basis=None,
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
        return record.to_dict()

    record = _record_from_provider_payload(article_id=article_id, url=url, payload=payload)
    return record.to_dict()


# ---------------------------------------------------------------------------
# Multi-input collector
# ---------------------------------------------------------------------------


def _collect_unique_articles(
    inputs: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate ``inputs`` by ``article_id`` (then ``url``) preserving
    the *first* occurrence in input order. A bare URL is allowed in lieu of
    an ``article_id``; the canonical URL is used as the identity in that
    case.
    """

    from climate_monitor.dedupe import canonical_url

    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for raw in inputs:
        article_id = str(raw.get("article_id") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not article_id:
            if not url:
                # Skip silent drops: an entry with no article_id and no url
                # has no identity to operate on. We retain it under an
                # explicit sentinel so callers can see the miss.
                article_id = "missing-identity"
            else:
                article_id = canonical_url(url)
        if not url:
            url = ""
        identity = (article_id, canonical_url(url)) if url else (article_id, "")
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(
            {
                "article_id": article_id,
                "url": url,
                "title": str(raw.get("title") or ""),
            }
        )
    return ordered


def collect_evidence(
    unique_articles: Iterable[Mapping[str, Any]],
    *,
    providers: Sequence[ProviderCallable] = (),
) -> list[dict[str, Any]]:
    """Emit exactly one record per unique ``(article_id, url)`` input.

    * Inputs are deduplicated by ``(article_id, url)`` (first wins).
    * Each distinct ``article_id`` triggers exactly one upstream provider call.
    * Output order matches input order.
    * ``fetch_article_content`` is invoked once per unique identity and the
      returned record is materialised with a deterministic ``record_hash``.
    """

    articles = _collect_unique_articles(unique_articles)
    records: list[dict[str, Any]] = []
    for article in articles:
        record = fetch_article_content(
            article["article_id"], article["url"], providers=providers
        )
        record["record_hash"] = _record_digest(record)
        records.append(record)
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
    records = collect_evidence(unique_articles, providers=providers)
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
