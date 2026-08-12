import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from climate_delivery.errors import InputError
from climate_delivery.pdf import ascii_display_text, render_pdf
from climate_delivery.report import parse_weekly_report
from climate_delivery.summary import build_summary, write_summary


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

    pages = [page.extract_text() or "" for page in PdfReader(str(output)).pages]
    normalized_pages = [" ".join(page.split()) for page in pages]
    compact_pages = ["".join(page.split()) for page in pages]
    for page_number, page in enumerate(normalized_pages, start=1):
        assert f"Page {page_number}" in page

    for item in summary["highlights"]:
        title = ascii_display_text(item["title"])
        body = ascii_display_text(item["summary"])
        url = ascii_display_text(item["url"])
        matching_pages = [index for index, page in enumerate(normalized_pages) if title in page]
        assert len(matching_pages) == 1, title
        index = matching_pages[0]
        page = normalized_pages[index]
        assert body in page, title
        assert "".join(url.split()) in compact_pages[index], title
        assert page.index(title) < page.index(body), title
        assert compact_pages[index].index("".join(body.split())) < compact_pages[index].index("".join(url.split())), title
