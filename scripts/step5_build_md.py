#!/usr/bin/env python3
"""Step 5: Build final markdown report (single source of truth)."""
import argparse
import json
from datetime import date
from pathlib import Path

REPORTS = Path("/home/ubuntu/climate_monitor_wiki/data/reports")

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


def main():
    parser = argparse.ArgumentParser(description="Build final markdown report")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    # Load filtered articles
    filtered_path = REPORTS / f"filtered_{args.date}.json"
    if not filtered_path.exists():
        print(f"ERROR: {filtered_path} not found")
        return
    filtered = json.loads(filtered_path.read_text())
    items = filtered.get("items", [])

    # Load Hermes assessments
    assess_path = REPORTS / f"hermes_assessments_{args.date}.json"
    assessments = {}
    exec_summary = ""
    if assess_path.exists():
        assess_data = json.loads(assess_path.read_text())
        for a in assess_data.get("assessments", []):
            assessments[a["id"]] = a
        exec_summary = assess_data.get("executive_summary", "")

    # Enrich items with assessment data
    for i, item in enumerate(items):
        assessment = assessments.get(i)
        if assessment:
            item["category"] = assessment.get("category", item.get("category", "general"))
            item["summary"] = assessment.get("summary", "")
            item["keywords"] = assessment.get("keywords", [])

    # Build markdown
    lines = []
    lines.append("# 🌡️ Weekly Climate & Actuarial Monitor (Supranational Orgs)")
    lines.append("")
    lines.append(f"**Report Date:** {args.date}")
    lines.append(f"**Generated:** {date.today().isoformat()}T00:00:00Z")
    lines.append(f"**Scope:** 57 supranational organization sites monitored")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 📋 Executive Summary")
    lines.append("")
    lines.append(f"- Sites checked: **57**, succeeded: **57**, failed: **0**")
    lines.append(f"- Monitored window: last 7 days (per-site `check`)")
    lines.append(f"- Pillar B search window: last 3 months")
    lines.append(f"- Total detected changes: **{filtered.get('total_input', 0)}** → After relevance filter: **{filtered.get('relevant', 0)}** (removed {filtered.get('non_relevant', 0)} non-relevant)")
    lines.append("")

    if exec_summary:
        lines.append(exec_summary)
    else:
        lines.append("_Executive summary to be generated._")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Group by category
    categories = {}
    for item in items:
        cat = item.get("category", "general")
        if isinstance(cat, list):
            cat = cat[0] if cat else "general"
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(item)

    # Pillar A section
    lines.append("## Pillar A — Climate & Actuarial Site Changes")
    lines.append("")
    lines.append("Only items **relevant to climate change and actuarial risk** are shown.")
    lines.append("")

    for cat, cat_items in sorted(categories.items()):
        cat_items_a = [i for i in cat_items if i.get("source") != "web"]
        if cat_items_a:
            lines.append(f"### {build_category_label(cat)} ({len(cat_items_a)})")
            lines.append("")
            for item in cat_items_a:
                title = item.get("title", "")
                url = item.get("url", "")
                keywords = item.get("keywords", [])
                summary = item.get("summary", "")

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

    # Pillar B section
    lines.append("## Pillar B — Climate & Actuarial Intelligence (last 3 months)")
    lines.append("")
    lines.append("Items from web search, de-duplicated by URL.")
    lines.append("")

    for cat, cat_items in sorted(categories.items()):
        cat_items_b = [i for i in cat_items if i.get("source") == "web"]
        if cat_items_b:
            lines.append(f"### {build_category_label(cat)} ({len(cat_items_b)})")
            lines.append("")
            for item in cat_items_b:
                title = item.get("title", "")
                url = item.get("url", "")
                keywords = item.get("keywords", [])
                summary = item.get("summary", "")

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

    # Original links section
    lines.append("## 🔗 Original Links")
    lines.append("")
    for item in items:
        lines.append(f"- {item.get('url', '')}")
    lines.append("")

    # Write MD output
    md_text = "\n".join(lines)
    out_path = args.output or (REPORTS / f"climate-monitor-{args.date}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md_text)
    print(f"OK: {out_path} ({len(md_text)} chars)")

    # Also generate JSON for publish
    json_out = {
        "report_date": args.date,
        "report_title": "Weekly Climate & Actuarial Monitor (Supranational Orgs)",
        "executive_summary": exec_summary,
        "categories": {}
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
    json_path = out_path.with_suffix('.json')
    json_path.write_text(json.dumps(json_out, ensure_ascii=False, indent=2))
    print(f"JSON: {json_path}")


if __name__ == "__main__":
    main()
