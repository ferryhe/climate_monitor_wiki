import json
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4

from climate_delivery.errors import InputError
from climate_delivery.pdf import ascii_display_text, render_pdf
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


def test_original_links_allows_explanatory_bullets_and_http_markdown_links(tmp_path):
    text = REPORT.replace(
        "- https://example.test/first\n- https://example.test/second",
        "- Source status: reviewed by the monitor\n- [First source](https://example.test/first)\n- https://example.test/second",
    )
    report = parse_weekly_report(report_file(tmp_path, text=text))
    assert report.original_links == ("https://example.test/first", "https://example.test/second")


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
    ],
)
def test_invalid_weekly_report_is_rejected(tmp_path, name, text, message):
    with pytest.raises(InputError, match=message):
        parse_weekly_report(report_file(tmp_path, text=text, name=name))


def test_pdf_display_text_is_ascii_safe_for_real_weekly_report(tmp_path):
    source = Path(__file__).parents[1] / "sources" / "climate-monitor-2026-08-10.md"
    summary = build_summary(parse_weekly_report(source))
    displayed = [summary["report"]["title"], *summary["executive_summary"]]
    for item in summary["highlights"]:
        displayed.extend([item["pillar"], item["title"], item["summary"], item["url"]])

    converted = [ascii_display_text(value) for value in displayed]
    assert all(value.isascii() for value in converted)
    assert all("■" not in value and "\u25a0" not in value for value in converted)
    assert ascii_display_text("Climate 🌡️ — change → outcome • evidence") == "Climate - change -> outcome * evidence"

    summary["highlights"][0]["url"] = "https://example.test/" + "a" * 240
    output = tmp_path / "real-report.pdf"
    render_pdf(summary, output)
    assert output.read_bytes().startswith(b"%PDF")


def test_real_report_pdf_keeps_each_highlight_together_and_numbers_pages(tmp_path):
    source = Path(__file__).parents[1] / "sources" / "climate-monitor-2026-08-10.md"
    summary = build_summary(parse_weekly_report(source))
    assert len(summary["highlights"]) == 30
    output = tmp_path / "real-report.pdf"
    render_pdf(summary, output)

    reader = PdfReader(str(output))
    assert len(reader.pages) == 5
    assert reader.metadata.author == "IAA Weekly Climate Newsletter"
    first_page = reader.pages[0]
    assert float(first_page.mediabox.width) == pytest.approx(A4[0], abs=0.1)
    assert float(first_page.mediabox.height) == pytest.approx(A4[1], abs=0.1)
    pages = [page.extract_text() or "" for page in reader.pages]
    normalized_pages = [" ".join(page.split()) for page in pages]
    compact_pages = ["".join(page.split()) for page in pages]
    for page_number, page in enumerate(normalized_pages, start=1):
        assert f"Page {page_number}" in page
        assert "Weekly Climate & Actuarial Monitor - Supranational Organizations" in page

    assert "57 sites checked - 57 succeeded - 0 failed" in normalized_pages[0]
    assert "Executive Summary" in normalized_pages[0]
    assert "Monitoring Snapshot" in normalized_pages[0]
    assert "Pillar A updates 9" in normalized_pages[0]
    assert "Pillar B updates 21" in normalized_pages[0]
    assert "Pillar A" in " ".join(normalized_pages)
    assert "Pillar B" in " ".join(normalized_pages)
    first_b_title = "1. " + ascii_display_text(next(item["title"] for item in summary["highlights"] if item["pillar"] == "B"))
    first_b_page = next(page for page in normalized_pages if first_b_title in page)
    assert "Pillar B" in first_b_page

    assert len(summary["executive_summary"]) in {3, 4}
    assert not any("sites checked" in item.casefold() for item in summary["executive_summary"])
    assert "climate disclosure and reporting" in summary["executive_summary"][0]

    linked_urls = {
        annotation.get_object().get("/A", {}).get("/URI")
        for page in reader.pages
        for annotation in page.get("/Annots", [])
        if annotation.get_object().get("/Subtype") == "/Link"
    }
    assert linked_urls == {item["url"] for item in summary["highlights"]}

    pillar_numbers = {"A": 0, "B": 0}
    for item in summary["highlights"]:
        pillar_numbers[item["pillar"]] += 1
        title = ascii_display_text(item["title"])
        numbered_title = f"{pillar_numbers[item['pillar']]}. {title}"
        body = ascii_display_text(item["summary"])
        matching_pages = [index for index, page in enumerate(normalized_pages) if numbered_title in page]
        assert len(matching_pages) == 1, title
        index = matching_pages[0]
        page = normalized_pages[index]
        assert body in page, title
        assert page.index(numbered_title) < page.index(body), title
        assert "".join(ascii_display_text(item["url"]).split()) not in "".join(compact_pages), title
