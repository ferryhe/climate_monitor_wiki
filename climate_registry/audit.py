from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from climate_monitor.dedupe import canonical_title, canonical_url

from . import schema
from .classification import classify_document
from .errors import RegistryBuildError, RegistryInputError
from .reports import ParsedReport, parse_report_directory


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _content_fingerprint(title: str, summary: str) -> str:
    normalized = f"{canonical_title(title)}\n{canonical_title(summary)}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _json_write(payload: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _validate_inputs(source_dir: Path, database: Path, output_dir: Path) -> tuple[Path, Path, Path]:
    source_dir = source_dir.resolve()
    database = database.resolve()
    output_dir = output_dir.resolve()
    if not source_dir.is_dir():
        raise RegistryInputError(f"source directory does not exist: {source_dir}")
    if database.exists():
        raise RegistryInputError(f"database already exists; refusing to modify it: {database}")
    if output_dir.exists():
        raise RegistryInputError(f"output directory already exists; refusing to overwrite it: {output_dir}")
    if database == source_dir or source_dir in database.parents:
        raise RegistryInputError("database must be outside the read-only source directory")
    if output_dir == source_dir or source_dir in output_dir.parents:
        raise RegistryInputError("output directory must be outside the read-only source directory")
    if database == output_dir or database in output_dir.parents or output_dir in database.parents:
        raise RegistryInputError("database and output directory must be separate destinations")
    reports = list(source_dir.glob("climate-monitor-*.md"))
    if not reports:
        raise RegistryInputError(f"no climate-monitor Markdown reports found in: {source_dir}")
    return source_dir, database, output_dir


