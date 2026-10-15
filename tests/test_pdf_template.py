"""Traceable synthetic fixtures: no facts copied from the visual reference."""
import copy
import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest
from pypdf import PdfReader

from climate_delivery.pdf import render_pdf
from climate_registry import range_reports


def range_fixture(*, long=False):
    url = "https://example.test/" + "long-segment-" * 30
    article = {
        "article_id": "fixture-article", "content_version_id": "fixture-version",
        "canonical_url": url, "title": "Fixture 气候风险 café μ → " + "long title " * 8,
        "publication_date": "2026-09-20", "information_date": None,
        "date_basis": "publication_date", "range_date": "2026-09-20", "collected_at": None,
        "publisher": "Alpha Institution",
        "summary": "Frozen summary 中文 — μ ≥ 2.",
        "content": ("Frozen full body 中文 café μ. " * (400 if long else 2)) + "FINAL BODY SENTINEL",
        "categories": ["Physical risk"], "keywords": ["pricing"],
        "source_observations": [], "provenance": {},
        "citations": [{"kind": "url", "url": url},
                      {"kind": "pdf", "filename": "fixture-source.pdf", "page": 7}],
    }
    frozen = {
        "schema_version": range_reports.SCHEMA_VERSION,
        "date_range": {"start": "2026-09-17", "end": "2026-09-30", "inclusive": True},
        "timezone": "UTC", "articles": [article],
        "unknown_publication_date_count": 1,
        "unknown_publication_date_article_ids": ["fixture-unknown-date"],
        "date_unknown_count": 1, "date_unknown_article_ids": ["fixture-unknown-date"],
        "meeting": {"status": "failed", "source": "snapshot", "snapshot_id": None,
                    "snapshot_sha256": None, "records": [], "coverage": {"status": "failed"}},
        "pdf_calendar": {"status": "included", "coverage": {"status": "complete"},
            "records": [{"kind": "deadline", "name": f"Deadline {i}", "raw_date": "2026-10-05",
                "end_date": "2026-10-05", "source_filename": "fixture-source.pdf", "page": 8,
                "source_document_sha256": "b" * 64, "publisher": "Alpha Institution",
                "relevance": "Frozen relevance 中文."} for i in range(70 if long else 2)]},
        "pdf_source_updates": [{"pdf_article_id": "fixture-pdf-only", "title": "PDF-only fixture",
            "summary": "PDF frozen summary.", "filename": "fixture-source.pdf", "page": 9,
            "document_sha256": "b" * 64, "content_sha256": "c" * 64,
            "url": "https://example.test/pdf-only", "publication_date_label": "文章发布日期未确认",
            "coverage_period": {"start": "2026-09-01", "end": "2026-09-30"},
            "citations": [{"kind": "pdf", "filename": "fixture-source.pdf", "page": 9}]}],
        "pdf_source_exclusion_counts": {"unknown_coverage": 2, "non_overlapping_coverage": 3},
    }
    frozen["executive_summary"] = range_reports._freeze_executive_summary(frozen)
    digest = range_reports._digest(frozen)
    return {**frozen, "snapshot_id": "range-report-" + digest[:24], "snapshot_sha256": digest,
            "created_at": "2026-10-01T08:00:00Z"}


def weekly_fixture(*, long=False):
    snapshot = range_fixture(long=long)
    article = snapshot["articles"][0]
    return {
        "schema_version": 1,
        "report": {"date": "2026-09-28", "title": "Fixture weekly climate report",
                   "sha256": "a" * 64, "sites": {"checked": 4, "succeeded": 3, "failed": 1}},
        "executive_summary": [article["summary"]],
        "monitoring_notes": ["Fixture source unavailable: reader failed."],
        "highlights": [{"pillar": "A", "title": article["title"], "summary": article["content"],
                        "url": article["canonical_url"]}],
        "article_provenance": {article["canonical_url"]: {
            "article_id": article["article_id"], "content_hash": "c" * 64,
            "source": article["publisher"], "publication_date": article["publication_date"],
            "citations": article["citations"]}},
        "original_links": [article["canonical_url"]],
        "key_dates": snapshot["pdf_calendar"]["records"],
        "coverage": [{"institution": "Alpha Institution", "status": "failed", "detail": "reader failed"}],
        "route_corrections": [{"source": "Fixture", "detail": "Frozen route correction"}],
        "glossary": [{"term": "Fixture term", "definition": "Frozen definition 中文"}],
    }


def text_of(path):
    reader = PdfReader(path)
    return reader, "\n".join(page.extract_text() or "" for page in reader.pages)


@pytest.mark.parametrize("pillars", [[], ["A", "A", "B", "B", "B"]])
def test_weekly_snapshot_preserves_exact_frozen_pillar_counts(tmp_path, pillars):
    from climate_delivery.templates.adapters import adapt_weekly_report
    summary = weekly_fixture()
    highlight = summary["highlights"][0]
    summary["highlights"] = [dict(highlight, pillar=pillar, url=f"https://example.test/update-{i}")
                             for i, pillar in enumerate(pillars)]
    original = copy.deepcopy(summary)
    counts = (("Pillar A updates", str(pillars.count("A"))),
              ("Pillar B updates", str(pillars.count("B"))))
    assert adapt_weekly_report(summary).statistics[-2:] == counts
    path = tmp_path / "weekly-pillar-counts.pdf"
    render_pdf(summary, path)
    _, text = text_of(path)
    normalized = " ".join(text.split())
    for label, count in counts:
        assert f"{label} {count}" in normalized
    assert summary == original


