from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from climate_delivery.artifacts import load_report_artifact
from climate_monitor.dedupe import canonical_url
from climate_monitor.run_ledger import (
    LedgerError,
    RunLedgerReader,
    SUCCESS_STATUSES,
)

from .audit import _stable_id
from .capture import MAX_BATCH, capture_enrich_registry
from .classification import classify_document
from .errors import RegistryBuildError, RegistryInputError, RegistryLockError
from .fetch import (
    DEFAULT_TIMEOUT,
    PinnedTransport,
    Resolver,
    Transport,
    _default_resolver,
)
from .persistent import (
    _exclusive_database_lock,
    _file_sha256,
    _fsync_parent,
    _validate_database,
    plan_registry_update,
    update_registry,
)
from .reports import ParsedReport, parse_historical_report


_REPORT_SHA = "report_sha256"
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class WeeklyPreflightError(RegistryInputError):
    """The weekly sync cannot safely start."""

    kind = "preflight"


class WeeklyPartialError(RegistryBuildError):
    """One or more target articles could not be captured and enriched."""

    kind = "partial"

    def __init__(self, message: str, *, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result


class WeeklyValidationError(RegistryBuildError):
    """A candidate, backup, or promotion failed validation."""

    kind = "validation"


GitRunner = Callable[[Sequence[str], Path], Any]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class _Preflight:
    target_date: str
    repository_root: Path
    source_dir: Path
    source_path: Path
    database: Path
    artifact_root: Path
    backup_dir: Path
    lock_file: Path
    publisher_ledger_dir: Path
    report: ParsedReport
    live_sha256: str
    update_plan: dict[str, Any]
    target_article_ids: tuple[str, ...]
    target_eligible_ids: tuple[str, ...]
    new_article_ids: tuple[str, ...]
    missing_enrichment_ids: tuple[str, ...]


@dataclass(frozen=True)
class _RestorePreflight:
    database: Path
    backup: Path
    backup_dir: Path
    lock_file: Path
    expected_sha256: str
    live_sha256: str
    live_mode: int


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _clock_value(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: invalid UTC clock"
        )
    if value.utcoffset() != timedelta(0):
        raise WeeklyPreflightError(
            "weekly registry preflight failed: invalid UTC clock"
        )
    return value.astimezone(timezone.utc)


def _target_day(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: target date is invalid"
        ) from exc
    if parsed.isoformat() != value or parsed.weekday() != 0:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: target date must be a Monday"
        )
    return value


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: a configured path is unavailable"
        ) from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse
    )


def _sqlite_entries(database: Path) -> tuple[Path, ...]:
    """Return every sidecar directory entry, including dangling links."""

    entries: list[Path] = []
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        candidate = Path(f"{database}{suffix}")
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            # An entry that cannot be safely classified is still active for
            # fail-closed coordination purposes.
            entries.append(candidate)
        else:
            entries.append(candidate)
    return tuple(entries)


def _existing_components(path: Path) -> tuple[Path, ...]:
    current = Path(path.anchor)
    output: list[Path] = []
    for part in path.parts[1:]:
        current /= part
        try:
            os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise WeeklyPreflightError(
                "weekly registry preflight failed: a configured path is unavailable"
            ) from exc
        output.append(current)
    return tuple(output)


def _absolute_path(value: str | Path, *, label: str) -> Path:
    try:
        raw = Path(value).expanduser()
    except (OSError, RuntimeError, TypeError) as exc:
        raise WeeklyPreflightError(
            f"weekly registry preflight failed: {label} path is invalid"
        ) from exc
    if not raw.is_absolute():
        raise WeeklyPreflightError(
            f"weekly registry preflight failed: {label} path must be absolute"
        )
    if any(_is_link_or_reparse(component) for component in _existing_components(raw)):
        raise WeeklyPreflightError(
            f"weekly registry preflight failed: {label} path is unsafe"
        )
    try:
        return raw.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise WeeklyPreflightError(
            f"weekly registry preflight failed: {label} path is invalid"
        ) from exc


def _outside_repository(path: Path, repository_root: Path, *, label: str) -> None:
    try:
        path.relative_to(repository_root)
    except ValueError:
        return
    raise WeeklyPreflightError(
        f"weekly registry preflight failed: {label} must be outside the repository"
    )


def _default_git_runner(
    arguments: Sequence[str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )


def _git(
    arguments: Sequence[str],
    *,
    cwd: Path,
    runner: GitRunner,
    failure: str,
) -> str:
    try:
        result = runner(("git", *arguments), cwd)
    except Exception as exc:
        raise WeeklyPreflightError(
            f"weekly registry preflight failed: {failure}"
        ) from exc
    if (
        not isinstance(getattr(result, "returncode", None), int)
        or result.returncode != 0
    ):
        raise WeeklyPreflightError(f"weekly registry preflight failed: {failure}")
    stdout = getattr(result, "stdout", "")
    if not isinstance(stdout, str):
        raise WeeklyPreflightError(f"weekly registry preflight failed: {failure}")
    return stdout


def _repository_for_source(source_dir: Path, *, runner: GitRunner) -> Path:
    if not source_dir.is_dir():
        raise WeeklyPreflightError(
            "weekly registry preflight failed: source directory is unavailable"
        )
    root_text = _git(
        ("rev-parse", "--show-toplevel"),
        cwd=source_dir,
        runner=runner,
        failure="source directory is not a Git checkout",
    ).strip()
    root = _absolute_path(root_text, label="repository")
    try:
        source_dir.relative_to(root)
    except ValueError as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: source directory escapes its checkout"
        ) from exc
    status = _git(
        ("status", "--porcelain=v1", "--untracked-files=all"),
        cwd=root,
        runner=runner,
        failure="checkout cleanliness could not be verified",
    )
    if status:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: checkout is not clean"
        )
    return root


