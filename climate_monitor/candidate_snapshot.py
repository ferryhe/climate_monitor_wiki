"""Deterministic full CandidateItem state bound to combined candidate evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, fields
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from .candidate_aggregation import (
    serialize_combined_candidates,
    validate_combined_candidates,
)
from .dedupe import canonical_url
from .models import CandidateItem
from .semantic_bundle import article_identity


CANDIDATE_ITEM_SNAPSHOT_VERSION = "candidate-items-snapshot.v1"
_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "report_date",
        "report_sha256",
        "combined_sha256",
        "items",
    }
)
_ENTRY_FIELDS = frozenset({"article_id", "item"})
_ITEM_FIELDS = tuple(field.name for field in fields(CandidateItem))
_ITEM_FIELD_SET = frozenset(_ITEM_FIELDS)
_BOOL_FIELDS = frozenset({"climate_related", "actuarial_related"})
_LIST_FIELDS = frozenset({"topics", "categories", "keywords"})
_MAPPING_FIELDS = frozenset({"asset_metadata", "semantics"})
_NON_STRING_FIELDS = _BOOL_FIELDS | _LIST_FIELDS | _MAPPING_FIELDS | {
    "asset_bytes",
    "confidence",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CandidateSnapshotError(ValueError):
    """A candidate item snapshot is incomplete or inconsistent."""


def candidate_item_snapshot_path(
    directory: str | Path, report_date: date | str
) -> Path:
    value = (
        report_date.isoformat() if isinstance(report_date, date) else str(report_date)
    )
    return Path(directory) / f"candidate-items_{value}.json"


def _item_payload(item: CandidateItem) -> dict[str, Any]:
    payload = asdict(item)
    for name in _LIST_FIELDS:
        payload[name] = list(payload[name])
    return payload


def _validated_date(value: Any) -> str:
    if not isinstance(value, str):
        raise CandidateSnapshotError("candidate item snapshot report_date is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CandidateSnapshotError(
            "candidate item snapshot report_date is invalid"
        ) from exc
    if parsed.isoformat() != value:
        raise CandidateSnapshotError("candidate item snapshot report_date is invalid")
    return value


def _validate_json_value(value: Any, *, label: str) -> None:
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise CandidateSnapshotError(f"candidate item snapshot {label} is not finite")
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, label=label)
        return
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise CandidateSnapshotError(
                f"candidate item snapshot {label} keys must be strings"
            )
        for item in value.values():
            _validate_json_value(item, label=label)
        return
    raise CandidateSnapshotError(
        f"candidate item snapshot {label} must contain JSON values"
    )


def _candidate_item(payload: Any) -> CandidateItem:
    if not isinstance(payload, Mapping) or set(payload) != _ITEM_FIELD_SET:
        raise CandidateSnapshotError(
            "candidate item snapshot item has an invalid field set"
        )
    for name in _ITEM_FIELD_SET - _NON_STRING_FIELDS:
        if type(payload[name]) is not str:
            raise CandidateSnapshotError(
                f"candidate item snapshot item {name} must be a string"
            )
    if payload["lane"] not in {"website", "research", "document"}:
        raise CandidateSnapshotError("candidate item snapshot item lane is invalid")
    for name in _BOOL_FIELDS:
        if type(payload[name]) is not bool:
            raise CandidateSnapshotError(
                f"candidate item snapshot item {name} must be a boolean"
            )
    for name in _LIST_FIELDS:
        value = payload[name]
        if not isinstance(value, list) or any(type(item) is not str for item in value):
            raise CandidateSnapshotError(
                f"candidate item snapshot item {name} must be a string array"
            )
    asset_bytes = payload["asset_bytes"]
    if asset_bytes is not None and (
        type(asset_bytes) is not int or asset_bytes < 0
    ):
        raise CandidateSnapshotError(
            "candidate item snapshot item asset_bytes must be a non-negative integer or null"
        )
    confidence = payload["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
    ):
        raise CandidateSnapshotError(
            "candidate item snapshot item confidence must be a finite number"
        )
    for name in _MAPPING_FIELDS:
        value = payload[name]
        if value is not None and not isinstance(value, Mapping):
            raise CandidateSnapshotError(
                f"candidate item snapshot item {name} must be an object or null"
            )
        if value is not None:
            _validate_json_value(value, label=f"item {name}")
    if not canonical_url(payload["url"]).strip():
        raise CandidateSnapshotError(
            "candidate item snapshot item has no canonical URL identity"
        )
    values = dict(payload)
    for name in _LIST_FIELDS:
        values[name] = tuple(values[name])
    for name in _MAPPING_FIELDS:
        values[name] = dict(values[name]) if values[name] is not None else None
    return CandidateItem(**values)


def _validate_structure(value: Any) -> tuple[dict[str, Any], tuple[CandidateItem, ...]]:
    if not isinstance(value, Mapping) or set(value) != _ROOT_FIELDS:
        raise CandidateSnapshotError("candidate item snapshot has an invalid field set")
    if value["schema_version"] != CANDIDATE_ITEM_SNAPSHOT_VERSION:
        raise CandidateSnapshotError("candidate item snapshot version is unsupported")
    report_date = _validated_date(value["report_date"])
    report_sha256 = value["report_sha256"]
    if not isinstance(report_sha256, str) or _SHA256.fullmatch(report_sha256) is None:
        raise CandidateSnapshotError("candidate item snapshot report_sha256 is invalid")
    combined_sha256 = value["combined_sha256"]
    if (
        not isinstance(combined_sha256, str)
        or _SHA256.fullmatch(combined_sha256) is None
    ):
        raise CandidateSnapshotError("candidate item snapshot combined_sha256 is invalid")
    raw_items = value["items"]
    if not isinstance(raw_items, list):
        raise CandidateSnapshotError("candidate item snapshot items must be an array")
    entries: list[dict[str, Any]] = []
    items: list[CandidateItem] = []
    identities: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping) or set(raw) != _ENTRY_FIELDS:
            raise CandidateSnapshotError(
                "candidate item snapshot entry has an invalid field set"
            )
        item = _candidate_item(raw["item"])
        expected_identity = article_identity(item)
        if raw["article_id"] != expected_identity:
            raise CandidateSnapshotError(
                "candidate item snapshot article identity is inconsistent"
            )
        if expected_identity in identities:
            raise CandidateSnapshotError(
                "candidate item snapshot contains a duplicate article identity"
            )
        identities.add(expected_identity)
        entries.append({"article_id": expected_identity, "item": _item_payload(item)})
        items.append(item)
    return (
        {
            "schema_version": CANDIDATE_ITEM_SNAPSHOT_VERSION,
            "report_date": report_date,
            "report_sha256": report_sha256,
            "combined_sha256": combined_sha256,
            "items": entries,
        },
        tuple(items),
    )


def serialize_candidate_item_snapshot(value: Mapping[str, Any]) -> bytes:
    checked, _ = _validate_structure(value)
    try:
        content = json.dumps(
            checked,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CandidateSnapshotError(
            "candidate item snapshot is not canonical-JSON encodable"
        ) from exc
    return content + b"\n"


def _validated_combined_bytes(combined_bytes: bytes) -> dict[str, Any]:
    try:
        combined = validate_combined_candidates(
            json.loads(combined_bytes.decode("utf-8"))
        )
    except (UnicodeError, ValueError) as exc:
        raise CandidateSnapshotError(
            "candidate item snapshot combined evidence is invalid"
        ) from exc
    if serialize_combined_candidates(combined) != combined_bytes:
        raise CandidateSnapshotError(
            "candidate item snapshot combined evidence is not canonical"
        )
    return combined


def validate_candidate_item_snapshot(
    value: Mapping[str, Any],
    *,
    combined_bytes: bytes,
    report_date: date | str,
    report_sha256: str,
) -> tuple[CandidateItem, ...]:
    checked, items = _validate_structure(value)
    combined = _validated_combined_bytes(combined_bytes)
    expected_date = (
        report_date.isoformat() if isinstance(report_date, date) else str(report_date)
    )
    if (
        checked["report_date"] != expected_date
        or combined["report_date"] != expected_date
    ):
        raise CandidateSnapshotError(
            "candidate item snapshot report identity does not match"
        )
    if checked["combined_sha256"] != hashlib.sha256(combined_bytes).hexdigest():
        raise CandidateSnapshotError(
            "candidate item snapshot is not bound to the combined evidence"
        )
    if checked["report_sha256"] != report_sha256:
        raise CandidateSnapshotError(
            "candidate item snapshot is not bound to the canonical report"
        )
    expected_ids = [candidate["article_id"] for candidate in combined["items"]]
    actual_ids = [article_identity(item) for item in items]
    if actual_ids != expected_ids:
        raise CandidateSnapshotError(
            "candidate item snapshot does not cover combined candidates exactly"
        )
    return items


def build_candidate_item_snapshot(
    *,
    report_date: date | str,
    combined_bytes: bytes,
    items: Sequence[CandidateItem],
    report_sha256: str,
) -> tuple[dict[str, Any], bytes]:
    combined = _validated_combined_bytes(combined_bytes)
    expected_date = (
        report_date.isoformat() if isinstance(report_date, date) else str(report_date)
    )
    if combined["report_date"] != expected_date:
        raise CandidateSnapshotError(
            "candidate item snapshot report identity does not match"
        )
    by_identity: dict[str, CandidateItem] = {}
    for item in items:
        identity = article_identity(item)
        if identity in by_identity:
            raise CandidateSnapshotError(
                "candidate item snapshot contains a duplicate article identity"
            )
        by_identity[identity] = item
    expected_ids = [candidate["article_id"] for candidate in combined["items"]]
    if set(by_identity) != set(expected_ids):
        raise CandidateSnapshotError(
            "candidate item snapshot does not cover combined candidates exactly"
        )
    payload = {
        "schema_version": CANDIDATE_ITEM_SNAPSHOT_VERSION,
        "report_date": expected_date,
        "report_sha256": report_sha256,
        "combined_sha256": hashlib.sha256(combined_bytes).hexdigest(),
        "items": [
            {"article_id": identity, "item": _item_payload(by_identity[identity])}
            for identity in expected_ids
        ],
    }
    snapshot_bytes = serialize_candidate_item_snapshot(payload)
    validate_candidate_item_snapshot(
        payload,
        combined_bytes=combined_bytes,
        report_date=expected_date,
        report_sha256=report_sha256,
    )
    return payload, snapshot_bytes


def verify_candidate_item_snapshot(
    snapshot_path: str | Path,
    *,
    combined_path: str | Path,
    report_path: str | Path,
    report_date: date | str,
) -> tuple[CandidateItem, ...]:
    path = Path(snapshot_path)
    try:
        snapshot_bytes = path.read_bytes()
        payload = json.loads(snapshot_bytes.decode("utf-8"))
        if serialize_candidate_item_snapshot(payload) != snapshot_bytes:
            raise CandidateSnapshotError("candidate item snapshot is not canonical")
        return validate_candidate_item_snapshot(
            payload,
            combined_bytes=Path(combined_path).read_bytes(),
            report_date=report_date,
            report_sha256=hashlib.sha256(Path(report_path).read_bytes()).hexdigest(),
        )
    except CandidateSnapshotError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise CandidateSnapshotError("candidate item snapshot is invalid") from exc


__all__ = [
    "CANDIDATE_ITEM_SNAPSHOT_VERSION",
    "CandidateSnapshotError",
    "build_candidate_item_snapshot",
    "candidate_item_snapshot_path",
    "serialize_candidate_item_snapshot",
    "validate_candidate_item_snapshot",
    "verify_candidate_item_snapshot",
]