def _refresh_snapshot_identity(snapshot):
    frozen = {key: value for key, value in snapshot.items()
              if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    snapshot["snapshot_sha256"] = range_reports._digest(frozen)
    snapshot["snapshot_id"] = "range-report-" + snapshot["snapshot_sha256"][:24]


def test_range_executive_narrative_is_source_bound_provider_variant_and_offline_safe(tmp_path):
    from types import SimpleNamespace

    snapshot = range_fixture()
    original = copy.deepcopy(snapshot)
    offline = SimpleNamespace(chat_evidence=SimpleNamespace(
        provider="offline", model="offline-extractive", client=None))
    offline_snapshot = range_reports.ensure_range_executive_narrative(snapshot, tmp_path, offline)
    assert snapshot == original
    assert offline_snapshot["executive_narrative"]["status"] == "offline"
    assert "LLM narrative status: offline" in " ".join(
        range_reports.render_range_report_chat(offline_snapshot, web_url="/report", pdf_url="/report.pdf").split())

    content = json.dumps({
        "Most important developments": ["A selected development [A1][C1]"],
        "Actuarial implications": ["A selected actuarial implication [P1]"],
        "Next 14 days": ["Review the cited issue [A1]"],
    })
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_: response)))
    configured = SimpleNamespace(chat_evidence=SimpleNamespace(
        provider="openai", model="fake-model", client=client))
    online_snapshot = range_reports.ensure_range_executive_narrative(snapshot, tmp_path, configured)
    assert online_snapshot["executive_narrative"]["status"] == "available"
    assert snapshot == original
    anthropic_response = SimpleNamespace(content=[SimpleNamespace(type="text", text=content)])
    anthropic_calls = []
    anthropic_client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: (
        anthropic_calls.append(kwargs) or anthropic_response)))
    anthropic = SimpleNamespace(chat_evidence=SimpleNamespace(
        provider="anthropic", model="fake-claude", client=anthropic_client))
    anthropic_snapshot = range_reports.ensure_range_executive_narrative(snapshot, tmp_path, anthropic)
    assert anthropic_snapshot["executive_narrative"]["status"] == "available"
    assert anthropic_calls[0]["model"] == "fake-claude" and anthropic_calls[0]["max_tokens"] == 1800
    from climate_delivery.templates.adapters import adapt_range_report
    online_report = adapt_range_report(online_snapshot)
    assert len(online_report.executive_citations[0]) == 6
    ordered_updates = sorted(online_report.updates,
        key=lambda update: (update.institution.casefold(), update.topic.casefold()))
    first_article_number = next(index for index, update in enumerate(ordered_updates, 1)
        if update.article_id == snapshot["articles"][0]["article_id"])
    assert online_report.executive_citations[0][1] == f"Update {first_article_number}: " + snapshot["articles"][0]["title"]
    pdf_update_number = next(index for index, update in enumerate(ordered_updates, 1)
        if update.title == "PDF-only fixture")
    assert online_report.executive_citations[1][0:2] == ("P1", f"Update {pdf_update_number}: PDF-only fixture")
    assert online_report.input_sha256 == snapshot["snapshot_sha256"]
    assert online_report.executive_citations[2][1].startswith("Calendar: Deadline 0 — 2026-10-05")
    failed_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_: (_ for _ in ()).throw(RuntimeError("provider unavailable")))))
    failed_responder = SimpleNamespace(chat_evidence=SimpleNamespace(
        provider="openai", model="failed-model", client=failed_client))
    failed_snapshot = range_reports.ensure_range_executive_narrative(snapshot, tmp_path, failed_responder)
    assert failed_snapshot["executive_narrative"]["status"] == "failed"
    assert range_reports.report_renderer_version(failed_snapshot).endswith(
        hashlib.sha256(range_reports._canonical(failed_snapshot["executive_narrative"]).encode()).hexdigest()[:12])
    offline_path = range_reports.ensure_range_report_pdf(offline_snapshot, tmp_path)
    online_path = range_reports.ensure_range_report_pdf(online_snapshot, tmp_path)
    assert offline_path != online_path and offline_path.is_file() and online_path.is_file()
    _, online_text = text_of(online_path)
    assert f"Update {first_article_number}: Fixture" in online_text
    assert f"Update {pdf_update_number}: PDF-only fixture" in online_text
    assert "Calendar: Deadline 0" in online_text
    assert all(f"[{identity}]" not in online_text for identity in ("A1", "P1", "C1"))
    assert "original source" in online_text and "https://example.test/" in online_text
    failed_path = range_reports.ensure_range_report_pdf(failed_snapshot, tmp_path)
    assert failed_path not in {offline_path, online_path}
    assert "-n" in range_reports.report_renderer_version(online_snapshot)
    failed_bytes = failed_path.read_bytes()
    recovery_calls = []
    recovery_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: (recovery_calls.append(kwargs) or response))))
    recovered_responder = SimpleNamespace(chat_evidence=SimpleNamespace(
        provider="openai", model="failed-model", client=recovery_client))
    recovered_snapshot = range_reports.ensure_range_executive_narrative(snapshot, tmp_path, recovered_responder)
    assert recovery_calls and recovered_snapshot["executive_narrative"]["status"] == "available"
    recovered_path = range_reports.ensure_range_report_pdf(recovered_snapshot, tmp_path)
    assert recovered_path != failed_path and failed_path.read_bytes() == failed_bytes

    snapshot_dir = tmp_path / snapshot["snapshot_id"]
    snapshot_dir.mkdir(exist_ok=True)
    (snapshot_dir / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    static_loaded = range_reports.load_range_report(tmp_path, snapshot["snapshot_id"], range_reports.RENDERER_VERSION)
    online_loaded = range_reports.load_range_report(tmp_path, snapshot["snapshot_id"],
        range_reports.report_renderer_version(online_snapshot))
    failed_loaded = range_reports.load_range_report(tmp_path, snapshot["snapshot_id"],
        range_reports.report_renderer_version(failed_snapshot))
    recovered_loaded = range_reports.load_range_report(tmp_path, snapshot["snapshot_id"],
        range_reports.report_renderer_version(recovered_snapshot))
    legacy_loaded = range_reports.load_range_report(tmp_path, snapshot["snapshot_id"], "range-report-v2")
    assert static_loaded["executive_narrative"]["status"] == "offline"
    assert online_loaded["executive_narrative"]["status"] == "available"
    assert failed_loaded["executive_narrative"]["status"] == "failed"
    assert recovered_loaded["executive_narrative"]["status"] == "available"
    assert "executive_narrative" not in legacy_loaded
    manifest = json.loads(online_path.with_suffix(".render.json").read_text(encoding="utf-8"))
    assert manifest["executive_narrative_sha256"] == range_reports._digest(online_snapshot["executive_narrative"])


def test_pdf_observations_with_shared_core_id_keep_distinct_update_targets_and_numbers(tmp_path):
    from climate_delivery.templates.adapters import adapt_range_report
    snapshot = range_fixture()
    snapshot["articles"][0]["publisher"] = "Zulu Institute"
    one = dict(snapshot["pdf_source_updates"][0], core_article_id="shared-core", publisher="Alpha Institute",
        categories=["A topic"], title="First PDF observation", url="https://example.test/pdf-one")
    two = dict(one, pdf_article_id="fixture-pdf-two", categories=["Z topic"],
        title="Second PDF observation", url="https://example.test/pdf-two")
    snapshot["pdf_source_updates"] = [one, two]
    _refresh_snapshot_identity(snapshot)
    snapshot["executive_narrative"] = {"status": "available", "evidence": [
        {"id": "A1", "article_id": snapshot["articles"][0]["article_id"], "article_index": 1,
            "title": snapshot["articles"][0]["title"], "url": snapshot["articles"][0]["canonical_url"]},
        {"id": "P1", "article_id": "shared-core", "pdf_index": 1, "title": one["title"], "url": one["url"]},
        {"id": "P2", "article_id": "shared-core", "pdf_index": 2, "title": two["title"], "url": two["url"]}],
        "sections": [{"heading": "Most important developments", "items": ["P1 [P1]", "P2 [P2]"]}]}
    report = adapt_range_report(snapshot)
    citations = {row[0]: row for row in report.executive_citations}
    update_numbers = dict(report.reader_update_targets)
    one_number, two_number = update_numbers["pdf-update-1"], update_numbers["pdf-update-2"]
    assert citations["P1"][1:5] == (f"Update {one_number}: First PDF observation", one["url"], f"update-{one_number}", "pdf-update-1")
    assert citations["P2"][1:5] == (f"Update {two_number}: Second PDF observation", two["url"], f"update-{two_number}", "pdf-update-2")
    assert citations["A1"][1].startswith(f"Update {update_numbers['article-1']}:")
    html = range_reports.render_range_report_html(snapshot)
    chat = range_reports.render_range_report_chat(snapshot, web_url="/report", pdf_url="/report.pdf")
    assert f'<article id="pdf-update-1"><h2>{one_number}. First PDF observation' in html
    assert f'<article id="pdf-update-2"><h2>{two_number}. Second PDF observation' in html
    assert f"#### {one_number}. [First PDF observation]" in chat
    assert f"#### {two_number}. [Second PDF observation]" in chat
def test_executive_evidence_ids_map_to_sorted_numbered_updates_and_source_links(tmp_path):
    from climate_delivery.templates.adapters import adapt_range_report

    snapshot = range_fixture()
    first = snapshot["articles"][0]
    first.update(publisher="Zulu Institution", article_id="source-first",
        title="Source-first article", canonical_url="https://zulu.test/zulu",
        citations=[{"kind": "url", "url": "https://zulu.test/zulu"}])
    second = copy.deepcopy(first)
    second.update(publisher="Alpha Institution", article_id="source-second",
        title="Alpha topic article", categories=["Zeta topic"], canonical_url="https://alpha.test/alpha",
        citations=[{"kind": "url", "url": "https://alpha.test/alpha"}])
    third = copy.deepcopy(second)
    third.update(article_id="source-third", title="Sorted-first article", categories=["Alpha topic"],
        canonical_url="https://alpha.test/alpha-first",
        citations=[{"kind": "url", "url": "https://alpha.test/alpha-first"}])
    snapshot["articles"] = [first, second, third]
    snapshot["executive_summary"] = range_reports._freeze_executive_summary(snapshot)
    _refresh_snapshot_identity(snapshot)
    snapshot["executive_narrative"] = {
        "status": "available", "provider": "openai", "model": "fake",
        "sections": [{"heading": "Most important developments", "items": ["Evidence from the first source [A1]"]},
            {"heading": "Actuarial implications", "items": ["Assess the second source [A2] and third [A3]"]},
            {"heading": "Next 14 days", "items": ["Review the event [C1]"]}],
        "evidence": [{"id": "A1", "title": first["title"], "article_id": "source-first",
                "article_index": 1, "url": first["canonical_url"]},
            {"id": "A2", "title": second["title"], "article_id": "source-second",
                "article_index": 2, "url": second["canonical_url"]},
            {"id": "A3", "title": third["title"], "article_id": "source-third",
                "article_index": 3, "url": third["canonical_url"]},
            {"id": "C1", "name": "Calendar item", "date": "2026-10-10",
                "sources": ["https://example.test/calendar"]}],
    }
    report = adapt_range_report(snapshot)
    evidence_labels = {row[0]: (row[1], row[3], row[4]) for row in report.executive_citations}
    assert evidence_labels["A1"] == ("Update 4: Source-first article", "update-4", "article-1")
    assert evidence_labels["A2"] == ("Update 2: Alpha topic article", "update-2", "article-2")
    assert evidence_labels["A3"] == ("Update 1: Sorted-first article", "update-1", "article-3")
    path = tmp_path / "numbered-references.pdf"
    from climate_delivery.templates.iaa_csc import render_report
    render_report(report, path)
    reader, text = text_of(path)
    assert "Update 4: Source-first article" in text
    assert "Update 2: Alpha topic article" in text and "Update 1: Sorted-first article" in text
    assert "[A1]" not in text and "[A2]" not in text and "[A3]" not in text
    annotations = [reference.get_object() for page in reader.pages for reference in page.get("/Annots", [])]
    linked_urls = {annotation.get("/A", {}).get("/URI") for annotation in annotations}
    assert "https://zulu.test/zulu" in linked_urls
    assert "https://alpha.test/alpha-first" in linked_urls
    assert "https://example.test/calendar" in linked_urls
    def outline_entries(entries):
        for entry in entries:
            if isinstance(entry, list):
                yield from outline_entries(entry)
            else:
                yield entry
    outline_titles = {entry.title for entry in outline_entries(reader.outline) if hasattr(entry, "title")}
    assert "Update 2: Alpha topic article" in outline_titles
    assert "Update 1: Sorted-first article" in outline_titles
    assert "Update 4: Source-first article" in outline_titles
    for entry in outline_entries(reader.outline):
        if hasattr(entry, "title") and entry.title.startswith("Update "):
            page_number = reader.get_destination_page_number(entry)
            assert entry.title.split(": ", 1)[1] in reader.pages[page_number].extract_text()
    chat = range_reports.render_range_report_chat(snapshot, web_url="/report", pdf_url="/report.pdf")
    assert "[Update 4: Source-first article](/report#article-1)" in chat
    assert "[original source](https://zulu.test/zulu)" in chat
    assert "[Calendar: Calendar item — 2026-10-10](/report#calendar-record-1)" in chat
    html_report = range_reports.render_range_report_html(snapshot)
    assert 'href="#calendar-record-1">[Calendar: Calendar item — 2026-10-10]</a>' in html_report
    assert '<li id="calendar-record-1">' in html_report
    assert 'href="https://example.test/calendar"' in html_report


def test_calendar_evidence_links_to_its_pdf_row_and_html_anchor(tmp_path):
    snapshot = range_fixture(long=True)
    records = [{"event_id": f"pdf-event-{index}", "canonical_event_id": f"canonical-{index}",
        "kind": "deadline", "name": f"Deadline {index - 1}", "start_date": "2026-10-05",
        "end_date": "2026-10-05", "date_precision": "day", "source_kind": "pdf",
        "source_document_sha256": "a" * 64, "source_filename": "calendar.pdf", "page": index + 1,
        "publisher": "Example Institute", "relevance": "Fixture relevance"}
        for index in range(1, 71)]
    snapshot["pdf_calendar"]["records"] = records
    _refresh_snapshot_identity(snapshot)
    snapshot["executive_narrative"] = {"status": "available", "evidence": [
        {"id": "C70", "name": "Deadline 69", "date": "2026-10-05"}],
        "sections": [{"heading": "Most important developments", "items": ["Deadline cited [C70]"]}]}
    from climate_delivery.templates.adapters import adapt_range_report
    report = adapt_range_report(snapshot)
    citation = next(row for row in report.executive_citations if row[0] == "C70")
    assert citation[3:5] == ("calendar-record-70", "calendar-record-70")

    output = tmp_path / "calendar-row-target.pdf"
    range_reports.render_range_report_pdf(snapshot, output)
    reader, _ = text_of(output)
    destination_link = next(annotation.get_object() for annotation in reader.pages[2].get("/Annots", [])
        if annotation.get_object().get("/Dest"))
    page_number = reader.get_page_number(destination_link["/Dest"][0])
    assert "Deadline 69" in (reader.pages[page_number].extract_text() or "")
    html = range_reports.render_range_report_html(snapshot)
    assert '<li id="calendar-record-70">' in html
    assert 'href="#calendar-record-70">[Calendar: Deadline 69' in html


def test_merged_calendar_rows_retain_end_uncertainty_and_pdf_locators(tmp_path):
    snapshot = range_fixture()
    native = {"event_id": "shared-event", "name": "Climate Forum", "event_type": "conference",
        "date_precision": "day", "start_date": "2026-10-20", "end_date": None,
        "needs_confirmation": 1, "raw_date": None, "organizer": "Example Institute"}
    pdf_observation = {"event_id": "pdf-event", "canonical_event_id": "shared-event", "kind": "event",
        "source_kind": "pdf", "source_document_sha256": "c" * 64, "name": "Climate Forum",
        "date_precision": "day", "start_date": "2026-10-20", "end_date": None,
        "raw_date": "20 October 2026", "source_filename": "calendar-confirmation.pdf", "page": 12}
    imported = {**pdf_observation, "pdf_observations": [pdf_observation]}
    snapshot["meeting"]["records"] = [native]
    snapshot["pdf_calendar"]["records"] = [imported]
    _refresh_snapshot_identity(snapshot)
    original = copy.deepcopy(snapshot)
    from climate_delivery.templates.adapters import adapt_range_report
    report = adapt_range_report(snapshot)
    row = report.key_dates[0]
    assert "End date unconfirmed" in row[0]
    assert "calendar-confirmation.pdf, page 12" in row[1]
    assert "calendar-confirmation.pdf, page 12" in row[4]
    assert dict(report.calendar_targets)["C1"] == dict(report.calendar_targets)["C2"]
    html = range_reports.render_range_report_html(snapshot)
    assert "End date unconfirmed" in html
    assert "Source: calendar-confirmation.pdf, page 12" in html
    path = tmp_path / "merged-calendar-details.pdf"
    range_reports.render_range_report_pdf(snapshot, path)
    _, text = text_of(path)
    normalized = " ".join(text.split())
    assert "End date unconfirmed" in normalized
    assert "Source: calendar-confirmation.pdf, page 12" in normalized
    assert snapshot == original


def test_executive_next_14_days_uses_overlap_and_independent_deadline(tmp_path):
    from types import SimpleNamespace

    snapshot = range_fixture()
    snapshot["created_at"] = "2026-10-14T08:00:00Z"
    snapshot["articles"][0].update(publication_date="2024-07-01", information_date="2026-10-12",
        date_basis="collection_time", range_date="2026-10-14", collected_at="2026-10-14T07:00:00Z")
    snapshot["meeting"]["records"] = [{"name": "COP17 meeting", "start_date": "2026-10-19",
        "end_date": "2026-10-30", "date_precision": "day"},
        {"name": "November conference with verified deadline", "start_date": "2026-11-02",
            "end_date": "2026-11-06", "date_precision": "day", "deadline_date": "2026-10-20"},
        {"name": "November conference", "start_date": "2026-11-02",
            "end_date": "2026-11-06", "date_precision": "day"}]
    _refresh_snapshot_identity(snapshot)
    content = json.dumps({
        "Most important developments": ["Selected evidence [A1]"],
        "Actuarial implications": ["Assess exposure [A1]"],
        "Next 14 days": ["Prepare for the event and deadline [C1][C2]"],
    })
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    prompts = []
    def create(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return response
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    responder = SimpleNamespace(chat_evidence=SimpleNamespace(provider="openai", model="fake",
        client=client))
    report = range_reports.ensure_range_executive_narrative(snapshot, tmp_path, responder)
    assert report["executive_narrative"]["status"] == "available"
    assert report["executive_narrative"]["next_14_calendar_ids"] == ["C1", "C2"]
    assert "Calendar IDs that fall in that interval: C1, C2." in prompts[0]
    assert '"information_date"' in prompts[0] and '"date_basis":"collection_time"' in prompts[0]


@pytest.mark.parametrize("question,start,end", [
    ("查询2026-09-20到2026-10-03的项目和当前会议", "2026-09-20", "2026-10-03"),
    ("List climate updates for the last 14 days", "2026-09-21", "2026-10-04"),
    ("What changed in the last 4 weeks?", "2026-09-07", "2026-10-04"),
    ("查询过去2周的项目和当前会议", "2026-09-21", "2026-10-04"),
    ("查询最近两周的项目", "2026-09-21", "2026-10-04"),
])
def test_period_queries_use_report_route(question, start, end):
    from datetime import date
    route = range_reports.resolve_report_route(question, today=date(2026, 10, 4),
        typesafe_router=lambda _: None)
    assert (route.action, route.start_date, route.end_date) == ("generate", start, end)


@pytest.mark.parametrize("question,action", [
    ("Explain pricing implications between 2026-09-20 and 2026-10-03", "normal_chat"),
    ("List projects from 2026-10-03 to 2026-10-05", "clarify"),
    ("查询最近53周的项目", "clarify"),
    ("List projects on 2026-09-20", "clarify"),
])
def test_period_query_validation_preserves_ordinary_chat(question, action):
    from datetime import date
    assert range_reports.resolve_report_route(question, today=date(2026, 10, 4),
        typesafe_router=lambda _: None).action == action


def test_chat_report_lists_frozen_projects_and_complete_calendar():
    snapshot = range_fixture(long=True)
    snapshot["pdf_calendar"]["records"].append({"kind": "event", "name": "Broader window",
        "start_date": "2026-11", "end_date": "2026-11", "date_precision": "month",
        "source_url": "https://example.test/event_a_b?title=(Climate)|meeting"})
    _refresh_snapshot_identity(snapshot)
    original = copy.deepcopy(snapshot)
    text = range_reports.render_range_report_chat(snapshot, web_url="/reports/snapshot",
        pdf_url="/reports/snapshot.pdf")
    assert "# Climate Risk Intelligence Report" in text
    assert "**Calendar as at:** 2026-10-01 (UTC)" in text
    assert "### example.test" in text and "FINAL BODY SENTINEL" not in text
    assert "## PDF Source Context - Publication Date Unconfirmed" in text
    assert "PDF-only fixture" in text and "not confirmed publications" in text
    assert "Meeting coverage is failed; listed entries may be incomplete." in text
    assert "Meeting snapshot status" not in text and "PDF calendar status" not in text
    assert "Imported PDF calendar coverage is complete" not in text
    assert "| Date(s) | Event | Host | Relevance |\n| --- | --- | --- | --- |\n" in text
    assert all(f"| Deadline {i}" in text for i in range(70))
    assert "### Broader Windows - Day Unconfirmed" in text
    assert "2026-11 Precision: month (future)" in text
    assert "| Not provided | Not provided |" in text
    assert "event_a_b?title=%28Climate%29%7Cmeeting" in text
    assert "fixture-source.pdf, page 8" in text
    assert "PDF import" not in text and snapshot["articles"][0]["canonical_url"] in text
    assert "[Download PDF](/reports/snapshot.pdf)" in text
    assert "b" * 64 not in text
    assert snapshot == original


@pytest.mark.parametrize("route", ["range", "weekly"])
@pytest.mark.parametrize("run_date", ["2027-06-09", "2027-06-10", "2027-06-11"])
def test_unknown_meeting_end_has_no_invented_marker(tmp_path, route, run_date):
    from climate_monitor.meetings import process_batch, query_events
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    from test_issue136_meetings import _database, _candidate
    body = "Alpha Climate Summit 2027 starts June 10, 2027; end date TBC."
    database = _database(tmp_path, [body])
    candidate = _candidate(name="Alpha Climate Summit 2027", organizer=None,
        start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
        date_evidence="June 10, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None)
    process_batch(database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda _: {"events": [candidate]})
    record = query_events(database, base_date="2027-06-11", start_date="2027-06-11",
        end_date="2027-06-11", include_unknown=True, timezone_name="UTC")["records"][0]
    assert record["needs_confirmation"] == 1 and record["end_date"] is None
    snapshot, summary = range_fixture(), weekly_fixture()
    snapshot["created_at"] = run_date + "T08:00:00Z"
    snapshot["meeting"]["records"] = [record]
    snapshot["pdf_calendar"]["records"] = []
    _refresh_snapshot_identity(snapshot)
    summary["report"]["run_date"] = run_date
    summary["key_dates"] = [record]
    original = copy.deepcopy((snapshot, summary))
    model = adapt_range_report(snapshot) if route == "range" else adapt_weekly_report(summary)
    assert not any(f"({marker})" in model.key_dates[0][0] for marker in ("past", "current", "future"))
    path = tmp_path / "unknown-end.pdf"
    if route == "range":
        range_reports.render_range_report_pdf(snapshot, path)
    else:
        render_pdf(summary, path)
    _, text = text_of(path)
    html = range_reports.render_range_report_html(snapshot)
    assert "Enddateunconfirmed" in "".join(text.split()) and "End date unconfirmed" in html
    assert (snapshot, summary) == original


def test_supported_start_only_single_day_keeps_marker():
    from climate_delivery.templates.adapters import _key_date_rows
    record = {"name": "Verified single day", "start_date": "2027-06-10",
        "end_date": None, "date_precision": "day", "needs_confirmation": 0}
    assert "(past)" in _key_date_rows(record, "2027-06-11")[0][0]


@pytest.mark.parametrize("route", ["range", "weekly"])
def test_calendar_urls_and_verbatim_context_survive_both_outputs(tmp_path, route):
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    # Shape emitted by pdf_intake calendar extraction and retained by Registry.
    record = {"kind": "event", "name": "Calendar context sentinel", "start_date": "2026-10-05",
        "end_date": "2026-10-08", "date_precision": "day", "raw_date": "5–8 October 2026",
        "source_filename": "calendar.pdf", "page": 2, "source_document_sha256": "d" * 64,
        "source_urls": ["https://example.test/calendar-primary", "https://example.test/calendar-secondary"],
        "summary": "Frozen verbatim calendar context 中文 sentinel.", "summary_basis": "verbatim_pdf_row"}
    snapshot, summary = range_fixture(), weekly_fixture()
    snapshot["pdf_calendar"]["records"] = [record]
    _refresh_snapshot_identity(snapshot)
    summary["key_dates"] = [record]
    original = copy.deepcopy((snapshot, summary))
    model = adapt_range_report(snapshot) if route == "range" else adapt_weekly_report(summary)
    assert model.key_dates[0][2:4] == ("Not provided", "Not provided")
    path = tmp_path / "calendar-context.pdf"
    if route == "range":
        range_reports.render_range_report_pdf(snapshot, path)
    else:
        render_pdf(summary, path)
    _, text = text_of(path)
    html = range_reports.render_range_report_html(snapshot)
    assert "".join(record["source_urls"][1].split()) in "".join(text.split())
    assert record["source_urls"][1] in html
    assert record["summary"] not in text and record["summary"] not in html
    assert "Verbatim context:" not in text and "PDF import" not in text
    annotations = [ref.get_object() for page in PdfReader(path).pages for ref in page.get("/Annots", [])]
    assert any(annotation.get("/A", {}).get("/URI") == record["source_urls"][0] for annotation in annotations)
    assert "calendar.pdf, page 2" in text and "calendar.pdf, page 2" in html
    assert (snapshot, summary) == original


def test_linked_pdf_update_retains_ids_in_metadata_and_displays_url(tmp_path):
    from climate_delivery.templates.adapters import adapt_range_report
    snapshot = range_fixture()
    snapshot["pdf_source_updates"][0]["core_article_id"] = "fixture-linked-core"
    _refresh_snapshot_identity(snapshot)
    original = copy.deepcopy(snapshot)
    path = tmp_path / "linked-pdf.pdf"
    range_reports.render_range_report_pdf(snapshot, path)
    _, text = text_of(path)
    html = range_reports.render_range_report_html(snapshot)
    update = adapt_range_report(snapshot).updates[-1]
    assert update.article_id == "fixture-linked-core"
    assert ("PDF article ID", "fixture-pdf-only") in update.metadata
    for value in ("fixture-linked-core", "fixture-pdf-only"):
        assert value not in text and value not in html
    assert "fixture-source.pdf, page 7" in html and "fixture-source.pdf" in text
    assert "https://example.test/pdf-only" in text and "PDF import" not in text
    assert snapshot == original


@pytest.mark.parametrize("categories", [["Climate & Health"], []])
def test_pdf_update_uses_current_categories_and_keeps_source_topic_separate(tmp_path, categories):
    from climate_delivery.templates.adapters import adapt_range_report
    snapshot = range_fixture()
    item = snapshot["pdf_source_updates"][0]
    item.update(categories=categories, topic="Old source topic")
    _refresh_snapshot_identity(snapshot)
    update = adapt_range_report(snapshot).updates[-1]
    assert update.topic == (", ".join(categories) or "PDF Source Updates")
    assert ("Source topic", "Old source topic") in update.metadata
    if categories:
        assert ("Categories", "Climate & Health") in update.metadata
        path = tmp_path / "category-pdf.pdf"
        range_reports.render_range_report_pdf(snapshot, path)
        assert "Climate & Health" in text_of(path)[1]
        assert "Climate &amp; Health" in range_reports.render_range_report_html(snapshot)
    else:
        assert not any(label == "Categories" for label, _ in update.metadata)


def test_key_dates_preserve_real_meeting_query_fields_and_sources(tmp_path):
    from datetime import datetime, timezone
    from climate_monitor.meetings import process_batch, query_events
    from climate_delivery.templates.adapters import adapt_range_report
    from test_issue136_meetings import _database, _candidate
    body = ("Example presents World Climate Summit 2027 in New York, June 10–12, 2027. "
            "Registration closes May 1, 2027 at https://example.com/register. "
            "It includes a climate risk agenda.")
    database = _database(tmp_path, [body])
    result = process_batch(database, "batch", prompt_text="extract candidates", prompt_version="v1",
        provider="openai-api", model="gpt-test", extractor=lambda _: {"events": [_candidate()]},
        now=datetime(2026, 1, 5, tzinfo=timezone.utc))
    assert result["status"] == "succeeded"
    query = query_events(database, base_date="2027-06-11", timezone_name="UTC")
    record = query["records"][0]
    snapshot = range_fixture()
    snapshot["created_at"] = "2027-06-11T08:00:00Z"
    snapshot["meeting"]["status"] = "included"
    snapshot["meeting"]["records"] = [record]
    _refresh_snapshot_identity(snapshot)
    original = copy.deepcopy(snapshot)
    model = adapt_range_report(snapshot)
    assert model.key_dates[0][2:4] == (record["organizer"], record["relevance_reason"])
    assert any(value.startswith("Registration / event link: ") for value in model.key_dates[0][1].splitlines())
    path = tmp_path / "real-meeting.pdf"
    range_reports.render_range_report_pdf(snapshot, path)
    reader, text = text_of(path)
    compact_text = "".join(text.split())
    html = range_reports.render_range_report_html(snapshot)
    for value in [record["organizer"], record["relevance_reason"], record["start_date"],
                  record["end_date"], record["raw_time_text"]]:
        assert value in html and "".join(value.split()) in compact_text
    annotations = [ref.get_object() for page in reader.pages for ref in page.get("/Annots", [])]
    linked_urls = {annotation.get("/A", {}).get("/URI") for annotation in annotations}
    for source in record["sources"]:
        assert source["source_url"] in html
        assert source["source_url"] in linked_urls or source["source_url"] in text
    if record.get("online_url"):
        assert "Registration / event link:" in html
        assert record["online_url"] in linked_urls or record["online_url"] in text
    assert "(current)" in model.key_dates[0][0]
    assert snapshot == original


@pytest.mark.parametrize("route", ["range", "weekly"])
@pytest.mark.parametrize("standalone", [True, False])
@pytest.mark.parametrize("run_date, marker", [("2027-04-20", "future"),
    ("2027-05-01", "current"), ("2027-05-02", "past")])
def test_key_dates_preserve_real_independent_deadline(tmp_path, route, standalone, run_date, marker):
    from datetime import datetime, timezone
    from climate_monitor.meetings import process_batch, query_events
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    from test_issue136_meetings import _database, _candidate
    if standalone:
        body = "Example opens Climate Consultation Deadline; consultation responses are due May 1, 2027."
        candidate = _candidate(name="Climate Consultation Deadline", event_type="deadline",
            date_precision="unknown", start_date=None, end_date=None, raw_time_text=None,
            date_evidence=None, location=None, online_url=None, deadline_type="consultation",
            deadline_date="2027-05-01", deadline_evidence="May 1, 2027")
    else:
        body = ("Example presents World Climate Summit 2027 in New York, June 10–12, 2027. "
                "Registration closes May 1, 2027 at https://example.com/register. "
                "It includes a climate risk agenda.")
        candidate = _candidate()
    database = _database(tmp_path, [body])
    result = process_batch(database, "batch", prompt_text="extract candidates", prompt_version="v1",
        provider="openai-api", model="gpt-test", extractor=lambda _: {"events": [candidate]},
        now=datetime(2026, 1, 5, tzinfo=timezone.utc))
    assert result["status"] == "succeeded"
    query = query_events(database, include_deadlines=True, base_date="2027-05-01",
        start_date="2027-05-01", end_date="2027-05-01", timezone_name="UTC")
    record = query["records"][0]
    assert record["deadline_date"] == "2027-05-01"
    snapshot = range_fixture()
    snapshot["created_at"] = run_date + "T08:00:00Z"
    snapshot["meeting"]["status"] = "included"
    snapshot["meeting"]["records"] = [record]
    snapshot["pdf_calendar"]["records"] = []
    _refresh_snapshot_identity(snapshot)
    summary = weekly_fixture()
    summary["report"]["run_date"] = run_date
    summary["key_dates"] = [record]
    original = copy.deepcopy((snapshot, summary))
    model = adapt_range_report(snapshot) if route == "range" else adapt_weekly_report(summary)
    deadline_rows = [row for row in model.key_dates if "Deadline:" in row[1]]
    assert len(deadline_rows) == 1
    deadline = deadline_rows[0]
    assert "2027-05-01" in deadline[0] and f"({marker})" in deadline[0]
    assert f"Deadline: {record['deadline_type']}" in deadline[1]
    assert record["name"] in deadline[1]
    if standalone:
        assert len(model.key_dates) == 1
    else:
        assert len(model.key_dates) == 2
        event = model.key_dates[0]
        assert event[1].startswith(record["name"] + "\nLocation: New York")
        assert "Registration / event link: https://example.com/register" in event[1]
        assert "Deadline type: registration" in event[1]
        assert "2027-06-10" in event[0] and "2027-06-12" in event[0] and "(future)" in event[0]
    path = tmp_path / f"deadline-{route}.pdf"
    if route == "range":
        range_reports.render_range_report_pdf(snapshot, path)
    else:
        render_pdf(summary, path)
    _, text = text_of(path)
    html = range_reports.render_range_report_html(snapshot)
    annotations = [ref.get_object() for page in PdfReader(path).pages for ref in page.get("/Annots", [])]
    linked_urls = {annotation.get("/A", {}).get("/URI") for annotation in annotations}
    import html as html_module
    import re
    html_text = html_module.unescape(re.sub(r"<[^>]+>", " ", html))
    html_text = " ".join(html_text.split())
    for row in model.key_dates:
        for value in row[:4]:
            assert "".join(value.split()) in "".join(text.split())
            if route == "range":
                assert " ".join(value.split()) in html_text
        for source_url in row[4].splitlines():
            if route == "range":
                assert source_url in html
            assert source_url in linked_urls or source_url in text
    assert (snapshot, summary) == original


@pytest.mark.parametrize("route", ["range", "weekly"])
@pytest.mark.parametrize("run_date, marker", [("2026-10-04", "future"), ("2026-10-05", "current"),
    ("2026-10-06", "current"), ("2026-10-08", "current"), ("2026-10-09", "past")])
def test_key_dates_keep_full_calendar_interval_and_frozen_run_marker(tmp_path, route, run_date, marker):
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report, calendar_date_bounds
    record = {"kind": "event", "name": "Forum interval sentinel", "start_date": "2026-10-05",
        "end_date": "2026-10-08", "raw_date": "5–8 October 2026", "date_precision": "day",
        "source_filename": "calendar.pdf", "page": 2, "source_document_sha256": "d" * 64}
    snapshot = range_fixture()
    snapshot["created_at"] = run_date + "T08:00:00Z"
    snapshot["pdf_calendar"]["records"] = [record]
    _refresh_snapshot_identity(snapshot)
    summary = weekly_fixture()
    summary["report"]["run_date"] = run_date
    summary["key_dates"] = [record]
    original = copy.deepcopy((snapshot, summary))
    model = adapt_range_report(snapshot) if route == "range" else adapt_weekly_report(summary)
    path = tmp_path / f"interval-{route}.pdf"
    if route == "range":
        range_reports.render_range_report_pdf(snapshot, path)
    else:
        render_pdf(summary, path)
    _, text = text_of(path)
    compact_text = "".join(text.split())
    html = range_reports.render_range_report_html(snapshot)
    assert "2026-10-05 through 2026-10-08" in model.key_dates[0][0]
    assert record["raw_date"] in model.key_dates[0][0]
    assert calendar_date_bounds(model.key_dates[0][0]) == ("2026-10-05", "2026-10-08", "day")
    assert record["raw_date"] in html and "".join(record["raw_date"].split()) in compact_text
    assert snapshot["pdf_calendar"]["records"][0]["end_date"] == "2026-10-08"
    assert (snapshot, summary) == original


@pytest.mark.parametrize("raw", ["5–8 October 2026 09:00–10:30 GMT+8", "5–8 October 2026\nAmerica/New_York"])
def test_imported_day_interval_with_clock_or_timezone_keeps_date_classification(tmp_path, raw):
    from climate_delivery.templates.adapters import adapt_range_report, calendar_date_bounds
    snapshot = range_fixture()
    record = dict(snapshot["pdf_calendar"]["records"][0], raw_date=raw,
        start_date="2026-10-05", end_date="2026-10-08", date_precision="day")
    snapshot["pdf_calendar"]["records"] = [record]
    _refresh_snapshot_identity(snapshot)
    report = adapt_range_report(snapshot)
    assert "2026-10-05 through 2026-10-08" in report.key_dates[0][0] and raw in report.key_dates[0][0]
    assert calendar_date_bounds(report.key_dates[0][0]) == ("2026-10-05", "2026-10-08", "day")
    path = tmp_path / "clock-calendar.pdf"
    range_reports.render_range_report_pdf(snapshot, path)
    _, text = text_of(path)
    assert "BROADER WINDOWS - DAY UNCONFIRMED" not in text
    assert "Broader Windows - Day Unconfirmed" not in range_reports.render_range_report_html(snapshot)


@pytest.mark.parametrize("start,raw", [("2026-10", "October 2026"), ("2026-10", "2026-10"),
    ("2026-10-05", "October 2026")])
def test_imported_month_calendar_retains_recorded_precision_and_bounds(start, raw):
    from climate_delivery.templates.adapters import adapt_range_report, calendar_date_bounds
    snapshot = range_fixture()
    record = dict(snapshot["pdf_calendar"]["records"][0], start_date=start, end_date=None,
        raw_date=raw, date_precision="month")
    snapshot["pdf_calendar"]["records"] = [record]
    _refresh_snapshot_identity(snapshot)
    row = adapt_range_report(snapshot).key_dates[0]
    assert raw in row[0] and "Precision: month" in row[0]
    assert "2026-10-05" not in row[0]
    assert calendar_date_bounds(row[0]) == ("2026-10-01", "2026-10-31", "month")


def test_merged_collected_meeting_preserves_original_pdf_date_text_and_snapshot():
    from climate_delivery.templates.adapters import adapt_range_report
    snapshot = range_fixture()
    native = {"event_id": "event-shared", "name": "Climate conference", "event_type": "conference",
        "organizer": "Example Institute", "date_precision": "day", "start_date": "2026-10-05", "end_date": "2026-10-08"}
    original = dict(native, event_id="pdf-event", raw_date="5–8 October 2026", publisher="Example Institute",
        source_document_sha256="b" * 64, source_urls=["https://example.test/event"])
    imported = dict(original, event_id="event-shared", canonical_event_id="event-shared", pdf_observations=[original])
    snapshot["meeting"]["records"] = [native]
    snapshot["pdf_calendar"]["records"] = [imported]
    _refresh_snapshot_identity(snapshot)
    frozen = copy.deepcopy(snapshot)
    report = adapt_range_report(snapshot)
    assert len(report.key_dates) == 1 and original["raw_date"] in report.key_dates[0][0]
    assert "2026-10-05 through 2026-10-08" in report.key_dates[0][0]
    assert "PDF import" not in report.key_dates[0][4]
    page = range_reports.render_range_report_html(snapshot)
    assert page.count("Climate conference") == 1
    assert 'href="https://example.test/event"' in page
    calendar = page.split('<h2 id="key-dates">', 1)[1].split('<h2 id="updates">', 1)[0]
    assert original["raw_date"] in calendar and "2026-10-05 through 2026-10-08" in calendar
    assert "PDF import" not in calendar
    assert snapshot == frozen


@pytest.mark.parametrize("start, end, raw, precision, marker", [
    ("2026-10", "2026-11", "October–November 2026", "month", "current"),
    ("2026-Q4", "2026-Q4", "Q4 2026", "quarter", "current"),
    ("2025", "2025", "2025", "year", "past"),
    (None, None, "TBA", "unknown", None),
])
def test_key_date_preserves_precision_and_all_existing_source_shapes(start, end, raw, precision, marker):
    from climate_delivery.templates.adapters import _key_date
    record = {"name": "Precision fixture", "start_date": start, "end_date": end,
        "raw_date": raw, "date_precision": precision, "organizer": "Meeting host",
        "relevance_reason": "Frozen relevance", "source_filename": "calendar.pdf", "page": 3,
        "source_url": "https://example.test/top-level", "sources": [
            {"source_url": "https://example.test/first"}, {"source_url": "https://example.test/second"}]}
    row = _key_date(record, "2026-10-06")
    for value in [raw, f"Precision: {precision}", *[value for value in (start, end) if value]]:
        assert value in row[0]
    assert row[2:4] == ("Meeting host", "Frozen relevance")
    for value in ["calendar.pdf, page 3", "https://example.test/top-level",
                  "https://example.test/first", "https://example.test/second"]:
        assert value in row[4]
    if marker:
        assert f"({marker})" in row[0]
    else:
        assert not any(f"({status})" in row[0] for status in ("past", "current", "future"))


def test_two_adapters_are_read_only_and_preserve_frozen_facts():
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    snapshot, summary = range_fixture(), weekly_fixture()
    originals = copy.deepcopy((snapshot, summary))
    range_model = adapt_range_report(snapshot)
    weekly_model = adapt_weekly_report(summary)
    assert (snapshot, summary) == originals
    assert range_model.input_sha256 == snapshot["snapshot_sha256"]
    assert weekly_model.input_sha256 == summary["report"]["sha256"]
    assert range_model.updates[0].article_id == "fixture-article"
    assert range_model.updates[0].content_version == "fixture-version"
    assert weekly_model.updates[0].content_version is None
    assert weekly_model.updates[0].content_sha256 == "c" * 64
    assert weekly_model.statistics == (("Sites checked", "4"), ("Succeeded", "3"), ("Failed", "1"),
                                       ("Pillar A updates", "1"), ("Pillar B updates", "0"))
    assert "failed" in " ".join(range_model.date_notes)
    assert range_model.updates[-1].publication_date is None
    assert range_model.updates[-1].coverage_period == ("2026-09-01", "2026-09-30")
    with pytest.raises(FrozenInstanceError):
        range_model.title = "changed"


@pytest.mark.parametrize("route", ["range", "weekly"])
@pytest.mark.parametrize("long", [False, True])
def test_shared_template_content_links_unicode_and_pagination(tmp_path, route, long):
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    input_value = range_fixture(long=long) if route == "range" else weekly_fixture(long=long)
    frozen = copy.deepcopy(input_value)
    path = tmp_path / f"{route}.pdf"
    if route == "range":
        range_reports.render_range_report_pdf(input_value, path)
    else:
        render_pdf(input_value, path)
    assert input_value == frozen
    reader, text = text_of(path)
    body_text = "\n".join("\n".join(line for line in page.extract_text().splitlines()
        if not line.startswith("IAA CSC Climate Risk Intelligence Report") and not line.startswith("AI-assisted") and not line.startswith("Page "))
        for page in reader.pages)
    expected_body = (input_value["articles"][0]["summary"] if route == "range"
                     else input_value["highlights"][0]["summary"])
    assert "".join(expected_body.split()) in "".join(body_text.split())
    assert len(reader.pages) >= 3
    for page_number, page in enumerate(reader.pages, 1):
        assert f"Page {page_number} of {len(reader.pages)}" in page.extract_text()
    for value in ["Contents", "Executive Summary", "Key Dates", "中文", "café μ"]:
        assert value in text
    assert "PDF import" not in text
    model = adapt_range_report(input_value) if route == "range" else adapt_weekly_report(input_value)
    assert model.updates[0].article_id == "fixture-article"
    for value in ("fixture-article", "SHA-256", "Template ID"):
        assert value not in text
    assert "fixture-source.pdf, page 7" in text
    outlines = []
    def walk(entries, depth=0):
        for entry in entries:
            if isinstance(entry, list):
                walk(entry, depth + 1)
            else:
                page_number = reader.get_destination_page_number(entry)
                assert 0 <= page_number < len(reader.pages)
                outlines.append((entry.title, page_number, depth))
    walk(reader.outline)
    assert any(title == "Executive Summary" for title, _, _ in outlines)
    assert any(title == "Key Dates" for title, _, _ in outlines)
    assert any(title == "example.test" for title, _, _ in outlines)
    page_indexes = {page.indirect_reference.idnum: index for index, page in enumerate(reader.pages)}
    toc_destination_pages = {page_indexes[annotation["/Dest"][0].idnum]
        for ref in reader.pages[1].get("/Annots", [])
        if (annotation := ref.get_object()).get("/Dest")}
    assert toc_destination_pages == {page_number for _, page_number, depth in outlines if depth <= 1}
    annotations = [ref.get_object() for page in reader.pages for ref in page.get("/Annots", [])]
    assert any(item.get("/Dest") for item in annotations)
    assert any(item.get("/A", {}).get("/URI", "").startswith("https://example.test/") for item in annotations)
    if long:
        assert sum("RELEVANCE" in page.extract_text() for page in reader.pages) > 1
    if route == "range":
        html = range_reports.render_range_report_html(input_value)
        for value in ["Frozen summary 中文", "PDF frozen summary."]:
            assert value in html and value in text
        assert "FINAL BODY SENTINEL" not in html and "FINAL BODY SENTINEL" not in text
        assert input_value["articles"][0]["content"].endswith("FINAL BODY SENTINEL")
        assert "article publication date unconfirmed" in text.lower()
    else:
        assert "Appendix A" in text and "Appendix B" not in text and "Appendix C" not in text
        assert "Glossary" in text
        assert "Sites checked" in text and "reader failed" in text


def test_range_body_only_fallback_is_clean_and_paginates(tmp_path):
    snapshot = range_fixture()
    article = snapshot["articles"][0]
    article["summary"] = ""
    article["content"] = ("PERSONALBUSINESSABOUT USCONTACT USLOGIN\n\n"
        + "Full body paragraph 中文, retained in the reader PDF.\n\n" * 300
        + "FINAL BODY ONLY SENTINEL")
    _refresh_snapshot_identity(snapshot)
    original_body = article["content"]
    path = tmp_path / "body-only.pdf"
    range_reports.render_range_report_pdf(snapshot, path)
    reader, text = text_of(path)
    assert len(reader.pages) >= 6
    assert "PERSONALBUSINESSABOUT" not in text
    assert "Full body paragraph 中文, retained in the reader PDF." in text
    assert "FINAL BODY ONLY SENTINEL" in text
    assert snapshot["articles"][0]["content"] == original_body


def test_range_summary_cleanup_removes_known_axa_navigation_and_duplicate_title():
    from climate_delivery.templates.adapters import _paragraphs
    title = "AXA Launches Pioneering Heatwave Parametric Insurance"
    summary = (title + " PERSONALBUSINESSABOUT AXABetterMe BlogCONTACT USLOGINOUR COMPANY"
        "NEWS ROOMJOIN USNews Room " + title + " To safeguard outdoor practitioners amid escalating climate risks.")
    paragraphs = _paragraphs(summary, "# truncated navigation body")
    assert len(paragraphs) == 1
    assert paragraphs[0] == title + " To safeguard outdoor practitioners amid escalating climate risks."


def test_range_summary_preserves_unpunctuated_prose_abbreviations_and_figures():
    from climate_delivery.templates.adapters import _paragraphs
    summary = "Losses increased. The report recommends revising flood assumptions"
    assert _paragraphs(summary, "body remains frozen") == (summary,)
    source_like = "A briefing by Dr. A. Example states. The 2026/2030 outlook is 148 186"
    assert _paragraphs(source_like, "captured source body") == (source_like,)
    comparison = "Estimated losses remain < 5% under the base scenario but exceed > 20% under stress."
    assert _paragraphs(comparison, "captured source body") == (comparison,)
    body = "Body-only evidence ends with this incomplete source phrase"
    assert _paragraphs(None, body) == (body,)


def test_swiss_re_source_summary_keeps_projection_figures_in_reader_pdf(tmp_path):
    from climate_delivery.templates.adapters import adapt_range_report
    summary = ("Media Dialogue 2026 Natural catastrophes in 2025: the persistent rise of wildfire and storm risk "
        "Lessons learned from 2025 1 Dr. Jérôme Haegeli Group Chief Economist and Head Swiss Re Institute "
        "148 186 0 50 100 150 200 250 300 350 400 450 1996 1997 1998 1999 2000 2001 2002 2003 2004 "
        "2005 2006 2007 2008 2009 2010 2011 2012 2013 2014 2015 2016 2017 2018 2019 2020 2021 2022 "
        "2023 2024 2025 2026F 2027 2028 2029 2030F Insured loss Exponential trend Trend + 1 standard deviation "
        "USD billion, 2025 prices Scenario 2026 2030 On trend 148 186 Peak loss year (1-in-10) 320 400 "
        "Losses driven by fires and sto")
    snapshot = range_fixture()
    snapshot["articles"][0]["title"] = "Swiss Re: 2025 Natural Catastrophe Losses & 2026 Outlook"
    snapshot["articles"][0]["summary"] = summary
    for point in snapshot["executive_summary"]:
        if point["kind"] == "registry_article":
            point["title"] = snapshot["articles"][0]["title"]
            point["text"] = summary
    _refresh_snapshot_identity(snapshot)

    model = adapt_range_report(snapshot)
    assert summary in model.updates[0].paragraphs
    output = tmp_path / "swiss-re-source-summary.pdf"
    range_reports.render_range_report_pdf(snapshot, output)
    _, text = text_of(output)
    normalized = " ".join(text.split())
    assert "Dr. Jérôme Haegeli" in normalized
    assert "Scenario 2026 2030 On trend 148 186" in normalized
    assert "Peak loss year (1-in-10) 320 400" in normalized


def test_core_identity_date_and_citations_fail_explicitly(tmp_path):
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    snapshot = range_fixture()
    snapshot["articles"][0]["citations"] = []
    snapshot["articles"][0]["canonical_url"] = None
    frozen = {key: value for key, value in snapshot.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    snapshot["snapshot_sha256"] = range_reports._digest(frozen)
    snapshot["snapshot_id"] = "range-report-" + snapshot["snapshot_sha256"][:24]
    with pytest.raises(ValueError, match="citation"):
        adapt_range_report(snapshot)
    weekly = weekly_fixture()
    weekly["report"]["sha256"] = "invalid"
    with pytest.raises(ValueError, match="sha256"):
        adapt_weekly_report(weekly)


def test_legacy_links_reuse_original_pdf_bytes(tmp_path, monkeypatch):
    import api_server
    snapshot = range_fixture()
    root = tmp_path / "range"
    folder = root / snapshot["snapshot_id"]
    folder.mkdir(parents=True)
    (folder / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(api_server, "RANGE_REPORT_DIR", root)
    for version in ("range-report-v1", "range-report-v2"):
        original = b"%PDF-1.4\noriginal archived " + version.encode()
        path = folder / f"{snapshot['snapshot_id']}-{version}.pdf"
        path.write_bytes(original)
        response = api_server.registry_range_report_pdf(snapshot["snapshot_id"], version)
        assert response.body == original
        assert path.read_bytes() == original


def test_render_identity_changes_without_changing_snapshot_or_old_artifact(tmp_path, monkeypatch):
    from climate_delivery import templates
    snapshot = range_fixture()
    before = copy.deepcopy(snapshot)
    old = range_reports.ensure_range_report_pdf(snapshot, tmp_path)
    original = old.read_bytes()
    monkeypatch.setattr(templates, "TEMPLATE_VERSION", "2")
    new = range_reports.ensure_range_report_pdf(snapshot, tmp_path)
    assert new != old and new.exists()
    assert old.read_bytes() == original
    assert snapshot == before


def test_weekly_independent_render_versions_have_distinct_manifests(tmp_path, monkeypatch):
    from climate_delivery import templates
    from climate_delivery.pdf import ensure_weekly_report_pdf
    summary = weekly_fixture()
    old = ensure_weekly_report_pdf(summary, tmp_path)
    original = old.read_bytes()
    manifest = json.loads((old.parent / "manifest.json").read_text())
    assert manifest["rendering"] == templates.rendering_metadata()
    assert manifest["pdf"]["sha256"] == hashlib.sha256(original).hexdigest()
    monkeypatch.setattr(templates, "TEMPLATE_VERSION", "2")
    new = ensure_weekly_report_pdf(summary, tmp_path)
    assert new != old and new.exists()
    assert old.read_bytes() == original
    assert json.loads((new.parent / "manifest.json").read_text())["rendering"]["template_version"] == "2"


@pytest.mark.parametrize("route", ["range", "weekly"])
def test_empty_inputs_omit_optional_appendices_and_state_missing_data(tmp_path, route):
    if route == "range":
        snapshot = range_fixture()
        snapshot["articles"] = []
        snapshot["pdf_source_updates"] = []
        snapshot["executive_summary"] = []
        frozen = {key: value for key, value in snapshot.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
        snapshot["snapshot_sha256"] = range_reports._digest(frozen)
        snapshot["snapshot_id"] = "range-report-" + snapshot["snapshot_sha256"][:24]
        range_reports.render_range_report_pdf(snapshot, tmp_path / "empty.pdf")
    else:
        summary = weekly_fixture()
        summary["highlights"] = []
        summary["executive_summary"] = []
        for field in ("key_dates", "coverage", "glossary", "route_corrections"):
            summary.pop(field)
        render_pdf(summary, tmp_path / "empty.pdf")
    _, text = text_of(tmp_path / "empty.pdf")
    assert "No selected updates" in text
    assert "Appendix A" not in text and "Appendix C" not in text
    if route == "weekly":
        assert "Key dates were not provided" in text and "not provided in frozen input" in text


def test_unsupported_glyph_fails_without_installing_an_incomplete_pdf(tmp_path):
    from climate_delivery.errors import GenerationError
    summary = weekly_fixture()
    summary["executive_summary"] = ["Missing glyph " + chr(0x10FFFF)]
    with pytest.raises(GenerationError, match="U\\+10FFFF"):
        render_pdf(summary, tmp_path / "unsupported.pdf")
    assert not (tmp_path / "unsupported.pdf").exists()


def test_real_cookie_consent_and_navigation_are_removed_from_reader_summary(tmp_path):
    summary = (
        "The 2026 Progress Report Skip to content Cookies 🍪 This site uses cookies that need "
        "consent. Accept all Accept selected Reject all Open main menu Our work Get involved "
        "Resources About Ceres Donate Search Search Issues we work on Climate change Advancing "
        "business solutions for a cleaner, more resilient economy. Nature and biodiversity loss "
        "Restoring and preserving our natural ecosystems."
    )
    snapshot = range_fixture()
    snapshot["articles"][0]["summary"] = summary
    for point in snapshot["executive_summary"]:
        if point["kind"] == "registry_article":
            point["text"] = summary
    _refresh_snapshot_identity(snapshot)

    output = tmp_path / "cookie-source.pdf"
    range_reports.render_range_report_pdf(snapshot, output)
    _, pdf_text = text_of(output)
    html = range_reports.render_range_report_html(snapshot)

    assert "[cookie]" not in pdf_text and "🍪" not in pdf_text
    assert "Cookies" not in pdf_text and "Skip to content" not in pdf_text
    assert "🍪" not in html and "Accept selected" not in html
    assert "Advancing business solutions" in pdf_text
    assert snapshot["articles"][0]["summary"] == summary


def test_reader_summary_keeps_complete_quoted_sentence_and_cleans_iea_navigation():
    from climate_delivery.templates.adapters import _paragraphs
    complete = 'The report found higher losses. Its conclusion was "Insurance is critical."'
    assert _paragraphs(complete, "Full body.") == (complete,)
    iea = "IEA analysis finds transition risks. About News Events Programmes Help centre Skip navigation Energy an"
    assert _paragraphs(iea, "Full article body.") == ("IEA analysis finds transition risks. Energy an",)


def test_large_cover_title_and_single_table_row_split_without_text_loss(tmp_path):
    summary = weekly_fixture()
    summary["report"]["title"] = ("Long cover 中文 🌡️ title " * 140) + "FINAL COVER SENTINEL"
    summary["glossary"] = [{"term": "Oversized row", "definition": ("Long table 中文 definition " * 600) + "FINAL TABLE SENTINEL"}]
    output = tmp_path / "oversized.pdf"
    render_pdf(summary, output)
    reader, text = text_of(output)
    assert "FINAL COVER SENTINEL" in text and "FINAL TABLE SENTINEL" in text
    assert "🌡" in text
    assert sum("DEFINITION" in page.extract_text() for page in reader.pages) >= 2


def test_range_html_and_pdf_preserve_caveats_optional_tables_and_date_basis(tmp_path):
    from climate_delivery.templates.adapters import adapt_range_report
    snapshot = range_fixture()
    snapshot["articles"][0]["caveat"] = "Frozen caveat 中文."
    snapshot["articles"][0]["provenance"] = {"publication_date": {"basis": "fixture-publisher-date", "selected": {"date": "2026-09-20"}}}
    for field in ("coverage", "route_corrections", "glossary"):
        snapshot[field] = weekly_fixture()[field]
    snapshot["cross_cutting_watch"] = ["Frozen watch text 中文."]
    frozen = {key: value for key, value in snapshot.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    snapshot["snapshot_sha256"] = range_reports._digest(frozen)
    snapshot["snapshot_id"] = "range-report-" + snapshot["snapshot_sha256"][:24]
    range_reports.render_range_report_pdf(snapshot, tmp_path / "optional.pdf")
    _, pdf = text_of(tmp_path / "optional.pdf")
    html = range_reports.render_range_report_html(snapshot)
    for value in ("Frozen caveat 中文.", "Frozen relevance 中文.", "Frozen definition 中文", "reader failed", "Frozen route correction", "Frozen watch text 中文."):
        assert value in html and value in pdf
    model = adapt_range_report(snapshot)
    assert ("Date basis", "publication date 2026-09-20") in model.updates[0].metadata
    assert "fixture-publisher-date" in html
    assert model.updates[-1].article_id == "fixture-pdf-only"
    assert "fixture-publisher-date" not in pdf and "fixture-pdf-only" not in pdf


def test_rendering_does_not_call_network_database_llm_or_mail(tmp_path, monkeypatch):
    import sqlite3
    import smtplib
    import urllib.request
    import httpx
    def forbidden(*args, **kwargs):
        pytest.fail("renderer attempted an external side effect")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    render_pdf(weekly_fixture(), tmp_path / "weekly.pdf")
    range_reports.render_range_report_pdf(range_fixture(), tmp_path / "range.pdf")


@pytest.mark.parametrize("legacy_manifest", [False, True])
def test_retained_email_retry_uses_original_pdf_after_template_change(tmp_path, monkeypatch, legacy_manifest):
    from climate_delivery import templates
    from climate_delivery.errors import DeliveryError
    from climate_delivery.pipeline import run_delivery
    from test_climate_delivery_pipeline import configure_env, delivery_report
    from test_climate_delivery_email import config_file
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output, state = tmp_path / "output", tmp_path / "state"
    config = config_file(tmp_path)
    attached_hashes = []
    reject = True
    class MockSMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def starttls(self, **kwargs): pass
        def login(self, *args): pass
        def send_message(self, message):
            attachment = next(message.iter_attachments()).get_payload(decode=True)
            attached_hashes.append(hashlib.sha256(attachment).hexdigest())
            return {str(message["To"]): (550, b"fixture rejection")} if reject else {}
    with pytest.raises(DeliveryError):
        run_delivery(report, output, state, config, smtp_factory=MockSMTP)
    manifest_path = next(output.rglob("manifest.json"))
    original_manifest = json.loads(manifest_path.read_text())
    if legacy_manifest:
        original_manifest.pop("rendering")
        manifest_path.write_text(json.dumps(original_manifest), encoding="utf-8")
    pdf_path = manifest_path.parent / original_manifest["artifacts"]["pdf"]["path"]
    original_pdf, original_summary = pdf_path.read_bytes(), (manifest_path.parent / "summary.json").read_bytes()
    original_hash = hashlib.sha256(original_pdf).hexdigest()
    assert original_manifest["delivery"]["status"] == "failed"
    monkeypatch.setattr(templates, "TEMPLATE_VERSION", "2")
    monkeypatch.setattr("climate_delivery.pipeline.render_pdf", lambda *a, **k: pytest.fail("retry rendered a new attachment"))
    reject = False
    result = run_delivery(report, output, state, config, smtp_factory=MockSMTP)
    assert result["status"] == "sent"
    assert set(attached_hashes) == {original_hash}
    assert pdf_path.read_bytes() == original_pdf
    assert (manifest_path.parent / "summary.json").read_bytes() == original_summary
    final_manifest = json.loads(manifest_path.read_text())
    assert final_manifest.get("rendering") == original_manifest.get("rendering")
    retained = json.loads(next(state.glob("*.json")).read_text())
    assert retained["artifacts"]["pdf_sha256"] == original_hash


def test_unknown_weekly_statistics_remain_unknown_in_valid_repeat_archive(tmp_path):
    from climate_delivery.pipeline import run_delivery
    from test_climate_delivery_pipeline import delivery_report, DELIVERY_REPORT
    report = delivery_report(tmp_path, text=DELIVERY_REPORT.replace(
        "Sites checked: **3**, succeeded: **2**, failed: **1**",
        "Sites checked: **unknown**, succeeded: **unknown**, failed: **unknown**"))
    output, state = tmp_path / "output", tmp_path / "state"
    first = run_delivery(report, output, state, None, artifact_only=True)
    assert run_delivery(report, output, state, None, artifact_only=True) == first


def test_prior_shared_template_download_link_survives_new_default(tmp_path, monkeypatch):
    import api_server
    from climate_delivery import templates
    snapshot = range_fixture()
    root = tmp_path / "range"
    old = range_reports.ensure_range_report_pdf(snapshot, root)
    old_bytes = old.read_bytes()
    (old.parent / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    old_version = templates.render_identity()
    monkeypatch.setattr(api_server, "RANGE_REPORT_DIR", root)
    monkeypatch.setattr(templates, "TEMPLATE_VERSION", "2")
    monkeypatch.setattr(api_server, "RENDERER_VERSION", templates.render_identity())
    new = range_reports.ensure_range_report_pdf(snapshot, root)
    assert new != old
    assert api_server.registry_range_report_pdf(snapshot["snapshot_id"], old_version).body == old_bytes
    assert old_version in api_server.registry_range_report(snapshot["snapshot_id"], old_version).body.decode()
    assert old.read_bytes() == old_bytes


def test_reviewed_pdf_meeting_aliases_follow_their_merged_pdf_and_html_row(tmp_path):
    from climate_delivery.templates.adapters import adapt_range_report
    snapshot = range_fixture()
    snapshot["meeting"]["records"] = []
    records = []
    for identity, occurrence, document, name in (("source", "source-occ", "a"*64, "IAIS 2027"),
            ("target", "target-occ", "b"*64, "IAIS 2027"),
            ("unrelated", "other-occ", "c"*64, "Other meeting")):
        observation = {"event_id": identity, "occurrence_id": occurrence, "name": name,
            "source_document_sha256": document, "source_filename": identity+".pdf", "page": 1,
            "source_urls": [], "start_date": "2026-10-05", "end_date": "2026-10-05", "date_precision": "day"}
        item = dict(observation)
        if identity != "unrelated":
            item.update(event_id="target", pdf_event_id=identity, canonical_pdf_event_id="target",
                verification_status="verified", collected_candidate={"name": name}, pdf_observations=[observation])
        records.append(item)
    snapshot["pdf_calendar"]["records"] = records
    _refresh_snapshot_identity(snapshot)
    snapshot["executive_narrative"] = {"status": "available", "evidence": [
        {"id": "C1", "name": "Source evidence", "date": "2026-10-05"},
        {"id": "C2", "name": "Target evidence", "date": "2026-10-05"},
        {"id": "C3", "name": "Other evidence", "date": "2026-10-05"}],
        "sections": [{"heading": "Most important developments", "items": ["Source [C1], target [C2], other [C3]."]}]}
    frozen = copy.deepcopy(snapshot)
    report = adapt_range_report(snapshot)
    assert dict(report.calendar_targets) == {"C1": "calendar-record-1", "C2": "calendar-record-1", "C3": "calendar-record-2"}
    assert {row[0]: row[3:5] for row in report.executive_citations} == {
        "C1": ("calendar-record-1", "calendar-record-1"), "C2": ("calendar-record-1", "calendar-record-1"),
        "C3": ("calendar-record-2", "calendar-record-2")}
    html = range_reports.render_range_report_html(snapshot)
    assert 'href="#calendar-record-1">[Calendar: Source evidence' in html
    assert 'href="#calendar-record-1">[Calendar: Target evidence' in html
    assert 'href="#calendar-record-2">[Calendar: Other evidence' in html
    output = tmp_path/"reviewed-pdf-alias-citations.pdf"
    range_reports.render_range_report_pdf(snapshot, output)
    reader, text = text_of(output)
    destinations = [ref.get_object()["/Dest"] for ref in reader.pages[2].get("/Annots", []) if ref.get_object().get("/Dest")]
    assert len(destinations) == 3
    assert destinations[0] == destinations[1] and destinations[2] != destinations[1]
    for name in ("IAIS 2027", "Other meeting"):
        assert name in text
    assert snapshot == frozen