def _tracked_at_head(
    source_path: Path,
    *,
    repository_root: Path,
    runner: GitRunner,
) -> None:
    try:
        relative = source_path.relative_to(repository_root).as_posix()
    except ValueError as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: target source escapes its checkout"
        ) from exc
    tracked = _git(
        ("ls-files", "--error-unmatch", "--", relative),
        cwd=repository_root,
        runner=runner,
        failure="target source is not tracked at HEAD",
    ).strip()
    if tracked != relative:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: target source is not tracked at HEAD"
        )
    _git(
        ("cat-file", "-e", f"HEAD:{relative}"),
        cwd=repository_root,
        runner=runner,
        failure="target source is not tracked at HEAD",
    )


def _parse_target(source_dir: Path, target_date: str) -> tuple[Path, ParsedReport]:
    source_path = source_dir / f"climate-monitor-{target_date}.md"
    if _is_link_or_reparse(source_path):
        raise WeeklyPreflightError(
            "weekly registry preflight failed: target source is unsafe"
        )
    try:
        metadata = os.lstat(source_path)
    except OSError as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: target source is unavailable"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise WeeklyPreflightError(
            "weekly registry preflight failed: target source is not a regular file"
        )
    try:
        report = parse_historical_report(source_path)
    except Exception as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: target source could not be parsed"
        ) from exc
    if report.report_date != target_date or report.cadence != "weekly":
        raise WeeklyPreflightError(
            "weekly registry preflight failed: target source identity is invalid"
        )
    return source_path, report


def _latest_publisher_attempt(
    ledger_dir: Path,
    *,
    repository_root: Path,
    target_date: str,
    report_sha256: str,
    now: datetime,
) -> None:
    try:
        reader = RunLedgerReader(ledger_dir, repository_root=repository_root)
        attempts = reader._load()
    except LedgerError as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: publisher ledger is invalid"
        ) from exc
    matching = [
        attempt
        for attempt in attempts
        if attempt["stage"] == "publisher" and attempt["report_date"] == target_date
    ]
    if not matching:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: publisher success is missing"
        )
    latest = matching[-1]
    try:
        finished = datetime.strptime(
            latest["finished_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: publisher ledger is invalid"
        ) from exc
    if finished > now or latest["status"] not in SUCCESS_STATUSES:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: latest publisher attempt is not successful"
        )
    ledger_report = latest.get("report")
    if (
        not isinstance(ledger_report, dict)
        or ledger_report.get("report_date") != target_date
        or ledger_report.get("sha256") != report_sha256
    ):
        raise WeeklyPreflightError(
            "weekly registry preflight failed: publisher report identity does not match"
        )


def _validate_artifact(artifact_root: Path, report: ParsedReport) -> None:
    artifact = load_report_artifact(
        artifact_root,
        report_date=report.report_date,
        report_filename=report.path.name,
        report_title=report.title,
        report_sha256=report.sha256,
        include_pdf_bytes=False,
    )
    if artifact is None:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: delivery artifact is invalid"
        )


def _target_identities(report: ParsedReport) -> tuple[tuple[str, ...], tuple[str, ...]]:
    article_ids: list[str] = []
    eligible_ids: list[str] = []
    seen: set[str] = set()
    for article in report.articles:
        normalized = canonical_url(article.url)
        if not normalized:
            continue
        article_id = _stable_id("article", normalized)
        if article_id in seen:
            continue
        seen.add(article_id)
        article_ids.append(article_id)
        if classify_document(normalized).publication_eligible:
            eligible_ids.append(article_id)
    return tuple(article_ids), tuple(eligible_ids)


def _rows_for_ids(
    connection: sqlite3.Connection,
    article_ids: Sequence[str],
) -> dict[str, sqlite3.Row]:
    if not article_ids:
        return {}
    connection.row_factory = sqlite3.Row
    output: dict[str, sqlite3.Row] = {}
    for offset in range(0, len(article_ids), 500):
        batch = article_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            "SELECT article_id, publication_eligible, current_content_version_id "
            f"FROM articles WHERE article_id IN ({placeholders})",
            tuple(batch),
        ).fetchall()
        output.update((row["article_id"], row) for row in rows)
    return output


def _has_complete_current_enrichment(
    connection: sqlite3.Connection,
    article_id: str,
    content_version_id: str | None,
) -> bool:
    if content_version_id is None:
        return False
    latest_fetch = connection.execute(
        """
        SELECT fetch_status
        FROM article_fetches
        WHERE article_id = ?
        ORDER BY fetched_at DESC, fetch_id DESC
        LIMIT 1
        """,
        (article_id,),
    ).fetchone()
    if latest_fetch is None or latest_fetch["fetch_status"] not in {
        "success",
        "not_modified",
    }:
        return False
    row = connection.execute(
        """
        SELECT categories_json, keywords_json
        FROM article_enrichments
        WHERE content_version_id = ? AND status = 'complete'
          AND summary IS NOT NULL AND length(trim(summary)) > 0
          AND categories_json IS NOT NULL AND length(trim(categories_json)) > 0
          AND keywords_json IS NOT NULL AND length(trim(keywords_json)) > 0
          AND language IS NOT NULL AND length(trim(language)) > 0
          AND generator_name IS NOT NULL AND length(trim(generator_name)) > 0
          AND generator_version IS NOT NULL AND length(trim(generator_version)) > 0
          AND generated_at IS NOT NULL AND length(trim(generated_at)) > 0
        ORDER BY generated_at DESC, enrichment_id DESC LIMIT 1
        """,
        (content_version_id,),
    ).fetchone()
    return bool(
        row is not None
        and _validate_json_list(row["categories_json"])
        and _validate_json_list(row["keywords_json"])
    )


