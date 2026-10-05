import json
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4

from climate_delivery.errors import InputError
from climate_delivery.pdf import render_pdf
from climate_delivery.report import parse_weekly_report
from climate_delivery.summary import build_summary, format_scope_line, write_summary


REPORT = """# Weekly Climate Monitor

**Report Date:** 2026-08-10

## Executive Summary

- Sites checked: **3**, succeeded: **2**, failed: **1**
- One deterministic observation.

## Pillar A — Site Changes

- **First finding**
  - First supporting sentence.
  🔗 https://example.test/first

## Pillar B — Intelligence

- **Second finding** (web)
  - Second supporting sentence.
  🔗 https://example.test/second

## Original Links

- https://example.test/first
- https://example.test/second
"""


def test_scope_line_uses_shared_site_counts_with_safe_fallback():
    assert format_scope_line({"report": {"sites": {"checked": 3, "succeeded": 2, "failed": 1}}}) == (
        "3 sites checked - 2 succeeded - 1 failed"
    )
    assert format_scope_line({"report": {}}) == "Weekly report"


def report_file(tmp_path: Path, text: str = REPORT, name: str = "climate-monitor-2026-08-10.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_weekly_report_is_strictly_validated_and_summary_is_deterministic(tmp_path):
    report = parse_weekly_report(report_file(tmp_path))
    first = build_summary(report)
    second = build_summary(parse_weekly_report(report_file(tmp_path)))

    assert first == second
    assert first["schema_version"] == 1
    assert first["report"]["date"] == "2026-08-10"
    assert first["report"]["sites"] == {"checked": 3, "succeeded": 2, "failed": 1}
    assert first["executive_summary"] == [
        "This week's report contains 2 climate and actuarial updates: 1 newly detected site change and 1 wider intelligence item.",
        "New monitored-site developments include First finding.",
        "The wider intelligence set includes Second finding.",
    ]
    assert first["monitoring_notes"] == ["One deterministic observation."]
    assert [item["pillar"] for item in first["highlights"]] == ["A", "B"]
    assert first["highlights"][0]["url"] == "https://example.test/first"
    assert first["original_links"] == ["https://example.test/first", "https://example.test/second"]

    output = tmp_path / "summary.json"
    write_summary(first, output)
    assert json.loads(output.read_text(encoding="utf-8")) == first
    assert not list(tmp_path.glob("*.tmp"))


def test_delivery_reuses_monitor_narrative_in_summary_and_pdf(tmp_path):
    paragraphs = [
        "Flood risk changes require updated insurance pricing assumptions.",
        "Supervisors should compare capital resilience under climate scenarios.",
    ]
    text = REPORT.replace("- One deterministic observation.", "\n\n".join(paragraphs))
    report = parse_weekly_report(report_file(tmp_path, text=text))
    summary = build_summary(report)
    assert summary["executive_summary"] == paragraphs
    assert report.executive_summary == tuple(paragraphs)
    output = tmp_path / "same-summary.pdf"
    render_pdf(summary, output)
    pdf_text = " ".join(" ".join(page.extract_text().split()) for page in PdfReader(output).pages)
    assert all(paragraph in pdf_text for paragraph in paragraphs)
    assert "This week's report contains" not in pdf_text


def test_delivery_carries_coverage_limitations_without_polluting_executive_prose(tmp_path):
    text = REPORT.replace(
        "- One deterministic observation.",
        "Verified eligible evidence was retained.\n\n"
        "### Coverage Limitations\n\n"
        "- Pillar A source wmo was blocked: scope.acquisition_failed",
    )
    report = parse_weekly_report(report_file(tmp_path, text=text))
    summary = build_summary(report)
    assert report.executive_summary == ("Verified eligible evidence was retained.",)
    assert summary["monitoring_notes"] == [
        "Pillar A source wmo was blocked: scope.acquisition_failed"
    ]
    output = tmp_path / "partial.pdf"
    render_pdf(summary, output)
    pdf_text = " ".join(" ".join(page.extract_text().split()) for page in PdfReader(output).pages)
    assert "scope.acquisition_failed" in pdf_text


def test_failed_item_identity_and_reason_reach_monitoring_notes_and_pdf(tmp_path):
    from climate_registry.acquisition import build_reportability_projection

    failed_url = "https://example.org/excluded-item"
    projection = build_reportability_projection({
        "completed_at": None, "source_outcomes": [], "searches": [],
        "items": [{
            "url": failed_url, "title": "Excluded climate filing",
            "processing_status": "failed", "processing_error": "reader timed out",
            "evidence": {"failure_reason": "fallback reason"},
        }],
        "blocked_tool_prechecks": [],
    }, {"record_count": 1})
    limitation = (
        "Excluded item Excluded climate filing "
        f"({failed_url}): reader timed out."
    )
    assert limitation in projection["limitations"]
    text = REPORT.replace(
        "- One deterministic observation.",
        "Verified eligible evidence was retained.\n\n"
        "### Coverage Limitations\n\n" + "\n".join(
            f"- {value}" for value in projection["limitations"]
        ),
    )
    report = parse_weekly_report(report_file(tmp_path, text=text))
    summary = build_summary(report)
    assert limitation in summary["monitoring_notes"]
    assert failed_url not in report.original_links
    output = tmp_path / "failed-item-partial.pdf"
    render_pdf(summary, output)
    pdf_text = " ".join(
        " ".join(page.extract_text().split()) for page in PdfReader(output).pages
    )
    assert "Excluded climate filing" in pdf_text
    assert "reader timed out" in pdf_text


def test_weekly_report_preserves_explicitly_unknown_site_counts(tmp_path):
    text = REPORT.replace(
        "Sites checked: **3**, succeeded: **2**, failed: **1**",
        "Sites checked: **unknown**, succeeded: **unknown**, failed: **unknown**",
    )

    report = parse_weekly_report(report_file(tmp_path, text=text))
    summary = build_summary(report)

    assert (report.checked, report.succeeded, report.failed) == (None, None, None)
    assert summary["report"]["sites"] == {
        "checked": None,
        "succeeded": None,
        "failed": None,
    }
    assert format_scope_line(summary) == "Weekly report"


def test_original_links_allows_explanatory_bullets_and_http_markdown_links(tmp_path):
    text = REPORT.replace(
        "- https://example.test/first\n- https://example.test/second",
        "- Source status: reviewed by the monitor\n- [First source](https://example.test/first)\n- https://example.test/second",
    )
    report = parse_weekly_report(report_file(tmp_path, text=text))
    assert report.original_links == ("https://example.test/first", "https://example.test/second")


def test_weekly_highlights_parse_explicit_semantic_metadata(tmp_path):
    text = REPORT.replace(
        "  - First supporting sentence.\n  🔗 https://example.test/first",
        "  - First supporting sentence.\n"
        "  - **Categories:** Physical Risk, Insurance Risk\n"
        "  - **Keywords:** flood; pricing; Flood\n"
        "  🔗 https://example.test/first",
    )
    report = parse_weekly_report(report_file(tmp_path, text=text))

    assert report.highlights[0].categories == ("Physical Risk", "Insurance Risk")
    assert report.highlights[0].keywords == ("flood", "pricing")
    assert report.highlights[1].categories == ()
    assert report.highlights[1].keywords == ()


def test_content_executive_summary_stays_three_to_four_sentences_when_a_pillar_is_empty(tmp_path):
    text = REPORT.replace(
        "- **First finding**\n  - First supporting sentence.\n  🔗 https://example.test/first",
        "No qualifying site change was reported.",
    )
    summary = build_summary(parse_weekly_report(report_file(tmp_path, text=text)))

    assert len(summary["executive_summary"]) == 3
    assert "No newly detected site change" in summary["executive_summary"][1]


def test_content_executive_summary_uses_singular_update_for_one_themed_highlight(tmp_path):
    text = REPORT.replace("First finding", "Climate reporting finding").replace(
        "- **Second finding** (web)\n  - Second supporting sentence.\n  🔗 https://example.test/second",
        "No qualifying wider-intelligence item was reported.",
    )
    summary = build_summary(parse_weekly_report(report_file(tmp_path, text=text)))

    assert summary["executive_summary"][0].startswith(
        "Across 1 update, this week's evidence concentrated on climate disclosure and reporting."
    )


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("report.md", REPORT, "filename"),
        ("climate-monitor-2026-08-11.md", REPORT.replace("2026-08-10", "2026-08-11", 1), "Monday"),
        ("climate-monitor-2026-08-10.md", REPORT.replace("2026-08-10", "2026-08-03", 1), "filename"),
        ("climate-monitor-2026-08-10.md", REPORT.replace("# Weekly Climate Monitor", "plain text", 1), "H1"),
        ("climate-monitor-2026-08-10.md", REPORT.replace("## Executive Summary", "## Overview", 1), "Executive Summary"),
        ("climate-monitor-2026-08-10.md", REPORT.replace("## Pillar A — Site Changes", "## Site Changes", 1), "Pillar A"),
        ("climate-monitor-2026-08-10.md", REPORT.replace("## Pillar B — Intelligence", "## Intelligence", 1), "Pillar B"),
        ("climate-monitor-2026-08-10.md", REPORT.replace("## Original Links", "## Links", 1), "Original Links"),
        (
            "climate-monitor-2026-08-10.md",
            REPORT + "\n## Executive Summary\n\n- duplicate\n",
            "exactly one Executive Summary",
        ),
        (
            "climate-monitor-2026-08-10.md",
            REPORT.replace("https://example.test/first", "ftp://example.test/first", 1),
            "HTTP",
        ),
        (
            "climate-monitor-2026-08-10.md",
            REPORT.replace("- https://example.test/first\n- https://example.test/second", "- ftp://example.test/first\n- https://example.test/second"),
            "Original Links.*HTTP",
        ),
        (
            "climate-monitor-2026-08-10.md",
            REPORT + "\n- [unsafe link](javascript:alert(1))\n",
            "Original Links.*HTTP",
        ),
        (
            "climate-monitor-2026-08-10.md",
            REPORT.replace("Sites checked: **3**, succeeded: **2**, failed: **1**", "Sites checked: 3"),
            "checked",
        ),
        (
            "climate-monitor-2026-08-10.md",
            REPORT.replace(
                "Sites checked: **3**, succeeded: **2**, failed: **1**",
                "Sites checked: **3**, succeeded: **3**, failed: **1**",
            ),
            "sum",
        ),
        (
            "climate-monitor-2026-08-10.md",
            REPORT.replace(
                "Sites checked: **3**, succeeded: **2**, failed: **1**",
                "Sites checked: **unknown**, succeeded: **2**, failed: **1**",
            ),
            "all be integers or all unknown",
        ),
    ],
)
def test_invalid_weekly_report_is_rejected(tmp_path, name, text, message):
    with pytest.raises(InputError, match=message):
        parse_weekly_report(report_file(tmp_path, text=text, name=name))


