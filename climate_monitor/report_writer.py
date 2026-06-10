from __future__ import annotations

from datetime import date
from typing import Any


_MAX_WARNING_LINES = 20


def _item_value(item: Any, name: str, default: Any = "") -> Any:
    return getattr(item, name, default)


def _warning_line(warning: str) -> str:
    parts = []
    for line in str(warning or "").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("For more information check:"):
            continue
        parts.append(cleaned)
    return " ".join(parts)


def _render_warnings(warnings: list[str]) -> list[str]:
    cleaned = [line for warning in warnings if (line := _warning_line(warning))]
    lines = [f"- {warning}" for warning in cleaned[:_MAX_WARNING_LINES]]
    omitted = len(cleaned) - _MAX_WARNING_LINES
    if omitted > 0:
        lines.append(f"- {omitted} additional warning(s) omitted.")
    return lines


def _document_metadata_lines(item: Any) -> list[str]:
    if _item_value(item, "lane") != "document":
        return []

    lines: list[str] = []
    if _item_value(item, "asset_filename"):
        lines.append(f"**Document file:** {_item_value(item, 'asset_filename')} <br>")
    if _item_value(item, "asset_media_type"):
        lines.append(f"**File type:** {_item_value(item, 'asset_media_type')} <br>")
    if _item_value(item, "asset_bytes", None) is not None:
        lines.append(f"**File size:** {_item_value(item, 'asset_bytes')} bytes <br>")
    if _item_value(item, "asset_tracked_path"):
        lines.append(f"**Tracked asset:** {_item_value(item, 'asset_tracked_path')} <br>")
    if _item_value(item, "asset_id"):
        lines.append(f"**Asset ID:** {_item_value(item, 'asset_id')} <br>")
    if _item_value(item, "source_item_id"):
        lines.append(f"**Source item ID:** {_item_value(item, 'source_item_id')} <br>")
    checksum_value = _item_value(item, "asset_checksum_value")
    if checksum_value:
        checksum_algorithm = _item_value(item, "asset_checksum_algorithm") or "checksum"
        lines.append(f"**Checksum:** {checksum_algorithm}: {checksum_value} <br>")
    return lines


def _render_item(index: int, item: Any) -> str:
    title = str(_item_value(item, "title"))
    published = str(_item_value(item, "published") or _item_value(item, "detected_at") or "Unknown")
    topics = tuple(_item_value(item, "topics", ()) or ())
    topic_text = ", ".join(str(topic) for topic in topics) if topics else "climate risk"
    actuarial = "Yes" if bool(_item_value(item, "actuarial_related", False)) else "No"
    relevance_reason = str(_item_value(item, "relevance_reason", "") or "Matched monitor criteria.")
    evidence_snippet = str(_item_value(item, "evidence_snippet", "") or "")
    confidence = _item_value(item, "confidence", 0.0)
    lines = [
        f"### {index}. {title}",
        f"**Title:** {title} <br>",
        f"**Source:** {_item_value(item, 'source_name')} <br>",
        f"**Summary:** {_item_value(item, 'summary')} <br>",
        f"**URL:** {_item_value(item, 'url')} <br>",
        f"**Published:** {published} <br>",
        f"**Actuarial relevance:** {actuarial} <br>",
        f"**Climate signal:** {_item_value(item, 'climate_signal', 'general')} <br>",
        f"**Actuarial signal:** {_item_value(item, 'actuarial_signal', 'none')} <br>",
        f"**Confidence:** {confidence} <br>",
        f"**Relevance reason:** {relevance_reason} <br>",
        f"**Evidence:** {evidence_snippet} <br>",
    ]
    lines.extend(_document_metadata_lines(item))
    lines.extend(
        [
            f"**Topics:** {topic_text}",
            "",
            "---",
        ]
    )
    return "\n".join(lines)


def render_report(
    *,
    report_date: date,
    title: str,
    items: list[Any],
    dedup_notes: list[str],
    sites_monitored: int,
    warnings: list[str],
) -> str:
    website_items = [item for item in items if _item_value(item, "lane") == "website"]
    document_items = [item for item in items if _item_value(item, "lane") == "document"]
    research_items = [item for item in items if _item_value(item, "lane") == "research"]
    theme_terms = sorted({str(topic) for item in items for topic in (_item_value(item, "topics", ()) or ())})
    theme_text = ", ".join(theme_terms[:8]) if theme_terms else "climate risk and actuarial monitoring"
    summary = (
        f"This report captures {len(items)} climate-related item(s) from monitored websites, "
        f"document/report files, and recent research search. Key themes: {theme_text}."
    )

    lines = [
        f"# {title}",
        f"**Report Date:** {report_date.isoformat()}",
        "",
        "## Executive Summary",
        summary,
        "",
        "## Website Updates",
        "",
    ]
    if website_items:
        for index, item in enumerate(website_items, start=1):
            lines.append(_render_item(index, item))
    else:
        lines.extend(["No website updates matched the monitor criteria.", ""])

    lines.extend(["## Document & Report Files", ""])
    if document_items:
        for index, item in enumerate(document_items, start=1):
            lines.append(_render_item(index, item))
    else:
        lines.extend(["No document or report files matched the monitor criteria.", ""])

    lines.extend(["## New Research", ""])
    if research_items:
        for index, item in enumerate(research_items, start=1):
            lines.append(_render_item(index, item))
    else:
        lines.extend(["No new research items matched the monitor criteria.", ""])

    lines.extend(["## Dedup Notes"])
    if dedup_notes:
        lines.extend(f"- {note}" for note in dedup_notes)
    else:
        lines.append("- No duplicate items skipped.")

    if warnings:
        lines.extend(["", "## Warnings"])
        lines.extend(_render_warnings(warnings))

    lines.extend(
        [
            "",
            "## Summary",
            f"- Sites monitored: {sites_monitored}",
            f"- New items today: {len(items)}",
            f"- Key themes: {theme_text}",
            "",
        ]
    )
    return "\n".join(lines)
