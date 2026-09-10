"""Durable, report-independent acquisition batches for Registry schema v8.

The ingestion boundary consumes already-produced public article evidence and
native Agent search records.  It does not fetch, search, or infer publication
dates.  A complete transaction is committed before authoring; report handoff
then reads the exact content-version identities attached to that batch.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlparse

from climate_monitor.article_content_adapter import _artifact_digest, _record_digest
from climate_monitor.dedupe import canonical_url

from .audit import _stable_id
from .classification import classify_document
from .contract import validate_registry_contract
from .errors import RegistryInputError

BATCH_SCHEMA_VERSION = "pre-report-acquisition-batch.v1"
ACQUISITION_WRITER_SCHEMA_VERSION = 9
_SHA256_LENGTH = 64


class AcquisitionIncompleteError(ValueError):
    """The frozen batch still contains failures that authoring cannot hide."""


@dataclass(frozen=True)
class PublicationDatePolicy:
    """Publication-date policy resolved and frozen once at acquisition startup."""

    mode: str
    anchor_date: date
    frozen_at: str
    start: date | None = None
    end: date | None = None
    days: int | None = None

    @classmethod
    def resolve(
        cls,
        config: Mapping[str, Any] | None,
        *,
        anchor_date: date,
        frozen_at: str,
    ) -> "PublicationDatePolicy":
        _timestamp(frozen_at, "frozen_at")
        raw = dict(config or {"mode": "unlimited"})
        mode = raw.get("mode", "unlimited")
        if mode == "unlimited":
            if set(raw) - {"mode"}:
                raise ValueError("unlimited date policy has unexpected fields")
            return cls(mode=mode, anchor_date=anchor_date, frozen_at=frozen_at)
        if mode == "recent":
            if set(raw) != {"mode", "days"} or type(raw.get("days")) is not int or raw["days"] < 1:
                raise ValueError("recent date policy requires a positive integer days")
            start = anchor_date - timedelta(days=raw["days"] - 1)
            return cls(mode=mode, anchor_date=anchor_date, frozen_at=frozen_at,
                       start=start, end=anchor_date, days=raw["days"])
        if mode == "custom":
            if set(raw) != {"mode", "start", "end"}:
                raise ValueError("custom date policy requires start and end")
            start = _exact_date(raw["start"], "date policy start")
            end = _exact_date(raw["end"], "date policy end")
            if start > end:
                raise ValueError("custom date policy start must not follow end")
            return cls(mode=mode, anchor_date=anchor_date, frozen_at=frozen_at,
                       start=start, end=end)
        raise ValueError("date policy mode must be unlimited, recent, or custom")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PublicationDatePolicy":
        required = {"mode", "anchor_date", "frozen_at", "start", "end", "days"}
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("resolved date policy fields are invalid")
        anchor = _exact_date(value["anchor_date"], "date policy anchor_date")
        raw: dict[str, Any] = {"mode": value["mode"]}
        if value["mode"] == "recent":
            raw["days"] = value["days"]
        elif value["mode"] == "custom":
            raw.update(start=value["start"], end=value["end"])
        policy = cls.resolve(raw, anchor_date=anchor, frozen_at=str(value["frozen_at"]))
        if policy.to_dict() != dict(value):
            raise ValueError("resolved date policy values are inconsistent")
        return policy

    @property
    def search_time_filter(self) -> dict[str, str] | None:
        if self.mode == "unlimited":
            return None
        assert self.start is not None and self.end is not None
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

    def selects(self, published: date | None) -> bool:
        if self.mode == "unlimited":
            return True
        return published is not None and self.start <= published <= self.end  # type: ignore[operator]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "anchor_date": self.anchor_date.isoformat(),
            "frozen_at": self.frozen_at,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "days": self.days,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or value != value.strip() or (not allow_empty and not value):
        raise ValueError(f"{field} must be a trimmed string" + ("" if allow_empty else " and non-empty"))
    return value


def _timestamp(value: Any, field: str) -> str:
    value = _text(value, field)
    try:
        from datetime import datetime
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def _exact_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an exact date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an exact date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an exact date")
    return parsed


def _sha(value: Any, field: str) -> str:
    value = _text(value, field)
    if len(value) != _SHA256_LENGTH or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _optional_managed_snapshot(ref: Any, digest: Any) -> tuple[str | None, str | None]:
    if ref is None and digest is None:
        return None, None
    if ref is None or digest is None:
        raise ValueError("raw snapshot reference/hash must be paired")
    ref = _text(ref, "raw_snapshot_ref")
    path = PurePosixPath(ref)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("raw_snapshot_ref must be a managed relative path")
    return ref, _sha(digest, "raw_snapshot_sha256")


def _optional_managed_ref(ref: Any, field: str) -> str | None:
    if ref is None:
        return None
    ref = _text(ref, field)
    path = PurePosixPath(ref)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be a managed relative path")
    return ref


def _open_database(
    database: str | Path, *, read_only: bool = False, acquisition_writer: bool = False
) -> sqlite3.Connection:
    path = _canonical_database_path(database)
    connection = sqlite3.connect(f"{path.as_uri()}?mode={'ro' if read_only else 'rw'}", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    version = validate_registry_contract(connection)
    if acquisition_writer and version != ACQUISITION_WRITER_SCHEMA_VERSION:
        connection.close()
        raise RegistryInputError(
            f"acquisition writes require registry schema {ACQUISITION_WRITER_SCHEMA_VERSION}; "
            f"found schema {version}; migrate the registry"
        )
    return connection


def _canonical_database_path(database: str | Path) -> Path:
    path = Path(database)
    try:
        resolved = path.resolve(strict=True)
        metadata = os.lstat(path)
    except OSError as exc:
        raise RegistryInputError(f"registry database does not exist: {path}") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (not path.is_absolute() or path != resolved or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse)):
        raise RegistryInputError("registry database must be an absolute canonical regular file")
    return path


def _validate_searches(payload: Mapping[str, Any]) -> tuple[str, str | None, list[dict[str, Any]]]:
    decision = payload.get("search_decision")
    if not isinstance(decision, Mapping) or set(decision) != {"status", "reason"}:
        raise ValueError("search_decision fields are invalid")
    status = decision.get("status")
    reason = decision.get("reason")
    searches = payload.get("searches")
    if not isinstance(searches, list):
        raise ValueError("searches must be a list")
    if status == "no_search":
        reason = _text(reason, "no-search reason")
        if searches:
            raise ValueError("no_search decision cannot contain search attempts")
    elif status == "attempted":
        if reason is not None:
            raise ValueError("attempted search reason must be null")
        if not searches:
            raise ValueError("attempted search requires at least one true attempt")
    else:
        raise ValueError("search_decision status must be attempted or no_search")
    checked: list[dict[str, Any]] = []
    for raw in searches:
        if not isinstance(raw, Mapping) or set(raw) != {
            "search_ref", "query", "engine", "status", "attempted_at", "result_refs", "budget", "error"
        }:
            raise ValueError("search attempt fields are invalid")
        item = dict(raw)
        for field in ("search_ref", "query", "engine"):
            item[field] = _text(item[field], f"search {field}")
        if any(prior["search_ref"] == item["search_ref"] for prior in checked):
            raise ValueError("search_ref must be unique within a batch")
        item["attempted_at"] = _timestamp(item["attempted_at"], "search attempted_at")
        if item["status"] not in {"success", "failed"}:
            raise ValueError("search status must be success or failed")
        refs = item["result_refs"]
        if (not isinstance(refs, list)
                or any(not isinstance(ref, str) or not ref or ref != ref.strip() for ref in refs)
                or len(refs) != len(set(refs))):
            raise ValueError("search result_refs must be unique trimmed non-empty strings")
        budget = item["budget"]
        if not isinstance(budget, Mapping) or not budget:
            raise ValueError("search budget must be a non-empty object")
        for key, value in budget.items():
            _text(key, "search budget key")
            if type(value) is not int or value < 0:
                raise ValueError("search budget values must be non-negative integers")
        if item["status"] == "failed":
            item["error"] = _text(item["error"], "failed search error")
        elif item["error"] is not None:
            raise ValueError("successful search cannot have an error")
        checked.append(item)
    return status, reason, checked


def _bind_discovery_provenance(
    items: list[dict[str, Any]], searches: list[dict[str, Any]], batch_id: str
) -> None:
    successful = {
        search["search_ref"]: (set(search["result_refs"]), ordinal)
        for ordinal, search in enumerate(searches, 1) if search["status"] == "success"
    }
    for item in items:
        search_ref = item.get("discovery_search_ref")
        if item["discovery_kind"] == "site":
            if search_ref is not None:
                raise ValueError("site discovery must not identify a search attempt")
            item["discovery_search_id"] = None
            continue
        match = successful.get(search_ref) if isinstance(search_ref, str) else None
        if match is None or item["discovery_ref"] not in match[0]:
            raise ValueError("search discovery must link to a result from its successful search attempt")
        item["discovery_search_id"] = _stable_id("search", f"{batch_id}\n{match[1]}")


def _is_successful_fetch(item: Mapping[str, Any]) -> bool:
    evidence = item["evidence"]
    return evidence["status"] == "ok" and evidence["classification"] == "full_content"


def _is_successful_item(item: Mapping[str, Any]) -> bool:
    return item["processing_status"] == "complete" and _is_successful_fetch(item)


def _merge_same_batch_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge successful duplicate discoveries without erasing fetch outcomes."""
    successful_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    observation_groups: list[list[dict[str, Any]]] = []
    for item in items:
        if _is_successful_item(item):
            key = (item["canonical_url"], item["evidence"]["content_hash"])
            successful_groups.setdefault(key, []).append(item)
        else:
            # Every non-success item is a durable fetch/processing observation,
            # even when its URL and empty content hash match another failure.
            observation_groups.append([item])
    observation_groups.extend(successful_groups[key] for key in sorted(successful_groups))

    merged: list[dict[str, Any]] = []
    for grouped in observation_groups:
        selected_methods = {
            item["evidence"]["selected_method"]
            for item in grouped if _is_successful_item(item)
        }
        if len(selected_methods) > 1:
            raise ValueError("same content body has conflicting selected methods")
        grouped.sort(key=lambda item: (
            item["discovery_kind"], item["discovery_ref"], item["source"], item["url"],
            item.get("discovery_search_ref") or "", _canonical_json(item["evidence"]),
        ))
        primary = dict(grouped[0])
        origins = [{
            "discovery_kind": item["discovery_kind"],
            "discovery_ref": item["discovery_ref"],
            "search_ref": item.get("discovery_search_ref"),
            "source": item["source"], "url": item["url"],
            "discovered_at": item["discovered_at"],
            "disposition": "successful" if _is_successful_item(item) else "unresolved",
        } for item in grouped]
        primary["origins"] = origins
        primary["selected"] = any(bool(item.get("selected")) for item in grouped)
        primary["effective_selected"] = any(item["effective_selected"] for item in grouped)
        attempts = {
            _canonical_json(dict(attempt)): dict(attempt)
            for item in grouped for attempt in item["evidence"]["attempts"]
        }
        primary["evidence"] = dict(primary["evidence"])
        primary["evidence"]["attempts"] = [attempts[key] for key in sorted(attempts)]
        merged.append(primary)
    merged.sort(key=lambda item: (
        item["canonical_url"], 0 if _is_successful_item(item) else 1,
        item["evidence"].get("content_hash") or "", item["discovery_kind"],
        item["discovery_ref"], item["source"], item["url"],
        _canonical_json(item["evidence"]),
    ))
    by_url: dict[str, list[dict[str, Any]]] = {}
    for item in merged:
        by_url.setdefault(item["canonical_url"], []).append(item)
    reconciled: list[dict[str, Any]] = []
    for canonical in sorted(by_url):
        observations = by_url[canonical]
        successes = [item for item in observations if _is_successful_item(item)]
        fetch_failures = [item for item in observations if not _is_successful_fetch(item)]
        processing_unresolved = [
            item for item in observations
            if _is_successful_fetch(item) and not _is_successful_item(item)
        ]
        if not successes or not fetch_failures:
            reconciled.extend(observations)
            continue
        selected = [item for item in successes if item["effective_selected"]]
        target = min(selected or successes, key=lambda item: (
            item["evidence"].get("content_hash") or "", item["discovery_kind"],
            item["discovery_ref"], item["url"],
        ))
        other_successes = [item for item in successes if item is not target]
        target = dict(target)
        target["evidence"] = dict(target["evidence"])
        attempts = {
            _canonical_json(dict(attempt)): dict(attempt)
            for item in [target, *fetch_failures]
            for attempt in item["evidence"]["attempts"]
        }
        target["evidence"]["attempts"] = [attempts[key] for key in sorted(attempts)]
        resolved_origins = [
            {**origin, "disposition": "resolved_by_success"}
            for item in fetch_failures for origin in item["origins"]
        ]
        target["origins"] = sorted(
            [*target["origins"], *resolved_origins],
            key=lambda origin: (origin["discovery_kind"], origin["discovery_ref"],
                                origin["source"], origin["url"]),
        )
        resolved_failures = []
        for failure in fetch_failures:
            failure = dict(failure)
            failure["origins"] = [
                {**origin, "disposition": "resolved_by_success"}
                for origin in failure["origins"]
            ]
            failure["effective_selected"] = False
            failure["_resolved_by_content_hash"] = target["evidence"]["content_hash"]
            resolved_failures.append(failure)
        reconciled.extend([
            target, *other_successes, *processing_unresolved, *resolved_failures
        ])
    return reconciled