def test_offcycle_weekly_report_requires_explicit_opt_in(tmp_path):
    tuesday_text = REPORT.replace("2026-08-10", "2026-08-11", 1)
    path = report_file(
        tmp_path, text=tuesday_text, name="climate-monitor-2026-08-11.md"
    )
    with pytest.raises(InputError, match="Monday"):
        parse_weekly_report(path)
    report = parse_weekly_report(path, allow_offcycle=True)
    assert report.report_date == "2026-08-11"


def test_pdf_preserves_unicode_and_long_source_links(tmp_path):
    source = Path(__file__).parents[1] / "sources" / "climate-monitor-2026-08-10.md"
    summary = build_summary(parse_weekly_report(source))
    summary["highlights"][0]["url"] = "https://example.test/" + "a" * 240
    summary["highlights"][0]["summary"] += " Unicode evidence: 气候 café μ → outcome."
    output = tmp_path / "real-report.pdf"
    render_pdf(summary, output)
    reader = PdfReader(output)
    text = " ".join(" ".join(page.extract_text().split()) for page in reader.pages)
    assert "气候 café μ → outcome" in text
    assert "■" not in text
    assert output.read_bytes().startswith(b"%PDF")


def test_real_report_pdf_retains_all_content_links_and_page_totals(tmp_path):
    source = Path(__file__).parents[1] / "sources" / "climate-monitor-2026-08-10.md"
    summary = build_summary(parse_weekly_report(source))
    assert len(summary["highlights"]) == 30
    output = tmp_path / "real-report.pdf"
    render_pdf(summary, output)
    reader = PdfReader(output)
    assert reader.metadata.author == "IAA Weekly Climate Newsletter"
    assert float(reader.pages[0].mediabox.width) == pytest.approx(595.92, abs=0.1)
    assert float(reader.pages[0].mediabox.height) == pytest.approx(842.88, abs=0.1)
    pages = [page.extract_text() or "" for page in reader.pages]
    normalized = " ".join(" ".join(page.split()) for page in pages)
    for number, page in enumerate(pages, 1):
        assert f"Page {number} of {len(pages)}" in page
    for label, count in [("Sites checked", 57), ("Succeeded", 57), ("Failed", 0),
                         ("Pillar A updates", 9), ("Pillar B updates", 21)]:
        assert f"{label} {count}" in normalized
    assert "Executive Summary" in normalized and "MONITORING SNAPSHOT" in normalized
    for item in summary["highlights"]:
        assert " ".join(item["title"].split()) in normalized
        assert " ".join(item["summary"].split()) in normalized
    linked_urls = {annotation.get_object().get("/A", {}).get("/URI")
        for page in reader.pages for annotation in page.get("/Annots", [])
        if annotation.get_object().get("/A", {}).get("/URI")}
    assert linked_urls == {item["url"] for item in summary["highlights"]}
