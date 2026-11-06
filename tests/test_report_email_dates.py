import json
import sqlite3

from climate_registry.annotations import load_report_email_dates_catalog
from climate_registry.display import DisplayReader
from climate_registry.list_projection import RegistryListProjection, sort_reports_by_email_date


SHA = "cb1dec18f41a58f7d4b085d0406a0355faee79894ad90ccb008d2ad4ea3ac786"


def _reports():
    return [{"report_id": f"report-{index:02}", "report_date": f"2026-10-{index + 1:02}",
        "source_kind": "registry"} for index in range(1, 13)] + [
        {"report_id": SHA, "document_sha256": SHA, "report_date": None,
            "source_kind": "pdf", "report_title": "Edition 5"}]


def test_report_email_date_annotation_is_strict_and_detects_changes(tmp_path):
    status, values, absent_fingerprint = load_report_email_dates_catalog(tmp_path)
    assert (status, values) == ("absent", {})
    path = tmp_path / "reports-email-dates.json"
    payload = {"schema_version": 1, "reports": [
        {"document_sha256": SHA, "email_sent_date": "2026-10-31"}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    status, values, fingerprint = load_report_email_dates_catalog(tmp_path)
    assert status == "valid" and values == {SHA: "2026-10-31"}
    assert fingerprint != absent_fingerprint
    payload["reports"].append(payload["reports"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_report_email_dates_catalog(tmp_path)[:2] == ("invalid", {})
    payload["reports"] = [{"document_sha256": SHA, "email_sent_date": "2026-02-30"}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_report_email_dates_catalog(tmp_path)[:2] == ("invalid", {})


def test_email_date_sorts_before_pagination_without_changing_report_date(tmp_path):
    class Reader:
        def _public_candidates(self, *kinds): pass
        def approved_article_cards_all(self): return []
        def pdf_articles_all(self, **kwargs): return []
        def approved_reports_all(self): return _reports()[:-1]
        def pdf_reports_all(self): return [_reports()[-1]]
        def status(self): return {"available": True}
        def approved_merged_article_count(self): return 6
        def publishers(self, **kwargs): return {"items": [], "total": 0, "truncated": False}

    projection = RegistryListProjection(Reader())
    try:
        first = projection.reports(page=1, page_size=12, include_pdf=True,
            email_dates={SHA: "2026-10-31"})
        second = projection.reports(page=2, page_size=12, include_pdf=True,
            email_dates={SHA: "2026-10-31"})
        without_manifest = projection.reports(page=1, page_size=12, include_pdf=True)
        assert first["pagination"]["total"] == 13
        assert first["items"][0]["report_title"] == "Edition 5"
        assert first["items"][0]["email_sent_date"] == "2026-10-31"
        assert first["items"][0]["report_date"] is None
        assert len(second["items"]) == 1
        assert without_manifest["items"][0]["source_kind"] == "registry"
        assert "email_sent_date" not in without_manifest["items"][0]
        core = sort_reports_by_email_date([{"source_kind": "registry", "document_sha256": SHA,
            "report_date": "2026-10-01", "report_id": "core"}], {SHA: "2026-10-31"})[0]
        assert core["report_date"] == "2026-10-01" and "email_sent_date" not in core
    finally:
        projection.close()


def test_display_reader_and_fallback_apply_same_email_date_order(monkeypatch, tmp_path):
    import api_server

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE display_reports(report_id TEXT, report_date TEXT, source_kind TEXT, card TEXT)")
    connection.executemany("INSERT INTO display_reports VALUES(?,?,?,?)", [
        (row["report_id"], row.get("report_date") or "", row["source_kind"], json.dumps(row)) for row in _reports()])
    display = object.__new__(DisplayReader)
    display.connection, display.prefix = connection, "display_"
    payload = display.reports(page=1, page_size=12, include_pdf=True, email_dates={SHA: "2026-10-31"})
    assert payload["pagination"]["total"] == 13 and payload["items"][0]["report_title"] == "Edition 5"

    class Reader:
        def reports(self, *, page, page_size):
            rows = _reports()[:-1][(page - 1) * page_size:page * page_size]
            pages = 1
            return {"items": rows, "pagination": {"page": page, "page_size": page_size, "pages": pages}}

    monkeypatch.setattr(api_server, "_pdf_reports", lambda _reader: [_reports()[-1]])
    monkeypatch.setattr(api_server, "_report_email_dates", lambda: ("valid", {SHA: "2026-10-31"}, "fp"))
    fallback = api_server._registry_report_page(Reader(), 1, 12, include_pdf=True)
    assert fallback["pagination"]["total"] == 13 and fallback["items"][0]["report_title"] == "Edition 5"
    assert fallback["items"][0]["report_date"] is None
    monkeypatch.setattr(api_server, "_pdf_report", lambda *_args, **_kwargs: {
        "report_date": None, "articles": [], "calendar_items": [], "report_title": "Edition 5"})
    detail = api_server.registry_pdf_report(SHA)
    assert detail["report_date"] is None and detail["email_sent_date"] == "2026-10-31"
    connection.close()


def test_registry_reports_binds_email_data_and_cache_version_to_one_read(monkeypatch):
    from contextlib import contextmanager
    import api_server

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE display_reports(report_id TEXT, report_date TEXT, source_kind TEXT, card TEXT)")
    connection.executemany("INSERT INTO display_reports VALUES(?,?,?,?)", [
        (row["report_id"], row.get("report_date") or "", row["source_kind"], json.dumps(row)) for row in _reports()])
    projection = object.__new__(DisplayReader)
    projection.connection, projection.prefix = connection, "display_"
    catalogs = [("absent", {}, "a" * 32), ("valid", {SHA: "2026-10-31"}, "b" * 32)]
    reads = []

    def read_catalog():
        result = catalogs[len(reads)]
        reads.append(result)
        return result

    @contextmanager
    def project(_reader, *, report_email_catalog=None):
        projection.version = report_email_catalog[2]
        yield projection

    monkeypatch.setenv("CLIMATE_REGISTRY_DB", "unused-for-mocked-route")
    monkeypatch.setattr(api_server, "_registry_reader", lambda: object())
    monkeypatch.setattr(api_server, "_report_email_dates", read_catalog)
    monkeypatch.setattr(api_server, "_registry_list_projection", project)
    old = api_server.registry_reports(page="1", page_size="12", include_pdf=True)
    new = api_server.registry_reports(page="1", page_size="12", include_pdf=True)
    assert len(reads) == 2
    assert len(old["cache_version"]) == 32 and old["cache_version"] == "a" * 32
    assert "email_sent_date" not in old["items"][0]
    assert len(new["cache_version"]) == 32 and new["cache_version"] == "b" * 32
    assert new["items"][0]["email_sent_date"] == "2026-10-31"
    connection.close()