def _validate_fetch_attempts(value: Any, *, evidence_status: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(attempt, Mapping) for attempt in value):
        raise ValueError("evidence attempts must be a list of objects")
    if not value and evidence_status not in {"unavailable", "deferred"}:
        raise ValueError("evidence attempts must record at least one attempt")
    checked: list[dict[str, Any]] = []
    valid_statuses = {"success", "ok", "present", "failed", "error", "unavailable", "deferred"}
    for ordinal, raw in enumerate(value, 1):
        attempt = dict(raw)
        identifiers = [attempt.get("engine"), attempt.get("tool")]
        present = [identifier for identifier in identifiers if identifier is not None]
        if not present:
            raise ValueError(f"evidence attempt {ordinal} requires engine or tool")
        for identifier in present:
            _text(identifier, f"evidence attempt {ordinal} engine or tool")
        attempt_status = attempt.get("status", attempt.get("data_status"))
        if attempt_status not in valid_statuses:
            raise ValueError(f"evidence attempt {ordinal} status is invalid")
        if "attempted_at" in attempt:
            _timestamp(attempt["attempted_at"], f"evidence attempt {ordinal} attempted_at")
        if "http_status" in attempt:
            http_status = attempt["http_status"]
            if type(http_status) is not int or not 100 <= http_status <= 599:
                raise ValueError(f"evidence attempt {ordinal} http_status is invalid")
        for field in ("error", "error_code", "stop_reason"):
            if field not in attempt or attempt[field] is None:
                continue
            detail = attempt[field]
            if isinstance(detail, str):
                _text(detail, f"evidence attempt {ordinal} {field}")
            elif not isinstance(detail, Mapping) or not detail:
                raise ValueError(f"evidence attempt {ordinal} {field} must be meaningful")
        checked.append(attempt)
    return checked


