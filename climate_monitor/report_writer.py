from __future__ import annotations

from datetime import date
from typing import Any


def _item_value(item: Any, name: str, default: Any = "") -> Any:
    return getattr(item, name, default)


def _render_item(index: int, item: Any) -> str:
    title = str(_item_value(item, "title"))
    published = str(_item_value(item, "published") or _item_value(item, "detected_at") or "Unknown")
    topics = tuple(_item_value(item, "topics", ()) or ())
    topic_text = ", ".join(str(topic) for topic in topics) if topics else "climate risk"
    actuarial = "Yes" if bool(_item_value(item, "actuarial_related", False)) else "No"
    relevance_reason = str(_item_value(item, "relevance_reason", "") or "Matched monitor criteria.")
    evidence_snippet = str(_item_value(item, "evidence_snippet", "") or "")
    confidence = _item_value(item, "confidence", 0.0)
    return "\n".join(
        [
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
            f"**Topics:** {topic_text}",
            "",
            "---",
        ]
    )


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
    research_items = [item for item in items if _item_value(item, "lane") == "research"]
    theme_terms = sorted({str(topic) for item in items for topic in (_item_value(item, "topics", ()) or ())})
    theme_text = ", ".join(theme_terms[:8]) if theme_terms else "climate risk and actuarial monitoring"
    summary = (
        f"This report captures {len(items)} climate-related item(s) from monitored websites "
        f"and recent research search. Key themes: {theme_text}."
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
