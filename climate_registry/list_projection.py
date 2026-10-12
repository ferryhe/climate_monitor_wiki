"""Rebuildable list data; approved Registry snapshots remain the business source."""
from __future__ import annotations

import json
import sqlite3

from .publication import _pdf_dto, _public_dto
from .read_api import RegistryQueryError, _pagination, filter_pdf_articles, validate_page, validate_report_date


ARTICLE_FIELDS = {
    "article_id", "canonical_url", "title", "publisher", "source", "source_kind", "source_label",
    "first_seen", "last_seen", "pdf_occurrence_count", "occurrence_count",
}
REPORT_FIELDS = {
    "report_id", "document_sha256", "source_kind", "report_title", "report_date", "filename",
    "source_label", "article_count", "calendar_count", "page_count", "cadence", "report_format",
    "sites_checked", "sites_succeeded", "sites_failed", "monitoring_status",
}


class RegistryListProjection:
    def __init__(self, reader):
        # Prime once so all readers share candidate decoding and hidden-alias propagation.
        reader._public_candidates("article", "pdf_article", "meeting", "pdf_meeting")
        core = reader.approved_article_cards_all()
        pdf = reader.pdf_articles_all(include_linked=True)
        reports = reader.approved_reports_all() + reader.pdf_reports_all()
        self.status_core = reader.status()
        self.status_combined = dict(self.status_core, articles=reader.approved_merged_article_count(),
            reports=len(reports), latest_report_date=max((item.get("report_date") or "" for item in reports), default="") or None)
        publishers = {include_pdf: reader.publishers(include_pdf=include_pdf) for include_pdf in (False, True)}
        db = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection = db
        try:
            db.executescript("""
                CREATE TABLE articles (url TEXT PRIMARY KEY, article_id TEXT, last_seen TEXT, first_seen TEXT,
                    core_card TEXT, merged_card TEXT, core_search TEXT, core_source TEXT, publisher TEXT,
                    source_id TEXT, pdf_search TEXT, pdf_source TEXT);
                CREATE TABLE appearances (url TEXT, kind TEXT, report_date TEXT, pillar TEXT);
                CREATE INDEX appearances_url ON appearances(url, kind, report_date, pillar);
                CREATE TABLE reports (report_id TEXT, report_date TEXT, source_kind TEXT, card TEXT);
                CREATE TABLE publishers (include_pdf INTEGER, position INTEGER, card TEXT);
            """)
            by_url = {item["canonical_url"]: item for item in core}
            pdf_by_url = {item["canonical_url"]: item for item in pdf}
            keyword_items = []
            for url in by_url.keys() | pdf_by_url.keys():
                original = by_url.get(url)
                imported = pdf_by_url.get(url)
                core_item = ({key: value for key, value in original.items() if key not in {"appearances", "source_id"}}
                    if original else None)
                if core_item is not None:
                    merged = dict(core_item, source_kind="registry")
                    if imported:
                        merged.update(source_label="Registry · PDF import", pdf_occurrence_count=len(imported["occurrences"]))
                    keyword_items.append(_public_dto(merged))
                else:
                    merged = filter_pdf_articles([imported])[0]
                    keyword_items.append(_pdf_dto(merged))
                card = {key: value for key, value in merged.items() if key in ARTICLE_FIELDS}
                core_card = ({key: value for key, value in core_item.items() if key in ARTICLE_FIELDS}
                    if core_item is not None else None)
                core_search = " ".join(str(original.get(key) or "") for key in
                    ("title", "summary", "canonical_url", "categories", "keywords")).casefold() if original else None
                pdf_search = " ".join((imported["title"] or "", imported["summary"] or "", url)).casefold() if imported else None
                db.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                    url, merged["article_id"], merged.get("last_seen") or "", merged.get("first_seen") or "",
                    json.dumps(core_card, ensure_ascii=False) if core_card is not None else None,
                    json.dumps(card, ensure_ascii=False), core_search, original.get("source") if original else None,
                    original.get("publisher") if original else None, original.get("source_id") if original else None,
                    pdf_search, imported.get("source") if imported else None))
                db.executemany("INSERT INTO appearances VALUES (?,?,?,?)", (
                    (url, "core", row.get("report_date"), row.get("pillar")) for row in (original or {}).get("appearances", [])))
                db.executemany("INSERT INTO appearances VALUES (?,?,?,?)", (
                    (url, "pdf", row.get("report_date"), None) for row in (imported or {}).get("occurrences", [])))
            keyword_items.sort(key=lambda item: (item.get("last_seen") or "", item["article_id"]), reverse=True)
            self.keyword_body = json.dumps({"items": keyword_items}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            db.executemany("INSERT INTO reports VALUES (?,?,?,?)", (
                (item.get("report_id") or "", item.get("report_date") or "", item.get("source_kind") or "registry",
                    json.dumps({key: value for key, value in item.items() if key in REPORT_FIELDS}, ensure_ascii=False))
                for item in reports))
            self.publisher_metadata = {}
            for include_pdf, payload in publishers.items():
                self.publisher_metadata[include_pdf] = {key: value for key, value in payload.items() if key != "items"}
                db.executemany("INSERT INTO publishers VALUES (?,?,?)", (
                    (include_pdf, index, json.dumps(item, ensure_ascii=False)) for index, item in enumerate(payload["items"])))
            db.commit()
            db.execute("PRAGMA query_only=ON")
        except Exception:
            db.close()
            raise

    def close(self):
        self.connection.close()

    def articles(self, *, page, page_size, include_pdf=False, query="", source="", pillar="", report_date=""):
        validate_page(page, page_size)
        if len(query) > 200 or len(source) > 253:
            raise RegistryQueryError("filter is too long")
        if pillar and pillar not in {"A", "B"}:
            raise RegistryQueryError("invalid pillar")
        if report_date:
            validate_report_date(report_date)
        core = ["a.core_card IS NOT NULL"]
        params = []
        if query.strip():
            core.append("instr(a.core_search, ?) > 0")
            params.append(query.strip().casefold())
        if source.strip():
            core.append("? IN (a.core_source, a.publisher, a.source_id)")
            params.append(source.strip())
        if report_date or pillar:
            appearance = ["x.url=a.url", "x.kind='core'"]
            if report_date:
                appearance.append("x.report_date=?")
                params.append(report_date)
            if pillar:
                appearance.append("x.pillar=?")
                params.append(pillar)
            core.append("EXISTS (SELECT 1 FROM appearances x WHERE " + " AND ".join(appearance) + ")")
        where = "(" + " AND ".join(core) + ")"
        if include_pdf and not pillar:
            pdf = ["a.pdf_search IS NOT NULL"]
            if query:
                pdf.append("instr(a.pdf_search, ?) > 0")
                params.append(query.casefold())
            if source:
                pdf.append("a.pdf_source=?")
                params.append(source.strip().lower().removeprefix("www."))
            if report_date:
                pdf.append("EXISTS (SELECT 1 FROM appearances x WHERE x.url=a.url AND x.kind='pdf' AND x.report_date=?)")
                params.append(report_date)
            where += " OR (" + " AND ".join(pdf) + ")"
        total = self.connection.execute("SELECT COUNT(*) FROM articles a WHERE " + where, params).fetchone()[0]
        column = "merged_card" if include_pdf else "core_card"
        order = "a.last_seen DESC, " + ("" if include_pdf else "a.first_seen DESC, ") + "a.article_id DESC"
        rows = self.connection.execute(f"SELECT {column} FROM articles a WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size))
        return {"items": [json.loads(row[0]) for row in rows], "pagination": _pagination(page, page_size, total)}

    def reports(self, *, page, page_size, include_pdf=False):
        validate_page(page, page_size)
        where = "" if include_pdf else " WHERE source_kind='registry'"
        total = self.connection.execute("SELECT COUNT(*) FROM reports" + where).fetchone()[0]
        order = "report_date DESC, report_id DESC" if include_pdf else "report_date DESC, report_id"
        rows = self.connection.execute("SELECT card FROM reports" + where + " ORDER BY " + order + " LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size))
        return {"items": [json.loads(row[0]) for row in rows], "pagination": _pagination(page, page_size, total)}

    def publishers(self, *, include_pdf=False):
        rows = self.connection.execute("SELECT card FROM publishers WHERE include_pdf=? ORDER BY position LIMIT 500", (include_pdf,))
        return {**self.publisher_metadata[include_pdf], "items": [json.loads(row[0]) for row in rows]}