def _validate_item(raw: Any, policy: PublicationDatePolicy) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("acquisition item must be an object")
    item = dict(raw)
    for field in ("url", "source", "discovered_at", "discovery_kind", "discovery_ref",
                  "selection_reason", "processing_status"):
        item[field] = _text(item.get(field), field)
    item["title"] = _text(item.get("title", ""), "title", allow_empty=True)
    item["summary"] = _text(item.get("summary", ""), "summary", allow_empty=True)
    item["discovered_at"] = _timestamp(item["discovered_at"], "discovered_at")
    canonical = canonical_url(item["url"])
    parsed = urlparse(canonical)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("item url must be a public HTTP(S) URL")
    if item["discovery_kind"] not in {"site", "search"}:
        raise ValueError("discovery_kind must be site or search")
    if item["processing_status"] not in {"pending", "complete", "failed"}:
        raise ValueError("processing_status is invalid")
    if type(item.get("selected")) is not bool:
        raise ValueError("selected must be a boolean")
    processing_error = item.get("processing_error")
    if item["processing_status"] == "failed":
        processing_error = _text(processing_error, "processing_error")
    elif processing_error is not None:
        raise ValueError("only failed processing may have processing_error")

    published = None if item.get("published_date") is None else _exact_date(
        item["published_date"], "published_date")
    date_evidence = item.get("publication_date_evidence")
    if published is None:
        if date_evidence is not None:
            raise ValueError("unknown publication date cannot have date evidence")
        date_status = "eligible" if policy.mode == "unlimited" else "unknown_pending_review"
    else:
        if not isinstance(date_evidence, Mapping) or set(date_evidence) != {"kind", "url", "text"}:
            raise ValueError("publication date requires exact evidence")
        if date_evidence["kind"] not in {"publisher", "search_result"}:
            raise ValueError("publication date evidence cannot use event, issue, or fetch dates")
        if canonical_url(str(date_evidence["url"])) != canonical:
            raise ValueError("publication date evidence must identify this article")
        _text(date_evidence["text"], "publication date evidence text")
        date_status = "eligible" if policy.selects(published) else "outside_window"

    evidence = item.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("item evidence must be an object")
    evidence = dict(evidence)
    status = evidence.get("status")
    if status not in {"ok", "no_content", "failed", "unavailable", "deferred"}:
        raise ValueError("evidence status is invalid")
    classification = evidence.get("classification")
    if classification not in {"full_content", "snippet", "error"}:
        raise ValueError("evidence classification is invalid")
    evidence["attempts"] = _validate_fetch_attempts(
        evidence.get("attempts"), evidence_status=status
    )
    evidence["fetched_at"] = _timestamp(evidence.get("fetched_at"), "fetched_at")
    final_url = evidence.get("final_url")
    if final_url is not None:
        final_url = _text(final_url, "final_url")
    raw_ref, raw_sha = _optional_managed_snapshot(
        evidence.get("raw_snapshot_ref"), evidence.get("raw_snapshot_sha256"))
    content_ref = _optional_managed_ref(evidence.get("content_ref"), "content_ref")
    content_version_id = None
    content = evidence.get("content")
    content_hash = evidence.get("content_hash")
    if classification == "full_content":
        if status != "ok" or not isinstance(content, str) or not content:
            raise ValueError("full_content requires successful non-empty content")
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if _sha(content_hash, "content_hash") != actual:
            raise ValueError("content_hash does not match content")
        if final_url is None:
            raise ValueError("successful content requires final_url")
        if content_ref is None or raw_ref is None or raw_sha is None:
            raise ValueError(
                "successful full_content requires content_ref and raw snapshot reference/hash")
        if content_ref == raw_ref:
            raise ValueError("content_ref and raw_snapshot_ref must be distinct")
        selected_method = _text(evidence.get("selected_method"), "selected_method")
        matching_attempts = [
            attempt for attempt in evidence["attempts"]
            if (attempt.get("engine") or attempt.get("tool")) == selected_method
            and (
                attempt.get("status") == "success"
                or attempt.get("data_status") in {"ok", "present", "success"}
            )
            and (attempt.get("content_hash") or attempt.get("sha256") or actual) == actual
        ]
        if not matching_attempts:
            raise ValueError("selected_method must identify a successful attempt for the content body")
        evidence["selected_method"] = selected_method
    else:
        if content is not None or content_hash is not None:
            raise ValueError("snippet/error evidence cannot carry full content")
        if status == "ok":
            raise ValueError("ok evidence must be classified full_content")
    failure_reason = evidence.get("failure_reason")
    if status != "ok":
        failure_reason = _text(failure_reason, "failure_reason")
    elif failure_reason is not None:
        raise ValueError("successful evidence cannot have failure_reason")
    http_status = evidence.get("http_status")
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        raise ValueError("http_status is invalid")

    effective_selected = (
        bool(item.get("selected")) and date_status == "eligible"
        and classification == "full_content" and item["processing_status"] == "complete"
    )
    return {
        **item, "canonical_url": canonical, "published": published,
        "date_status": date_status, "date_evidence": date_evidence,
        "effective_selected": effective_selected, "evidence": evidence,
        "content_ref": content_ref,
        "raw_snapshot_ref": raw_ref, "raw_snapshot_sha256": raw_sha,
        "processing_error": processing_error, "content_version_id": content_version_id,
    }


