from dataclasses import dataclass
from datetime import date

from agentic_wiki.wiki_agent import URL_RE
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
    source_item_id: str = ""
    asset_id: str = ""
    asset_local_path: str = ""
    asset_canonical_blob_path: str = ""
    asset_tracked_path: str = ""
    asset_filename: str = ""
    asset_media_type: str = ""
    asset_bytes: int | None = None
    asset_checksum_algorithm: str = ""
    asset_checksum_value: str = ""
    topics: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


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
        categories=("Climate Risk", "Capital & Solvency"),
        keywords=("climate risk", "solvency"),
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
    assert "**Categories:** Climate Risk, Capital & Solvency" in text
    assert "**Keywords:** climate risk, solvency" in text
    assert "**Climate signal:** general_climate" in text
    assert "**Actuarial signal:** capital_solvency" in text
    assert "**Relevance reason:** Climate signal `general_climate` from terms: climate" in text
    assert "**Evidence:** Evidence mentions climate risk and solvency." in text
    assert "## Dedup Notes" in text
    assert "## Summary" in text
    assert "- Sites monitored: 34" in text
    assert all(line == line.rstrip() for line in text.splitlines())
    assert URL_RE.findall(text) == ["https://example.com/report.pdf"]


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


def test_render_report_places_document_files_between_website_updates_and_research_with_metadata():
    website_item = CandidateItem(
        title="Climate supervision update",
        url="https://example.com/supervision",
        summary="Website update summary.",
        source_name="IAIS",
        lane="website",
    )
    document_item = CandidateItem(
        title="Climate report PDF",
        url="https://example.com/climate-report.pdf",
        summary="Document summary.",
        source_name="IAIS",
        lane="document",
        source_item_id="file-1",
        asset_id="sha256-abc123",
        asset_local_path="C:\\Users\\ferry\\Downloads\\climate-report.pdf",
        asset_canonical_blob_path="data/downloads/_blobs/ab/abc123.pdf",
        asset_tracked_path="data/downloads/_tracked/iais/climate-report.pdf",
        asset_filename="climate-report.pdf",
        asset_media_type="application/pdf",
        asset_bytes=123456,
        asset_checksum_algorithm="sha256",
        asset_checksum_value="abc123",
    )
    research_item = CandidateItem(
        title="Climate insurance capital research",
        url="https://example.com/research",
        summary="Research summary.",
        source_name="Example Research",
        lane="research",
    )

    text = render_report(
        report_date=date(2026, 5, 14),
        title="Daily Climate & Actuarial Monitor",
        items=[website_item, document_item, research_item],
        dedup_notes=[],
        sites_monitored=34,
        warnings=[],
    )

    assert text.index("## Website Updates") < text.index("## Document & Report Files")
    assert text.index("## Document & Report Files") < text.index("Climate report PDF")
    assert text.index("Climate report PDF") < text.index("## New Research")
    assert "**Document file:** climate-report.pdf" in text
    assert "**File type:** application/pdf" in text
    assert "**File size:** 123456 bytes" in text
    assert "**Local asset:**" not in text
    assert "C:\\Users\\ferry" not in text
    assert "**Tracked asset:** data/downloads/_tracked/iais/climate-report.pdf" in text
    assert "**Canonical blob:**" not in text
    assert "**Asset ID:** sha256-abc123" in text
    assert "**Source item ID:** file-1" in text
    assert "**Checksum:** sha256: abc123" in text


def test_render_report_sanitizes_and_limits_warning_lines():
    warnings = [
        "iea seed https://iea.org: Client error '403 Forbidden'\nFor more information check: https://developer.mozilla.org/",
        *[f"warning {index}" for index in range(25)],
    ]

    text = render_report(
        report_date=date(2026, 5, 14),
        title="Daily Climate & Actuarial Monitor",
        items=[],
        dedup_notes=[],
        sites_monitored=34,
        warnings=warnings,
    )

    assert "For more information check:" not in text
    assert "- iea seed https://iea.org: Client error '403 Forbidden'" in text
    assert "- 6 additional warning(s) omitted." in text
    assert "warning 24" not in text