def _dry_plan_articles(
    database: Path,
    *,
    article_ids: tuple[str, ...],
    eligible_ids: tuple[str, ...],
    schema_version: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        rows = _rows_for_ids(connection, article_ids)
        new_ids = tuple(
            article_id for article_id in article_ids if article_id not in rows
        )
        missing: list[str] = []
        for article_id in eligible_ids:
            row = rows.get(article_id)
            if row is None or schema_version < 3:
                missing.append(article_id)
                continue
            if not _has_complete_current_enrichment(
                connection, article_id, row["current_content_version_id"]
            ):
                missing.append(article_id)
        return new_ids, tuple(sorted(missing))
    except sqlite3.DatabaseError as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: registry article plan is invalid"
        ) from exc
    finally:
        connection.close()


def _safe_update_plan(
    source_dir: Path, database: Path, target_date: str
) -> dict[str, Any]:
    try:
        plan = plan_registry_update(source_dir, database)
    except (RegistryInputError, RegistryBuildError) as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: registry update plan is invalid"
        ) from exc
    if plan.get("conflicts"):
        raise WeeklyPreflightError(
            "weekly registry preflight failed: registry update plan has conflicts"
        )
    new_dates = {item.get("date") for item in plan.get("new_reports", [])}
    if not new_dates <= {target_date}:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: update plan includes another report date"
        )
    return plan


def _preflight(
    *,
    target_date: str,
    source_dir: str | Path,
    database: str | Path,
    artifact_root: str | Path,
    backup_dir: str | Path,
    lock_file: str | Path,
    publisher_ledger_dir: str | Path,
    clock: Clock,
    git_runner: GitRunner,
    check_lock: bool,
) -> _Preflight:
    target_date = _target_day(target_date)
    source_dir_path = _absolute_path(source_dir, label="source directory")
    repository_root = _repository_for_source(source_dir_path, runner=git_runner)
    source_path, report = _parse_target(source_dir_path, target_date)
    _tracked_at_head(source_path, repository_root=repository_root, runner=git_runner)

    database_path = _absolute_path(database, label="database")
    if _is_link_or_reparse(database_path):
        raise WeeklyPreflightError(
            "weekly registry preflight failed: database is unsafe"
        )
    try:
        database_metadata = os.lstat(database_path)
    except OSError as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: database is unavailable"
        ) from exc
    if not stat.S_ISREG(database_metadata.st_mode):
        raise WeeklyPreflightError(
            "weekly registry preflight failed: database is not a regular file"
        )
    _outside_repository(database_path, repository_root, label="database")
    if _sqlite_entries(database_path):
        raise WeeklyPreflightError(
            "weekly registry preflight failed: database has active SQLite sidecars"
        )

    backup_dir_path = _absolute_path(backup_dir, label="backup directory")
    _outside_repository(backup_dir_path, repository_root, label="backup directory")
    if backup_dir_path.exists() and not backup_dir_path.is_dir():
        raise WeeklyPreflightError(
            "weekly registry preflight failed: backup destination is invalid"
        )
    if backup_dir_path == database_path or backup_dir_path in database_path.parents:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: backup directory contains the live database"
        )

    lock_file_path = _absolute_path(lock_file, label="lock file")
    expected_lock = database_path.with_name(f"{database_path.name}.lock")
    if lock_file_path != expected_lock:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: lock file does not coordinate with the database"
        )
    _outside_repository(lock_file_path, repository_root, label="lock file")
    if _is_link_or_reparse(lock_file_path):
        raise WeeklyPreflightError(
            "weekly registry preflight failed: lock file is unsafe"
        )
    if check_lock and lock_file_path.exists():
        raise RegistryLockError("weekly registry sync is already locked")

    artifact_root_path = _absolute_path(artifact_root, label="artifact root")
    ledger_path = _absolute_path(publisher_ledger_dir, label="publisher ledger")
    _outside_repository(ledger_path, repository_root, label="publisher ledger")

    now = _clock_value(clock)
    _latest_publisher_attempt(
        ledger_path,
        repository_root=repository_root,
        target_date=target_date,
        report_sha256=report.sha256,
        now=now,
    )
    _validate_artifact(artifact_root_path, report)
    plan = _safe_update_plan(source_dir_path, database_path, target_date)
    article_ids, eligible_ids = _target_identities(report)
    new_ids, missing_ids = _dry_plan_articles(
        database_path,
        article_ids=article_ids,
        eligible_ids=eligible_ids,
        schema_version=plan["database_schema_version"],
    )
    return _Preflight(
        target_date=target_date,
        repository_root=repository_root,
        source_dir=source_dir_path,
        source_path=source_path,
        database=database_path,
        artifact_root=artifact_root_path,
        backup_dir=backup_dir_path,
        lock_file=lock_file_path,
        publisher_ledger_dir=ledger_path,
        report=report,
        live_sha256=_safe_file_sha256(database_path),
        update_plan=plan,
        target_article_ids=article_ids,
        target_eligible_ids=eligible_ids,
        new_article_ids=new_ids,
        missing_enrichment_ids=missing_ids,
    )