def store_acquisition_batch(database: str | Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Persist under the same lock used by atomic Registry replacement."""
    path = _canonical_database_path(database)
    from .persistent import _exclusive_database_lock

    with _exclusive_database_lock(path):
        return _store_acquisition_batch_locked(path, payload)


def _store_acquisition_batch_locked(
    database: Path, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate and atomically persist one pre-report batch and all evidence."""
    if not isinstance(payload, Mapping) or payload.get("schema_version") != BATCH_SCHEMA_VERSION:
        raise ValueError(f"acquisition batch requires {BATCH_SCHEMA_VERSION}")
    batch_id = _text(payload.get("batch_id"), "batch_id")
    report_date = _exact_date(payload.get("report_date"), "report_date")
    started_at = _timestamp(payload.get("started_at"), "started_at")
    completed_at = payload.get("completed_at")
    if completed_at is not None:
        completed_at = _timestamp(completed_at, "completed_at")
    policy = PublicationDatePolicy.from_dict(payload.get("date_policy"))
    if policy.anchor_date != report_date:
        raise ValueError("date policy anchor must equal batch report_date")
    decision, no_search_reason, searches = _validate_searches(payload)
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("items must be a list")
    items = [_validate_item(item, policy) for item in raw_items]
    _bind_discovery_provenance(items, searches, batch_id)
    items = _merge_same_batch_items(items)
    success_fetch_ids = {
        (item["canonical_url"], item["evidence"]["content_hash"]):
            _stable_id("fetch", f"{batch_id}\n{ordinal}")
        for ordinal, item in enumerate(items, 1)
        if _is_successful_item(item)
    }
    for item in items:
        resolved_hash = item.get("_resolved_by_content_hash")
        item["resolved_by_fetch_id"] = (
            success_fetch_ids[(item["canonical_url"], resolved_hash)]
            if resolved_hash is not None else None
        )
    selected_versions: dict[str, set[str]] = {}
    for item in items:
        if item["effective_selected"]:
            selected_versions.setdefault(item["canonical_url"], set()).add(
                item["evidence"]["content_hash"])
    if any(len(hashes) > 1 for hashes in selected_versions.values()):
        raise ValueError("same batch has conflicting selected content versions for one article")
    unresolved_searches = [search for search in searches if search["status"] == "failed"]
    unresolved_items = [
        item for item in items
        if item["resolved_by_fetch_id"] is None and (
            item["processing_status"] != "complete"
            or item["evidence"]["status"] != "ok"
            or item["evidence"]["classification"] != "full_content"
        )
    ]
    if completed_at is not None and (unresolved_searches or unresolved_items):
        raise ValueError("completed batch contains unresolved work")
    payload_sha256 = _digest(payload)

    connection = _open_database(database, acquisition_writer=True)
    try:
        existing = connection.execute(
            "SELECT * FROM acquisition_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != payload_sha256:
                return _reconcile_acquisition_batch(
                    connection, batch_id=batch_id, report_date=report_date,
                    started_at=started_at, completed_at=completed_at, policy=policy,
                    decision=decision, no_search_reason=no_search_reason,
                    payload_sha256=payload_sha256, searches=searches, items=items,
                )
            result = _batch_summary(connection, batch_id)
            result.update(new_article_count=0, new_content_version_count=0)
            return result
        article_count_before = connection.execute("SELECT count(*) FROM articles").fetchone()[0]
        content_count_before = connection.execute(
            "SELECT count(*) FROM article_content_versions").fetchone()[0]
        with connection:
            connection.execute(
                "INSERT INTO acquisition_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (batch_id, BATCH_SCHEMA_VERSION, report_date.isoformat(), started_at, completed_at,
                 _canonical_json(policy.to_dict()), decision, no_search_reason, payload_sha256),
            )
            for ordinal, search in enumerate(searches, 1):
                search_id = _stable_id("search", f"{batch_id}\n{ordinal}")
                connection.execute(
                    "INSERT INTO acquisition_searches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (search_id, batch_id, ordinal, search["search_ref"], search["query"], search["engine"],
                     search["status"], search["attempted_at"],
                     _canonical_json(search["result_refs"]), _canonical_json(search["budget"]),
                     search["error"]),
                )
            for ordinal, item in enumerate(items, 1):
                _insert_item(connection, batch_id, ordinal, item)
        result = _batch_summary(connection, batch_id)
        result["new_article_count"] = connection.execute(
            "SELECT count(*) FROM articles").fetchone()[0] - article_count_before
        result["new_content_version_count"] = connection.execute(
            "SELECT count(*) FROM article_content_versions").fetchone()[0] - content_count_before
        return result
    finally:
        connection.close()


