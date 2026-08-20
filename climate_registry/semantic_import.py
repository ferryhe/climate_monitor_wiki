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
  on any missing/stale/mismatched/tampered artifact. When an apply fails, the
  pre-apply exact backup is restored, so the live database is never left in a
  partial state.

Design notes (see PR-D):

* The Registry's ``article_id`` is ``_stable_id("article", canonical_url(url))``.
  The sidecar carries its own ``article_id`` (a different, full-length identity),
  so for the join we *recompute* the Registry ``article_id`` from each sidecar
  article's canonical URL using the exact same ``_stable_id``/``canonical_url``
  the Registry uses.
* A sidecar article is "matched" only if its recomputed Registry ``article_id``
  is present in the target report's parsed article set (derived from the same
  canonical Markdown the sidecar is bound to). Unmatched sidecar articles are
  dropped and never invent rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from climate_monitor.dedupe import canonical_url
from climate_monitor.semantic_bundle import verify_semantic_sidecar

from .audit import _stable_id
from .errors import RegistryInputError
from .reports import parse_historical_report
from .schema import apply_migrations
from .weekly import _backup_name, _exclusive_database_lock


_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_VALIDATED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _report_sha256(report_path: Path) -> str:
    """Return the canonical report sha256 (sha256 of the Markdown file bytes).

    This is the same derivation ``ParsedReport.sha256`` uses: the sidecar is
    bound to this exact value and ``verify_semantic_sidecar`` has already
    confirmed the committed sidecar matches it.
    """

    return hashlib.sha256(report_path.read_bytes()).hexdigest()


def _target_article_identities(
    report_path: Path,
) -> dict[str, tuple[str, str, str]]:
    """Return the Registry article identities present in the target report.

    Maps ``registry_article_id`` -> ``(canonical_url, title, summary)`` using
    the same ``_stable_id("article", canonical_url(url))`` derivation the
    Registry pipeline uses. This is the authoritative "target report's article
    set" the sidecar is joined against.
    """

    try:
        report = parse_historical_report(report_path)
    except Exception as exc:  # noqa: BLE001 - fail closed on any parse error
        raise RegistryInputError(
            f"could not parse the target report for semantic import: {exc}"
        ) from exc

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


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime(_VALIDATED_AT_FORMAT)


def _exact_backup(source: Path, destination: Path) -> None:
    """Copy ``source`` to ``destination`` exactly (O_EXCL, 0o600), registry-style."""

    source_descriptor = os.open(
        str(source),
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        source_metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_metadata.st_mode):
            raise OSError("live database is not regular")
        destination_descriptor = os.open(
            str(destination),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            while chunk := os.read(source_descriptor, 1024 * 1024):
                os.write(destination_descriptor, chunk)
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)
    os.chmod(destination, 0o600)


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
            "canonical_url": article.get("canonical_url"),
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

    # 1. Verify the sidecar (fail-closed). Propagates SemanticBundleError.
    payload = verify_semantic_sidecar(report_path)

    # 2. The SHA-gate: exact report sha256 is required for any import.
    report_sha = _report_sha256(report_path)
    if (
        expected_report_sha256 is None
        or _SHA256.fullmatch(expected_report_sha256) is None
        or expected_report_sha256 != report_sha
    ):
        raise RegistryInputError("exact report SHA required to import semantics")

    # 3. Build the join against the target report's article set.
    target = _target_article_identities(report_path)
    matched, unmatched = _build_records(payload, target)

    matched_sorted = sorted(matched, key=lambda record: record["article_id"])
    unmatched_sorted = sorted(unmatched, key=lambda record: record["article_id"])

    plan: dict[str, Any] = {
        "status": "dry-run" if dry_run else "applied",
        "report_sha256": report_sha,
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

    plan["written"] = len(matched_sorted)
    plan["database"] = str(database)
    _apply(report_sha, matched_sorted, database, backup_dir)
    return plan


def _apply(
    report_sha: str,
    matched: list[dict],
    database: str | Path | None,
    backup_dir: str | Path | None,
) -> None:
    if database is None:
        raise RegistryInputError("a registry database is required to apply semantic import")
    database = Path(database)
    if not database.is_file():
        raise RegistryInputError(f"registry database does not exist: {database}")
    if backup_dir is None:
        raise RegistryInputError("a backup directory is required to apply semantic import")
    backup_dir = Path(backup_dir).resolve()
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RegistryInputError(f"could not prepare the backup directory: {exc}") from exc

    backup_path = backup_dir / _backup_name(database)
    if backup_path.exists():
        raise RegistryInputError(f"backup destination already exists: {backup_path.name}")

    with _exclusive_database_lock(database):
        # Exact pre-apply backup so any failure restores the untouched state.
        _exact_backup(database, backup_path)
        try:
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                # Ensure the article_semantics table exists (idempotent).
                apply_migrations(connection)
                connection.execute("BEGIN IMMEDIATE")
                for record in matched:
                    connection.execute(
                        """
                        INSERT INTO article_semantics (
                            report_sha256, article_id, canonical_url, title, summary,
                            categories_json, keywords_json, taxonomy_id,
                            taxonomy_raw_sha256, bundle_sha256, validated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(report_sha256, article_id) DO UPDATE SET
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
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except Exception:
            # Restore the exact pre-apply backup: no orphan rows, no schema drift.
            shutil.copy2(backup_path, database)
            try:
                backup_path.unlink()
            except OSError:
                pass
            raise
