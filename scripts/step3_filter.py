#!/usr/bin/env python3
"""Step 3 Filter: Apply article assessments with keyword fallback.

Assessments are joined to articles by URL (the deterministic fallback
generator writes the article URL into each assessment record). When an
assessment set has no URLs (plain Hermes output keyed by id), the join falls
back to the positional id — safe only because this stage consumes the
aggregated list before any filtering happens.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = HOME / "data" / "reports"

CLIMATE_KEYWORDS = [
    "climate", "warming", "emission", "carbon", "greenhouse", "ghg",
    "renewable", "energy transition", "net zero", "decarboni",
    "sustainability", "esg", "green finance", "taxonomi",
    "physical risk", "transition risk",
    "extreme weather", "flood", "drought", "storm", "wildfire",
    "catastrophe", "nat cat", "hazard", "resilien",
    "adaptation", "mitigation", "carbon price", "carbon tax",
    "issb", "ifrs s2", "tcfd", "tnfd", "csrd",
    "carbon capture", "ccs", "cdr",
    "pollution", "environment", "biodiversity", "nature",
    "water", "ocean", "sea level", "solar", "wind", "hydrogen",
    "methane", "deforestation", "ecosystem", "forest",
]

ACTUARIAL_KEYWORDS = [
    "actuarial", "actuary", "insurance", "reinsurance", "underwriting",
    "pricing", "reserving", "risk assessment", "risk management",
    "solvency", "orsa", "stress test", "scenario analysi",
    "mortality", "morbidity", "longevity", "pandemic",
    "claims", "loss", "exposure", "accumulation",
    "premium", "technical provision", "combined ratio",
    "life insurance", "non-life", "pension", "annuiti",
    "health insurance", "disability", "critical illness",
    "insurtech", "parametric", "index insurance", "cat bond",
    "financial stability", "banking", "central bank",
]

CATEGORY_MAP = {
    "climate_disclosure": ["disclosure", "issb", "ifrs s2", "tcfd", "csrd", "reporting"],
    "scenario_analysis": ["scenario", "stress test", "orsa", "modelling"],
    "catastrophe_natcat": ["catastrophe", "nat cat", "disaster", "flood", "drought", "storm", "wildfire", "hazard"],
    "adaptation_resilience": ["adaptation", "resilien", "protection gap"],
    "mitigation_energy": ["mitigation", "renewable", "energy transition", "decarboni", "net zero", "carbon capture"],
    "parametric_insurance": ["parametric", "index insurance", "cat bond", "weather derivative"],
    "financial_risk": ["solvency", "financial stability", "banking", "central bank", "systemic risk"],
    "health_mortality": ["mortality", "morbidity", "health", "pandemic", "longevity"],
    "regulation_standards": ["regulat", "supervis", "compliance", "standard", "guidance"],
    "biodiversity_nature": ["biodiversity", "nature", "ecosystem", "deforestation", "forest"],
}


def keyword_classify(title: str, url: str) -> list[str]:
    text = f"{title} {url}".lower()
    categories = [cat for cat, keywords in CATEGORY_MAP.items() if any(kw in text for kw in keywords)]
    return categories or ["general"]


def keyword_relevant(title: str, url: str) -> bool:
    text = f"{title} {url}".lower()
    return any(kw in text for kw in CLIMATE_KEYWORDS) or any(kw in text for kw in ACTUARIAL_KEYWORDS)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply assessments with keyword fallback")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    agg_path = REPORTS / f"aggregated_{args.date}.json"
    if not agg_path.exists():
        print(f"ERROR: {agg_path} not found")
        return 1
    items = json.loads(agg_path.read_text()).get("items", [])

    assessments = []
    assess_path = REPORTS / f"hermes_assessments_{args.date}.json"
    if assess_path.exists():
        assessments = json.loads(assess_path.read_text()).get("assessments", [])

    by_url = {a.get("url"): a for a in assessments if a.get("url")}
    by_id = {a["id"]: a for a in assessments if "id" in a}

    relevant_items = []
    non_relevant_count = 0
    for i, item in enumerate(items):
        assessment = by_url.get(item.get("url")) or by_id.get(i) or {}
        if assessment:
            is_rel = bool(assessment.get("relevant", False))
            category = assessment.get("category", "general")
            summary = assessment.get("summary", "")
            keywords = assessment.get("keywords", [])
        else:
            is_rel = keyword_relevant(item.get("title", ""), item.get("url", ""))
            category = keyword_classify(item.get("title", ""), item.get("url", ""))
            summary = ""
            keywords = []

        if is_rel:
            item["relevant"] = True
            item["category"] = category
            item["summary"] = summary
            item["keywords"] = keywords
            relevant_items.append(item)
        else:
            non_relevant_count += 1

    output = {
        "date": args.date,
        "total_input": len(items),
        "relevant": len(relevant_items),
        "non_relevant": non_relevant_count,
        "items": relevant_items,
    }
    out_path = REPORTS / f"filtered_{args.date}.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"Step 3 Filter: {len(relevant_items)} relevant, {non_relevant_count} non-relevant")
    print(f"OK: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
