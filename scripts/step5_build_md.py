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
REPORTS = HOME / "data" / "reports"

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


def build_category_label(cat):
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
    args = parser.parse_args()

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
        categories.setdefault(cat, []).append(item)

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
            lines.append(f"  - **Categories:** {build_category_label(cat)}")
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
            lines.append(f"  - **Categories:** {build_category_label(cat)}")
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
    out_path.write_text(md_text)
    print(f"OK: {out_path} ({len(md_text)} chars)")

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
                "categories": [build_category_label(cat)],
                "keywords": item.get("keywords", []),
                "source": item.get("source", ""),
                "pillar": "A" if item.get("source") != "web" else "B",
            }
            for item in cat_items
        ]
    json_path = out_path.with_suffix(".json")
    json_path.write_text(json.dumps(json_out, ensure_ascii=False, indent=2))
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
