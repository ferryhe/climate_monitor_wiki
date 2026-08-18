from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from climate_monitor.dedupe import canonical_url
from climate_registry.annotations import ArticleAnnotation, load_article_annotations
from climate_registry.read_api import RegistryContractError, RegistryReader

from .artifacts import ARTIFACT_ONLY_DELIVERY_STATUS, load_report_artifact
from .errors import GenerationError, InputError
from .io import _fsync_parent, atomic_write_json
from .paths import (
    external_directory_root,
    external_file_path,
    require_separate_trees,
)
from .pdf import render_pdf
from .pipeline import _file_sha256, _manifest
from .report import Highlight, WeeklyReport, parse_weekly_report
from .summary import build_summary, write_summary


REPORT_FILE = re.compile(r"^climate-monitor-(\d{4}-\d{2}-\d{2})\.md$")
PROTECTED_DELIVERY_REPORT_DATE = "2026-08-17"


@dataclass(frozen=True)
class RegistryReport:
    report_id: str
    report_date: str
    filename: str
    title: str
    sha256: str
    cadence: str
    report_format: str
    checked: int | None
    succeeded: int | None
    failed: int | None


@dataclass(frozen=True)
class RegistryArticle:
    ordinal: int
    section: str
    pillar: str | None
    canonical_url: str
    raw_url: str
    observed_title: str
    observed_summary: str
    version_title: str
    version_summary: str


