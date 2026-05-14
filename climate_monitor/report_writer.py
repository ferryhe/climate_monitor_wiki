from __future__ import annotations

from datetime import date
from typing import Any


def _item_value(item: Any, name: str, default: Any = "") -> Any:
    return getattr(item, name, default)


def _document_metadata_lines(item: Any) -> list[str]:
    if _item_value(item, "lane") != "document":
        return []

    lines: list[str] = []
    if _item_value(item, "asset_filename"):
        lines.append(f"**Document file:** {_item_value(item, 'asset_filename')}  ")
    if _item_value(item, "asset_media_type"):
        lines.append(f"**File type:** {_item_value(item, 'asset_media_type')}  ")
    if _item_value(item, "asset_bytes", None) is not None:
        lines.append(f"**File size:** {_item_value(item, 'asset_bytes')} bytes  ")
    if _item_value(item, "asset_tracked_path"):
        lines.append(f"**Tracked asset:** {_item_value(item, 'asset_tracked_path')}  ")
    if _item_value(item, "asset_id"):
        lines.append(f"**Asset ID:** {_item_value(item, 'asset_id')}  ")
    if _item_value(item, "source_item_id"):
        lines.append(f"**Source item ID:** {_item_value(item, 'source_item_id')}  ")
    checksum_value = _item_value(item, "asset_checksum_value")
    if checksum_value:
        checksum_algorithm = _item_value(item, "asset_checksum_algorithm") or "checksum"
        lines.append(f"**Checksum:** {checksum_algorithm}: {checksum_value}  ")
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
        f"**Title:** {title}  ",
        f"**Source:** {_item_value(item, 'source_name')}  ",
        f"**Summary:** {_item_value(item, 'summary')}  ",
        f"**URL:** {_item_value(item, 'url')}  ",
        f"**Published:** {published}  ",
        f"**Actuarial relevance:** {actuarial}  ",
        f"**Climate signal:** {_item_value(item, 'climate_signal', 'general')}  ",
        f"**Actuarial signal:** {_item_value(item, 'actuarial_signal', 'none')}  ",
        f"**Confidence:** {confidence}  ",
        f"**Relevance reason:** {relevance_reason}  ",
        f"**Evidence:** {evidence_snippet}  ",
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
        lines.extend(f"- {warning}" for warning in warnings)

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
