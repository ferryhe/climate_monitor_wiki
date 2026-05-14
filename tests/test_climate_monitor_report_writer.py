from dataclasses import dataclass
from datetime import date

from climate_monitor.report_writer import render_report


@dataclass(frozen=True)
class CandidateItem:
    title: str
    url: str
    summary: str
    source_name: str
    lane: str
    published: str = ""
    detected_at: str = ""
    climate_related: bool = True
    actuarial_related: bool = True
    relevance_reason: str = ""
    climate_signal: str = "none"
    actuarial_signal: str = "none"
    confidence: float = 0.0
    evidence_snippet: str = ""
    topics: tuple[str, ...] = ()


def test_render_report_matches_existing_source_shape():
    item = CandidateItem(
        title="Climate solvency report",
        url="https://example.com/report.pdf",
        summary="A concise English summary.",
        source_name="IAIS",
        lane="website",
        published="2026-05-01",
        climate_related=True,
        actuarial_related=True,
        topics=("solvency", "climate risk"),
        relevance_reason="Climate signal `general_climate` from terms: climate",
        climate_signal="general_climate",
        actuarial_signal="capital_solvency",
        confidence=0.7,
        evidence_snippet="Evidence mentions climate risk and solvency.",
    )

    text = render_report(
        report_date=date(2026, 5, 14),
        title="Daily Climate & Actuarial Monitor",
        items=[item],
        dedup_notes=["Older duplicate skipped"],
        sites_monitored=34,
        warnings=[],
    )

    assert text.startswith("# Daily Climate & Actuarial Monitor")
    assert "**Report Date:** 2026-05-14" in text
    assert "## Executive Summary" in text
    assert "## Website Updates" in text
    assert "## New Research" in text
    assert "**Title:** Climate solvency report" in text
    assert "**URL:** https://example.com/report.pdf" in text
    assert "**Climate signal:** general_climate" in text
    assert "**Actuarial signal:** capital_solvency" in text
    assert "**Relevance reason:** Climate signal `general_climate` from terms: climate" in text
    assert "**Evidence:** Evidence mentions climate risk and solvency." in text
    assert "## Dedup Notes" in text
    assert "## Summary" in text
    assert "- Sites monitored: 34" in text


def test_render_report_groups_website_updates_and_research_items():
    website_item = CandidateItem(
        title="Climate supervision update",
        url="https://example.com/supervision",
        summary="Website update summary.",
        source_name="IAIS",
        lane="website",
    )
    research_item = CandidateItem(
        title="Climate insurance capital research",
        url="https://example.com/research",
        summary="Research summary.",
        source_name="Example Research",
        lane="research",
        actuarial_related=False,
    )

    text = render_report(
        report_date=date(2026, 5, 14),
        title="Daily Climate & Actuarial Monitor",
        items=[website_item, research_item],
        dedup_notes=[],
        sites_monitored=34,
        warnings=["warning text"],
    )

    assert text.index("## Website Updates") < text.index("Climate supervision update")
    assert text.index("## New Research") < text.index("Climate insurance capital research")
    assert "## Warnings" in text
    assert "- warning text" in text
