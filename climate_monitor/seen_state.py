"""Two-phase canonical URL history updates for report publication."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from .dedupe import canonical_url


PENDING_SEEN_URL_DELTA_VERSION = "pending-seen-url-delta.v1"


class SeenStateError(ValueError):
    """Seen URL state or a pending delta is malformed or stale."""


def pending_seen_url_delta_path(state_path: str | Path) -> Path:
    path = Path(state_path)
    return path.with_name(path.name + ".pending-urls.json")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_state_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _load_url_list(payload: bytes | None) -> list[str]:
    if payload is None:
        return []
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SeenStateError("seen URL state is not valid UTF-8 JSON") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SeenStateError("seen URL state must be a JSON array of strings")
    return list(value)


def _load_legacy_map(payload: bytes | None) -> dict[str, list[str]]:
    if payload is None:
        return {}
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SeenStateError("legacy seen URL state is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SeenStateError("legacy seen URL state must be a JSON object")
    checked: dict[str, list[str]] = {}
    for bucket, urls in value.items():
        if not isinstance(bucket, str) or not bucket or not isinstance(urls, list):
            raise SeenStateError("legacy seen URL state must map names to URL arrays")
        if any(not isinstance(url, str) for url in urls):
            raise SeenStateError("legacy seen URL state must contain string URLs")
        checked[bucket] = list(urls)
    return checked


def _canonical_values(urls: Iterable[str]) -> list[str]:
    values: set[str] = set()
    for url in urls:
        canonical = canonical_url(url)
        if canonical.strip():
            values.add(canonical)
    return sorted(values)


def _serialize(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _transaction_identity(
    report_date: str,
    combined_sha256: str,
    report_sha256: str | None,
) -> tuple[str, str, str | None]:
    if not isinstance(report_date, str):
        raise SeenStateError("pending seen URL report_date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(report_date)
    except ValueError as exc:
        raise SeenStateError("pending seen URL report_date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != report_date:
        raise SeenStateError("pending seen URL report_date must be YYYY-MM-DD")
    if not (
        isinstance(combined_sha256, str)
        and len(combined_sha256) == 64
        and all(character in "0123456789abcdef" for character in combined_sha256)
    ):
        raise SeenStateError("pending seen URL combined digest is invalid")
    if report_sha256 is not None and not (
        isinstance(report_sha256, str)
        and len(report_sha256) == 64
        and all(character in "0123456789abcdef" for character in report_sha256)
    ):
        raise SeenStateError("pending seen URL report digest is invalid")
    return report_date, combined_sha256, report_sha256


def prepare_seen_url_delta(
    state_path: str | Path,
    urls: Iterable[str],
    *,
    report_date: str,
    combined_sha256: str,
    report_sha256: str,
    snapshot_sha256: str | None = None,
    pending_path: str | Path | None = None,
) -> Path:
    """Stage URL-list additions without changing canonical state."""

    path = Path(state_path)
    before = _read_state_bytes(path)
    _load_url_list(before)
    transaction_date, transaction_digest, transaction_report_digest = (
        _transaction_identity(report_date, combined_sha256, report_sha256)
    )
    if snapshot_sha256 is not None and not (
        isinstance(snapshot_sha256, str)
        and len(snapshot_sha256) == 64
        and all(character in "0123456789abcdef" for character in snapshot_sha256)
    ):
        raise SeenStateError("pending seen URL snapshot digest is invalid")
    payload = {
        "schema_version": PENDING_SEEN_URL_DELTA_VERSION,
        "state_format": "url-list",
        "state_filename": path.name,
        "base_sha256": _sha256(before) if before is not None else None,
        "report_date": transaction_date,
        "combined_sha256": transaction_digest,
        "report_sha256": transaction_report_digest,
        "additions": _canonical_values(urls),
    }
    if snapshot_sha256 is not None:
        payload["snapshot_sha256"] = snapshot_sha256
    destination = Path(pending_path) if pending_path is not None else pending_seen_url_delta_path(path)
    _write_atomic(destination, _serialize(payload))
    return destination


def prepare_legacy_seen_url_delta(
    state_path: str | Path,
    additions: Mapping[str, Iterable[str]],
    *,
    report_date: str,
    combined_sha256: str,
    pending_path: str | Path | None = None,
) -> Path:
    """Stage bucketed additions for the historical article_state.json shape."""

    path = Path(state_path)
    before = _read_state_bytes(path)
    _load_legacy_map(before)
    transaction_date, transaction_digest, _ = _transaction_identity(
        report_date, combined_sha256, None
    )
    rows = []
    for bucket, urls in sorted(additions.items()):
        if not isinstance(bucket, str) or not bucket:
            raise SeenStateError("legacy pending additions require non-empty bucket names")
        canonical = _canonical_values(urls)
        if canonical:
            rows.append({"bucket": bucket, "urls": canonical})
    payload = {
        "schema_version": PENDING_SEEN_URL_DELTA_VERSION,
        "state_format": "legacy-pillar-map",
        "state_filename": path.name,
        "base_sha256": _sha256(before) if before is not None else None,
        "report_date": transaction_date,
        "combined_sha256": transaction_digest,
        "report_sha256": None,
        "additions": rows,
    }
    destination = Path(pending_path) if pending_path is not None else pending_seen_url_delta_path(path)
    _write_atomic(destination, _serialize(payload))
    return destination


def _load_delta(path: Path, *, state_path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SeenStateError("pending seen URL delta is not valid UTF-8 JSON") from exc
    required_fields = {
        "schema_version",
        "state_format",
        "state_filename",
        "base_sha256",
        "report_date",
        "combined_sha256",
        "report_sha256",
        "additions",
    }
    if not isinstance(payload, dict) or set(payload) not in {
        frozenset(required_fields),
        frozenset(required_fields | {"snapshot_sha256"}),
    }:
        raise SeenStateError("pending seen URL delta has an invalid field set")
    if payload["schema_version"] != PENDING_SEEN_URL_DELTA_VERSION:
        raise SeenStateError("pending seen URL delta has an unsupported version")
    if payload["state_filename"] != state_path.name:
        raise SeenStateError("pending seen URL delta targets a different state file")
    if payload["base_sha256"] is not None and not (
        isinstance(payload["base_sha256"], str)
        and len(payload["base_sha256"]) == 64
        and all(character in "0123456789abcdef" for character in payload["base_sha256"])
    ):
        raise SeenStateError("pending seen URL delta has an invalid base digest")
    _transaction_identity(
        payload["report_date"],
        payload["combined_sha256"],
        payload["report_sha256"],
    )
    additions = payload["additions"]
    if payload["state_format"] == "url-list":
        if payload["report_sha256"] is None:
            raise SeenStateError("pending seen URL report digest is required")
        if (
            not isinstance(additions, list)
            or any(
                not isinstance(url, str)
                or not url
                or canonical_url(url) != url
                for url in additions
            )
            or additions != sorted(set(additions))
        ):
            raise SeenStateError(
                "pending seen URL additions must be sorted unique canonical URLs"
            )
        if "snapshot_sha256" in payload:
            snapshot_sha256 = payload["snapshot_sha256"]
            if not (
                isinstance(snapshot_sha256, str)
                and len(snapshot_sha256) == 64
                and all(
                    character in "0123456789abcdef"
                    for character in snapshot_sha256
                )
            ):
                raise SeenStateError("pending seen URL snapshot digest is invalid")
    elif payload["state_format"] == "legacy-pillar-map":
        if "snapshot_sha256" in payload:
            raise SeenStateError("legacy pending seen URL cannot bind a snapshot")
        if payload["report_sha256"] is not None:
            raise SeenStateError("legacy pending seen URL report digest must be null")
        if not isinstance(additions, list):
            raise SeenStateError("legacy pending additions must be an array")
        buckets: list[str] = []
        for row in additions:
            if not isinstance(row, dict) or set(row) != {"bucket", "urls"}:
                raise SeenStateError("legacy pending addition has an invalid field set")
            bucket = row["bucket"]
            urls = row["urls"]
            if not isinstance(bucket, str) or not bucket:
                raise SeenStateError("legacy pending addition has an invalid bucket")
            if (
                not isinstance(urls, list)
                or any(
                    not isinstance(url, str)
                    or not url
                    or canonical_url(url) != url
                    for url in urls
                )
                or urls != sorted(set(urls))
            ):
                raise SeenStateError(
                    "legacy pending additions must contain sorted unique canonical URLs"
                )
            buckets.append(bucket)
        if buckets != sorted(set(buckets)):
            raise SeenStateError("legacy pending additions must have sorted unique buckets")
    else:
        raise SeenStateError("pending seen URL delta has an invalid state format")
    return payload


def _delta_already_applied(payload: Mapping[str, Any], current: Any) -> bool:
    if payload["state_format"] == "url-list":
        present = set(_canonical_values(current))
        return all(url in present for url in payload["additions"])
    if payload["state_format"] == "legacy-pillar-map":
        return all(
            set(row["urls"]).issubset(set(_canonical_values(current.get(row["bucket"], []))))
            for row in payload["additions"]
        )
    raise SeenStateError("pending seen URL delta has an invalid state format")


def commit_seen_url_delta(
    state_path: str | Path,
    *,
    pending_path: str | Path | None = None,
) -> bool:
    """Atomically apply a staged delta; replay after success is a no-op."""

    path = Path(state_path)
    pending = Path(pending_path) if pending_path is not None else pending_seen_url_delta_path(path)
    payload = _load_delta(pending, state_path=path)
    if payload is None:
        return False

    before = _read_state_bytes(path)
    if payload["state_format"] == "url-list":
        current: Any = _load_url_list(before)
    elif payload["state_format"] == "legacy-pillar-map":
        current = _load_legacy_map(before)
    else:
        raise SeenStateError("pending seen URL delta has an invalid state format")

    current_digest = _sha256(before) if before is not None else None
    if current_digest != payload["base_sha256"]:
        if _delta_already_applied(payload, current):
            pending.unlink(missing_ok=True)
            return False
        raise SeenStateError("seen URL state changed after the pending delta was prepared")

    if payload["state_format"] == "url-list":
        present = set(_canonical_values(current))
        updated = list(current)
        for url in payload["additions"]:
            if url not in present:
                updated.append(url)
                present.add(url)
    else:
        updated = {bucket: list(urls) for bucket, urls in current.items()}
        for row in payload["additions"]:
            bucket = row["bucket"]
            values = updated.setdefault(bucket, [])
            present = set(_canonical_values(values))
            for url in row["urls"]:
                if url not in present:
                    values.append(url)
                    present.add(url)

    if updated == current:
        pending.unlink(missing_ok=True)
        return False
    _write_atomic(path, (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    pending.unlink(missing_ok=True)
    return True


def discard_pending_seen_url_delta_if_base_matches(
    state_path: str | Path,
    *,
    pending_path: str | Path | None = None,
) -> bool:
    """Discard an abandoned delta only while its canonical base is unchanged."""

    path = Path(state_path)
    pending = Path(pending_path) if pending_path is not None else pending_seen_url_delta_path(path)
    payload = _load_delta(pending, state_path=path)
    if payload is None:
        return False
    before = _read_state_bytes(path)
    if payload["state_format"] == "url-list":
        _load_url_list(before)
    elif payload["state_format"] == "legacy-pillar-map":
        _load_legacy_map(before)
    else:
        raise SeenStateError("pending seen URL delta has an invalid state format")
    current_digest = _sha256(before) if before is not None else None
    if current_digest != payload["base_sha256"]:
        raise SeenStateError("seen URL state changed after the pending delta was prepared")
    pending.unlink()
    return True


def load_seen_urls(state_path: str | Path) -> set[str]:
    """Read a URL-list state file as canonical identities."""

    return set(_canonical_values(_load_url_list(_read_state_bytes(Path(state_path)))))


def load_legacy_seen_urls(state_path: str | Path) -> set[str]:
    """Read every historical bucket as canonical URL identities."""

    state = _load_legacy_map(_read_state_bytes(Path(state_path)))
    return set(_canonical_values(url for urls in state.values() for url in urls))


def load_pending_seen_url_additions(
    state_path: str | Path,
    *,
    pending_path: str | Path | None = None,
) -> set[str]:
    """Read and validate the canonical identities in a staged delta."""

    path = Path(state_path)
    pending = Path(pending_path) if pending_path is not None else pending_seen_url_delta_path(path)
    payload = _load_delta(pending, state_path=path)
    if payload is None:
        raise SeenStateError("pending seen URL delta is missing")
    if payload["state_format"] == "url-list":
        return set(payload["additions"])
    return {
        url
        for row in payload["additions"]
        for url in row["urls"]
    }


def load_pending_seen_url_transaction(
    state_path: str | Path,
    *,
    pending_path: str | Path | None = None,
) -> tuple[str, str, str | None]:
    """Return the report date and evidence digests bound to a delta."""

    path = Path(state_path)
    pending = Path(pending_path) if pending_path is not None else pending_seen_url_delta_path(path)
    payload = _load_delta(pending, state_path=path)
    if payload is None:
        raise SeenStateError("pending seen URL delta is missing")
    return (
        payload["report_date"],
        payload["combined_sha256"],
        payload["report_sha256"],
    )


def load_pending_seen_url_snapshot_sha256(
    state_path: str | Path,
    *,
    pending_path: str | Path | None = None,
) -> str | None:
    """Return the optional full-item snapshot digest bound to a modern delta."""

    path = Path(state_path)
    pending = (
        Path(pending_path)
        if pending_path is not None
        else pending_seen_url_delta_path(path)
    )
    payload = _load_delta(pending, state_path=path)
    if payload is None:
        raise SeenStateError("pending seen URL delta is missing")
    return payload.get("snapshot_sha256")


__all__ = [
    "PENDING_SEEN_URL_DELTA_VERSION",
    "SeenStateError",
    "commit_seen_url_delta",
    "discard_pending_seen_url_delta_if_base_matches",
    "load_legacy_seen_urls",
    "load_pending_seen_url_additions",
    "load_pending_seen_url_snapshot_sha256",
    "load_pending_seen_url_transaction",
    "load_seen_urls",
    "pending_seen_url_delta_path",
    "prepare_legacy_seen_url_delta",
    "prepare_seen_url_delta",
]
