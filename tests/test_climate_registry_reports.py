from pathlib import Path

from climate_registry.reports import parse_historical_report, parse_report_directory


def _write(tmp_path: Path, date: str, body: str) -> Path:
    path = tmp_path / f"climate-monitor-{date}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_parses_legacy_source_style(tmp_path):
    report = parse_historical_report(
        _write(
            tmp_path,
            "2026-04-01",
            """# Daily Climate Monitor
**Report Date:** April 1, 2026
## Website Updates
### Example Org
→ **A Real Update**
Summary: A useful climate update.
Source: https://example.com/update?utm_source=newsletter
## All Source Links
- https://example.com/update?utm_source=newsletter
""",
        )
    )

    assert report.cadence == "legacy-daily"
    assert report.report_format == "legacy-markdown"
    assert [(item.section, item.title, item.summary, item.url) for item in report.articles] == [
        (
            "legacy-website",
            "A Real Update",
            "A useful climate update.",
            "https://example.com/update?utm_source=newsletter",
        )
    ]


def test_parses_numbered_markdown_link_and_structured_fields(tmp_path):
    report = parse_historical_report(
        _write(
            tmp_path,
            "2026-04-22",
            """# Daily Climate Monitor
## New Research
### Topic Group
**1. First title** (April 2026)
First item summary.
[Link](https://example.com/first)
### 2. Heading title
**Title:** Original source title <br>
**Summary:** Second item summary. <br>
**URL:** https://example.org/second <br>
**Evidence:** https://example.org/second evidence text<br>
""",
        )
    )

    assert [(item.title, item.summary, item.url) for item in report.articles] == [
        ("First title", "First item summary.", "https://example.com/first"),
        ("Original source title", "Second item summary.", "https://example.org/second"),
    ]


def test_parses_numbered_list_title_before_generic_read_more_link(tmp_path):
    report = parse_historical_report(
        _write(
            tmp_path,
            "2026-04-23",
            """# Daily Climate Monitor
## New Research
### Climate Finance
1. **UN DESA: Financing for Sustainable Development Report 2026**
   A concise report summary.
   [🔗 Read more](https://example.com/report)
""",
        )
    )

    assert [(item.title, item.summary) for item in report.articles] == [
        ("UN DESA: Financing for Sustainable Development Report 2026", "A concise report summary.")
    ]


def test_legacy_parser_preserves_repeated_urls_for_audit(tmp_path):
    report = parse_historical_report(
        _write(
            tmp_path,
            "2026-04-24",
            """# Daily Climate Monitor
## New Research
### 1. First observation
**Summary:** First summary.
**URL:** https://example.com/same
### 2. Second observation
**Summary:** Second summary.
**URL:** https://example.com/same
""",
        )
    )

    assert [item.title for item in report.articles] == ["First observation", "Second observation"]
    assert [item.url for item in report.articles] == ["https://example.com/same"] * 2


def test_current_weekly_parser_keeps_pillars(tmp_path):
    report = parse_historical_report(
        _write(
            tmp_path,
            "2026-08-10",
            """# Weekly Climate & Actuarial Monitor
**Report Date:** 2026-08-10
## Executive Summary
- Sites checked: **2**, succeeded: **2**, failed: **0**
## Pillar A — Changes
- **A title** (web)
  - A summary.
  🔗 https://example.com/a
## Pillar B — Intelligence
- **B title** (web)
  - B summary.
  🔗 https://example.com/b
## Original Links
- https://example.com/a
- https://example.com/b
""",
        )
    )

    assert report.cadence == "weekly"
    assert report.sites_checked == 2
    assert [(item.pillar, item.title) for item in report.articles] == [("A", "A title"), ("B", "B title")]


def test_bundled_history_includes_legacy_and_weekly_formats():
    source_dir = Path(__file__).resolve().parents[1] / "sources"
    reports = parse_report_directory(source_dir)
    by_date = {report.report_date: report for report in reports}

    assert len(reports) >= 24
    assert all(report.articles for report in reports)
    assert len(by_date["2026-04-23"].articles) == 10
    assert all(item.title != "🔗 Read more" for item in by_date["2026-04-23"].articles)
    assert len(by_date["2026-08-10"].articles) == 30
    assert {item.pillar for item in by_date["2026-08-10"].articles} == {"A", "B"}
