"""Persist provenance-preserving PDF intake bundles in the Registry."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
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
    _preserve_database_metadata,
    _sqlite_sidecars,
    _validate_database,
)
from .schema import apply_migrations, reconcile_pdf_article_links


def validate_source_coverage(bundle: dict[str, Any]) -> None:
    """Replay evidence and judgments from the original bytes before any write."""
    if "source_coverage" not in bundle:
        return  # Legacy v1 bundles remain supported by the original writer.
    from climate_monitor.pdf_intake import (
        _read_pdf, _article_records,
        build_source_coverage, reconcile_source_coverage,
    )
    documents = []
    for document in bundle["documents"]:
        raw = base64.b64decode(document["source"]["original_pdf_base64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != document["source"]["sha256"]:
            raise RegistryInputError("coverage original PDF hash differs")
        original = _read_pdf(Path(document["source"]["filename"]), raw_pdf=raw)
        for key, value in original["source"].items():
            if key not in {"path", "filename", "source_observations"} and document["source"].get(key) != value:
                raise RegistryInputError("coverage source metadata differs from original PDF")
        if len(original["pages"]) != len(document["pages"]):
            raise RegistryInputError("coverage PDF page count differs")
        for actual, stored in zip(original["pages"], document["pages"]):
            if {key: value for key, value in actual.items() if key != "_text_fragments"} != stored:
                raise RegistryInputError("coverage page text, native links or region differs from original PDF")
        if {key: value for key, value in original.items() if key not in {"source", "pages", "calendar_items"}} != {
            key: value for key, value in document.items() if key not in {"source", "pages", "calendar_items"}
        }:
            raise RegistryInputError("coverage report source fields differ from original PDF")
        original["source"] = document["source"]
        documents.append(original)

    def raw_calendar(items):
        return [{key: value for key, value in item.items() if key not in {"type_safe_classification", "management_batch_id"}} for item in items]

    for original, document in zip(documents, bundle["documents"]):
        if raw_calendar(original["calendar_items"]) != raw_calendar(document["calendar_items"]):
            raise RegistryInputError("coverage calendar original text, hashes or source fields differ from PDF")
    calendar_items = [item for document in documents for item in document["calendar_items"]]
    if raw_calendar(calendar_items) != raw_calendar(bundle["calendar_items"]):
        raise RegistryInputError("coverage calendar original text, hashes or source fields differ from PDF")
    articles = _article_records(documents)
    def raw_articles(items):
        return [{key: ([{field: value for field, value in occurrence.items() if field != "management_batch_id"}
            for occurrence in item["occurrences"]] if key == "occurrences" else value)
            for key, value in item.items() if key != "type_safe_classification"} for item in items]
    if raw_articles(articles) != raw_articles(bundle["articles"]):
        raise RegistryInputError("coverage article original text, hashes or source fields differ from PDF")
    replay = dict(bundle, articles=articles, calendar_items=calendar_items,
        source_coverage=build_source_coverage(documents, articles))
    try:
        for source in bundle["source_coverage"]:
            for entry in source["entries"]:
                for revision in entry["revisions"]:
                    reconcile_source_coverage(replay, [{key: value for key, value in revision.items() if key != "revision_id"}])
        if replay["source_coverage"] != bundle["source_coverage"]:
            raise ValueError("source coverage does not replay from exact PDF evidence")
    except (KeyError, TypeError, ValueError) as exc:
        raise RegistryInputError(f"invalid source coverage: {exc}") from exc


def _persist_source_coverage(connection, bundle, now, article_matches, calendar_matches):
    from climate_monitor.pdf_intake import _coverage_id
    for source in bundle.get("source_coverage", []):
        coverage_id = _coverage_id("pdf-coverage-", source)
        if connection.execute("SELECT 1 FROM pdf_source_coverage WHERE coverage_id=?", (coverage_id,)).fetchone():
            continue
        histories = [json.loads(row[0]) for row in connection.execute(
            "SELECT coverage_json FROM pdf_source_coverage WHERE document_sha256=? ORDER BY rowid", (source["source_document_sha256"],))]
        latest_revisions = {}
        for history in histories:
            for entry in history["entries"]:
                if entry["revisions"]:
                    latest_revisions[entry["entry_id"]] = entry["revisions"]
        for entry in source["entries"]:
            previous = latest_revisions.get(entry["entry_id"], [])
            if entry["revisions"] and entry["revisions"][:len(previous)] != previous:
                raise RegistryInputError("stored PDF coverage judgment changed; re-read before appending")
        baselines = []
        occurrence_bindings = {}
        for article in bundle["articles"]:
            for occurrence in article["occurrences"]:
                if occurrence["source_document_sha256"] == source["source_document_sha256"]:
                    key = occurrence["occurrence_id"]
                    occurrence_bindings[key] = article_matches.get(key, key)
        for occurrence in bundle["calendar_items"]:
            if occurrence["source_document_sha256"] == source["source_document_sha256"]:
                key = occurrence["occurrence_id"]
                occurrence_bindings[key] = calendar_matches.get(key, key)
        for article in bundle["articles"]:
            if not any(item["source_document_sha256"] == source["source_document_sha256"] for item in article["occurrences"]):
                continue
            from .publication import canonical_entity
            kind, identity = canonical_entity(connection, "pdf_article", article["article_id"])
            row = connection.execute("""SELECT p.published_candidate_sha256,c.snapshot_json FROM registry_publication p
                JOIN registry_candidates c ON c.candidate_sha256=p.published_candidate_sha256
                WHERE p.entity_kind=? AND p.entity_id=?""", (kind, identity)).fetchone()
            from .owner_dates import current_date_view
            baselines.append({"observation_article_id": article["article_id"], "read_at": now,
                "entity_kind": kind, "entity_id": identity, "status": "published" if row else "pending_g4",
                "candidate_sha256": row[0] if row else None, "snapshot": json.loads(row[1]) if row else None,
                "public_view": current_date_view(connection, kind, identity) if row else None,
                "requires_downstream_recheck": True})
        for occurrence in bundle["calendar_items"]:
            if occurrence["source_document_sha256"] != source["source_document_sha256"]:
                continue
            key = occurrence["occurrence_id"]
            translated = calendar_matches.get(key)
            stored = connection.execute("""SELECT event_id FROM pdf_intake_calendar_items
                WHERE occurrence_id=? AND source_document_sha256=? AND page=?""",
                (translated, source["source_document_sha256"], occurrence["page"])).fetchone() if translated else None
            published = []
            if stored:
                # Only a persisted source identity or its approved, explicit native link is known here.
                published = connection.execute("""SELECT p.entity_kind,p.entity_id,p.published_candidate_sha256,c.snapshot_json
                    FROM registry_publication p JOIN registry_candidates c ON c.candidate_sha256=p.published_candidate_sha256
                    WHERE (p.entity_kind='pdf_meeting' AND p.entity_id=?) OR (p.entity_kind='meeting'
                        AND EXISTS (SELECT 1 FROM json_each(c.snapshot_json,'$.tables.pdf_intake_calendar_items') item
                            WHERE json_extract(item.value,'$.occurrence_id')=?
                                AND json_extract(item.value,'$.source_document_sha256')=?
                                AND json_extract(item.value,'$.page')=? AND json_extract(item.value,'$.event_id')=?)
                        AND EXISTS (SELECT 1 FROM json_each(c.snapshot_json,'$.tables.meeting_check_attempts') check_row
                            WHERE json_extract(check_row.value,'$.occurrence_id')=?
                                AND json_extract(check_row.value,'$.verification_status')='verified'
                                AND json_extract(json_extract(check_row.value,'$.packet_json'),'$.canonical_event_id')=p.entity_id))""",
                    (stored[0], translated, source["source_document_sha256"], occurrence["page"], stored[0], translated)).fetchall()
            row = published[0] if len(published) == 1 else None
            kind, identity = row[:2] if row else (None, None)
            from .owner_dates import current_date_view
            baselines.append({"observation_occurrence_id": key, "registry_occurrence_id": occurrence_bindings[key],
                "entity_kind": kind, "entity_id": identity, "status": "published" if row else "pending_g4",
                "candidate_sha256": row[2] if row else None, "snapshot": json.loads(row[3]) if row else None,
                "public_view": current_date_view(connection, kind, identity) if row else None,
                "read_at": now, "requires_downstream_recheck": True})
        connection.execute("INSERT INTO pdf_source_coverage VALUES(?,?,?,?,?,?)", (
            coverage_id, source["source_document_sha256"], json.dumps(source, ensure_ascii=False, sort_keys=True),
            json.dumps(occurrence_bindings, sort_keys=True),
            json.dumps(baselines, ensure_ascii=False, sort_keys=True), now))


def _existing_occurrence_ids(
    connection: sqlite3.Connection,
    documents: list[dict[str, Any]],
    articles: list[dict[str, Any]],
    calendar_items: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    article_rows = defaultdict(list)
    calendar_rows = defaultdict(list)
    for source_sha in {document["source"]["sha256"] for document in documents}:
        for row in connection.execute(
            """SELECT occurrence_id, article_id, source_document_sha256, page, raw_url, occurrence_json
               FROM pdf_intake_article_occurrences
               WHERE source_document_sha256=? ORDER BY rowid""",
            (source_sha,),
        ):
            key = (row[2], row[1], row[3], row[4])
            article_rows[key].append((row[0], json.loads(row[5])))
        for row in connection.execute(
            """SELECT occurrence_id, event_id, source_document_sha256, page, item_json
               FROM pdf_intake_calendar_items
               WHERE source_document_sha256=? ORDER BY rowid""",
            (source_sha,),
        ):
            key = (row[2], row[3])
            calendar_rows[key].append((row[0], json.loads(row[4])))

    def match(rows, occurrence, evidence):
        matches = [row for row in rows if row[0] == occurrence["occurrence_id"]]
        if not matches:
            matches = [row for row in rows if evidence(row[1], occurrence)]
        if len(matches) > 1:
            raise RegistryInputError("PDF legacy occurrence source evidence is ambiguous; cannot translate its ID")
        if matches:
            rows.remove(matches[0])
            return matches[0][0]
        return None

    article_matches: dict[str, str] = {}
    for article in articles:
        for occurrence in article["occurrences"]:
            key = (occurrence["source_document_sha256"], article["article_id"],
                   occurrence["page"], occurrence["raw_url"])
            identity = match(article_rows[key], occurrence,
                lambda old, new: bool(old.get("link_rects")) and old["link_rects"] == new.get("link_rects"))
            if not identity and any(not old.get("link_rects") for _, old in article_rows[key]):
                raise RegistryInputError("PDF legacy occurrence has no unique source rectangle; cannot translate its ID")
            if identity:
                article_matches[occurrence["occurrence_id"]] = identity

    calendar_matches: dict[str, str] = {}
    for item in calendar_items:
        key = (item["source_document_sha256"], item["page"])
        identity = match(calendar_rows[key], item,
            lambda old, new: bool(old.get("raw_text")) and old["raw_text"] == new.get("raw_text")
                and (not old.get("source_region") or old["source_region"] == new.get("source_region")))
        if not identity and any(not old.get("raw_text") and old.get("event_id") == item["event_id"]
                for _, old in calendar_rows[key]):
            raise RegistryInputError("PDF legacy occurrence has no unique source row; cannot translate its ID")
        if identity:
            calendar_matches[item["occurrence_id"]] = identity
    return article_matches, calendar_matches


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
    validate_source_coverage(bundle)
    original_pdfs = {}
    pdf_metadata_json_by_sha = {}
    source_observations_by_sha = {}
    for document in documents:
        if not isinstance(document, dict) or not isinstance(document.get("source"), dict):
            raise RegistryInputError("PDF intake document is missing source bytes or metadata")
        source_info = document["source"]
        try:
            original_pdf = base64.b64decode(source_info["original_pdf_base64"], validate=True)
            sha256 = source_info["sha256"]
            size_bytes = source_info["size_bytes"]
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise RegistryInputError("PDF intake document has invalid original bytes") from exc
        if (not isinstance(sha256, str) or len(sha256) != 64
                or len(original_pdf) != size_bytes
                or hashlib.sha256(original_pdf).hexdigest() != sha256):
            raise RegistryInputError("original PDF bytes do not match the declared source hash")
        observations = source_info.get("source_observations")
        if observations is None:
            observations = [{"path": source_info.get("path"), "filename": source_info.get("filename")}]
        if (not isinstance(observations, list) or not observations or any(
            not isinstance(item, dict) or not isinstance(item.get("path"), str)
            or not item["path"] or not isinstance(item.get("filename"), str)
            or not item["filename"] for item in observations
        )):
            raise RegistryInputError("PDF intake document has invalid source observations")
        source_observations_by_sha[sha256] = observations
        original_pdfs[sha256] = original_pdf
        metadata = source_info.get("pdf_metadata")
        pdf_metadata_json_by_sha[sha256] = (
            json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata is not None else None
        )

    now = datetime.now(timezone.utc).isoformat()
    with _exclusive_database_lock(database):
        live_metadata = database.stat()
        if sidecars := _sqlite_sidecars(database):
            raise RegistryInputError(
                "registry has active SQLite sidecar files; reconcile before PDF import: "
                + ", ".join(path.name for path in sidecars)
            )
        fingerprint = _file_sha256(database)
        source = _read_only_connection(database)
        try:
            _validate_database(source)
            from .publication import require_publication_migration
            require_publication_migration(source)
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
            _fsync_parent(backup)

            connection = sqlite3.connect(candidate)
            try:
                apply_migrations(connection)
                connection.execute("PRAGMA foreign_keys = ON")
                added = {"documents": 0, "article_occurrences": 0, "calendar_items": 0}
                connection.execute("BEGIN IMMEDIATE")
                try:
                    existing_documents = sum(
                        connection.execute(
                            "SELECT COUNT(*) FROM pdf_intake_documents WHERE document_sha256=?",
                            (document["source"]["sha256"],),
                        ).fetchone()[0]
                        for document in documents
                    )
                    existing_article_ids, existing_calendar_ids = _existing_occurrence_ids(
                        connection, documents, articles, calendar_items
                    )
                    known_hashes = set()
                    for document in documents:
                        source_info = document["source"]
                        sha = source_info["sha256"]
                        known_hashes.add(sha)
                        stored_document = dict(document)
                        stored_source = dict(source_info)
                        stored_source.pop("original_pdf_base64", None)
                        stored_document["source"] = stored_source
                        cursor = connection.execute(
                            """INSERT OR IGNORE INTO pdf_intake_documents (
                                document_sha256, source_path, filename, media_type, size_bytes,
                                date_of_run, period_start, period_end, pdf_created_at, pdf_modified_at,
                                original_pdf, pdf_metadata_json, extracted_text_sha256, document_json,
                                imported_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (sha, source_info["path"], source_info["filename"],
                             source_info["media_type"], source_info["size_bytes"],
                             document.get("date_of_run"), document.get("period_start"),
                             document.get("period_end"), source_info.get("pdf_created_at"),
                             source_info.get("pdf_modified_at"), original_pdfs[sha],
                             pdf_metadata_json_by_sha[sha],
                             document["extracted_text_sha256"],
                             json.dumps(stored_document, ensure_ascii=False, sort_keys=True), now),
                        )
                        added["documents"] += cursor.rowcount
                        if not cursor.rowcount:
                            connection.execute(
                                """UPDATE pdf_intake_documents
                                   SET pdf_created_at=COALESCE(pdf_created_at, ?),
                                       pdf_modified_at=COALESCE(pdf_modified_at, ?),
                                       original_pdf=COALESCE(original_pdf, ?),
                                       pdf_metadata_json=COALESCE(pdf_metadata_json, ?)
                                   WHERE document_sha256=? AND (
                                       original_pdf IS NULL
                                       OR (? IS NOT NULL AND pdf_created_at IS NULL)
                                       OR (? IS NOT NULL AND pdf_modified_at IS NULL)
                                       OR (? IS NOT NULL AND pdf_metadata_json IS NULL)
                                   )""",
                                (source_info.get("pdf_created_at"), source_info.get("pdf_modified_at"),
                                 original_pdfs[sha],
                                 pdf_metadata_json_by_sha[sha], sha, source_info.get("pdf_created_at"),
                                 source_info.get("pdf_modified_at"), pdf_metadata_json_by_sha[sha]),
                            )
                        for observation in source_observations_by_sha[sha]:
                            connection.execute(
                                """INSERT OR IGNORE INTO pdf_intake_document_sources (
                                    document_sha256, source_path, filename, observed_at
                                ) VALUES (?, ?, ?, ?)""",
                                (sha, observation["path"], observation["filename"], now),
                            )

                    for article in articles:
                        classification = article.get("type_safe_classification")
                        connection.execute(
                            """INSERT INTO pdf_intake_articles (
                                article_id, canonical_url, title, type_safe_classification_json, imported_at
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(article_id) DO UPDATE SET
                                title = COALESCE(pdf_intake_articles.title, excluded.title),
                                type_safe_classification_json = COALESCE(
                                    pdf_intake_articles.type_safe_classification_json,
                                    excluded.type_safe_classification_json
                                )""",
                            (article["article_id"], article["canonical_url"], article.get("title"),
                             json.dumps(classification, ensure_ascii=False, sort_keys=True)
                             if classification else None, now),
                        )
                        for occurrence in article["occurrences"]:
                            if occurrence["source_document_sha256"] not in known_hashes:
                                raise RegistryInputError("article occurrence references an unknown PDF")
                            occurrence_id = existing_article_ids.get(
                                occurrence["occurrence_id"], occurrence["occurrence_id"]
                            )
                            stored_occurrence = dict(occurrence, occurrence_id=occurrence_id)
                            cursor = connection.execute(
                                """INSERT OR IGNORE INTO pdf_intake_article_occurrences (
                                    occurrence_id, article_id, source_document_sha256, page, raw_url,
                                    report_date, publication_date, content_sha256, page_sha256,
                                    occurrence_json
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (occurrence_id, article["article_id"],
                                 occurrence["source_document_sha256"], occurrence["page"],
                                 occurrence["raw_url"], occurrence.get("report_date"),
                                 occurrence.get("publication_date"), occurrence["content_sha256"],
                                 occurrence["page_sha256"],
                                 json.dumps(stored_occurrence, ensure_ascii=False, sort_keys=True)),
                            )
                            added["article_occurrences"] += cursor.rowcount
                            if cursor.rowcount:
                                from .acquisition_review import record_knowledge
                                record_knowledge(connection, kind="article", entity_id=article["article_id"],
                                    source_kind="pdf", source_ref=occurrence_id, recorded_at=now,
                                    fields={key: stored_occurrence.get(key) for key in ("title", "anchor_text", "summary", "publication_date")},
                                    evidence={"document_sha256": occurrence["source_document_sha256"], "page": occurrence["page"]})

                    reconcile_pdf_article_links(connection, observed_at=now)

                    for item in calendar_items:
                        if item["source_document_sha256"] not in known_hashes:
                            raise RegistryInputError("calendar item references an unknown PDF")
                        occurrence_id = existing_calendar_ids.get(item["occurrence_id"], item["occurrence_id"])
                        stored_item = dict(item, occurrence_id=occurrence_id)
                        classification = item.get("type_safe_classification")
                        cursor = connection.execute(
                            """INSERT OR IGNORE INTO pdf_intake_calendar_items (
                                occurrence_id, event_id, source_document_sha256, page, name, kind,
                                raw_date, date_precision, start_date, end_date, summary,
                                content_sha256, type_safe_classification_json, item_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (occurrence_id, item["event_id"], item["source_document_sha256"],
                             item["page"], item.get("name"), item["kind"], item["raw_date"],
                             item["date_precision"], item.get("start_date"), item.get("end_date"),
                             item["summary"], item["content_sha256"],
                             json.dumps(classification, ensure_ascii=False, sort_keys=True)
                             if classification else None,
                             json.dumps(stored_item, ensure_ascii=False, sort_keys=True)),
                        )
                        added["calendar_items"] += cursor.rowcount
                        if cursor.rowcount:
                            from .acquisition_review import record_knowledge
                            from climate_monitor.meeting_fields import pdf_meeting_fields, MEETING_FIELDS
                            normalized = pdf_meeting_fields(stored_item)
                            record_knowledge(connection, kind="meeting", entity_id=item["event_id"],
                                source_kind="pdf", source_ref=occurrence_id, recorded_at=now,
                                fields={key: normalized.get(key) for key in MEETING_FIELDS},
                                evidence={"document_sha256": item["source_document_sha256"], "page": item["page"]})
                        if classification:
                            connection.execute(
                                """UPDATE pdf_intake_calendar_items
                                   SET type_safe_classification_json=?
                                   WHERE occurrence_id=? AND type_safe_classification_json IS NULL""",
                                (json.dumps(classification, ensure_ascii=False, sort_keys=True),
                                 occurrence_id),
                            )
                    _persist_source_coverage(connection, bundle, now, existing_article_ids, existing_calendar_ids)
                    from .publication import stage_entities
                    stage_entities(connection)
                    from .display import refresh_display
                    refresh_display(connection)
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
            _preserve_database_metadata(candidate, live_metadata)
            os.replace(candidate, database)
            _fsync_parent(database)
            return {"status": "updated", "schema_version": LATEST_SCHEMA_VERSION,
                    "added": added, "new_documents": added["documents"],
                    "existing_documents": existing_documents, "backup": str(backup)}
        except (RegistryInputError, RegistryBuildError, RegistryLockError):
            raise
        except Exception as exc:
            raise RegistryBuildError(f"PDF intake Registry update failed: {exc}") from exc
        finally:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