def _insert_report(connection: sqlite3.Connection, report: ParsedReport) -> None:
    report_id = f"report-{report.report_date}"
    connection.execute(
        """
        INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_id,
            report.report_date,
            report.path.name,
            report.title,
            report.sha256,
            report.cadence,
            report.report_format,
            report.sites_checked,
            report.sites_succeeded,
            report.sites_failed,
            json.dumps(report.warnings, separators=(",", ":")),
        ),
    )

    selected_in_report: dict[str, str] = {}
    seen_versions: dict[str, set[str]] = {}
    for ordinal, item in enumerate(report.articles, start=1):
        normalized_url = canonical_url(item.url)
        if not normalized_url:
            continue
        article_id = _stable_id("article", normalized_url)
        hostname = (urlparse(normalized_url).hostname or "unknown").removeprefix("www.")
        source_id = _stable_id("source", hostname)
        fingerprint = _content_fingerprint(item.title, item.summary)
        version_id = _stable_id("version", f"{article_id}\n{fingerprint}")
        discovery_id = _stable_id("discovery", f"{report_id}\n{ordinal}\n{item.url}")
        policy = classify_document(normalized_url)

        connection.execute(
            """
            INSERT INTO sources(source_id, hostname, display_name, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen = MAX(last_seen, excluded.last_seen)
            """,
            (source_id, hostname, hostname, report.report_date, report.report_date),
        )
        connection.execute(
            """
            INSERT INTO articles(
                article_id, canonical_url, source_id, first_seen, last_seen, current_version_id,
                document_kind, publication_eligible, exclusion_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen = MAX(last_seen, excluded.last_seen),
                document_kind = excluded.document_kind,
                publication_eligible = excluded.publication_eligible,
                exclusion_reason = excluded.exclusion_reason
            """,
            (
                article_id,
                normalized_url,
                source_id,
                report.report_date,
                report.report_date,
                version_id,
                policy.document_kind,
                int(policy.publication_eligible),
                policy.exclusion_reason,
            ),
        )
        connection.execute(
            """
            INSERT INTO url_aliases(raw_url, canonical_url, article_id, first_seen, last_seen, times_seen)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(raw_url) DO UPDATE SET
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen = MAX(last_seen, excluded.last_seen),
                times_seen = times_seen + 1
            """,
            (item.url, normalized_url, article_id, report.report_date, report.report_date),
        )
        connection.execute(
            """
            INSERT INTO article_versions(
                version_id, article_id, observed_title, canonical_title, observed_summary,
                content_fingerprint, content_basis, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, 'report-title-summary', ?, ?)
            ON CONFLICT(version_id) DO UPDATE SET
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen = MAX(last_seen, excluded.last_seen)
            """,
            (
                version_id,
                article_id,
                item.title,
                canonical_title(item.title),
                item.summary,
                fingerprint,
                report.report_date,
                report.report_date,
            ),
        )

        duplicate_of = selected_in_report.get(article_id)
        selected = int(duplicate_of is None)
        connection.execute(
            """
            INSERT INTO discoveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                discovery_id,
                report_id,
                ordinal,
                item.section,
                item.pillar,
                article_id,
                version_id,
                item.url,
                item.title,
                item.summary,
                selected,
                duplicate_of,
            ),
        )
        if duplicate_of is not None:
            continue

        connection.execute(
            """
            UPDATE articles
            SET current_version_id = ?
            WHERE article_id = ? AND last_seen <= ?
            """,
            (version_id, article_id, report.report_date),
        )

        previous_versions = seen_versions.setdefault(
            article_id,
            {
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT d.version_id
                    FROM discoveries d JOIN reports r ON r.report_id = d.report_id
                    WHERE d.article_id = ? AND r.report_date < ?
                    """,
                    (article_id, report.report_date),
                )
            },
        )
        if not previous_versions:
            disposition, observation_status = "new", "new_article"
        elif version_id in previous_versions:
            disposition, observation_status = "previously-seen", "previously_seen"
        else:
            disposition, observation_status = "updated", "new_report_representation"
        previous_versions.add(version_id)
        connection.execute(
            """
            INSERT INTO report_appearances(
                report_id, article_id, version_id, discovery_id, section, pillar, ordinal,
                disposition, observation_status, external_content_change
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown')
            """,
            (
                report_id,
                article_id,
                version_id,
                discovery_id,
                item.section,
                item.pillar,
                ordinal,
                disposition,
                observation_status,
            ),
        )
        selected_in_report[article_id] = discovery_id


def _appearance_rows(connection: sqlite3.Connection, where: str = "", params: tuple = ()) -> list[dict]:
    query = f"""
        SELECT r.report_date, r.filename, a.article_id, a.canonical_url,
               v.version_id, d.raw_url, d.observed_title, d.observed_summary,
               d.section, d.pillar, d.ordinal, ra.disposition, ra.observation_status,
               ra.external_content_change, a.document_kind, a.publication_eligible,
               a.exclusion_reason
        FROM report_appearances ra
        JOIN reports r ON r.report_id = ra.report_id
        JOIN articles a ON a.article_id = ra.article_id
        JOIN article_versions v ON v.version_id = ra.version_id
        JOIN discoveries d ON d.discovery_id = ra.discovery_id
        {where}
        ORDER BY r.report_date, d.ordinal
    """
    keys = (
        "report_date",
        "filename",
        "article_id",
        "canonical_url",
        "version_id",
        "url",
        "title",
        "summary",
        "section",
        "pillar",
        "ordinal",
        "disposition",
        "observation_status",
        "external_content_change",
        "document_kind",
        "publication_eligible",
        "exclusion_reason",
    )
    output = [dict(zip(keys, row, strict=True)) for row in connection.execute(query, params)]
    for item in output:
        item["publication_eligible"] = bool(item["publication_eligible"])
    return output


def _duplicate_report(connection: sqlite3.Connection) -> dict:
    appearances = _appearance_rows(connection)
    by_article: dict[str, list[dict]] = {}
    for row in appearances:
        by_article.setdefault(row["article_id"], []).append(row)
    repeated = [
        {
            "article_id": rows[0]["article_id"],
            "canonical_url": rows[0]["canonical_url"],
            "appearances": rows,
        }
        for rows in by_article.values()
        if len(rows) > 1
    ]
    repeated.sort(key=lambda item: item["canonical_url"])

    title_groups: dict[str, dict[str, dict]] = {}
    for row in connection.execute(
        """
        SELECT DISTINCT v.canonical_title, a.article_id, a.canonical_url, v.observed_title
        FROM article_versions v JOIN articles a ON a.article_id = v.article_id
        WHERE v.canonical_title <> ''
        ORDER BY v.canonical_title, a.canonical_url
        """
    ):
        title_groups.setdefault(row[0], {})[row[1]] = {
            "article_id": row[1],
            "canonical_url": row[2],
            "observed_title": row[3],
        }
    title_collisions = [
        {"canonical_title": key, "articles": list(group.values())}
        for key, group in title_groups.items()
        if len(group) > 1
    ]

    discovery_history: dict[str, list[dict]] = {}
    for row in connection.execute(
        """
        SELECT d.article_id, a.canonical_url, r.report_date, d.pillar, d.section,
               d.ordinal, d.raw_url, d.observed_title, d.version_id, d.selected
        FROM discoveries d
        JOIN articles a ON a.article_id = d.article_id
        JOIN reports r ON r.report_id = d.report_id
        ORDER BY r.report_date, d.ordinal
        """
    ):
        discovery_history.setdefault(row[0], []).append(
            {
                "article_id": row[0],
                "canonical_url": row[1],
                "report_date": row[2],
                "pillar": row[3],
                "section": row[4],
                "ordinal": row[5],
                "url": row[6],
                "title": row[7],
                "version_id": row[8],
                "selected": bool(row[9]),
            }
        )
    cross_pillar = []
    for article_id, discoveries in discovery_history.items():
        pillars = sorted({item["pillar"] for item in discoveries if item["pillar"]})
        if pillars == ["A", "B"]:
            cross_pillar.append(
                {
                    "article_id": article_id,
                    "canonical_url": discoveries[0]["canonical_url"],
                    "pillars": pillars,
                    "discoveries": discoveries,
                }
            )
    cross_pillar.sort(key=lambda item: item["canonical_url"])

    content_versions = []
    for article_id, discoveries in discovery_history.items():
        versions = sorted({row["version_id"] for row in discoveries})
        if len(versions) > 1:
            content_versions.append(
                {
                    "article_id": article_id,
                    "canonical_url": discoveries[0]["canonical_url"],
                    "version_ids": versions,
                    "discoveries": discoveries,
                }
            )
    content_versions.sort(key=lambda item: item["canonical_url"])

    within_report = [
        {
            "report_date": row[0],
            "article_id": row[1],
            "canonical_url": row[2],
            "duplicate_ordinal": row[3],
            "duplicate_of": row[4],
        }
        for row in connection.execute(
            """
            SELECT r.report_date, a.article_id, a.canonical_url, d.ordinal, d.duplicate_of
            FROM discoveries d
            JOIN reports r ON r.report_id = d.report_id
            JOIN articles a ON a.article_id = d.article_id
            WHERE d.selected = 0
            ORDER BY r.report_date, d.ordinal
            """
        )
    ]
    report_count = connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    article_count = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    discovery_count = connection.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0]
    return {
        "schema_version": schema.MIGRATIONS[-1][0],
        "content_version_basis": "normalized title and summary observed in each canonical Markdown report; not external page content",
        "counts": {
            "reports": report_count,
            "discoveries": discovery_count,
            "unique_articles": article_count,
            "repeated_url_articles": len(repeated),
            "repeat_appearances": sum(len(item["appearances"]) - 1 for item in repeated),
            "within_report_duplicates": len(within_report),
            "title_collisions": len(title_collisions),
            "cross_pillar_articles": len(cross_pillar),
            "content_version_changes": len(content_versions),
        },
        "repeated_urls": repeated,
        "within_report_duplicates": within_report,
        "title_collisions": title_collisions,
        "cross_pillar_articles": cross_pillar,
        "content_versions": content_versions,
    }


def _weekly_manifest(connection: sqlite3.Connection, report_date: str) -> dict:
    report = connection.execute(
        """
        SELECT report_date, filename, report_title, report_sha256, cadence, report_format,
               sites_checked, sites_succeeded, sites_failed, parse_warnings_json
        FROM reports WHERE report_date = ?
        """,
        (report_date,),
    ).fetchone()
    if report is None:
        raise RegistryBuildError(f"report missing while writing manifest: {report_date}")
    all_articles = _appearance_rows(connection, "WHERE r.report_date = ?", (report_date,))
    articles = [article for article in all_articles if article["publication_eligible"]]
    excluded_articles = [article for article in all_articles if not article["publication_eligible"]]
    dispositions = {name: 0 for name in ("new", "updated", "previously-seen")}
    for article in articles:
        dispositions[article["disposition"]] += 1
    duplicate_count = connection.execute(
        """
        SELECT COUNT(*) FROM discoveries d JOIN reports r ON r.report_id = d.report_id
        WHERE r.report_date = ? AND d.selected = 0
        """,
        (report_date,),
    ).fetchone()[0]
    return {
        "schema_version": schema.MIGRATIONS[-1][0],
        "content_version_basis": "normalized title and summary observed in this canonical Markdown report",
        "report": {
            "date": report[0],
            "filename": report[1],
            "title": report[2],
            "sha256": report[3],
            "cadence": report[4],
            "format": report[5],
            "monitoring": {"checked": report[6], "succeeded": report[7], "failed": report[8]},
            "parse_warnings": json.loads(report[9]),
        },
        "counts": {
            "articles": len(articles),
            "eligible_articles": len(articles),
            "excluded_articles": len(excluded_articles),
            "new": dispositions["new"],
            "updated": dispositions["updated"],
            "previously_seen": dispositions["previously-seen"],
            "within_report_duplicates": duplicate_count,
            "pillar_a": sum(article["pillar"] == "A" for article in articles),
            "pillar_b": sum(article["pillar"] == "B" for article in articles),
        },
        "articles": articles,
        "excluded_articles": excluded_articles,
    }


def refresh_article_policy(connection: sqlite3.Connection) -> None:
    articles = connection.execute("SELECT article_id, canonical_url FROM articles").fetchall()
    for article_id, url in articles:
        policy = classify_document(url)
        connection.execute(
            """
            UPDATE articles
            SET document_kind = ?, publication_eligible = ?, exclusion_reason = ?
            WHERE article_id = ?
            """,
            (policy.document_kind, int(policy.publication_eligible), policy.exclusion_reason, article_id),
        )


def _populate(connection: sqlite3.Connection, reports: tuple[ParsedReport, ...]) -> None:
    with connection:
        for report in reports:
            _insert_report(connection, report)


def build_audit_registry(source_dir: Path, database: Path, output_dir: Path) -> dict:
    """Build a new, read-only-to-sources registry snapshot and JSON audit outputs."""

    source_dir, database, output_dir = _validate_inputs(source_dir, database, output_dir)
    try:
        # The source tree is already accepted publication history and can
        # legitimately include an explicitly published off-cycle capture.
        # Monday-only policy remains at the ingestion boundary; rebuilding the
        # read-only audit/Render registry must preserve that history.
        reports = parse_report_directory(source_dir, allow_offcycle=True)
    except Exception as exc:
        raise RegistryBuildError(f"could not parse report history: {exc}") from exc
    database.parent.mkdir(parents=True, exist_ok=True)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_database_root = Path(tempfile.mkdtemp(prefix="climate-registry-db-", dir=database.parent))
    temp_output_root = Path(tempfile.mkdtemp(prefix="climate-registry-output-", dir=output_dir.parent))
    temp_database = temp_database_root / database.name
    temp_output = temp_output_root / "audit-output"
    try:
        connection = sqlite3.connect(temp_database)
        try:
            schema.apply_migrations(connection)
            refresh_article_policy(connection)
            _populate(connection, reports)
            connection.execute("PRAGMA optimize")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != "ok" or foreign_keys:
                raise RegistryBuildError("SQLite integrity validation failed")
            duplicate_report = _duplicate_report(connection)
            weekly_dates = [
                row[0]
                for row in connection.execute(
                    "SELECT report_date FROM reports WHERE cadence = 'weekly' ORDER BY report_date"
                )
            ]
            manifests = {date: _weekly_manifest(connection, date) for date in weekly_dates}
        finally:
            connection.close()

        _json_write(duplicate_report, temp_output / "duplicate-report.json")
        for report_date, manifest in manifests.items():
            _json_write(manifest, temp_output / "weekly-manifests" / f"weekly-manifest-{report_date}.json")
        os.replace(temp_database, database)
        os.replace(temp_output, output_dir)
        return {
            "status": "success",
            "database": str(database),
            "output_dir": str(output_dir),
            "reports": len(reports),
            "weekly_manifests": len(manifests),
            **duplicate_report["counts"],
        }
    except (RegistryInputError, RegistryBuildError):
        raise
    except Exception as exc:
        if database.exists():
            database.unlink()
        if output_dir.exists():
            shutil.rmtree(output_dir)
        raise RegistryBuildError(str(exc)) from exc
    finally:
        shutil.rmtree(temp_database_root, ignore_errors=True)
        shutil.rmtree(temp_output_root, ignore_errors=True)
