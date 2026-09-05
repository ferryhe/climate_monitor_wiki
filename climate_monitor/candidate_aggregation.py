"""Shared URL-first aggregation for legacy and modern monitor entrypoints."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .article_candidate_contract import (
    ArticleCandidate,
    CandidateOrigin,
    CandidateContractError,
    adapt_article_changes,
    adapt_pillar_b,
    build_candidate,
    merge_candidates,
    validate_candidate,
)
from .dedupe import canonical_url
from .models import CandidateItem


COMBINED_CANDIDATES_SCHEMA_VERSION = "combined-candidates.v1"
COMBINED_CANDIDATES_DIGEST_VERSION = "combined-candidates-digest.v1"
_COUNT_FIELDS = frozenset(
    {
        "pillar_a_rows",
        "pillar_b_rows",
        "unique_urls",
        "cross_pillar_merges",
        "history_skips",
        "invalid_rows",
    }
)
_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "report_date",
        "counts",
        "items",
        "history_skips",
        "invalid_rows",
        "artifact_digest",
    }
)
_INVALID_FIELDS = frozenset({"pillar", "input_artifact", "row", "reasons"})


class CombinedCandidatesError(ValueError):
    """The combined candidate evidence is incomplete or inconsistent."""


@dataclass(frozen=True)
class CombinedCandidatesResult:
    candidates: tuple[ArticleCandidate, ...]
    history_skips: tuple[ArticleCandidate, ...]
    invalid_rows: tuple[dict[str, Any], ...]
    artifact: dict[str, Any]
    artifact_bytes: bytes


@dataclass(frozen=True)
class RuntimeCombination:
    combined: CombinedCandidatesResult
    items: tuple[CandidateItem, ...]
    invalid_notes: tuple[str, ...]


def combined_candidates_path(directory: str | Path, report_date: date | str) -> Path:
    value = report_date.isoformat() if isinstance(report_date, date) else str(report_date)
    return Path(directory) / f"combined-candidates_{value}.json"


def staged_combined_candidates_path(path: str | Path) -> Path:
    """Return the Step 3 candidate evidence staged for the next report commit."""

    destination = Path(path)
    return destination.with_name(destination.name + ".next")


def commit_combined_candidates(path: str | Path, payload: bytes) -> Path:
    """Atomically publish one already-validated canonical evidence artifact."""

    destination = Path(path)
    try:
        decoded = json.loads(payload.decode("utf-8"))
        if serialize_combined_candidates(decoded) != payload:
            raise CombinedCandidatesError("combined candidate evidence is not canonical")
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, CombinedCandidatesError):
            raise
        raise CombinedCandidatesError("combined candidate evidence is not valid JSON") from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        try:
            descriptor = os.open(
                destination.parent,
                getattr(os, "O_DIRECTORY", os.O_RDONLY),
            )
        except (OSError, AttributeError):
            descriptor = None
        if descriptor is not None:
            try:
                os.fsync(descriptor)
            except OSError:
                pass
            finally:
                os.close(descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CombinedCandidatesError("combined candidate artifact is not canonical-JSON encodable") from exc
    return encoded


def _digest(payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(COMBINED_CANDIDATES_DIGEST_VERSION.encode("ascii"))
    digest.update(b"\n")
    digest.update(_canonical_json(payload))
    return digest.hexdigest()


def _date(value: Any) -> str:
    if not isinstance(value, str):
        raise CombinedCandidatesError("report_date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CombinedCandidatesError("report_date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise CombinedCandidatesError("report_date must be YYYY-MM-DD")
    return value


def _candidate_payloads(values: Sequence[ArticleCandidate]) -> list[dict[str, Any]]:
    return [candidate.model_dump(mode="json") for candidate in values]


def _invalid_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    artifact = row["input_artifact"]
    return (
        row["pillar"],
        artifact["artifact_id"],
        artifact["sha256"],
        row["row"],
        tuple(row["reasons"]),
    )


def _validate_invalid_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise CombinedCandidatesError("invalid_rows must be an array")
    checked: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != _INVALID_FIELDS:
            raise CombinedCandidatesError("invalid row evidence has an invalid field set")
        pillar = row["pillar"]
        artifact = row["input_artifact"]
        pointer = row["row"]
        reasons = row["reasons"]
        if pillar not in {"A", "B"}:
            raise CombinedCandidatesError("invalid row evidence has an invalid pillar")
        if not isinstance(artifact, Mapping) or set(artifact) != {"artifact_id", "sha256"}:
            raise CombinedCandidatesError("invalid row evidence has an invalid artifact identity")
        if not isinstance(artifact["artifact_id"], str) or not artifact["artifact_id"]:
            raise CombinedCandidatesError("invalid row artifact_id must be non-empty")
        sha = artifact["sha256"]
        if not isinstance(sha, str) or len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise CombinedCandidatesError("invalid row artifact sha256 is invalid")
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            raise CombinedCandidatesError("invalid row pointer must be a JSON Pointer")
        if (
            not isinstance(reasons, list)
            or not reasons
            or any(not isinstance(reason, str) or not reason for reason in reasons)
            or reasons != sorted(set(reasons))
        ):
            raise CombinedCandidatesError("invalid row reasons must be sorted unique strings")
        identity = (artifact["artifact_id"], sha, pointer)
        if identity in identities:
            raise CombinedCandidatesError("invalid row evidence contains a duplicate artifact row")
        identities.add(identity)
        checked.append(
            {
                "pillar": pillar,
                "input_artifact": {
                    "artifact_id": artifact["artifact_id"],
                    "sha256": sha,
                },
                "row": pointer,
                "reasons": list(reasons),
            }
        )
    if checked != sorted(checked, key=_invalid_sort_key):
        raise CombinedCandidatesError("invalid row evidence is not in canonical order")
    return checked


def _derived_counts(
    items: Sequence[ArticleCandidate],
    history_skips: Sequence[ArticleCandidate],
    invalid_rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    all_candidates = [*items, *history_skips]
    origins = [origin for candidate in all_candidates for origin in candidate.origins]
    return {
        "pillar_a_rows": sum(origin.pillar == "A" for origin in origins)
        + sum(row["pillar"] == "A" for row in invalid_rows),
        "pillar_b_rows": sum(origin.pillar == "B" for origin in origins)
        + sum(row["pillar"] == "B" for row in invalid_rows),
        "unique_urls": len(all_candidates),
        "cross_pillar_merges": sum(
            {origin.pillar for origin in candidate.origins} == {"A", "B"}
            for candidate in all_candidates
        ),
        "history_skips": len(history_skips),
        "invalid_rows": len(invalid_rows),
    }


def validate_combined_candidates(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ROOT_FIELDS:
        raise CombinedCandidatesError("combined candidate artifact has an invalid field set")
    if value["schema_version"] != COMBINED_CANDIDATES_SCHEMA_VERSION:
        raise CombinedCandidatesError("combined candidate artifact has an unsupported version")
    report_date = _date(value["report_date"])
    counts = value["counts"]
    if not isinstance(counts, Mapping) or set(counts) != _COUNT_FIELDS:
        raise CombinedCandidatesError("combined candidate counts have an invalid field set")
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts.values()):
        raise CombinedCandidatesError("combined candidate counts must be non-negative integers")
    if not isinstance(value["items"], list) or not isinstance(value["history_skips"], list):
        raise CombinedCandidatesError("combined candidate items and history_skips must be arrays")
    try:
        items = [validate_candidate(candidate) for candidate in value["items"]]
        skipped = [validate_candidate(candidate) for candidate in value["history_skips"]]
    except CandidateContractError as exc:
        raise CombinedCandidatesError(f"combined candidate is invalid: {exc}") from exc
    for collection, label in ((items, "items"), (skipped, "history_skips")):
        if collection != sorted(collection, key=lambda candidate: candidate.canonical_url):
            raise CombinedCandidatesError(f"combined candidate {label} are not in canonical URL order")
    identities = [candidate.canonical_url for candidate in [*items, *skipped]]
    if len(set(identities)) != len(identities):
        raise CombinedCandidatesError("combined candidate URL identities are not unique")
    invalid_rows = _validate_invalid_rows(value["invalid_rows"])
    expected_counts = _derived_counts(items, skipped, invalid_rows)
    if dict(counts) != expected_counts:
        raise CombinedCandidatesError("combined candidate counts are not recomputable from evidence")
    canonical = {
        "schema_version": COMBINED_CANDIDATES_SCHEMA_VERSION,
        "report_date": report_date,
        "counts": expected_counts,
        "items": _candidate_payloads(items),
        "history_skips": _candidate_payloads(skipped),
        "invalid_rows": invalid_rows,
        "artifact_digest": value["artifact_digest"],
    }
    digest_payload = {key: canonical[key] for key in canonical if key != "artifact_digest"}
    if canonical["artifact_digest"] != _digest(digest_payload):
        raise CombinedCandidatesError("combined candidate artifact_digest is invalid")
    return canonical


def serialize_combined_candidates(value: Mapping[str, Any]) -> bytes:
    return _canonical_json(validate_combined_candidates(value)) + b"\n"


def _build_artifact(
    *,
    report_date: str,
    items: Sequence[ArticleCandidate],
    history_skips: Sequence[ArticleCandidate],
    invalid_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    sorted_items = sorted(items, key=lambda candidate: candidate.canonical_url)
    sorted_skips = sorted(history_skips, key=lambda candidate: candidate.canonical_url)
    sorted_invalid = sorted((dict(row) for row in invalid_rows), key=_invalid_sort_key)
    payload: dict[str, Any] = {
        "schema_version": COMBINED_CANDIDATES_SCHEMA_VERSION,
        "report_date": _date(report_date),
        "counts": _derived_counts(sorted_items, sorted_skips, sorted_invalid),
        "items": _candidate_payloads(sorted_items),
        "history_skips": _candidate_payloads(sorted_skips),
        "invalid_rows": sorted_invalid,
    }
    payload["artifact_digest"] = _digest(payload)
    return validate_combined_candidates(payload)


def combine_candidate_collections(
    *collections: Iterable[ArticleCandidate | Mapping[str, Any]],
    report_date: str,
    seen_urls: Iterable[str],
    invalid_rows: Sequence[Mapping[str, Any]] = (),
) -> CombinedCandidatesResult:
    merged = merge_candidates(*collections)
    history = {canonical_url(url) for url in seen_urls}
    candidates = tuple(candidate for candidate in merged if candidate.canonical_url not in history)
    skipped = tuple(candidate for candidate in merged if candidate.canonical_url in history)
    artifact = _build_artifact(
        report_date=report_date,
        items=candidates,
        history_skips=skipped,
        invalid_rows=invalid_rows,
    )
    return CombinedCandidatesResult(
        candidates=candidates,
        history_skips=skipped,
        invalid_rows=tuple(dict(row) for row in artifact["invalid_rows"]),
        artifact=artifact,
        artifact_bytes=serialize_combined_candidates(artifact),
    )


def combine_current_artifacts(
    pillar_a_payload: Mapping[str, Any],
    pillar_b_payload: Sequence[Mapping[str, Any]],
    *,
    report_date: str,
    pillar_a_artifact_id: str,
    pillar_a_artifact_sha256: str,
    pillar_b_artifact_id: str,
    pillar_b_artifact_sha256: str,
    pillar_b_discovered_at: str,
    seen_urls: Iterable[str],
    carry_forward_candidates: Sequence[ArticleCandidate | Mapping[str, Any]] = (),
) -> CombinedCandidatesResult:
    if pillar_a_payload.get("date") != report_date:
        raise CandidateContractError("article_changes date does not match report_date")
    pillar_a = adapt_article_changes(
        pillar_a_payload,
        artifact_id=pillar_a_artifact_id,
        artifact_sha256=pillar_a_artifact_sha256,
    )
    pillar_b = adapt_pillar_b(
        pillar_b_payload,
        artifact_id=pillar_b_artifact_id,
        artifact_sha256=pillar_b_artifact_sha256,
        discovered_at=pillar_b_discovered_at,
    )
    return combine_candidate_collections(
        carry_forward_candidates,
        pillar_a,
        pillar_b,
        report_date=report_date,
        seen_urls=seen_urls,
    )


def _runtime_origin(
    item: CandidateItem,
    *,
    pillar: str,
    artifact_id: str,
    artifact_sha256: str,
    row: str,
    discovered_at: str,
) -> dict[str, Any]:
    basis = "upstream_artifact" if pillar == "A" else "search_result"
    origin: dict[str, Any] = {
        "pillar": pillar,
        "source": item.source_name,
        "url": item.url,
        "input_artifact": {"artifact_id": artifact_id, "sha256": artifact_sha256},
        "row": row,
        "discovered_at": item.detected_at or discovered_at,
    }
    if item.title:
        origin.update(original_title=item.title, title_basis=basis)
    if item.summary:
        origin.update(original_summary=item.summary, summary_basis=basis)
    if item.evidence_snippet:
        origin.update(original_snippet=item.evidence_snippet, snippet_basis=basis)
    return origin


def _runtime_candidate(
    item: CandidateItem,
    *,
    pillar: str,
    artifact_id: str,
    artifact_sha256: str,
    row: str,
    discovered_at: str,
) -> ArticleCandidate:
    origin = _runtime_origin(
        item,
        pillar=pillar,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        row=row,
        discovered_at=discovered_at,
    )
    categories = list(item.categories) or None
    return build_candidate(
        url=item.url,
        origins=[origin],
        title=(item.title or None) if pillar == "B" else None,
        title_basis="search_result" if pillar == "B" and item.title else None,
        summary=(item.summary or None) if pillar == "B" else None,
        summary_basis="search_result" if pillar == "B" and item.summary else None,
        categories=categories,
        categories_basis="upstream_classification" if categories else None,
    )


def _invalid_runtime_row(
    *, pillar: str, artifact_id: str, artifact_sha256: str, row: str, reason: str
) -> dict[str, Any]:
    return {
        "pillar": pillar,
        "input_artifact": {"artifact_id": artifact_id, "sha256": artifact_sha256},
        "row": row,
        "reasons": [reason],
    }


def _overlay_merged_candidate(
    candidate: ArticleCandidate, selected: CandidateItem
) -> CandidateItem:
    display_origin = next(
        origin
        for origin in candidate.origins
        if origin.pillar == candidate.display_pillar
    )
    fallback_title = display_origin.original_title or selected.title
    return replace(
        selected,
        url=candidate.url,
        title=candidate.title or fallback_title,
        summary=candidate.summary or selected.summary,
        source_name=display_origin.source,
        categories=tuple(candidate.categories or selected.categories),
    )


def combine_runtime_items(
    pillar_a_items: Sequence[CandidateItem],
    pillar_b_items: Sequence[CandidateItem],
    *,
    report_date: str,
    pillar_a_artifact_id: str,
    pillar_a_artifact_sha256: str,
    pillar_b_artifact_id: str,
    pillar_b_artifact_sha256: str,
    discovered_at: str,
    seen_urls: Iterable[str],
    carry_forward_candidates: Sequence[ArticleCandidate | Mapping[str, Any]] = (),
    carry_forward_items: Sequence[CandidateItem] = (),
) -> RuntimeCombination:
    candidates: list[ArticleCandidate] = []
    invalid_rows: list[dict[str, Any]] = []
    invalid_notes: list[str] = []
    raw_by_canonical: dict[str, list[tuple[CandidateOrigin, CandidateItem]]] = {}
    carried_by_canonical: dict[str, CandidateItem] = {}
    carried_origin_by_canonical: dict[str, CandidateOrigin] = {}
    order_by_canonical: dict[str, tuple[int, int, int]] = {}
    for index, item in enumerate(carry_forward_items):
        canonical = canonical_url(item.url)
        if canonical and canonical not in carried_by_canonical:
            carried_by_canonical[canonical] = item
            order_by_canonical[canonical] = (0, 0, index)
    carried_candidates = merge_candidates(carry_forward_candidates)
    for index, candidate in enumerate(carried_candidates):
        carried_origin_by_canonical[candidate.canonical_url] = next(
            origin
            for origin in candidate.origins
            if origin.pillar == candidate.display_pillar
        )
        if candidate.canonical_url not in carried_by_canonical:
            carried_by_canonical[candidate.canonical_url] = (
                items_from_current_candidates((candidate,))[0]
            )
            order_by_canonical[candidate.canonical_url] = (0, 1, index)
    for pillar, items, artifact_id, artifact_sha256 in (
        ("A", pillar_a_items, pillar_a_artifact_id, pillar_a_artifact_sha256),
        ("B", pillar_b_items, pillar_b_artifact_id, pillar_b_artifact_sha256),
    ):
        for index, item in enumerate(items):
            row = f"/{index}"
            try:
                candidate = _runtime_candidate(
                    item,
                    pillar=pillar,
                    artifact_id=artifact_id,
                    artifact_sha256=artifact_sha256,
                    row=row,
                    discovered_at=discovered_at,
                )
            except (CandidateContractError, ValueError):
                invalid_rows.append(
                    _invalid_runtime_row(
                        pillar=pillar,
                        artifact_id=artifact_id,
                        artifact_sha256=artifact_sha256,
                        row=row,
                        reason="invalid_candidate",
                    )
                )
                reason = (
                    "no canonical URL identity"
                    if not canonical_url(item.url).strip()
                    else "invalid article candidate"
                )
                invalid_notes.append(
                    f"dropped non-semantic article ({reason}): "
                    f"{item.title or '(untitled)'} [{item.url}]"
                )
                continue
            candidates.append(candidate)
            raw_by_canonical.setdefault(candidate.canonical_url, []).append(
                (candidate.origins[0], item)
            )
            order_by_canonical.setdefault(
                candidate.canonical_url,
                (1, 0 if pillar == "A" else 1, index),
            )

    combined = combine_candidate_collections(
        carry_forward_candidates,
        candidates,
        report_date=report_date,
        seen_urls=seen_urls,
        invalid_rows=invalid_rows,
    )
    output_items: list[CandidateItem] = []
    for candidate in combined.candidates:
        display_origin = next(
            origin for origin in candidate.origins if origin.pillar == candidate.display_pillar
        )
        raw_values = raw_by_canonical.get(candidate.canonical_url, [])
        display_values = [
            pair for pair in raw_values if pair[0] == display_origin
        ]
        carried = carried_by_canonical.get(candidate.canonical_url)
        if display_values:
            _, selected = display_values[0]
        elif (
            carried is not None
            and carried_origin_by_canonical.get(candidate.canonical_url)
            == display_origin
        ):
            selected = carried
        else:
            selected = items_from_current_candidates((candidate,))[0]
            order_by_canonical.setdefault(
                candidate.canonical_url,
                (0, 1, len(order_by_canonical)),
            )
        output_items.append(_overlay_merged_candidate(candidate, selected))
    output_items.sort(key=lambda item: order_by_canonical[canonical_url(item.url)])
    return RuntimeCombination(
        combined=combined,
        items=tuple(output_items),
        invalid_notes=tuple(invalid_notes),
    )


def items_from_current_candidates(candidates: Sequence[ArticleCandidate]) -> tuple[CandidateItem, ...]:
    items: list[CandidateItem] = []
    for candidate in candidates:
        display_origin = next(
            origin for origin in candidate.origins if origin.pillar == candidate.display_pillar
        )
        title = candidate.title or display_origin.original_title or candidate.url
        items.append(
            CandidateItem(
                title=title,
                url=candidate.url,
                summary=candidate.summary or display_origin.original_summary or "",
                source_name=display_origin.source,
                lane="website" if candidate.display_pillar == "A" else "research",
                detected_at=display_origin.discovered_at,
                categories=tuple(candidate.categories or ()),
            )
        )
    return tuple(items)


def items_from_merged_candidates_with_carry(
    candidates: Sequence[ArticleCandidate],
    *,
    carry_forward_candidates: Sequence[ArticleCandidate | Mapping[str, Any]],
    carry_forward_items: Sequence[CandidateItem],
) -> tuple[CandidateItem, ...]:
    """Use a full carried item only when it owns the merged display origin."""

    carried_items: dict[str, CandidateItem] = {}
    for item in carry_forward_items:
        canonical = canonical_url(item.url)
        if canonical and canonical not in carried_items:
            carried_items[canonical] = item
    carried_origins = {
        candidate.canonical_url: next(
            origin
            for origin in candidate.origins
            if origin.pillar == candidate.display_pillar
        )
        for candidate in merge_candidates(carry_forward_candidates)
    }
    output: list[CandidateItem] = []
    for candidate in merge_candidates(candidates):
        display_origin = next(
            origin
            for origin in candidate.origins
            if origin.pillar == candidate.display_pillar
        )
        carried = carried_items.get(candidate.canonical_url)
        if (
            carried is not None
            and carried_origins.get(candidate.canonical_url) == display_origin
        ):
            selected = carried
        else:
            selected = items_from_current_candidates((candidate,))[0]
        output.append(_overlay_merged_candidate(candidate, selected))
    return tuple(output)


__all__ = [
    "COMBINED_CANDIDATES_DIGEST_VERSION",
    "COMBINED_CANDIDATES_SCHEMA_VERSION",
    "CombinedCandidatesError",
    "CombinedCandidatesResult",
    "RuntimeCombination",
    "combine_candidate_collections",
    "combine_current_artifacts",
    "combine_runtime_items",
    "commit_combined_candidates",
    "combined_candidates_path",
    "items_from_current_candidates",
    "items_from_merged_candidates_with_carry",
    "serialize_combined_candidates",
    "staged_combined_candidates_path",
    "validate_combined_candidates",
]