def _revalidate_upstream(
    reference: _Preflight,
    *,
    clock: Clock,
    git_runner: GitRunner,
) -> _Preflight:
    current = _preflight(
        target_date=reference.target_date,
        source_dir=reference.source_dir,
        database=reference.database,
        artifact_root=reference.artifact_root,
        backup_dir=reference.backup_dir,
        lock_file=reference.lock_file,
        publisher_ledger_dir=reference.publisher_ledger_dir,
        clock=clock,
        git_runner=git_runner,
        check_lock=False,
    )
    if (
        current.live_sha256 != reference.live_sha256
        or current.report.sha256 != reference.report.sha256
        or current.update_plan != reference.update_plan
        or current.target_article_ids != reference.target_article_ids
        or current.target_eligible_ids != reference.target_eligible_ids
        or current.new_article_ids != reference.new_article_ids
        or current.missing_enrichment_ids != reference.missing_enrichment_ids
    ):
        raise RegistryLockError(
            "weekly registry inputs changed before candidate promotion"
        )
    return current


def _result(
    preflight: _Preflight,
    *,
    dry_run: bool,
    status: str,
    reports_added: int,
    articles_added: int,
    articles_captured: int,
    articles_failed: int = 0,
    promotion: str,
    before_sha256: str,
    after_sha256: str,
    backup_name: str | None = None,
) -> dict[str, Any]:
    would_add_reports = len(preflight.update_plan["new_reports"])
    would_add_articles = len(preflight.new_article_ids)
    would_capture = list(preflight.missing_enrichment_ids)
    would_promote = bool(
        preflight.update_plan["mutation_required"] or preflight.missing_enrichment_ids
    )
    return {
        "status": status,
        "date": preflight.target_date,
        _REPORT_SHA: preflight.report.sha256,
        "dry_run": dry_run,
        "reports_added": reports_added,
        "articles_added": articles_added,
        "articles_captured": articles_captured,
        "articles_failed": articles_failed,
        "target_article_count": len(preflight.target_article_ids),
        "target_eligible_article_count": len(preflight.target_eligible_ids),
        "target_article_ids": list(preflight.target_article_ids),
        "would_add_reports": would_add_reports,
        "would_add_articles": would_add_articles,
        "would_capture_article_ids": would_capture,
        "would_capture_count": len(would_capture),
        "would_promote": would_promote,
        "promotion": promotion,
        "reload_required": promotion == "performed",
        "database_sha256_before": before_sha256,
        "database_sha256_after": after_sha256,
        "backup_name": backup_name,
    }


