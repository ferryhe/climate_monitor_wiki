#!/usr/bin/env python3
"""Step 5: Build the final markdown report (single source of truth).

Reads filtered articles (already enriched with category/summary/keywords by
Step 3 Filter) plus the optional executive summary from the assessments file.
All statistics are derived from the input data; nothing is hardcoded. If a
number is unknown (e.g. per-site check totals live in the monitoring system),
the corresponding bullet is omitted instead of fabricating a value.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))

CATEGORY_LABELS = {
    "climate_disclosure": "Climate Disclosure",
    "scenario_analysis": "Scenario Analysis",
    "catastrophe_natcat": "Catastrophe & NatCat",
    "adaptation_resilience": "Adaptation & Resilience",
    "mitigation_energy": "Mitigation & Energy",
    "parametric_insurance": "Parametric Insurance",
    "financial_risk": "Financial Risk",
    "health_mortality": "Health & Mortality",
    "regulation_standards": "Regulation & Standards",
    "biodiversity_nature": "Biodiversity & Nature",
    "conference": "Conference & Events",
    "general": "General",
}


def build_category_label(cat: str) -> str:
    return CATEGORY_LABELS.get(cat, cat)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build final markdown report")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--scope-sites",
        type=int,
        default=None,
        help="Number of monitored sites (omits the Scope line when unknown)",
    )
    parser.add_argument(
        "--monitor-stats",
        type=Path,
        default=None,
        help="Required JSON with checked/succeeded/failed per-site check totals "
             "(the script fails closed when it is missing)",
    )
    parser.add_argument("--allow-offcycle", action="store_true", help="Allow non-Monday report dates")
    parser.add_argument("--allow-future", action="store_true", help="Allow future report dates")
    args = parser.parse_args()

    try:
        parsed_date = date.fromisoformat(args.date)
    except ValueError:
        print(f"ERROR: invalid --date {args.date!r} (expected YYYY-MM-DD)")
        return 1
    if parsed_date.isoformat() != args.date:
        print(f"ERROR: invalid --date {args.date!r} (expected YYYY-MM-DD)")
        return 1
    args.date = parsed_date.isoformat()
    if parsed_date > date.today() and not args.allow_future:
        print(f"ERROR: report date {args.date} is in the future; pass --allow-future to override")
        return 1
    if parsed_date.weekday() != 0 and not args.allow_offcycle:
        print(f"ERROR: report date {args.date} is not a Monday; pass --allow-offcycle to override")
        return 1

    filtered_path = REPORTS / f"filtered_{args.date}.json"
    if not filtered_path.exists():
        print(f"ERROR: {filtered_path} not found")
        return 1
    filtered = json.loads(filtered_path.read_text())
    items = filtered.get("items", [])

    exec_summary = ""
    assess_path = REPORTS / f"hermes_assessments_{args.date}.json"
    if assess_path.exists():
        exec_summary = json.loads(assess_path.read_text()).get("executive_summary", "") or ""

    sites_with_changes = None
    pillar_a_path = REPORTS / f"article_changes_{args.date}.json"
    if pillar_a_path.exists():
        sites_with_changes = json.loads(pillar_a_path.read_text()).get("sites_with_changes")

    checked = succeeded = failed = None
    if not args.monitor_stats or not args.monitor_stats.exists():
        print(
            "ERROR: --monitor-stats is required. The delivery contract needs real "
            "per-site check totals (checked/succeeded/failed); fabricating zeros "
            "is not allowed. Pass the monitoring run's stats JSON."
        )
        return 1
    stats = json.loads(args.monitor_stats.read_text())
    if not isinstance(stats, dict):
        print("ERROR: --monitor-stats must be a JSON object")
        return 1
    for key in ("checked", "succeeded", "failed"):
        value = stats.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            print(f"ERROR: --monitor-stats missing or invalid '{key}' (non-negative integer required)")
            return 1
    checked, succeeded, failed = stats["checked"], stats["succeeded"], stats["failed"]
    if checked != succeeded + failed:
        print(
            f"ERROR: --monitor-stats inconsistent: checked={checked} != succeeded+failed={succeeded + failed}"
        )
        return 1
    if checked == 0:
        print("ERROR: --monitor-stats contains all zeros; refusing to publish fabricated counts")
        return 1

    lines = ["# 🌡️ Weekly Climate & Actuarial Monitor (Supranational Orgs)", ""]
    lines.append(f"**Report Date:** {args.date}")
    lines.append(f"**Generated:** {date.today().isoformat()}T00:00:00Z")
    if args.scope_sites:
        lines.append(f"**Scope:** {args.scope_sites} supranational organization sites monitored")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 📋 Executive Summary")
    lines.append("")
    lines.append(f"- Sites checked: **{checked}**, succeeded: **{succeeded}**, failed: **{failed}**")
    if sites_with_changes is not None:
        lines.append(f"- Sites with changes in the monitored window: **{sites_with_changes}**")
    lines.append(f"- Monitored window: last 7 days (per-site `check`)")
    lines.append(f"- Pillar B search window: last 3 months")
    lines.append(
        f"- Total detected changes: **{filtered.get('total_input', 0)}** → After relevance filter: "
        f"**{filtered.get('relevant', 0)}** (removed {filtered.get('non_relevant', 0)} non-relevant)"
    )
    lines.append("")
    if exec_summary:
        lines.append(exec_summary)
    else:
        lines.append("_Executive summary not yet generated._")
    lines.append("")
    lines.append("---")
    lines.append("")

    categories = {}
    for item in items:
        cat = item.get("category", "general")
        if isinstance(cat, list):
            cat = cat[0] if cat else "general"
        if not isinstance(cat, str) or not cat:
            cat = "general"
        categories.setdefault(cat, []).append(item)

    def item_category_labels(item: dict, fallback: str) -> list[str]:
        """Full ordered category list (first = primary) as display labels."""
        values = item.get("categories")
        if isinstance(values, list):
            labels = [build_category_label(c) for c in values if isinstance(c, str) and c]
            if labels:
                return labels
        if not isinstance(fallback, str) or not fallback:
            fallback = "general"
        return [build_category_label(fallback)]

    lines.append("## Pillar A — Climate & Actuarial Site Changes")
    lines.append("")
    lines.append("Only items **relevant to climate change and actuarial risk** are shown.")
    lines.append("")
    for cat, cat_items in sorted(categories.items()):
        cat_items_a = [i for i in cat_items if i.get("source") != "web"]
        if not cat_items_a:
            continue
        lines.append(f"### {build_category_label(cat)} ({len(cat_items_a)})")
        lines.append("")
        for item in cat_items_a:
            title = item.get("title", "")
            url = item.get("url", "")
            summary = item.get("summary", "")
            keywords = item.get("keywords", [])
            lines.append(f"- **{title}**")
            lines.append(f"  - **Categories:** {', '.join(item_category_labels(item, cat))}")
            if summary:
                lines.append(f"  - {summary}")
            if keywords:
                lines.append(f"  - **Keywords:** {', '.join(keywords)}")
            lines.append(f"  🔗 {url}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Pillar B — Climate & Actuarial Intelligence (last 3 months)")
    lines.append("")
    lines.append("Items from web search, de-duplicated by URL.")
    lines.append("")
    for cat, cat_items in sorted(categories.items()):
        cat_items_b = [i for i in cat_items if i.get("source") == "web"]
        if not cat_items_b:
            continue
        lines.append(f"### {build_category_label(cat)} ({len(cat_items_b)})")
        lines.append("")
        for item in cat_items_b:
            title = item.get("title", "")
            url = item.get("url", "")
            summary = item.get("summary", "")
            keywords = item.get("keywords", [])
            lines.append(f"- **{title}**")
            lines.append(f"  - **Categories:** {', '.join(item_category_labels(item, cat))}")
            if summary:
                lines.append(f"  - {summary}")
            if keywords:
                lines.append(f"  - **Keywords:** {', '.join(keywords)}")
            lines.append(f"  🔗 {url}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 🔗 Original Links")
    lines.append("")
    for item in items:
        lines.append(f"- {item.get('url', '')}")
    lines.append("")

    md_text = "\n".join(lines)
    out_path = args.output or (REPORTS / f"climate-monitor-{args.date}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_md = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_md.write_text(md_text)
    print(f"Staged: {tmp_md} ({len(md_text)} chars)")

    json_out = {
        "report_date": args.date,
        "report_title": "Weekly Climate & Actuarial Monitor (Supranational Orgs)",
        "executive_summary": exec_summary,
        "total_input": filtered.get("total_input", 0),
        "relevant": filtered.get("relevant", 0),
        "non_relevant": filtered.get("non_relevant", 0),
        "sites_with_changes": sites_with_changes,
        "scope_sites": args.scope_sites,
        "categories": {},
    }
    for cat, cat_items in sorted(categories.items()):
        json_out["categories"][cat] = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "summary": item.get("summary", ""),
                "categories": item_category_labels(item, cat),
                "keywords": item.get("keywords", []),
                "source": item.get("source", ""),
                "pillar": "A" if item.get("source") != "web" else "B",
            }
            for item in cat_items
        ]
    json_path = out_path.with_suffix(".json")
    tmp_json = json_path.with_suffix(json_path.suffix + ".tmp")
    tmp_json.write_text(json.dumps(json_out, ensure_ascii=False, indent=2))
    # Commit both outputs only after both serializations succeeded.
    os.replace(tmp_json, json_path)
    os.replace(tmp_md, out_path)
    print(f"OK: {out_path} + {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
