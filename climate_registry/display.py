"""Published web DTOs, rebuilt by writers in their publication transaction."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .list_projection import RegistryListProjection
from .read_api import RegistryReader, RegistryContractError, RegistryNotFoundError, RegistryQueryError, _pagination, validate_page, validate_report_date, validate_article_id, filter_pdf_articles

FORMAT_VERSION = 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _stat(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _artifact(identity, output_dir):
    from climate_delivery.artifacts import load_report_artifact
    root = str(Path(output_dir).resolve()) if output_dir else ""
    if not root:
        return None
    artifact = load_report_artifact(root, report_date=identity.report_date,
        report_filename=identity.filename, report_title=identity.report_title,
        report_sha256=identity.report_sha256, include_pdf_bytes=False, allow_offcycle=True)
    if artifact is None:
        return None
    directory = Path(root) / identity.report_date / identity.report_sha256
    files = [directory / name for name in ("manifest.json", "summary.json", artifact.pdf_filename)]
    return {"root": root, "files": [[str(path), _stat(path)] for path in files],
        "report_pdf": {"filename": artifact.pdf_filename,
            "download_url": f"/api/registry/reports/{identity.report_date}/pdf"}}


def _boundaries(reader):
    from climate_monitor.meetings import _date_bounds
    from climate_monitor.meeting_fields import collected_pdf_meeting
    from .range_reports import _calendar_end_date
    ends = set()
    for (kind, _), snapshot in reader._public_candidates("meeting", "pdf_meeting").items():
        if kind == "meeting":
            for item in snapshot.get("tables", {}).get("climate_events", []):
                precision = item.get("date_precision")
                for field in ("start_date", "end_date"):
                    if item.get(field) and precision and precision != "unknown":
                        ends.add(_date_bounds(item[field], precision, end=True))
                if item.get("deadline_date"):
                    ends.add(date.fromisoformat(item["deadline_date"]))
    # Include both individual observations and their resolved/merged representations.
    imported = reader.pdf_calendar_items_all()
    for item in imported + reader.resolve_pdf_meeting_identities(imported):
        converted = collected_pdf_meeting(item)
        for value in (item, converted, converted.get("collected_candidate") or {},
                (converted.get("manual_verification") or {}).get("public_facts") or {}):
            end = _calendar_end_date(value)
            if end is not None:
                ends.add(end)
            if value.get("deadline_date"):
                ends.add(date.fromisoformat(value["deadline_date"]))
    return [date.min.isoformat()] + sorted((end + timedelta(days=1)).isoformat() for end in ends if end < date.max)


def refresh_display(connection, *, force=False, output_dir=None):
    """Refresh once after a writer's complete batch; failures roll back publication."""
    if connection.execute("PRAGMA user_version").fetchone()[0] < 24:
        return False
    from .verification import PUBLICATION_POLICY
    from climate_monitor.taxonomy import load_article_taxonomy
    taxonomy = load_article_taxonomy()
    state = connection.execute("SELECT generation,dirty,ready,format_version,policy,taxonomy_sha256,metadata FROM display_state WHERE singleton=1").fetchone()
    schema_cookie = connection.execute("PRAGMA schema_version").fetchone()[0]
    if not force and state and not state[1] and state[2] and tuple(state[3:6]) == (FORMAT_VERSION, PUBLICATION_POLICY, taxonomy.sha256) and json.loads(state[6]).get("schema_cookie") == schema_cookie:
        return False
    from .schema import DISPLAY_TABLE_COLUMNS
    from .publication import _public_dto, _pdf_dto, _calendar_dto, _merge_pdf_article_payloads
    database = next(row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main")
    root = Path(__file__).resolve().parents[1]
    reader = RegistryReader(database, repository_root=root, source_dir=root / os.getenv("SOURCE_DIR", "sources"),
        metadata_dir=root / os.getenv("ARTICLE_METADATA_DIR", "article_metadata"))
    previous_factory = connection.row_factory
    cache_spill = connection.execute("PRAGMA cache_spill").fetchone()[0]
    # Active transactions retain their pager spill flag, so raise its threshold too.
    if cache_spill:
        connection.execute("PRAGMA cache_spill=2147483647")
        connection.execute("PRAGMA cache_spill=OFF")
    try:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        connection.row_factory = sqlite3.Row
        reader._validate_contract(connection)
        with reader.public_snapshot(direct=True, source_connection=connection):
            projection = RegistryListProjection(reader)
            try:
                for table in DISPLAY_TABLE_COLUMNS:
                    if table != "display_state":
                        connection.execute(f"DELETE FROM {table}")
                for table in ("articles", "appearances", "reports", "publishers"):
                    source = projection.connection.execute(f"SELECT * FROM {table}")
                    marks = ",".join("?" for _ in source.description)
                    connection.executemany(f"INSERT INTO display_{table} VALUES({marks})", source)
                metadata = {"revision": uuid.uuid4().hex, "schema_cookie": schema_cookie, "status_core": projection.status_core, "status_combined": projection.status_combined,
                    "publisher_metadata": projection.publisher_metadata, "categories": projection.category_body.decode("utf-8")}
            finally:
                projection.close()
            standalone = {item["canonical_url"] for item in reader.pdf_articles_all()}
            for original in reader.pdf_articles_all(include_linked=True):
                item = _merge_pdf_article_payloads(original, None)
                card = _pdf_dto(filter_pdf_articles([item])[0])
                search = " ".join((item["title"] or "", item["summary"] or "", item["canonical_url"])).casefold()
                connection.execute("INSERT INTO display_pdf_articles VALUES(?,?,?,?,?,?,?)",
                    (item["canonical_url"], item["article_id"], item.get("last_seen") or "", item.get("source"), search,
                        item["canonical_url"] in standalone, _json(card)))
            for item in reader.pdf_calendar_items_all():
                search = " ".join((item.get("name") or "", item.get("kind") or "", item.get("raw_date") or "",
                    item.get("summary") or "", " ".join(item.get("source_urls") or []))).casefold()
                connection.execute("INSERT INTO display_pdf_calendar VALUES(?,?,?,?,?,?)", (item["occurrence_id"],
                    item.get("start_date") or "9999", item.get("name") or "", str(item.get("kind") or "").casefold(), search, _json(_calendar_dto(item))))
            candidates = reader._public_candidates("article", "pdf_article", "meeting", "pdf_meeting")
            pdf_ids = {row[0] for row in connection.execute("SELECT article_id FROM pdf_intake_articles")}
            for kind, identity in candidates:
                if kind == "article":
                    item = _public_dto(reader.article(identity))
                elif kind == "pdf_article":
                    item = _pdf_dto(_merge_pdf_article_payloads(reader.pdf_article(identity), None))
                    pdf_ids.discard(identity)
                else:
                    continue
                connection.execute("INSERT INTO display_details VALUES(?,?,?)", (kind, identity, _json(item)))
            # Linked PDF identities can be owned by an approved core snapshot.
            for identity in pdf_ids:
                try:
                    item = _pdf_dto(_merge_pdf_article_payloads(reader.pdf_article(identity), None))
                except RegistryNotFoundError:
                    continue
                connection.execute("INSERT INTO display_details VALUES('pdf_article',?,?)", (identity, _json(item)))
            reports = reader.approved_reports_all() + reader.pdf_reports_all()
            for card in reports:
                kind = card.get("source_kind") or "registry"
                identity = card["document_sha256"] if kind == "pdf" else card["report_date"]
                artifact = None
                if kind == "pdf":
                    report = reader.pdf_report(identity)
                    articles = [_pdf_dto(item) for item in report.pop("articles")]
                    calendars = [_calendar_dto(item) for item in report.pop("calendar_items")]
                else:
                    report, report_identity = reader.report_with_identity(identity)
                    articles = [_public_dto(item) for item in report.pop("articles")]
                    calendars = []
                    artifact = _artifact(report_identity, output_dir if output_dir is not None else os.getenv("CLIMATE_DELIVERY_OUTPUT_DIR", "").strip())
                    report["report_briefing"] = None
                    report["report_pdf"] = artifact["report_pdf"] if artifact else None
                modules = [("article", item) for item in articles] + [("calendar", item) for item in calendars]
                connection.execute("INSERT INTO display_report_details VALUES(?,?,?,?,?,?)",
                    (kind, identity, _json(report), len(articles), len(modules), _json(artifact) if artifact else None))
                connection.executemany("INSERT INTO display_modules VALUES(?,?,?,?,?)",
                    ((kind, identity, position, module_kind, _json(dto)) for position, (module_kind, dto) in enumerate(modules)))
            for boundary in _boundaries(reader):
                for unknown in (False, True):
                    items, counts, coverage = reader._direct_meeting_collection(base_date=boundary,
                        timezone_name="UTC", include_unknown=unknown)
                    connection.execute("INSERT INTO display_meeting_intervals VALUES(?,?,?)",
                        (boundary, unknown, _json({"verification_counts": counts, "coverage": coverage})))
                    connection.executemany("INSERT INTO display_meetings VALUES(?,?,?,?,?,?)", (
                        (boundary, unknown, position, item.get("verification_status"),
                            " ".join(str(item.get(key) or "") for key in ("name", "organizer", "relevance_reason", "raw_date")).casefold(),
                            _json(_calendar_dto(item))) for position, item in enumerate(items)))
                    reader._direct_meeting_collection_cache.clear()
                    reader._meeting_records_cache.clear()
            connection.execute("UPDATE display_state SET generation=?,format_version=?,policy=?,taxonomy_sha256=?,ready=1,dirty=0,metadata=? WHERE singleton=1",
                ((state[0] if state else 0) + 1, FORMAT_VERSION, PUBLICATION_POLICY, taxonomy.sha256, _json(metadata)))
    finally:
        connection.row_factory = previous_factory
        if cache_spill:
            connection.execute(f"PRAGMA cache_spill={cache_spill}")
    return True


class DisplayReader(RegistryListProjection):
    def __init__(self, connection):
        from .verification import PUBLICATION_POLICY
        self.connection = connection
        self.prefix = "display_"
        row = connection.execute("SELECT generation,format_version,policy,ready,dirty,metadata FROM display_state WHERE singleton=1").fetchone()
        if row is None or row[1] != FORMAT_VERSION or row[2] != PUBLICATION_POLICY or not row[3] or row[4]:
            raise RegistryContractError("published display generation is unavailable; run migrate-publication --apply")
        metadata = json.loads(row[5])
        if metadata.get("schema_cookie") != connection.execute("PRAGMA schema_version").fetchone()[0]:
            raise RegistryContractError("Registry schema differs from its published display generation")
        self.version = metadata["revision"]
        self.status_core, self.status_combined = metadata["status_core"], metadata["status_combined"]
        self.publisher_metadata = {key == "true": value for key, value in metadata["publisher_metadata"].items()}
        self.category_body = metadata["categories"].encode("utf-8")

    def article(self, identity, *, pdf=False):
        validate_article_id(identity)
        row = self.connection.execute("SELECT dto FROM display_details WHERE kind=? AND identity=?",
            ("pdf_article" if pdf else "article", identity)).fetchone()
        if row is None:
            raise RegistryNotFoundError("article not found")
        return json.loads(row[0])

    def pdf_articles(self, *, page, page_size, query="", source="", report_date="", include_linked=False):
        validate_page(page, page_size)
        if len(query) > 200 or len(source) > 253:
            raise RegistryQueryError("filter is too long")
        if report_date:
            validate_report_date(report_date)
        where, params = ["1=1" if include_linked else "a.standalone=1"], []
        if query:
            where.append("instr(a.search,?)>0")
            params.append(query.casefold())
        if source:
            where.append("a.source=?")
            params.append(source.strip().lower().removeprefix("www."))
        if report_date:
            where.append("EXISTS(SELECT 1 FROM display_appearances x WHERE x.url=a.url AND x.kind='pdf' AND x.report_date=?)")
            params.append(report_date)
        clause = " AND ".join(where)
        total = self.connection.execute("SELECT count(*) FROM display_pdf_articles a WHERE " + clause, params).fetchone()[0]
        rows = self.connection.execute("SELECT dto FROM display_pdf_articles a WHERE " + clause + " ORDER BY last_seen DESC,article_id DESC LIMIT ? OFFSET ?", (*params, page_size, (page - 1) * page_size))
        return {"items": [json.loads(row[0]) for row in rows], "pagination": _pagination(page, page_size, total)}

    def pdf_calendar(self, *, page, page_size, query="", kind=""):
        validate_page(page, page_size)
        if len(query) > 200 or len(kind) > 80:
            raise RegistryQueryError("filter is too long")
        clause, params = "1=1", []
        if kind:
            clause += " AND instr(kind,?)>0"
            params.append(kind.casefold())
        if query:
            clause += " AND instr(search,?)>0"
            params.append(query.casefold())
        total = self.connection.execute("SELECT count(*) FROM display_pdf_calendar WHERE " + clause, params).fetchone()[0]
        rows = self.connection.execute("SELECT dto FROM display_pdf_calendar WHERE " + clause + " ORDER BY start_date,name,occurrence_id LIMIT ? OFFSET ?", (*params, page_size, (page - 1) * page_size))
        return {"items": [json.loads(row[0]) for row in rows], "pagination": _pagination(page, page_size, total)}

    def report(self, identity, *, pdf=False, page=None, page_size=5):
        if pdf:
            if len(identity) != 64 or any(char not in "0123456789abcdef" for char in identity):
                raise RegistryQueryError("invalid PDF document id")
        else:
            validate_report_date(identity)
        if page is not None:
            validate_page(page, page_size)
        kind = "pdf" if pdf else "registry"
        row = self.connection.execute("SELECT header,article_count,module_count,artifact FROM display_report_details WHERE kind=? AND identity=?", (kind, identity)).fetchone()
        if row is None:
            raise RegistryNotFoundError("PDF report not found" if pdf else "report not found")
        header, article_count, total, artifact = json.loads(row[0]), row[1], row[2], row[3]
        if not pdf:
            available = False
            if artifact:
                binding = json.loads(artifact)
                root = os.getenv("CLIMATE_DELIVERY_OUTPUT_DIR", "").strip()
                try:
                    available = bool(root) and str(Path(root).resolve()) == binding["root"] and all(_stat(Path(path)) == stamp for path, stamp in binding["files"])
                except OSError:
                    pass
            header["report_pdf"] = binding["report_pdf"] if available else None
        sql = "SELECT module_kind,dto FROM display_modules WHERE kind=? AND identity=? ORDER BY position"
        params = (kind, identity)
        if page is not None:
            offset = (page - 1) * page_size
            sql += " LIMIT ? OFFSET ?"
            params += (page_size, offset)
            header.pop("pages", None)
            header.update(pagination=_pagination(page, page_size, total), article_offset=min(offset, article_count))
            if page > 1:
                header["executive_summary"] = []
        articles, calendars = [], []
        for module_kind, dto in self.connection.execute(sql, params):
            (articles if module_kind == "article" else calendars).append(json.loads(dto))
        header["articles"] = articles
        if pdf:
            if page is not None:
                from climate_monitor.meeting_fields import MEETING_FIELDS
                fields = set(MEETING_FIELDS) | {"kind", "relevance", "metadata_only", "verified_primary_display", "verified_primary_reference", "checked_source_url", "original_source_url", "page", "source_filename", "source_document_sha256", "occurrence_id", "event_id", "canonical_pdf_event_id", "verification_status", "access_status", "collection_status", "owner_agent_verification", "publisher"}
                calendars = [{key: value for key, value in item.items() if key in fields} for item in calendars]
            header["calendar_items"] = calendars
        return header

    def meetings(self, *, page, page_size, query="", verification_status="", base_date=None, include_unknown=False):
        validate_page(page, page_size)
        base_date = validate_report_date(base_date or datetime.now(timezone.utc).date().isoformat())
        row = self.connection.execute("SELECT start_date,metadata FROM display_meeting_intervals WHERE start_date<=? AND include_unknown=? ORDER BY start_date DESC LIMIT 1", (base_date, include_unknown)).fetchone()
        if row is None:
            raise RegistryContractError("published meeting intervals are unavailable")
        metadata = json.loads(row[1])
        from climate_monitor.meetings import _meeting_status_selection
        status, _, _ = _meeting_status_selection(self.connection)
        metadata["coverage"]["status"] = "processed" if status in {"complete", "not_processed"} else status
        where, params = "start_date=? AND include_unknown=?", [row[0], include_unknown]
        if verification_status:
            where += " AND verification_status=?"
            params.append(verification_status)
        if query:
            where += " AND instr(search,?)>0"
            params.append(query.casefold())
        total = self.connection.execute("SELECT count(*) FROM display_meetings WHERE " + where, params).fetchone()[0]
        rows = self.connection.execute("SELECT dto FROM display_meetings WHERE " + where + " ORDER BY position LIMIT ? OFFSET ?", (*params, page_size, (page - 1) * page_size))
        return {**metadata, "base_date": base_date, "items": [json.loads(item[0]) for item in rows], "pagination": _pagination(page, page_size, total)}


@contextmanager
def display_snapshot(reader):
    if reader.static_snapshot is not None or reader.database is None:
        yield None
        return
    with reader._source_connection(manage_wal=True, validate=False) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 24:
            yield None
        elif version != 24:
            raise RegistryContractError("unsupported display schema")
        else:
            yield DisplayReader(connection)


def refresh_artifact_display(report_date, *, output_dir, database=None):
    """Delivery has installed its manifest; update only the exact report link."""
    from .persistent import _exclusive_database_lock
    from .read_api import RegistryReportIdentity
    configured = database or os.getenv("CLIMATE_REGISTRY_DB", "").strip()
    if not configured:
        return
    database = Path(configured)
    with _exclusive_database_lock(database):
        with sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] < 24:
                return
            row = connection.execute("SELECT report_date,filename,report_title,report_sha256 FROM reports WHERE report_date=?", (report_date,)).fetchone()
            if row is None:
                return
            artifact = _artifact(RegistryReportIdentity(*row), str(output_dir))
            encoded = _json(artifact) if artifact else None
            cursor = connection.execute("UPDATE display_report_details SET artifact=? WHERE kind='registry' AND identity=? AND artifact IS NOT ?", (encoded, report_date, encoded))
            if cursor.rowcount:
                row = connection.execute("SELECT metadata FROM display_state WHERE singleton=1").fetchone()
                metadata = json.loads(row[0])
                metadata["revision"] = uuid.uuid4().hex
                connection.execute("UPDATE display_state SET metadata=?,generation=generation+1 WHERE singleton=1", (_json(metadata),))