def _candidate_missing_ids(database: Path, target_date: str) -> tuple[str, ...]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT a.article_id, a.current_content_version_id
            FROM report_appearances ra
            JOIN reports r ON r.report_id = ra.report_id
            JOIN articles a ON a.article_id = ra.article_id
            WHERE r.report_date = ? AND a.publication_eligible = 1
            ORDER BY ra.ordinal, a.article_id
            """,
            (target_date,),
        ).fetchall()
        return tuple(
            sorted(
                row["article_id"]
                for row in rows
                if not _has_complete_current_enrichment(
                    connection,
                    row["article_id"],
                    row["current_content_version_id"],
                )
            )
        )
    finally:
        connection.close()


def _validate_json_list(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        return False
    return isinstance(decoded, list) and all(isinstance(item, str) for item in decoded)


def _validate_candidate(candidate: Path, preflight: _Preflight) -> None:
    try:
        connection = sqlite3.connect(f"{candidate.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            _validate_database(connection)
            report = connection.execute(
                "SELECT report_date, filename, report_sha256 FROM reports WHERE report_date = ?",
                (preflight.target_date,),
            ).fetchone()
            if report is None or (
                report["report_date"] != preflight.target_date
                or report["filename"] != preflight.source_path.name
                or report["report_sha256"] != preflight.report.sha256
            ):
                raise WeeklyValidationError(
                    "weekly registry validation failed: target report identity is invalid"
                )
            expected_appearances: dict[str, tuple[Any, ...]] = {}
            for ordinal, article in enumerate(preflight.report.articles, start=1):
                normalized = canonical_url(article.url)
                if not normalized:
                    continue
                article_id = _stable_id("article", normalized)
                expected_appearances.setdefault(
                    article_id,
                    (
                        article_id,
                        article.url,
                        article.title,
                        article.summary,
                        article.section,
                        article.pillar,
                        ordinal,
                    ),
                )
            appearance_rows = connection.execute(
                """
                SELECT ra.article_id, d.raw_url, d.observed_title,
                       d.observed_summary, ra.section, ra.pillar, ra.ordinal
                FROM report_appearances ra
                JOIN reports r ON r.report_id = ra.report_id
                JOIN discoveries d ON d.discovery_id = ra.discovery_id
                WHERE r.report_date = ?
                ORDER BY ra.ordinal
                """,
                (preflight.target_date,),
            ).fetchall()
            actual_appearances = {
                row["article_id"]: tuple(row) for row in appearance_rows
            }
            if actual_appearances != expected_appearances:
                raise WeeklyValidationError(
                    "weekly registry validation failed: target article membership is invalid"
                )
            detail_rows = connection.execute(
                """
                SELECT a.article_id, a.canonical_url, a.current_content_version_id,
                       s.hostname, s.display_name,
                       e.status, e.summary, e.categories_json, e.keywords_json,
                       e.language, e.generator_kind, e.generator_name,
                       e.generator_version, e.generated_at,
                       (
                           SELECT f.fetch_status
                           FROM article_fetches f
                           WHERE f.article_id = a.article_id
                           ORDER BY f.fetched_at DESC, f.fetch_id DESC
                           LIMIT 1
                       ) AS latest_fetch_status
                FROM report_appearances ra
                JOIN reports r ON r.report_id = ra.report_id
                JOIN articles a ON a.article_id = ra.article_id
                JOIN sources s ON s.source_id = a.source_id
                LEFT JOIN article_enrichments e ON e.enrichment_id = (
                    SELECT candidate_e.enrichment_id
                    FROM article_enrichments candidate_e
                    WHERE candidate_e.content_version_id = a.current_content_version_id
                      AND candidate_e.status = 'complete'
                    ORDER BY candidate_e.generated_at DESC, candidate_e.enrichment_id DESC
                    LIMIT 1
                )
                WHERE r.report_date = ? AND a.publication_eligible = 1
                """,
                (preflight.target_date,),
            ).fetchall()
            if {row["article_id"] for row in detail_rows} != set(
                preflight.target_eligible_ids
            ):
                raise WeeklyValidationError(
                    "weekly registry validation failed: target article eligibility is invalid"
                )
            for row in detail_rows:
                required = (
                    row["canonical_url"],
                    row["current_content_version_id"],
                    row["hostname"],
                    row["display_name"],
                    row["summary"],
                    row["language"],
                    row["generator_kind"],
                    row["generator_name"],
                    row["generator_version"],
                    row["generated_at"],
                )
                if (
                    row["status"] != "complete"
                    or row["latest_fetch_status"] not in ("success", "not_modified")
                    or any(
                        not isinstance(value, str) or not value.strip()
                        for value in required
                    )
                    or not _validate_json_list(row["categories_json"])
                    or not _validate_json_list(row["keywords_json"])
                ):
                    raise WeeklyValidationError(
                        "weekly registry validation failed: target article detail is incomplete"
                    )
        finally:
            connection.close()
    except WeeklyValidationError:
        raise
    except (
        RegistryInputError,
        RegistryBuildError,
        sqlite3.DatabaseError,
        OSError,
    ) as exc:
        raise WeeklyValidationError(
            "weekly registry validation failed: candidate is invalid"
        ) from exc


def _backup_name(database: Path, now: datetime, live_sha256: str) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    return f"{database.name}.{stamp}.{live_sha256[:12]}.bak"


def _copy_exact(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)


def _safe_file_sha256(path: Path) -> str:
    try:
        return _file_sha256(path)
    except OSError as exc:
        raise WeeklyPreflightError(
            "weekly registry preflight failed: database could not be read"
        ) from exc


def _create_exact_backup(source: Path, destination: Path) -> None:
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    created = False
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        source_metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_metadata.st_mode):
            raise OSError("live database is not regular")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        while chunk := os.read(source_descriptor, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise OSError("short backup write")
                view = view[written:]
        os.fsync(destination_descriptor)
        after = os.fstat(source_descriptor)
        if (
            (source_metadata.st_dev, source_metadata.st_ino)
            != (after.st_dev, after.st_ino)
            or source_metadata.st_size != after.st_size
            or getattr(source_metadata, "st_mtime_ns", None)
            != getattr(after, "st_mtime_ns", None)
        ):
            raise OSError("live database changed while backing up")
    except Exception:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
            destination_descriptor = None
        if source_descriptor is not None:
            os.close(source_descriptor)
            source_descriptor = None
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)


def _validate_exact_backup(backup: Path, expected_sha256: str) -> None:
    try:
        if (
            _is_link_or_reparse(backup)
            or not backup.is_file()
            or _sqlite_entries(backup)
        ):
            raise WeeklyValidationError(
                "weekly registry validation failed: backup is invalid"
            )
        if _file_sha256(backup) != expected_sha256:
            raise WeeklyValidationError(
                "weekly registry validation failed: backup does not match the live database"
            )
        connection = sqlite3.connect(f"{backup.as_uri()}?mode=ro", uri=True)
        try:
            _validate_database(connection)
        finally:
            connection.close()
    except WeeklyValidationError:
        raise
    except (
        RegistryInputError,
        RegistryBuildError,
        sqlite3.DatabaseError,
        OSError,
    ) as exc:
        raise WeeklyValidationError(
            "weekly registry validation failed: backup is invalid"
        ) from exc


def _atomic_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _restore_if_needed(
    database: Path,
    backup: Path,
    expected_sha256: str,
    expected_mode: int,
    expected_uid: int | None,
    expected_gid: int | None,
) -> None:
    try:
        if database.is_file() and _file_sha256(database) == expected_sha256:
            os.chmod(database, expected_mode)
            if os.name == "posix" and expected_uid is not None and expected_gid is not None:
                os.chown(database, expected_uid, expected_gid)
            return
        descriptor, restore_name = tempfile.mkstemp(
            prefix=f".{database.name}.", suffix=".restore", dir=database.parent
        )
        os.close(descriptor)
        restore = Path(restore_name)
        try:
            _copy_exact(backup, restore)
            if _file_sha256(restore) != expected_sha256:
                raise OSError("restore copy mismatch")
            os.chmod(restore, expected_mode)
            if os.name == "posix" and expected_uid is not None and expected_gid is not None:
                os.chown(restore, expected_uid, expected_gid)
            _atomic_replace(restore, database)
            _fsync_parent(database)
        finally:
            restore.unlink(missing_ok=True)
        if _file_sha256(database) != expected_sha256:
            raise OSError("restore verification failed")
    except OSError as exc:
        raise WeeklyValidationError(
            "weekly registry promotion failed and restore could not be verified"
        ) from exc


def _promote(candidate: Path, database: Path, backup: Path, before_sha256: str) -> str:
    try:
        before_metadata = os.stat(database, follow_symlinks=False)
        before_mode = stat.S_IMODE(before_metadata.st_mode)
        before_uid = getattr(before_metadata, "st_uid", None)
        before_gid = getattr(before_metadata, "st_gid", None)
        os.chmod(candidate, before_mode)
        if os.name == "posix" and before_uid is not None and before_gid is not None:
            os.chown(candidate, before_uid, before_gid)
        live_changed = (
            _sqlite_entries(database) or _file_sha256(database) != before_sha256
        )
    except OSError as exc:
        raise WeeklyValidationError("weekly registry promotion failed") from exc
    if live_changed:
        raise RegistryLockError("live registry changed before weekly promotion")
    promoted = False
    try:
        _atomic_replace(candidate, database)
        promoted = True
        _fsync_parent(database)
        after_sha256 = _file_sha256(database)
        if after_sha256 == before_sha256:
            raise WeeklyValidationError(
                "weekly registry validation failed: promotion did not change the database"
            )
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        try:
            _validate_database(connection)
        finally:
            connection.close()
        return after_sha256
    except (RegistryLockError, WeeklyValidationError):
        if promoted:
            _restore_if_needed(
                database,
                backup,
                before_sha256,
                before_mode,
                before_uid,
                before_gid,
            )
        raise
    except (
        RegistryInputError,
        RegistryBuildError,
        sqlite3.DatabaseError,
        OSError,
    ) as exc:
        if promoted:
            _restore_if_needed(
                database,
                backup,
                before_sha256,
                before_mode,
                before_uid,
                before_gid,
            )
        raise WeeklyValidationError("weekly registry promotion failed") from exc


def weekly_sync(
    *,
    target_date: str,
    source_dir: str | Path,
    database: str | Path,
    artifact_root: str | Path,
    backup_dir: str | Path,
    lock_file: str | Path,
    publisher_ledger_dir: str | Path,
    expected_report_sha256: str | None = None,
    dry_run: bool = False,
    resolver: Resolver = _default_resolver,
    transport: Transport | None = None,
    clock: Clock = _default_clock,
    git_runner: GitRunner = _default_git_runner,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Safely update and enrich one exact Monday report in an external registry."""

    if (
        not isinstance(dry_run, bool)
        or timeout <= 0
        or (
            expected_report_sha256 is not None
            and (
                not isinstance(expected_report_sha256, str)
                or _SHA256.fullmatch(expected_report_sha256) is None
            )
        )
    ):
        raise WeeklyPreflightError(
            "weekly registry preflight failed: runtime options are invalid"
        )
    initial = _preflight(
        target_date=target_date,
        source_dir=source_dir,
        database=database,
        artifact_root=artifact_root,
        backup_dir=backup_dir,
        lock_file=lock_file,
        publisher_ledger_dir=publisher_ledger_dir,
        clock=clock,
        git_runner=git_runner,
        check_lock=True,
    )
    if (
        expected_report_sha256 is not None
        and initial.report.sha256 != expected_report_sha256
    ):
        raise WeeklyPreflightError(
            "weekly registry preflight failed: report SHA does not match the expected dry-run identity"
        )
    if dry_run:
        mutation_required = bool(
            initial.update_plan["mutation_required"]
            or initial.missing_enrichment_ids
        )
        if not mutation_required:
            _validate_candidate(initial.database, initial)
        return _result(
            initial,
            dry_run=True,
            status="no-op" if not mutation_required else "ok",
            reports_added=0,
            articles_added=0,
            articles_captured=0,
            promotion="not-needed",
            before_sha256=initial.live_sha256,
            after_sha256=initial.live_sha256,
        )

    with _exclusive_database_lock(initial.database):
        locked = _preflight(
            target_date=target_date,
            source_dir=source_dir,
            database=database,
            artifact_root=artifact_root,
            backup_dir=backup_dir,
            lock_file=lock_file,
            publisher_ledger_dir=publisher_ledger_dir,
            clock=clock,
            git_runner=git_runner,
            check_lock=False,
        )
        if (
            locked.live_sha256 != initial.live_sha256
            or locked.report.sha256 != initial.report.sha256
            or locked.update_plan != initial.update_plan
        ):
            raise RegistryLockError(
                "weekly registry inputs changed before candidate creation"
            )
        mutation_required = bool(
            locked.update_plan["mutation_required"] or locked.missing_enrichment_ids
        )
        if not mutation_required:
            _validate_candidate(locked.database, locked)
            return _result(
                locked,
                dry_run=False,
                status="no-op",
                reports_added=0,
                articles_added=0,
                articles_captured=0,
                promotion="not-needed",
                before_sha256=locked.live_sha256,
                after_sha256=locked.live_sha256,
            )

        candidate: Path | None = None
        scratch: Path | None = None
        backup_path: Path | None = None
        captured = 0
        try:
            try:
                descriptor, candidate_name = tempfile.mkstemp(
                    prefix=f".{locked.database.name}.",
                    suffix=".weekly-candidate",
                    dir=locked.database.parent,
                )
                os.close(descriptor)
                candidate = Path(candidate_name)
                scratch = Path(
                    tempfile.mkdtemp(
                        prefix=f".{locked.database.name}.", dir=locked.database.parent
                    )
                )
            except OSError as exc:
                raise WeeklyValidationError(
                    "weekly registry candidate workspace could not be created"
                ) from exc
            _copy_exact(locked.database, candidate)
            if _file_sha256(candidate) != locked.live_sha256:
                raise WeeklyValidationError(
                    "weekly registry validation failed: candidate copy is invalid"
                )
            try:
                update_result = update_registry(
                    locked.source_dir, candidate, scratch / "update-backups"
                )
            except (RegistryInputError, RegistryBuildError, RegistryLockError) as exc:
                raise WeeklyValidationError(
                    "weekly registry validation failed: candidate update failed"
                ) from exc
            imported = set(update_result.get("imported_reports", []))
            if not imported <= {locked.target_date}:
                raise WeeklyValidationError(
                    "weekly registry validation failed: candidate imported another report date"
                )

            try:
                capture_ids = _candidate_missing_ids(candidate, locked.target_date)
            except sqlite3.DatabaseError as exc:
                raise WeeklyValidationError(
                    "weekly registry validation failed: candidate capture plan is invalid"
                ) from exc
            if set(capture_ids) != set(locked.missing_enrichment_ids):
                raise WeeklyValidationError(
                    "weekly registry validation failed: candidate capture scope changed"
                )
            if capture_ids:
                capture_now = _clock_value(clock).isoformat().replace("+00:00", "Z")
                capture_transport = transport or PinnedTransport()
                succeeded_ids: list[str] = []
                for offset in range(0, len(capture_ids), MAX_BATCH):
                    batch = capture_ids[offset : offset + MAX_BATCH]
                    try:
                        capture_result = capture_enrich_registry(
                            candidate,
                            scratch / "capture-backups",
                            article_ids=batch,
                            refresh=True,
                            resolver=resolver,
                            transport=capture_transport,
                            clock=lambda: capture_now,
                            timeout=timeout,
                        )
                    except (
                        RegistryInputError,
                        RegistryBuildError,
                        RegistryLockError,
                    ) as exc:
                        raise WeeklyValidationError(
                            "weekly registry validation failed: candidate capture failed"
                        ) from exc
                    counts = capture_result.get("counts", {})
                    failed = int(counts.get("failed", 0)) + int(
                        counts.get("enrichment_failed", 0)
                    )
                    candidate_articles = capture_result.get("articles", [])
                    batch_successes = [
                        item["article_id"]
                        for item in candidate_articles
                        if item.get("status") not in {"failed", "enrichment_failed"}
                    ]
                    if capture_result.get("status") == "partial" or failed:
                        failures = [
                            {
                                "article_id": item["article_id"],
                                "status": item.get("status", "failed"),
                                **(
                                    {"error_code": item["error_code"]}
                                    if item.get("error_code")
                                    else {}
                                ),
                            }
                            for item in candidate_articles
                            if item.get("status") in {"failed", "enrichment_failed"}
                        ]
                        succeeded_ids.extend(batch_successes)
                        skipped_ids = list(capture_ids[offset + len(batch) :])
                        partial_result = _result(
                            locked,
                            dry_run=False,
                            status="partial",
                            reports_added=0,
                            articles_added=0,
                            articles_captured=len(succeeded_ids),
                            articles_failed=len(failures),
                            promotion="blocked",
                            before_sha256=locked.live_sha256,
                            after_sha256=locked.live_sha256,
                        )
                        partial_result["capture"] = {
                            "succeeded_article_ids": succeeded_ids,
                            "failures": failures,
                            "skipped_article_ids": skipped_ids,
                        }
                        raise WeeklyPartialError(
                            "weekly registry capture was partial; live registry was not changed",
                            result=partial_result,
                        )
                    batch_selected = int(capture_result.get("selected", 0))
                    if batch_selected != len(batch) or len(batch_successes) != len(batch):
                        raise WeeklyValidationError(
                            "weekly registry validation failed: capture result count is invalid"
                        )
                    succeeded_ids.extend(batch_successes)
                    captured += batch_selected

            if _sqlite_entries(candidate):
                raise WeeklyValidationError(
                    "weekly registry validation failed: candidate has SQLite sidecars"
                )
            _validate_candidate(candidate, locked)
            _revalidate_upstream(locked, clock=clock, git_runner=git_runner)

            try:
                locked.backup_dir.mkdir(parents=True, exist_ok=True)
                if (
                    _is_link_or_reparse(locked.backup_dir)
                    or not locked.backup_dir.is_dir()
                    or locked.backup_dir.resolve(strict=True) != locked.backup_dir
                ):
                    raise OSError("unsafe backup directory")
                backup_path = locked.backup_dir / _backup_name(
                    locked.database, _clock_value(clock), locked.live_sha256
                )
                if backup_path.exists() or backup_path.is_symlink():
                    raise OSError("backup already exists")
                _create_exact_backup(locked.database, backup_path)
                _fsync_parent(backup_path)
            except OSError as exc:
                raise WeeklyValidationError(
                    "weekly registry backup could not be created"
                ) from exc
            _validate_exact_backup(backup_path, locked.live_sha256)
            _revalidate_upstream(locked, clock=clock, git_runner=git_runner)
            after_sha256 = _promote(
                candidate, locked.database, backup_path, locked.live_sha256
            )
            return _result(
                locked,
                dry_run=False,
                status="ok",
                reports_added=len(locked.update_plan["new_reports"]),
                articles_added=len(locked.new_article_ids),
                articles_captured=captured,
                promotion="performed",
                before_sha256=locked.live_sha256,
                after_sha256=after_sha256,
                backup_name=backup_path.name,
            )
        finally:
            if candidate is not None:
                candidate.unlink(missing_ok=True)
                for sidecar in _sqlite_entries(candidate):
                    sidecar.unlink(missing_ok=True)
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)


