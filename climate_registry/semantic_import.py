"""Review-gated, SHA-bound semantic import for the Registry.

This module imports the verified ``<report>.semantics.json`` sidecar into a new
``article_semantics`` table in the Registry database. The import is:

* **Review-gated.** The CLI defaults to a dry-run that writes nothing. A human
  must pass ``--apply`` to mutate the database, and the apply path is *also*
  bound to the exact canonical report SHA.
* **SHA-bound.** Every call (dry-run and apply) requires the *exact* canonical
  report sha256 (``expected_report_sha256``). A missing, wrong, or unexpected
  SHA raises :class:`RegistryInputError` and never writes.
* **Fail-closed.** The sidecar is verified by
  :func:`climate_monitor.semantic_bundle.verify_semantic_sidecar`, which raises
  on any missing/stale/mismatched/tampered artifact. Apply writes are staged in
  a copied candidate database, backed up under the Registry lock, and promoted
  through the weekly verified-restore primitive.

Design notes (see PR-D):

* The Registry's ``article_id`` is ``_stable_id("article", canonical_url(url))``.
  The sidecar carries its own ``article_id`` (a different, full-length identity),
  so for the join we *recompute* the Registry ``article_id`` from each sidecar
  article's canonical URL using the exact same ``_stable_id``/``canonical_url``
  the Registry uses.
* A sidecar article is "matched" only if its recomputed Registry ``article_id``
  is present in the target report's parsed article set (derived from the same
  canonical Markdown the sidecar is bound to). Unmatched sidecar articles block
  dry-run plans and are refused on apply.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from climate_monitor.dedupe import canonical_url
from climate_monitor.semantic_bundle import SemanticBundleError, verify_semantic_sidecar

from .audit import _stable_id
from .errors import RegistryInputError, RegistryLockError
from .persistent import _file_sha256, _validate_database
from .reports import ParsedReport, parse_historical_report
from .schema import apply_migrations
from .weekly import (
    WeeklyPreflightError,
    _absolute_path,
    _backup_name,
    _candidate_identity,
    _create_exact_backup,
    _exclusive_database_lock,
    _fsync_parent,
    _is_link_or_reparse,
    _promote,
    _sqlite_entries,
    _validate_exact_backup,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_VALIDATED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _snapshot_report_bytes(report_path: Path) -> bytes:
    try:
        return report_path.read_bytes()
    except OSError as exc:
        raise SemanticBundleError("canonical report is unavailable") from exc


def _snapshot_sha256(report_bytes: bytes) -> str:
    return hashlib.sha256(report_bytes).hexdigest()


def _parse_report_snapshot(report_path: Path, report_bytes: bytes) -> ParsedReport:
    try:
        # Read-tolerant: the target report is already persisted (or explicitly
        # accepted) history; Monday-only policy lives at ingestion boundaries.
        return parse_historical_report(
            report_path, raw=report_bytes, allow_offcycle=True
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on any parse error
        raise RegistryInputError(
            f"could not parse the target report for semantic import: {exc}"
        ) from exc


def _article_identities_from_report(
    report: ParsedReport,
) -> dict[str, tuple[str, str, str]]:
    identities: dict[str, tuple[str, str, str]] = {}
    seen: set[str] = set()
    for article in report.articles:
        normalized = canonical_url(str(getattr(article, "url", "") or ""))
        if not normalized:
            continue
        article_id = _stable_id("article", normalized)
        if article_id in seen:
            continue
        seen.add(article_id)
        identities[article_id] = (
            normalized,
            getattr(article, "title", ""),
            getattr(article, "summary", ""),
        )
    return identities


def _target_article_identities(
    report_path: Path,
    report_bytes: bytes | None = None,
) -> dict[str, tuple[str, str, str]]:
    """Return the Registry article identities present in the target report.

    Maps ``registry_article_id`` -> ``(canonical_url, title, summary)`` using
    the same ``_stable_id("article", canonical_url(url))`` derivation the
    Registry pipeline uses. This is the authoritative "target report's article
    set" the sidecar is joined against.
    """

    if report_bytes is not None:
        report = _parse_report_snapshot(report_path, report_bytes)
    else:
        try:
            # Read-tolerant: already persisted history may include explicitly
            # accepted off-cycle reports.
            report = parse_historical_report(report_path, allow_offcycle=True)
        except Exception as exc:  # noqa: BLE001 - fail closed on any parse error
            raise RegistryInputError(
                f"could not parse the target report for semantic import: {exc}"
            ) from exc
    return _article_identities_from_report(report)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime(_VALIDATED_AT_FORMAT)


def _bundle_sha256(semantics: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(semantics, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _build_records(payload: Mapping[str, Any], target: Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    """Join the verified sidecar to the target report's article set.

    Returns ``(matched, unmatched)`` record lists. Each record carries the
    Registry ``article_id`` (recomputed from the sidecar's canonical URL), the
    semantics, and provenance. ``matched`` are those whose ``article_id`` is
    present in ``target``; ``unmatched`` are dropped from any write.
    """

    taxonomy = payload.get("taxonomy") or {}
    taxonomy_id = taxonomy.get("taxonomy_id")
    taxonomy_raw_sha256 = taxonomy.get("sha256")

    matched: list[dict] = []
    unmatched: list[dict] = []
    for article in payload.get("articles", []):
        canonical = canonical_url(str(article.get("url", "") or ""))
        registry_id = _stable_id("article", canonical)
        semantics = article.get("semantics") or {}
        record = {
            "article_id": registry_id,
            "canonical_url": canonical,
            "title": article.get("title"),
            "summary": semantics.get("summary"),
            "categories": list(semantics.get("categories") or []),
            "keywords": list(semantics.get("keywords") or []),
            "taxonomy_id": taxonomy_id,
            "taxonomy_raw_sha256": taxonomy_raw_sha256,
            "bundle_sha256": _bundle_sha256(semantics),
        }
        if registry_id in target:
            matched.append(record)
        else:
            unmatched.append(record)
    return matched, unmatched


def import_report_semantics(
    report_path: str | Path,
    *,
    expected_report_sha256: str | None,
    dry_run: bool = True,
    database: str | Path | None = None,
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Import verified report semantics into the Registry, SHA-gated.

    The flow is fail-closed and review-gated:

    1. Verify the sidecar via
       :func:`climate_monitor.semantic_bundle.verify_semantic_sidecar`. Any
       missing/stale/mismatched/tampered artifact raises (propagated, never
       swallowed).
    2. Compute the canonical report sha256 from the Markdown file bytes and
       require it to exactly equal ``expected_report_sha256``. Otherwise raise
       :class:`RegistryInputError` ("exact report SHA required to import
       semantics"). This is the SHA-gate and applies to dry-run and apply.
    3. Join each verified sidecar article to the target report's parsed article
       set, recomputing the Registry ``article_id`` from the canonical URL.

    Dry-run (default) returns a deterministic plan and touches nothing. Apply
    (``dry_run=False``) requires ``database`` and ``backup_dir`` and writes the
    matched rows inside a backup-guarded transaction.

    Returns a plan dict with at least ``status``, ``report_sha256``,
    ``matched``, ``unmatched``, and ``would_write``.
    """

    report_path = Path(report_path)

    report_bytes = _snapshot_report_bytes(report_path)

    # 1. Verify the sidecar (fail-closed). Propagates SemanticBundleError.
    payload = verify_semantic_sidecar(report_path, report_bytes=report_bytes)

    # 2. The SHA-gate: exact report sha256 is required for any import.
    report_sha = _snapshot_sha256(report_bytes)
    if (
        expected_report_sha256 is None
        or _SHA256.fullmatch(expected_report_sha256) is None
        or expected_report_sha256 != report_sha
    ):
        raise RegistryInputError("exact report SHA required to import semantics")

    # 3. Build the join against the target report's article set.
    parsed_report = _parse_report_snapshot(report_path, report_bytes)
    target = _target_article_identities(report_path, report_bytes)
    matched, unmatched = _build_records(payload, target)
    sidecar_count = len(payload.get("articles", []))

    matched_sorted = sorted(matched, key=lambda record: record["article_id"])
    unmatched_sorted = sorted(unmatched, key=lambda record: record["article_id"])
    blockers: list[str] = []
    if unmatched_sorted:
        blockers.append("unmatched_sidecar_rows")
    if len(matched_sorted) != sidecar_count:
        blockers.append("sidecar_match_count_mismatch")

    plan: dict[str, Any] = {
        "status": "blocked" if blockers else ("dry-run" if dry_run else "applied"),
        "report_sha256": report_sha,
        "sidecar_count": sidecar_count,
        "matched_count": len(matched_sorted),
        "unmatched_count": len(unmatched_sorted),
        "blocked": bool(blockers),
        "blockers": blockers,
        "matched": [
            (record["article_id"], record["title"], record["categories"], record["keywords"])
            for record in matched_sorted
        ],
        "unmatched": [
            (record["article_id"], record["canonical_url"], record["title"])
            for record in unmatched_sorted
        ],
        "would_write": len(matched_sorted),
    }

    if dry_run:
        return plan

    if unmatched_sorted:
        raise RegistryInputError("semantic import blocked: unmatched sidecar rows")
    if len(matched_sorted) != sidecar_count:
        raise RegistryInputError(
            "semantic import blocked: matched count does not match verified sidecar count"
        )

    plan["database"] = str(database)
    apply_result = _apply(
        report_sha,
        matched_sorted,
        database,
        backup_dir,
        target=target,
        report_date=parsed_report.report_date,
        report_filename=parsed_report.path.name,
        sidecar_count=sidecar_count,
    )
    plan.update(apply_result)
    return plan


def _assert_safe_database(database: Path) -> None:
    if _is_link_or_reparse(database):
        raise RegistryInputError("registry database is unsafe")
    try:
        metadata = os.lstat(database)
    except OSError as exc:
        raise RegistryInputError(f"registry database does not exist: {database}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RegistryInputError("registry database is not a regular file")
    sidecars = _sqlite_entries(database)
    if sidecars:
        names = ", ".join(path.name for path in sidecars)
        raise RegistryInputError(
            f"registry has active SQLite sidecar files; reconcile before semantic import: {names}"
        )


def _assert_safe_backup_dir(backup_dir: Path, database: Path) -> None:
    if backup_dir == database or backup_dir in database.parents:
        raise RegistryInputError(
            "backup directory must not contain the live registry database"
        )
    if backup_dir.exists() and (_is_link_or_reparse(backup_dir) or not backup_dir.is_dir()):
        raise RegistryInputError("backup directory is unsafe")


def _semantic_path(value: str | Path, *, label: str) -> Path:
    try:
        return _absolute_path(value, label=label)
    except WeeklyPreflightError as exc:
        message = str(exc).replace(
            "weekly registry preflight failed:",
            "semantic import preflight failed:",
            1,
        )
        raise RegistryInputError(message) from exc


def _open_read_only_database(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)


def _registry_report_id(
    connection: sqlite3.Connection,
    *,
    report_date: str,
    report_filename: str,
    report_sha: str,
) -> str:
    row = connection.execute(
        """
        SELECT report_id
        FROM reports
        WHERE report_date = ? AND filename = ? AND report_sha256 = ?
        """,
        (report_date, report_filename, report_sha),
    ).fetchone()
    if row is None:
        raise RegistryInputError(
            "exact report SHA is missing from the Registry DB"
        )
    return str(row[0])


def _verify_target_memberships(
    connection: sqlite3.Connection,
    *,
    report_id: str,
    target: Mapping[str, tuple[str, str, str]],
) -> None:
    rows = connection.execute(
        """
        SELECT ra.article_id, a.canonical_url
        FROM report_appearances ra
        JOIN articles a ON a.article_id = ra.article_id
        WHERE ra.report_id = ?
        """,
        (report_id,),
    ).fetchall()
    present = {str(row[0]): str(row[1]) for row in rows}
    missing = [
        article_id
        for article_id, (canonical, _title, _summary) in target.items()
        if present.get(article_id) != canonical
    ]
    if missing:
        raise RegistryInputError(
            "target article membership is missing from the Registry DB"
        )


def _verified_registry_target(
    database: Path,
    *,
    report_sha: str,
    report_date: str,
    report_filename: str,
    target: Mapping[str, tuple[str, str, str]],
) -> str:
    connection = _open_read_only_database(database)
    try:
        _validate_database(connection)
        report_id = _registry_report_id(
            connection,
            report_date=report_date,
            report_filename=report_filename,
            report_sha=report_sha,
        )
        _verify_target_memberships(
            connection,
            report_id=report_id,
            target=target,
        )
        return report_id
    finally:
        connection.close()


def _write_semantic_records(
    connection: sqlite3.Connection,
    *,
    report_id: str,
    report_sha: str,
    matched: list[dict],
) -> int:
    for record in matched:
        connection.execute(
            """
            INSERT INTO article_semantics (
                report_id, report_sha256, article_id, canonical_url, title, summary,
                categories_json, keywords_json, taxonomy_id,
                taxonomy_raw_sha256, bundle_sha256, validated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_id, article_id) DO UPDATE SET
                report_sha256 = excluded.report_sha256,
                canonical_url = excluded.canonical_url,
                title = excluded.title,
                summary = excluded.summary,
                categories_json = excluded.categories_json,
                keywords_json = excluded.keywords_json,
                taxonomy_id = excluded.taxonomy_id,
                taxonomy_raw_sha256 = excluded.taxonomy_raw_sha256,
                bundle_sha256 = excluded.bundle_sha256,
                validated_at = excluded.validated_at
            """,
            (
                report_id,
                report_sha,
                record["article_id"],
                record["canonical_url"],
                record["title"],
                record["summary"],
                json.dumps(record["categories"], ensure_ascii=False, sort_keys=True),
                json.dumps(record["keywords"], ensure_ascii=False, sort_keys=True),
                record["taxonomy_id"],
                record["taxonomy_raw_sha256"],
                record["bundle_sha256"],
                _utc_now(),
            ),
        )
    return len(matched)


def _apply_candidate(
    candidate: Path,
    *,
    report_id: str,
    report_sha: str,
    matched: list[dict],
    sidecar_count: int,
) -> int:
    connection = sqlite3.connect(candidate)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        apply_migrations(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            written = _write_semantic_records(
                connection,
                report_id=report_id,
                report_sha=report_sha,
                matched=matched,
            )
            if written != sidecar_count:
                raise RegistryInputError(
                    "semantic import written count does not match verified sidecar count"
                )
            persisted = connection.execute(
                "SELECT COUNT(*) FROM article_semantics WHERE report_id = ?",
                (report_id,),
            ).fetchone()[0]
            if persisted != sidecar_count:
                raise RegistryInputError(
                    "semantic import written count does not match verified sidecar count"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        _validate_database(connection)
        return written
    finally:
        connection.close()


def _apply(
    report_sha: str,
    matched: list[dict],
    database: str | Path | None,
    backup_dir: str | Path | None,
    *,
    target: Mapping[str, tuple[str, str, str]],
    report_date: str,
    report_filename: str,
    sidecar_count: int,
) -> dict[str, Any]:
    if database is None:
        raise RegistryInputError("a registry database is required to apply semantic import")
    database = _semantic_path(database, label="database")
    if not database.is_file():
        raise RegistryInputError(f"registry database does not exist: {database}")
    if backup_dir is None:
        raise RegistryInputError("a backup directory is required to apply semantic import")
    backup_dir = _semantic_path(backup_dir, label="backup directory")
    _assert_safe_backup_dir(backup_dir, database)

    with _exclusive_database_lock(database):
        _assert_safe_database(database)
        try:
            live_sha = _file_sha256(database)
        except OSError as exc:
            raise RegistryInputError("registry database could not be read") from exc
        report_id = _verified_registry_target(
            database,
            report_sha=report_sha,
            report_date=report_date,
            report_filename=report_filename,
            target=target,
        )
        candidate: Path | None = None
        backup_path: Path | None = None
        try:
            descriptor, candidate_name = tempfile.mkstemp(
                prefix=f".{database.name}.",
                suffix=".semantic-candidate",
                dir=database.parent,
            )
            os.close(descriptor)
            candidate = Path(candidate_name)
            candidate.unlink()
            _create_exact_backup(database, candidate)
            _validate_exact_backup(candidate, live_sha)
            written = _apply_candidate(
                candidate,
                report_id=report_id,
                report_sha=report_sha,
                matched=matched,
                sidecar_count=sidecar_count,
            )
            if sidecars := _sqlite_entries(candidate):
                names = ", ".join(path.name for path in sidecars)
                raise RegistryInputError(
                    f"semantic import candidate has active SQLite sidecar files: {names}"
                )
            candidate_sha = _file_sha256(candidate)
            if candidate_sha == live_sha:
                return {
                    "written": written,
                    "backup_name": None,
                    "database_sha256_before": live_sha,
                    "database_sha256_after": live_sha,
                }
            candidate_identity = _candidate_identity(candidate)
            try:
                backup_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RegistryInputError(
                    f"could not prepare the backup directory: {exc}"
                ) from exc
            if _is_link_or_reparse(backup_dir) or not backup_dir.is_dir():
                raise RegistryInputError("backup directory is unsafe")
            backup_path = backup_dir / _backup_name(
                database,
                datetime.now(timezone.utc),
                live_sha,
            )
            if backup_path.exists() or backup_path.is_symlink():
                raise RegistryInputError(
                    f"backup destination already exists: {backup_path.name}"
                )
            _create_exact_backup(database, backup_path)
            _fsync_parent(backup_path)
            _validate_exact_backup(backup_path, live_sha)
            if _sqlite_entries(database) or _file_sha256(database) != live_sha:
                raise RegistryLockError(
                    "live registry changed before semantic import promotion"
                )
            after_sha = _promote(
                candidate,
                database,
                backup_path,
                live_sha,
                expected_candidate_identity=candidate_identity,
            )
            return {
                "written": written,
                "backup_name": backup_path.name,
                "database_sha256_before": live_sha,
                "database_sha256_after": after_sha,
            }
        finally:
            if candidate is not None:
                candidate.unlink(missing_ok=True)
                for sidecar in _sqlite_entries(candidate):
                    sidecar.unlink(missing_ok=True)
