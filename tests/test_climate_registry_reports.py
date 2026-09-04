from pathlib import Path

from climate_monitor.taxonomy import load_article_taxonomy
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
**Categories:** Climate Risk, Capital & Solvency <br>
**Keywords:** climate; capital; Climate <br>
**URL:** https://example.org/second <br>
**Evidence:** https://example.org/second evidence text<br>
""",
        )
    )

    assert [(item.title, item.summary, item.url) for item in report.articles] == [
        ("First title", "First item summary.", "https://example.com/first"),
        ("Original source title", "Second item summary.", "https://example.org/second"),
    ]
    assert report.articles[1].categories == ("Climate Risk", "Capital & Solvency")
    assert report.articles[1].keywords == ("climate", "capital")


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
- A second source-backed observation.
## Pillar A — Changes
- **A title** (web)
  - A summary.
  - **Categories:** Physical Risk, Insurance Risk
  - **Keywords:** flood, pricing
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
    assert report.executive_summary == (
        "Sites checked: 2, succeeded: 2, failed: 0",
        "A second source-backed observation.",
    )
    assert [(item.pillar, item.title) for item in report.articles] == [("A", "A title"), ("B", "B title")]
    assert report.articles[0].categories == ("Physical Risk", "Insurance Risk")
    assert report.articles[0].keywords == ("flood", "pricing")
    assert report.articles[1].categories == ()


def test_historical_parser_hashes_the_same_byte_snapshot_it_parses(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        "2026-04-01",
        """# Daily Climate Monitor
## New Research
### Snapshot
**Summary:** One source-backed summary.
**URL:** https://example.com/snapshot
""",
    )
    original_read_bytes = Path.read_bytes
    reads = 0

    def counted_read_bytes(candidate):
        nonlocal reads
        reads += 1
        return original_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)

    report = parse_historical_report(path)

    assert reads == 1
    assert report.articles[0].summary == "One source-backed summary."


def test_weekly_historical_parser_uses_one_byte_snapshot(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        "2026-08-10",
        """# Weekly Climate Monitor
**Report Date:** 2026-08-10
## Executive Summary
- Sites checked: **1**, succeeded: **1**, failed: **0**
- One weekly observation.
## Pillar A — Changes
- **A title**
  - A summary.
  🔗 https://example.com/a
## Pillar B — Intelligence
No qualifying items.
## Original Links
- https://example.com/a
""",
    )
    original_read_bytes = Path.read_bytes
    reads = 0

    def counted_read_bytes(candidate):
        nonlocal reads
        reads += 1
        return original_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)

    report = parse_historical_report(path)

    assert reads == 1
    assert report.executive_summary[-1] == "One weekly observation."


def test_bundled_history_includes_legacy_and_weekly_formats():
    source_dir = Path(__file__).resolve().parents[1] / "sources"
    reports = parse_report_directory(source_dir, allow_offcycle=True)
    by_date = {report.report_date: report for report in reports}

    assert len(reports) >= 24
    assert all(report.articles for report in reports)
    assert len(by_date["2026-04-23"].articles) == 10
    assert all(item.title != "🔗 Read more" for item in by_date["2026-04-23"].articles)
    assert len(by_date["2026-08-10"].articles) == 30
    assert {item.pillar for item in by_date["2026-08-10"].articles} == {"A", "B"}


def test_bundled_manual_captures_preserve_dates_pillars_and_metadata():
    source_dir = Path(__file__).resolve().parents[1] / "sources"
    reports = parse_report_directory(source_dir, allow_offcycle=True)
    by_date = {report.report_date: report for report in reports}

    assert "2026-09-07" not in by_date
    assert "2026-08-31" in by_date
    assert "2026-09-03" in by_date

    august = by_date["2026-08-31"]
    september = by_date["2026-09-03"]
    assert (
        august.sites_checked,
        august.sites_succeeded,
        august.sites_failed,
    ) == (None, None, None)
    assert (
        september.sites_checked,
        september.sites_succeeded,
        september.sites_failed,
    ) == (None, None, None)
    assert [
        sum(item.pillar == pillar for item in august.articles)
        for pillar in ("A", "B")
    ] == [6, 2]
    assert [
        sum(item.pillar == pillar for item in september.articles)
        for pillar in ("A", "B")
    ] == [18, 4]
    assert all(
        item.categories and item.keywords
        for item in (*august.articles, *september.articles)
    )
    assert all(
        item.summary.endswith("no verified article synopsis was retained.")
        for item in september.articles
    )
    taxonomy = load_article_taxonomy()
    allowed_categories = taxonomy.allowed_labels
    assert all(
        set(item.categories) <= allowed_categories
        for item in (*august.articles, *september.articles)
    )
    assert all(
        taxonomy.constraints.keywords_min_items
        <= len(item.keywords)
        <= taxonomy.constraints.keywords_max_items
        for item in (*august.articles, *september.articles)
    )
    assert all(
        keyword.casefold() not in taxonomy.constraints.disallowed_keywords
        and len(keyword) <= taxonomy.constraints.keyword_max_chars
        for item in (*august.articles, *september.articles)
        for keyword in item.keywords
    )

    september_text = (source_dir / "climate-monitor-2026-09-03.md").read_text(encoding="utf-8")
    assert "**Generated:** 2026-09-03T00:00:00Z" in september_text
    assert "normalized to its actual generation date, 2026-09-03" in september_text
    assert "Retained after Registry publication checks: **22**" in september_text