def _insert_item(connection: sqlite3.Connection, batch_id: str, ordinal: int,
                 item: dict[str, Any]) -> None:
    canonical = item["canonical_url"]
    article_id = _stable_id("article", canonical)
    hostname = (urlparse(canonical).hostname or "unknown").removeprefix("www.")
    source_id = _stable_id("source", hostname)
    observed = item["discovered_at"]
    policy = classify_document(canonical)
    existing = connection.execute(
        "SELECT current_content_version_id FROM articles WHERE article_id = ?", (article_id,)
    ).fetchone()
    prior_content_count = connection.execute(
        "SELECT count(*) FROM article_content_versions WHERE article_id = ?", (article_id,)
    ).fetchone()[0]
    prior_content_hashes = {
        row[0] for row in connection.execute(
            "SELECT content_sha256 FROM article_content_versions WHERE article_id = ?",
            (article_id,),
        )
    }
    connection.execute(
        """INSERT INTO sources(source_id, hostname, display_name, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source_id) DO UPDATE SET first_seen=MIN(first_seen, excluded.first_seen),
             last_seen=MAX(last_seen, excluded.last_seen)""",
        (source_id, hostname, item["source"], observed, observed),
    )
    connection.execute(
        """INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen,
             current_version_id, document_kind, publication_eligible, exclusion_reason)
           VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
           ON CONFLICT(article_id) DO UPDATE SET first_seen=MIN(first_seen, excluded.first_seen),
             last_seen=MAX(last_seen, excluded.last_seen)""",
        (article_id, canonical, source_id, observed, observed, policy.document_kind,
         int(policy.publication_eligible), policy.exclusion_reason),
    )
    connection.execute(
        """INSERT INTO url_aliases(raw_url, canonical_url, article_id, first_seen, last_seen, times_seen)
           VALUES (?, ?, ?, ?, ?, 1)
           ON CONFLICT(raw_url) DO UPDATE SET first_seen=MIN(first_seen, excluded.first_seen),
             last_seen=MAX(last_seen, excluded.last_seen), times_seen=times_seen+1""",
        (item["url"], canonical, article_id, observed, observed),
    )
    evidence = item["evidence"]
    content_version_id = None
    if evidence["classification"] == "full_content":
        content_hash = evidence["content_hash"]
        content_version_id = _stable_id("content", f"{article_id}\n{content_hash}")
        extraction_method = evidence["selected_method"]
        existing_version = connection.execute(
            """SELECT extraction_method FROM article_content_versions
               WHERE article_id = ? AND content_sha256 = ?""",
            (article_id, content_hash),
        ).fetchone()
        if existing_version is not None and existing_version["extraction_method"] != extraction_method:
            raise ValueError("content body already has a different extraction method")
        connection.execute(
            """INSERT OR IGNORE INTO article_content_versions(
                 content_version_id, article_id, content_sha256, markdown_content,
                 markdown_sha256, content_type, source_bytes, extraction_method,
                 extraction_version, first_fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (content_version_id, article_id, content_hash, evidence["content"], content_hash,
             evidence.get("content_type") or "text/markdown",
             len(evidence["content"].encode("utf-8")), extraction_method,
             "web-listening-public.v1", evidence["fetched_at"]),
        )
    fetch_id = _stable_id("fetch", f"{batch_id}\n{ordinal}")
    if content_version_id is not None:
        http_status = evidence.get("http_status")
        if http_status is None or not 200 <= http_status <= 299:
            raise ValueError("successful evidence must preserve its actual 2xx http_status")
        fetch_values = (fetch_id, article_id, item["url"], evidence["final_url"],
                        evidence["fetched_at"], "success", http_status,
                        evidence.get("content_type"), None, None, None, None, content_version_id)
    else:
        code = evidence["status"] if evidence["classification"] == "error" else "snippet_only"
        fetch_values = (fetch_id, article_id, item["url"], evidence.get("final_url"),
                        evidence["fetched_at"], "failed", evidence.get("http_status"),
                        evidence.get("content_type"), None, None, code,
                        evidence.get("failure_reason") or code, None)
    connection.execute(
        """INSERT INTO article_fetches(fetch_id, article_id, requested_url, final_url, fetched_at,
             fetch_status, http_status, content_type, etag, last_modified, error_code,
             error_message, content_version_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        fetch_values,
    )
    if content_version_id is None:
        update_status = "failed"
    elif evidence["content_hash"] in prior_content_hashes:
        update_status = "unchanged"
    elif prior_content_count:
        update_status = "content_changed"
    else:
        update_status = "baseline"
    already_published = bool(existing and existing["current_content_version_id"] == content_version_id)
    selection_status = (
        "selected" if item["effective_selected"] and not already_published else "unselected"
    )
    selection_reason = item["selection_reason"]
    if item["date_status"] != "eligible":
        selection_reason = item["date_status"]
    elif evidence["classification"] != "full_content":
        selection_reason = "content_unavailable"
    elif already_published:
        selection_reason = "already_published_unchanged"
    acquisition_item_id = _stable_id("acquisition-item", f"{batch_id}\n{ordinal}")
    connection.execute(
        """INSERT INTO acquisition_items (
             acquisition_item_id, batch_id, ordinal, article_id, raw_url, source_name,
             title, summary, discovered_at, discovery_kind, discovery_ref, origins_json,
             search_id, publication_date, publication_date_evidence_json, date_status,
             selection_status, selection_reason, update_status, material_status, fetch_id,
             content_version_id, content_ref, raw_snapshot_ref, raw_snapshot_sha256,
             attempts_json, processing_status, processing_error, resolved_by_fetch_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                     ?, ?, ?, ?, ?, ?)""",
        (acquisition_item_id, batch_id, ordinal, article_id, item["url"], item["source"],
         item["title"], item["summary"], observed, item["discovery_kind"], item["discovery_ref"],
         _canonical_json(item["origins"]),
         item["discovery_search_id"],
         item["published"].isoformat() if item["published"] else None,
         _canonical_json(item["date_evidence"]) if item["date_evidence"] else None,
         item["date_status"], selection_status, selection_reason, update_status,
         evidence["classification"], fetch_id, content_version_id,
         item["content_ref"],
         item["raw_snapshot_ref"], item["raw_snapshot_sha256"],
         _canonical_json(evidence["attempts"]), item["processing_status"],
         item["processing_error"], item["resolved_by_fetch_id"]),
    )


def _batch_summary(connection: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    row = connection.execute(
        """SELECT count(DISTINCT article_id) AS article_count,
          count(DISTINCT content_version_id) AS content_version_count,
          sum(selection_status='selected') AS selected_count
          FROM acquisition_items WHERE batch_id = ?""", (batch_id,)).fetchone()
    return {"batch_id": batch_id, "article_count": row["article_count"],
            "content_version_count": row["content_version_count"],
            "selected_count": row["selected_count"] or 0}


def _verified_item_matches(row: sqlite3.Row, item: Mapping[str, Any]) -> bool:
    """Return whether an incoming row is the already-verified observation."""
    evidence = item["evidence"]
    expected = {
        "raw_url": item["url"], "source_name": item["source"],
        "title": item["title"], "summary": item["summary"],
        "discovered_at": item["discovered_at"],
        "discovery_kind": item["discovery_kind"],
        "discovery_ref": item["discovery_ref"],
        "publication_date": item["published"].isoformat() if item["published"] else None,
        "publication_date_evidence_json": (
            _canonical_json(item["date_evidence"]) if item["date_evidence"] else None
        ),
        "content_ref": item["content_ref"],
        "raw_snapshot_ref": item["raw_snapshot_ref"],
        "raw_snapshot_sha256": item["raw_snapshot_sha256"],
        "attempts_json": _canonical_json(evidence["attempts"]),
        "processing_status": item["processing_status"],
        "processing_error": item["processing_error"],
        "final_url": evidence["final_url"],
        "http_status": evidence["http_status"],
        "content_type": evidence.get("content_type"),
        "content_sha256": evidence["content_hash"],
    }
    return all(row[key] == value for key, value in expected.items())


def _reconcile_acquisition_batch(
    connection: sqlite3.Connection, *, batch_id: str, report_date: date,
    started_at: str, completed_at: str | None, policy: PublicationDatePolicy,
    decision: str, no_search_reason: str | None, payload_sha256: str,
    searches: list[dict[str, Any]], items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Append retry evidence while retaining verified observations in one batch."""
    batch = connection.execute(
        "SELECT * FROM acquisition_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    assert batch is not None
    immutable = {
        "report_date": report_date.isoformat(), "started_at": started_at,
        "date_policy_json": _canonical_json(policy.to_dict()),
        "search_decision": decision, "no_search_reason": no_search_reason,
    }
    if batch["frozen_at"] is not None:
        raise ValueError("frozen acquisition batch cannot be reconciled")
    if any(batch[key] != value for key, value in immutable.items()):
        raise ValueError("acquisition resume changed immutable batch fields")

    article_count_before = connection.execute("SELECT count(*) FROM articles").fetchone()[0]
    content_count_before = connection.execute(
        "SELECT count(*) FROM article_content_versions"
    ).fetchone()[0]
    existing_searches = {
        row["search_ref"]: row for row in connection.execute(
            "SELECT * FROM acquisition_searches WHERE batch_id = ?", (batch_id,)
        )
    }
    incoming_search_refs = {search["search_ref"] for search in searches}
    if any(row["status"] == "success" and ref not in incoming_search_refs
           for ref, row in existing_searches.items()):
        raise ValueError("acquisition resume omitted a verified successful search")

    with connection:
        search_ids: dict[str, str] = {}
        next_search_ordinal = connection.execute(
            "SELECT coalesce(max(ordinal), 0) + 1 FROM acquisition_searches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()[0]
        for search in searches:
            ref = search["search_ref"]
            existing = existing_searches.get(ref)
            values = (
                search["query"], search["engine"], search["status"],
                search["attempted_at"], _canonical_json(search["result_refs"]),
                _canonical_json(search["budget"]), search["error"],
            )
            if existing is not None:
                search_ids[ref] = existing["search_id"]
                existing_values = tuple(existing[key] for key in (
                    "query", "engine", "status", "attempted_at", "result_refs_json",
                    "budget_json", "error_message",
                ))
                if existing["status"] == "success":
                    if existing_values != values:
                        raise ValueError("acquisition resume changed a verified successful search")
                    continue
                if search["status"] == "failed":
                    if existing_values != values:
                        raise ValueError("retry must use a new search_ref for a new failed attempt")
                    continue
                connection.execute(
                    """UPDATE acquisition_searches
                       SET status=?, attempted_at=?, result_refs_json=?, budget_json=?, error_message=?
                       WHERE search_id=?""",
                    (search["status"], search["attempted_at"],
                     _canonical_json(search["result_refs"]), _canonical_json(search["budget"]),
                     search["error"], existing["search_id"]),
                )
                continue
            search_id = _stable_id("search", f"{batch_id}\n{next_search_ordinal}")
            connection.execute(
                "INSERT INTO acquisition_searches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (search_id, batch_id, next_search_ordinal, ref, *values),
            )
            search_ids[ref] = search_id
            next_search_ordinal += 1

        existing_successes = {}
        for row in connection.execute(
            """SELECT ai.*, a.canonical_url, cv.content_sha256,
                      f.final_url, f.http_status, f.content_type
               FROM acquisition_items ai
               JOIN articles a ON a.article_id=ai.article_id
               JOIN article_fetches f ON f.fetch_id=ai.fetch_id
               LEFT JOIN article_content_versions cv ON cv.content_version_id=ai.content_version_id
               WHERE ai.batch_id=? AND f.fetch_status='success'""",
            (batch_id,),
        ):
            existing_successes[(row["canonical_url"], row["content_sha256"])] = row
        incoming_success_keys = {
            (item["canonical_url"], item["evidence"]["content_hash"])
            for item in items if _is_successful_item(item)
        }
        if set(existing_successes) - incoming_success_keys:
            raise ValueError("acquisition resume omitted verified successful item evidence")

        next_item_ordinal = connection.execute(
            "SELECT coalesce(max(ordinal), 0) + 1 FROM acquisition_items WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()[0]
        success_fetch_ids = {
            key: row["fetch_id"] for key, row in existing_successes.items()
        }
        new_items = []
        for item in items:
            key = (item["canonical_url"], item["evidence"].get("content_hash"))
            existing = existing_successes.get(key) if _is_successful_item(item) else None
            if existing is not None:
                if not _verified_item_matches(existing, item):
                    raise ValueError("acquisition resume changed verified successful item evidence")
                continue
            item["discovery_search_id"] = (
                search_ids[item["discovery_search_ref"]]
                if item.get("discovery_search_ref") else None
            )
            new_items.append((next_item_ordinal, item))
            if _is_successful_item(item):
                success_fetch_ids[key] = _stable_id(
                    "fetch", f"{batch_id}\n{next_item_ordinal}"
                )
            next_item_ordinal += 1
        for ordinal, item in new_items:
            resolved_hash = item.get("_resolved_by_content_hash")
            item["resolved_by_fetch_id"] = (
                success_fetch_ids[(item["canonical_url"], resolved_hash)]
                if resolved_hash is not None else None
            )
            _insert_item(connection, batch_id, ordinal, item)

        for (canonical, _content_hash), fetch_id in success_fetch_ids.items():
            article_id = _stable_id("article", canonical)
            connection.execute(
                """UPDATE acquisition_items SET resolved_by_fetch_id=?
                   WHERE batch_id=? AND article_id=? AND resolved_by_fetch_id IS NULL
                     AND fetch_id IN (SELECT fetch_id FROM article_fetches WHERE fetch_status='failed')""",
                (fetch_id, batch_id, article_id),
            )
        failed_searches = connection.execute(
            "SELECT count(*) FROM acquisition_searches WHERE batch_id=? AND status='failed'",
            (batch_id,),
        ).fetchone()[0]
        unresolved_items = connection.execute(
            """SELECT count(*) FROM acquisition_items ai
               JOIN article_fetches f ON f.fetch_id=ai.fetch_id
               WHERE ai.batch_id=? AND ai.resolved_by_fetch_id IS NULL
                 AND (f.fetch_status!='success' OR ai.material_status!='full_content'
                      OR ai.processing_status!='complete')""",
            (batch_id,),
        ).fetchone()[0]
        if completed_at is not None and (failed_searches or unresolved_items):
            raise ValueError("completed batch contains unresolved persisted work")
        connection.execute(
            "UPDATE acquisition_batches SET completed_at=?, payload_sha256=? WHERE batch_id=?",
            (completed_at, payload_sha256, batch_id),
        )

    result = _batch_summary(connection, batch_id)
    result["new_article_count"] = (
        connection.execute("SELECT count(*) FROM articles").fetchone()[0] - article_count_before
    )
    result["new_content_version_count"] = (
        connection.execute("SELECT count(*) FROM article_content_versions").fetchone()[0]
        - content_count_before
    )
    return result


def load_acquisition_batch(database: str | Path, batch_id: str) -> dict[str, Any]:
    """Read a complete batch after restart, including unselected/failure history."""
    connection = _open_database(database, read_only=True)
    try:
        batch = connection.execute(
            "SELECT * FROM acquisition_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if batch is None:
            raise KeyError(f"unknown acquisition batch: {batch_id}")
        searches = [dict(row) for row in connection.execute(
            "SELECT * FROM acquisition_searches WHERE batch_id = ? ORDER BY ordinal", (batch_id,))]
        items = [dict(row) for row in connection.execute(
            """SELECT ai.*, a.canonical_url, cv.markdown_content, cv.content_sha256,
                 cv.extraction_method,
                 f.final_url, f.fetched_at, f.fetch_status, f.http_status, f.content_type,
                 f.error_code, f.error_message
               FROM acquisition_items ai JOIN articles a ON a.article_id=ai.article_id
               JOIN article_fetches f ON f.fetch_id=ai.fetch_id
               LEFT JOIN article_content_versions cv ON cv.content_version_id=ai.content_version_id
               WHERE ai.batch_id=? ORDER BY ai.ordinal""", (batch_id,))]
        result = dict(batch)
        result["date_policy"] = json.loads(result.pop("date_policy_json"))
        result["searches"] = searches
        for search in searches:
            search["result_refs"] = json.loads(search.pop("result_refs_json"))
            search["budget"] = json.loads(search.pop("budget_json"))
        result["items"] = items
        for item in items:
            item["publication_date_evidence"] = (
                json.loads(item.pop("publication_date_evidence_json"))
                if item["publication_date_evidence_json"] else None)
            item["origins"] = json.loads(item.pop("origins_json"))
            item["attempts"] = json.loads(item.pop("attempts_json"))
        return result
    finally:
        connection.close()


def freeze_acquisition_for_report(
    database: str | Path,
    batch_id: str,
    *,
    report_date: str,
    allow_unresolved: bool = False,
) -> dict[str, Any]:
    """Freeze exact selected content versions into the existing evidence contract."""
    _exact_date(report_date, "report_date")
    loaded = load_acquisition_batch(database, batch_id)
    if loaded["report_date"] != report_date:
        raise ValueError("acquisition batch report_date mismatch")
    failed_searches = [row for row in loaded["searches"] if row["status"] == "failed"]
    unresolved_items = [
        row for row in loaded["items"]
        if row["resolved_by_fetch_id"] is None and (
            row["fetch_status"] != "success"
            or row["material_status"] != "full_content"
            or row["processing_status"] != "complete"
        )
    ]
    incomplete = loaded["completed_at"] is None or failed_searches or unresolved_items
    if incomplete and not allow_unresolved:
        raise AcquisitionIncompleteError(
            f"acquisition batch has unresolved work: {len(failed_searches)} failed searches, "
            f"{len(unresolved_items)} unresolved items")
    records: list[dict[str, Any]] = []
    for item in loaded["items"]:
        if item["selection_status"] != "selected":
            continue
        if not item["content_version_id"] or not item["markdown_content"]:
            raise AcquisitionIncompleteError("selected item has no exact content version")
        record = {
            "article_id": item["article_id"], "requested_url": item["raw_url"],
            "final_url": item["final_url"], "status": "ok", "attempts": item["attempts"],
            "selected_method": item["extraction_method"],
            "content_type": item["content_type"], "content_ref": item["content_ref"],
            "content": item["markdown_content"], "content_hash": item["content_sha256"],
            "summary_basis": "page", "failure_reason": None,
            "content_version_id": item["content_version_id"],
            "title": item["title"] or None,
            "origins": [{"pillar": "B" if origin["discovery_kind"] == "search" else "A",
                         "source": origin["source"], "url": origin["url"],
                         "discovery_ref": origin["discovery_ref"],
                         "search_ref": origin["search_ref"],
                         "disposition": origin.get("disposition", "successful")}
                        for origin in item["origins"]],
            "acquisition": {
                "discovered_at": item["discovered_at"],
                "publication_date": item["publication_date"],
                "publication_date_evidence": item["publication_date_evidence"],
                "date_status": item["date_status"],
                "update_status": item["update_status"],
            },
            "extra": {"acquisition_batch_id": batch_id,
                      "raw_snapshot_ref": item["raw_snapshot_ref"],
                      "raw_snapshot_sha256": item["raw_snapshot_sha256"],
                      "publication_date": item["publication_date"],
                      "publication_date_evidence": item["publication_date_evidence"]},
        }
        record["record_hash"] = _record_digest(record)
        records.append(record)
    records.sort(key=lambda row: canonical_url(row["requested_url"]))
    dispositions = [{
        "requested_url": item["raw_url"],
        "canonical_url": item["canonical_url"],
        "discovered_at": item["discovered_at"],
        "publication_date": item["publication_date"],
        "publication_date_evidence": item["publication_date_evidence"],
        "date_status": item["date_status"],
        "update_status": item["update_status"],
        "selection_status": item["selection_status"],
        "material_status": item["material_status"],
        "resolved_by_fetch_id": item["resolved_by_fetch_id"],
    } for item in loaded["items"]]
    dispositions.sort(key=lambda row: (row["canonical_url"], row["requested_url"]))
    if not incomplete:
        connection = _open_database(database, acquisition_writer=True)
        try:
            with connection:
                connection.execute(
                    "UPDATE acquisition_batches SET frozen_at = ? "
                    "WHERE batch_id = ? AND frozen_at IS NULL",
                    (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), batch_id),
                )
        finally:
            connection.close()
    return {
        "schema_version": "article-evidence.v1", "report_date": report_date,
        "generated_at": loaded["completed_at"] or loaded["started_at"],
        "dependency_status": "partial" if incomplete else "available",
        "record_count": len(records), "records": records,
        "acquisition_dispositions": dispositions,
        "artifact_digest": _artifact_digest(records),
    }