def _restore_preflight(
    *,
    database: str | Path,
    backup: str | Path,
    expected_sha256: str,
    backup_dir: str | Path,
    lock_file: str | Path,
    check_lock: bool,
) -> _RestorePreflight:
    if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
        raise WeeklyPreflightError(
            "weekly registry restore preflight failed: expected backup SHA is invalid"
        )
    repository_root = Path(__file__).resolve().parents[1]
    database_path = _absolute_path(database, label="database")
    backup_path = _absolute_path(backup, label="backup")
    backup_dir_path = _absolute_path(backup_dir, label="backup directory")
    lock_path = _absolute_path(lock_file, label="lock file")
    for path, label in (
        (database_path, "database"),
        (backup_path, "backup"),
        (backup_dir_path, "backup directory"),
        (lock_path, "lock file"),
    ):
        _outside_repository(path, repository_root, label=label)
    if database_path == backup_path:
        raise WeeklyPreflightError(
            "weekly registry restore preflight failed: backup and database must differ"
        )
    for path, label in ((database_path, "database"), (backup_path, "backup")):
        if _is_link_or_reparse(path):
            raise WeeklyPreflightError(
                f"weekly registry restore preflight failed: {label} is unsafe"
            )
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise WeeklyPreflightError(
                f"weekly registry restore preflight failed: {label} is unavailable"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise WeeklyPreflightError(
                f"weekly registry restore preflight failed: {label} is not a regular file"
            )
    if _sqlite_entries(database_path):
        raise WeeklyPreflightError(
            "weekly registry restore preflight failed: database has active SQLite sidecars"
        )
    if backup_dir_path.exists() and not backup_dir_path.is_dir():
        raise WeeklyPreflightError(
            "weekly registry restore preflight failed: backup destination is invalid"
        )
    if backup_dir_path == database_path or backup_dir_path in database_path.parents:
        raise WeeklyPreflightError(
            "weekly registry restore preflight failed: backup directory contains the live database"
        )
    expected_lock = database_path.with_name(f"{database_path.name}.lock")
    if lock_path != expected_lock or _is_link_or_reparse(lock_path):
        raise WeeklyPreflightError(
            "weekly registry restore preflight failed: lock file is unsafe"
        )
    if check_lock and lock_path.exists():
        raise RegistryLockError("weekly registry restore is already locked")
    live_sha256 = _safe_file_sha256(database_path)
    live_mode = stat.S_IMODE(os.stat(database_path, follow_symlinks=False).st_mode)
    _validate_exact_backup(database_path, live_sha256)
    _validate_exact_backup(backup_path, expected_sha256)
    return _RestorePreflight(
        database=database_path,
        backup=backup_path,
        backup_dir=backup_dir_path,
        lock_file=lock_path,
        expected_sha256=expected_sha256,
        live_sha256=live_sha256,
        live_mode=live_mode,
    )


def restore_registry_backup(
    *,
    database: str | Path,
    backup: str | Path,
    expected_sha256: str,
    backup_dir: str | Path,
    lock_file: str | Path,
    clock: Clock = _default_clock,
) -> dict[str, Any]:
    """Atomically restore one exact verified Registry backup."""

    initial = _restore_preflight(
        database=database,
        backup=backup,
        expected_sha256=expected_sha256,
        backup_dir=backup_dir,
        lock_file=lock_file,
        check_lock=True,
    )
    if initial.live_sha256 == initial.expected_sha256:
        return {
            "status": "no-op",
            "promotion": "not-needed",
            "reload_required": False,
            "restored_database_sha256": initial.live_sha256,
            "replaced_database_sha256": initial.live_sha256,
            "rollback_backup_sha256": None,
            "rollback_backup_name": None,
        }
    with _exclusive_database_lock(initial.database):
        locked = _restore_preflight(
            database=database,
            backup=backup,
            expected_sha256=expected_sha256,
            backup_dir=backup_dir,
            lock_file=lock_file,
            check_lock=False,
        )
        if (
            locked.live_sha256 != initial.live_sha256
            or locked.expected_sha256 != initial.expected_sha256
            or locked.live_mode != initial.live_mode
        ):
            raise RegistryLockError(
                "weekly registry restore inputs changed before candidate creation"
            )
        candidate: Path | None = None
        rollback_backup: Path | None = None
        try:
            try:
                descriptor, candidate_name = tempfile.mkstemp(
                    prefix=f".{locked.database.name}.",
                    suffix=".restore-candidate",
                    dir=locked.database.parent,
                )
                os.close(descriptor)
                candidate = Path(candidate_name)
                _copy_exact(locked.backup, candidate)
                os.chmod(candidate, locked.live_mode)
            except OSError as exc:
                raise WeeklyValidationError(
                    "weekly registry restore candidate could not be created"
                ) from exc
            _validate_exact_backup(candidate, locked.expected_sha256)
            try:
                locked.backup_dir.mkdir(parents=True, exist_ok=True)
                if (
                    _is_link_or_reparse(locked.backup_dir)
                    or not locked.backup_dir.is_dir()
                    or locked.backup_dir.resolve(strict=True) != locked.backup_dir
                ):
                    raise OSError("unsafe backup directory")
                rollback_backup = locked.backup_dir / _backup_name(
                    locked.database, _clock_value(clock), locked.live_sha256
                )
                if rollback_backup.exists() or rollback_backup.is_symlink():
                    raise OSError("backup already exists")
                _create_exact_backup(locked.database, rollback_backup)
                _fsync_parent(rollback_backup)
            except OSError as exc:
                raise WeeklyValidationError(
                    "weekly registry restore rollback backup could not be created"
                ) from exc
            _validate_exact_backup(rollback_backup, locked.live_sha256)
            if (
                _sqlite_entries(locked.database)
                or _file_sha256(locked.database) != locked.live_sha256
                or _file_sha256(locked.backup) != locked.expected_sha256
            ):
                raise RegistryLockError(
                    "weekly registry restore inputs changed before promotion"
                )
            restored_sha256 = _promote(
                candidate,
                locked.database,
                rollback_backup,
                locked.live_sha256,
            )
            if restored_sha256 != locked.expected_sha256:
                raise WeeklyValidationError(
                    "weekly registry restore validation failed"
                )
            return {
                "status": "ok",
                "promotion": "performed",
                "reload_required": True,
                "restored_database_sha256": restored_sha256,
                "replaced_database_sha256": locked.live_sha256,
                "rollback_backup_sha256": locked.live_sha256,
                "rollback_backup_name": rollback_backup.name,
            }
        finally:
            if candidate is not None:
                candidate.unlink(missing_ok=True)
                for sidecar in _sqlite_entries(candidate):
                    sidecar.unlink(missing_ok=True)