class _SkipReport(ValueError):
    def __init__(self, reason: str, evidence: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.evidence = evidence


def _validate_report_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InputError("backfill date must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise InputError("backfill date must be an ISO date")
    return value


def _validate_paths(
    sources_dir: Path,
    registry_db: Path,
    article_artifacts_dir: Path,
    output_dir: Path,
) -> tuple[Path, Path, Path, Path]:
    sources = _absolute_input_directory(sources_dir, "sources-dir")
    registry = external_file_path(registry_db, "registry-db")
    article_artifacts = _absolute_input_directory(
        article_artifacts_dir, "article-artifacts-dir"
    )
    output = external_directory_root(output_dir, "output-dir")
    if not sources.is_dir():
        raise InputError("sources-dir must be a readable directory")
    if not registry.is_file():
        raise InputError("registry-db must be a readable file")
    if not article_artifacts.is_dir():
        raise InputError("article-artifacts-dir must be a readable directory")
    require_separate_trees(sources, output, "sources-dir", "output-dir")
    require_separate_trees(
        article_artifacts,
        output,
        "article-artifacts-dir",
        "output-dir",
    )
    if registry == output or registry.is_relative_to(output):
        raise InputError("registry-db and output-dir must be separate")
    for suffix in ("-journal", "-wal", "-shm"):
        if Path(f"{registry}{suffix}").exists():
            raise InputError("registry-db has active SQLite sidecars")
    return sources, registry, article_artifacts, output


def _absolute_input_directory(path: Path, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise InputError(f"{label} path must be absolute")
    try:
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise InputError(f"{label} path could not be resolved") from exc
    if not resolved.is_dir():
        raise InputError(f"{label} must be a readable directory")
    return resolved


def _connect_registry(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=2,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        RegistryReader._validate_contract(connection)
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise InputError("registry-db failed integrity validation")
        return connection
    except InputError:
        if "connection" in locals():
            connection.close()
        raise
    except (OSError, sqlite3.Error, RegistryContractError) as exc:
        if "connection" in locals():
            connection.close()
        raise InputError("registry-db is unreadable or has an unsupported schema") from exc


def _registry_reports(connection: sqlite3.Connection) -> dict[str, RegistryReport]:
    try:
        rows = connection.execute(
            """
            SELECT report_id, report_date, filename, report_title, report_sha256,
                   cadence, report_format, sites_checked, sites_succeeded, sites_failed
            FROM reports ORDER BY report_date, report_id
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise InputError("registry-db report identities are unreadable") from exc
    output: dict[str, RegistryReport] = {}
    for row in rows:
        report_date = _validate_report_date(row["report_date"])
        if (
            row["filename"] != f"climate-monitor-{report_date}.md"
            or not isinstance(row["report_title"], str)
            or not row["report_title"].strip()
            or not isinstance(row["report_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", row["report_sha256"]) is None
        ):
            raise InputError("registry-db contains an invalid report identity")
        output[report_date] = RegistryReport(
            report_id=row["report_id"],
            report_date=report_date,
            filename=row["filename"],
            title=row["report_title"],
            sha256=row["report_sha256"],
            cadence=row["cadence"],
            report_format=row["report_format"],
            checked=row["sites_checked"],
            succeeded=row["sites_succeeded"],
            failed=row["sites_failed"],
        )
    return output


def _registry_articles(
    connection: sqlite3.Connection, report_id: str
) -> tuple[RegistryArticle, ...]:
    try:
        rows = connection.execute(
            """
            SELECT ra.ordinal, ra.section, ra.pillar, a.canonical_url,
                   d.raw_url, d.observed_title, d.observed_summary,
                   av.observed_title AS version_title,
                   av.observed_summary AS version_summary
            FROM report_appearances ra
            JOIN articles a ON a.article_id = ra.article_id
            JOIN discoveries d ON d.discovery_id = ra.discovery_id
            JOIN article_versions av ON av.version_id = ra.version_id
            WHERE ra.report_id = ?
            ORDER BY ra.ordinal, a.article_id
            """,
            (report_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise InputError("registry-db report membership is unreadable") from exc
    return tuple(
        RegistryArticle(
            ordinal=row["ordinal"],
            section=row["section"],
            pillar=row["pillar"],
            canonical_url=row["canonical_url"],
            raw_url=row["raw_url"],
            observed_title=row["observed_title"],
            observed_summary=row["observed_summary"],
            version_title=row["version_title"],
            version_summary=row["version_summary"],
        )
        for row in rows
    )


def _source_dates(sources_dir: Path) -> set[str]:
    output: set[str] = set()
    try:
        paths = sources_dir.glob("climate-monitor-*.md")
        for path in paths:
            match = REPORT_FILE.fullmatch(path.name)
            if match is not None:
                try:
                    output.add(_validate_report_date(match.group(1)))
                except InputError:
                    continue
    except OSError as exc:
        raise InputError("sources-dir is unreadable") from exc
    return output


def _source_report(sources_dir: Path, report_date: str) -> WeeklyReport:
    path = sources_dir / f"climate-monitor-{report_date}.md"
    if not path.is_file():
        raise _SkipReport("missing_markdown")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise _SkipReport("unreadable_markdown") from exc
    if not (
        "Weekly Climate" in text
        and "Pillar A" in text
        and "Pillar B" in text
    ):
        raise _SkipReport("legacy_report_incomplete_for_backfill")
    try:
        return parse_weekly_report(path, raw=raw)
    except InputError as exc:
        message = str(exc).casefold()
        if (
            "checked, succeeded, and failed" in message
            or "succeeded and failed counts" in message
        ):
            raise _SkipReport("missing_monitoring_statistics") from exc
        raise _SkipReport("invalid_canonical_markdown") from exc


def _validate_report_identity(
    source: WeeklyReport, registry: RegistryReport
) -> None:
    if source.sha256 != registry.sha256:
        raise _SkipReport(
            "source_sha_mismatch",
            {
                "registry_sha256": registry.sha256,
                "source_sha256": source.sha256,
            },
        )
    identity_fields = {
        "report_date": source.report_date == registry.report_date,
        "filename": source.filename == registry.filename,
        "report_title": source.title == registry.title,
    }
    if not all(identity_fields.values()):
        raise _SkipReport(
            "source_registry_identity_mismatch",
            {"fields": [key for key, matches in identity_fields.items() if not matches]},
        )
    source_sites = {
        "checked": source.checked,
        "succeeded": source.succeeded,
        "failed": source.failed,
    }
    registry_sites = {
        "checked": registry.checked,
        "succeeded": registry.succeeded,
        "failed": registry.failed,
    }
    if source_sites != registry_sites:
        raise _SkipReport(
            "monitoring_statistics_conflict",
            {"registry": registry_sites, "source": source_sites},
        )
    if registry.cadence != "weekly" or registry.report_format != "weekly-pillars-v1":
        raise _SkipReport("legacy_report_incomplete_for_backfill")


def _normalized_source_articles(source: WeeklyReport) -> tuple[str, ...]:
    try:
        normalized = tuple(canonical_url(item.url) for item in source.highlights)
    except (TypeError, UnicodeError, ValueError) as exc:
        raise _SkipReport("invalid_source_article_url") from exc
    if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
        raise _SkipReport("duplicate_or_ambiguous_article_mapping")
    return normalized


def _validated_membership(
    source: WeeklyReport,
    registry_articles: tuple[RegistryArticle, ...],
) -> tuple[str, ...]:
    normalized = _normalized_source_articles(source)
    if len(registry_articles) != len(source.highlights):
        raise _SkipReport(
            "registry_membership_conflict",
            {"registry": len(registry_articles), "source": len(source.highlights)},
        )
    for ordinal, (source_item, source_url, registry_item) in enumerate(
        zip(source.highlights, normalized, registry_articles), start=1
    ):
        expected_section = f"pillar-{source_item.pillar.casefold()}"
        fields = {
            "ordinal": registry_item.ordinal == ordinal,
            "canonical_url": registry_item.canonical_url == source_url,
            "raw_url": registry_item.raw_url == source_item.url,
            "pillar": registry_item.pillar == source_item.pillar,
            "section": registry_item.section == expected_section,
            "discovery_title": registry_item.observed_title == source_item.title,
            "discovery_summary": registry_item.observed_summary == source_item.summary,
            "version_title": registry_item.version_title == source_item.title,
            "version_summary": registry_item.version_summary == source_item.summary,
        }
        conflicting_fields = [key for key, matches in fields.items() if not matches]
        if conflicting_fields:
            raise _SkipReport(
                "registry_membership_conflict",
                {
                    "ordinal": ordinal,
                    "conflicting_fields": conflicting_fields,
                },
            )
    return normalized


def _enriched_report(
    source: WeeklyReport,
    normalized_urls: tuple[str, ...],
    annotations: dict[str, ArticleAnnotation],
) -> WeeklyReport:
    matched = [annotations.get(url) for url in normalized_urls]
    if any(item is None for item in matched):
        raise _SkipReport(
            "incomplete_article_artifacts",
            {
                "expected": len(normalized_urls),
                "matched": sum(item is not None for item in matched),
            },
        )
    highlights = tuple(
        Highlight(
            pillar=source_item.pillar,
            title=annotation.title,
            summary=annotation.summary,
            url=source_item.url,
            categories=annotation.categories,
            keywords=annotation.keywords,
        )
        for source_item, annotation in zip(source.highlights, matched)
        if annotation is not None
    )
    enriched = WeeklyReport(
        path=source.path,
        filename=source.filename,
        report_date=source.report_date,
        title=source.title,
        sha256=source.sha256,
        checked=source.checked,
        succeeded=source.succeeded,
        failed=source.failed,
        monitoring_notes=source.monitoring_notes,
        highlights=highlights,
        original_links=source.original_links,
    )
    summary = build_summary(enriched)
    if (
        not summary["executive_summary"]
        or any(not item.strip() for item in summary["executive_summary"])
        or not summary["monitoring_notes"]
        or any(not item.strip() for item in summary["monitoring_notes"])
    ):
        raise _SkipReport("content_quality_gate_failed")
    return enriched


def _artifact_is_valid(
    output_dir: Path, report: WeeklyReport | RegistryReport
) -> bool:
    return (
        load_report_artifact(
            output_dir,
            report_date=report.report_date,
            report_filename=report.filename,
            report_title=report.title,
            report_sha256=report.sha256,
            include_pdf_bytes=False,
        )
        is not None
    )


def _write_candidate(staging_root: Path, report: WeeklyReport) -> Path:
    candidate = staging_root / report.report_date / report.sha256
    candidate.mkdir(parents=True)
    summary = build_summary(report)
    summary_path = candidate / "summary.json"
    pdf_name = f"climate-monitor-{report.report_date}.pdf"
    pdf_path = candidate / pdf_name
    manifest_path = candidate / "manifest.json"
    write_summary(summary, summary_path)
    render_pdf(summary, pdf_path)
    summary_sha256 = _file_sha256(summary_path)
    pdf_sha256 = _file_sha256(pdf_path)
    atomic_write_json(
        manifest_path,
        _manifest(
            summary,
            {"status": ARTIFACT_ONLY_DELIVERY_STATUS, "recipients": []},
            summary_sha256=summary_sha256,
            pdf_name=pdf_name,
            pdf_sha256=pdf_sha256,
        ),
    )
    if not _artifact_is_valid(staging_root, report):
        raise GenerationError("staged artifact failed validation")
    return candidate


def _publish_candidate(candidate: Path, destination: Path) -> None:
    date_dir = destination.parent
    root = date_dir.parent.resolve(strict=True)
    created_date_dir = not date_dir.exists()
    date_descriptor: int | None = None
    lock_descriptor: int | None = None
    lock_name = f".{destination.name}.backfill.lock"
    lock_path = date_dir / lock_name
    published = False
    try:
        date_dir.mkdir(parents=True, exist_ok=True)
        if date_dir.is_symlink() or date_dir.resolve(strict=True).parent != root:
            raise GenerationError("artifact date directory escapes output root")

        lock_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(
            os, "O_NOFOLLOW", 0
        )
        if os.name == "posix":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
                os, "O_NOFOLLOW", 0
            )
            date_descriptor = os.open(date_dir, directory_flags)
            lock_descriptor = os.open(
                lock_name, lock_flags, 0o600, dir_fd=date_descriptor
            )
        else:
            lock_descriptor = os.open(lock_path, lock_flags, 0o600)

        if date_descriptor is not None:
            try:
                os.stat(
                    destination.name,
                    dir_fd=date_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise GenerationError("artifact destination appeared during publish")
        elif destination.exists() or destination.is_symlink():
            raise GenerationError("artifact destination appeared during publish")
        _fsync_parent(candidate)
        if date_descriptor is not None:
            os.rename(candidate, destination.name, dst_dir_fd=date_descriptor)
        else:
            os.rename(candidate, destination)
        published = True
        try:
            if date_descriptor is not None:
                os.fsync(date_descriptor)
            else:
                _fsync_parent(destination)
        except OSError as exc:
            try:
                if date_descriptor is not None:
                    os.rename(
                        destination.name,
                        candidate,
                        src_dir_fd=date_descriptor,
                    )
                else:
                    os.rename(destination, candidate)
                published = False
            except OSError as rollback_error:
                raise GenerationError(
                    "could not confirm or roll back atomic artifact publish"
                ) from rollback_error
            raise GenerationError("could not durably publish artifact") from exc
    except GenerationError:
        raise
    except OSError as exc:
        raise GenerationError("could not atomically publish artifact") from exc
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
            try:
                if date_descriptor is not None:
                    os.unlink(lock_name, dir_fd=date_descriptor)
                else:
                    lock_path.unlink()
            except (FileNotFoundError, OSError):
                pass
        if date_descriptor is not None:
            os.close(date_descriptor)
        if created_date_dir and not published:
            try:
                date_dir.rmdir()
            except OSError:
                pass


def _generate(
    output_dir: Path,
    report: WeeklyReport,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="climate-delivery-backfill-") as temporary:
            _write_candidate(Path(temporary), report)
        return
    created_output = not output_dir.exists()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GenerationError("could not prepare output directory") from exc
    try:
        with tempfile.TemporaryDirectory(prefix=".backfill-", dir=output_dir) as temporary:
            candidate = _write_candidate(Path(temporary), report)
            _publish_candidate(
                candidate, output_dir / report.report_date / report.sha256
            )
    except Exception:
        if created_output:
            try:
                output_dir.rmdir()
            except OSError:
                pass
        raise


def _entry(
    report: WeeklyReport,
    *,
    action: str,
) -> dict[str, Any]:
    return {
        "report_date": report.report_date,
        "report_sha256": report.sha256,
        "pillar_a_updates": sum(item.pillar == "A" for item in report.highlights),
        "pillar_b_updates": sum(item.pillar == "B" for item in report.highlights),
        "action": action,
    }


def _skip_entry(report_date: str, skipped: _SkipReport) -> dict[str, Any]:
    value: dict[str, Any] = {"report_date": report_date, "reason": skipped.reason}
    if skipped.evidence is not None:
        value["evidence"] = skipped.evidence
    return value


def backfill_reports(
    *,
    sources_dir: Path,
    registry_db: Path,
    article_artifacts_dir: Path,
    output_dir: Path,
    report_date: str | None,
    all_missing: bool,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create deterministic historical artifacts without delivery side effects."""
    if (report_date is None) == (not all_missing):
        raise InputError("exactly one of --date or --all-missing is required")
    sources, registry_path, article_artifacts, output = _validate_paths(
        sources_dir, registry_db, article_artifacts_dir, output_dir
    )
    if report_date is not None:
        report_date = _validate_report_date(report_date)

    connection = _connect_registry(registry_path)
    try:
        registry_reports = _registry_reports(connection)
        dates = (
            sorted(_source_dates(sources) | set(registry_reports))
            if all_missing
            else [report_date]
        )
        try:
            annotation_batches = len(
                list(article_artifacts.glob("articles-*.json"))
            )
            annotations = load_article_annotations(article_artifacts)
        except OSError as exc:
            raise InputError("article-artifacts-dir is unreadable") from exc
        generated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        already_valid: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []

        for selected_date in dates:
            if selected_date is None:
                continue
            registry_report = registry_reports.get(selected_date)
            if registry_report is None:
                skipped.append(
                    {
                        "report_date": selected_date,
                        "reason": "missing_registry_membership",
                    }
                )
                continue
            try:
                if _artifact_is_valid(output, registry_report):
                    already_valid.append(
                        {
                            "report_date": registry_report.report_date,
                            "report_sha256": registry_report.sha256,
                        }
                    )
                    continue
                source = _source_report(sources, selected_date)
                _validate_report_identity(source, registry_report)
                destination = output / source.report_date / source.sha256
                if source.report_date == PROTECTED_DELIVERY_REPORT_DATE:
                    skipped.append(
                        {
                            "report_date": source.report_date,
                            "reason": "protected_delivery_artifact_unavailable",
                        }
                    )
                    continue
                if destination.exists() or destination.is_symlink():
                    if _artifact_is_valid(output, registry_report):
                        already_valid.append(
                            {
                                "report_date": registry_report.report_date,
                                "report_sha256": registry_report.sha256,
                            }
                        )
                        continue
                    failed.append(
                        {
                            "report_date": source.report_date,
                            "report_sha256": source.sha256,
                            "reason": "invalid_existing_artifact",
                        }
                    )
                    continue
                registry_articles = _registry_articles(
                    connection, registry_report.report_id
                )
                normalized = _validated_membership(source, registry_articles)
                if not annotations:
                    raise _SkipReport(
                        "invalid_or_ambiguous_article_artifacts",
                        {"batch_files": annotation_batches},
                    )
                enriched = _enriched_report(source, normalized, annotations)
                _generate(output, enriched, dry_run=dry_run)
                generated.append(
                    _entry(
                        enriched,
                        action="would_generate" if dry_run else "generated",
                    )
                )
            except _SkipReport as exc:
                skipped.append(_skip_entry(selected_date, exc))
            except (GenerationError, InputError, OSError, ValueError):
                failed.append(
                    {
                        "report_date": selected_date,
                        "report_sha256": registry_report.sha256,
                        "reason": "artifact_generation_failed",
                    }
                )

        result = {
            "status": "dry-run" if dry_run else "complete",
            "mode": "all-missing" if all_missing else "single-date",
            "generated": generated,
            "skipped": skipped,
            "already_valid": already_valid,
            "failed": failed,
        }
        result["counts"] = {
            key: len(result[key])
            for key in ("generated", "skipped", "already_valid", "failed")
        }
        return result
    finally:
        connection.close()
