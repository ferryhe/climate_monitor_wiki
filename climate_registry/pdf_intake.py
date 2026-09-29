"""Persist provenance-preserving PDF intake bundles in the Registry."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import RegistryBuildError, RegistryInputError, RegistryLockError
from .persistent import (
    LATEST_SCHEMA_VERSION,
    _backup_connection,
    _backup_name,
    _exclusive_database_lock,
    _file_sha256,
    _fsync_parent,
    _read_only_connection,
    _sqlite_sidecars,
    _validate_database,
)
from .schema import apply_migrations


def persist_pdf_intake(database: Path, backup_dir: Path, bundle: dict[str, Any]) -> dict[str, Any]:
    """Atomically append a parsed PDF bundle to an existing Registry."""
    database, backup_dir = database.resolve(), backup_dir.resolve()
    if not database.is_file():
        raise RegistryInputError(f"registry database does not exist: {database}")
    if backup_dir == database or database in backup_dir.parents:
        raise RegistryInputError("backup directory must not contain the live registry database")
    if backup_dir.exists() and not backup_dir.is_dir():
        raise RegistryInputError(f"backup directory path is not a directory: {backup_dir}")
    if not isinstance(bundle, dict):
        raise RegistryInputError("unsupported or malformed PDF intake bundle")
    documents = bundle.get("documents")
    articles = bundle.get("articles")
    calendar_items = bundle.get("calendar_items")
    if bundle.get("schema_version") != "climate-pdf-intake.v1" or not all(
        isinstance(items, list) for items in (documents, articles, calendar_items)
    ):
        raise RegistryInputError("unsupported or malformed PDF intake bundle")

    now = datetime.now(timezone.utc).isoformat()
    with _exclusive_database_lock(database):
        if sidecars := _sqlite_sidecars(database):
            raise RegistryInputError(
                "registry has active SQLite sidecar files; reconcile before PDF import: "
                + ", ".join(path.name for path in sidecars)
            )
        fingerprint = _file_sha256(database)
        source = _read_only_connection(database)
        try:
            _validate_database(source)
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / _backup_name(database)
            if backup.exists():
                raise RegistryBuildError(f"backup destination already exists: {backup.name}")
            _backup_connection(source, backup)
            descriptor, candidate_name = tempfile.mkstemp(
                prefix=f".{database.name}.", suffix=".candidate", dir=database.parent
            )
        finally:
            source.close()

        os.close(descriptor)
        candidate = Path(candidate_name)
        candidate.unlink()
        try:
            backup_connection = _read_only_connection(backup)
            try:
                _validate_database(backup_connection)
                _backup_connection(backup_connection, candidate)
            finally:
                backup_connection.close()
            if os.name == "posix":
                backup.chmod(0o600)
                shutil.copymode(database, candidate)
            _fsync_parent(backup)

            connection = sqlite3.connect(candidate)
            try:
                apply_migrations(connection)
                connection.execute("PRAGMA foreign_keys = ON")
                added = {"documents": 0, "article_occurrences": 0, "calendar_items": 0}
                connection.execute("BEGIN IMMEDIATE")
                try:
                    known_hashes = set()
                    for document in documents:
                        source_info = document["source"]
                        sha = source_info["sha256"]
                        known_hashes.add(sha)
                        cursor = connection.execute(
                            """INSERT OR IGNORE INTO pdf_intake_documents (
                                document_sha256, source_path, filename, media_type, size_bytes,
                                date_of_run, period_start, period_end, extracted_text_sha256,
                                document_json, imported_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (sha, source_info["path"], source_info["filename"],
                             source_info["media_type"], source_info["size_bytes"],
                             document.get("date_of_run"), document.get("period_start"),
                             document.get("period_end"), document["extracted_text_sha256"],
                             json.dumps(document, ensure_ascii=False, sort_keys=True), now),
                        )
                        added["documents"] += cursor.rowcount

                    for article in articles:
                        classification = article.get("type_safe_classification")
                        connection.execute(
                            """INSERT INTO pdf_intake_articles (
                                article_id, canonical_url, title, type_safe_classification_json, imported_at
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(article_id) DO UPDATE SET
                                title = COALESCE(pdf_intake_articles.title, excluded.title),
                                type_safe_classification_json = COALESCE(
                                    excluded.type_safe_classification_json,
                                    pdf_intake_articles.type_safe_classification_json
                                )""",
                            (article["article_id"], article["canonical_url"], article.get("title"),
                             json.dumps(classification, ensure_ascii=False, sort_keys=True)
                             if classification else None, now),
                        )
                        for occurrence in article["occurrences"]:
                            if occurrence["source_document_sha256"] not in known_hashes:
                                raise RegistryInputError("article occurrence references an unknown PDF")
                            cursor = connection.execute(
                                """INSERT OR IGNORE INTO pdf_intake_article_occurrences (
                                    occurrence_id, article_id, source_document_sha256, page, raw_url,
                                    report_date, publication_date, content_sha256, page_sha256,
                                    occurrence_json
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (occurrence["occurrence_id"], article["article_id"],
                                 occurrence["source_document_sha256"], occurrence["page"],
                                 occurrence["raw_url"], occurrence.get("report_date"),
                                 occurrence.get("publication_date"), occurrence["content_sha256"],
                                 occurrence["page_sha256"],
                                 json.dumps(occurrence, ensure_ascii=False, sort_keys=True)),
                            )
                            added["article_occurrences"] += cursor.rowcount

                    for item in calendar_items:
                        if item["source_document_sha256"] not in known_hashes:
                            raise RegistryInputError("calendar item references an unknown PDF")
                        classification = item.get("type_safe_classification")
                        cursor = connection.execute(
                            """INSERT OR IGNORE INTO pdf_intake_calendar_items (
                                occurrence_id, event_id, source_document_sha256, page, name, kind,
                                raw_date, date_precision, start_date, end_date, summary,
                                content_sha256, type_safe_classification_json, item_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (item["occurrence_id"], item["event_id"], item["source_document_sha256"],
                             item["page"], item.get("name"), item["kind"], item["raw_date"],
                             item["date_precision"], item.get("start_date"), item.get("end_date"),
                             item["summary"], item["content_sha256"],
                             json.dumps(classification, ensure_ascii=False, sort_keys=True)
                             if classification else None,
                             json.dumps(item, ensure_ascii=False, sort_keys=True)),
                        )
                        added["calendar_items"] += cursor.rowcount
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                if _validate_database(connection) != LATEST_SCHEMA_VERSION:
                    raise RegistryBuildError("PDF intake did not reach the target Registry schema")
            finally:
                connection.close()

            if _sqlite_sidecars(database) or _file_sha256(database) != fingerprint:
                raise RegistryLockError("live Registry changed while PDF intake was prepared")
            os.replace(candidate, database)
            _fsync_parent(database)
            return {"status": "updated", "schema_version": LATEST_SCHEMA_VERSION,
                    "added": added, "backup": str(backup)}
        except (RegistryInputError, RegistryBuildError, RegistryLockError):
            raise
        except Exception as exc:
            raise RegistryBuildError(f"PDF intake Registry update failed: {exc}") from exc
        finally:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
