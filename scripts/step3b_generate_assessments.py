#!/usr/bin/env python3
"""Step 3b fallback: deterministic keyword assessments for aggregated articles.

This is the *fallback* assessment generator. The authoritative assessments
come from the Hermes LLM step (see step3b_hermes_filter.py, which writes the
prompt the LLM cron job consumes). To avoid clobbering LLM output:

* the script refuses to overwrite an existing hermes_assessments_<date>.json
  unless --force is passed;
* summaries are left empty (the pipeline must never fabricate article
  content), so downstream report rendering only shows what is real;
* relevance and categories are keyword-based and deterministic.

Run: python scripts/step3b_generate_assessments.py --date 2026-09-07
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import date
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))

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

CATEGORY_KEYWORDS = {
    "climate_disclosure": ["disclosure", "issb", "ifrs", "tcfd", "csrd", "reporting", "sasb", "sustainability standard", "s2"],
    "scenario_analysis": ["scenario", "stress test", "orsa", "modelling", "model", "projection", "ngfs"],
    "catastrophe_natcat": ["catastrophe", "nat cat", "disaster", "flood", "drought", "storm", "wildfire", "hazard", "earthquake", "hurricane"],
    "adaptation_resilience": ["adaptation", "resilience", "protection gap", "preparedness", "risk communication"],
    "mitigation_energy": ["mitigation", "renewable", "energy transition", "decarbonization", "net zero", "carbon capture", "cdr"],
    "parametric_insurance": ["parametric", "index insurance", "cat bond", "weather derivative", "weather index"],
    "financial_risk": ["solvency", "financial stability", "banking", "central bank", "systemic risk", "supervisory", "iais"],
    "health_mortality": ["mortality", "morbidity", "health", "pandemic", "longevity", "disease"],
    "regulation_standards": ["regulat", "supervis", "compliance", "standard", "guidance", "iais"],
    "biodiversity_nature": ["biodiversity", "nature", "ecosystem", "deforestation", "forest"],
    "conference": ["conference", "meeting", "workshop", "seminar", "webinar", "summit", "symposium", "congress"],
}

EVENT_URL_PATTERNS = [r"/event/", r"/events/", r"/meeting/", r"/conference/", r"/workshop/", r"/seminar/", r"/webinar/"]

IMPLICATIONS = {
    "climate_disclosure": "disclosure quality and reporting controls under IFRS S2/TCFD-aligned regimes",
    "scenario_analysis": "scenario analysis and stress testing for transition risk",
    "catastrophe_natcat": "catastrophe modeling and hazard trend updates",
    "adaptation_resilience": "adaptation and resilience metrics for protection-gap analysis",
    "mitigation_energy": "energy-transition assumptions in pricing and asset strategies",
    "parametric_insurance": "parametric product design and basis-risk management",
    "financial_risk": "solvency and systemic-risk supervision of climate exposures",
    "health_mortality": "mortality and morbidity assumptions under climate-sensitive health risks",
    "regulation_standards": "regulatory and supervisory compliance requirements",
    "biodiversity_nature": "nature-related risk factors in physical-risk frameworks",
    "conference": "upcoming events for engagement and evidence gathering",
    "general": "general climate and actuarial evidence",
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "is", "are", "was", "were", "be", "been", "has",
    "have", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "can", "this", "that", "these", "those", "it", "its",
    "they", "them", "their", "we", "our", "you", "your", "new", "more",
    "most", "some", "any", "all", "each", "every", "both", "few", "many",
    "much", "such", "than", "too", "very", "just", "about", "also", "now",
    "here", "there", "when", "where", "why", "how", "what", "which", "who",
    "not", "no", "nor", "as", "if", "then", "else", "while", "because",
    "since", "until", "after", "before", "during", "without", "within",
    "through", "above", "below", "under", "over", "into", "out", "up",
    "down", "off", "away", "back", "so", "even", "still", "already", "yet",
    "once", "per", "cent", "year", "years", "month", "months", "week",
    "weeks", "day", "days", "time", "world", "state", "states", "report",
    "reports", "study", "studies", "research", "plan", "plans", "policy",
    "policies", "change", "changes", "level", "levels", "risk", "risks",
}


def is_relevant(title: str, url: str) -> bool:
    text = f"{title} {url}".lower()
    return any(kw in text for kw in CLIMATE_KEYWORDS) or any(kw in text for kw in ACTUARIAL_KEYWORDS)


def classify_article(title: str, url: str) -> list[str]:
    url_lower = url.lower()
    if any(re.search(pattern, url_lower) for pattern in EVENT_URL_PATTERNS):
        return ["conference"]
    text = f"{title} {url}".lower()
    scored = [
        (sum(1 for kw in keywords if kw in text), cat)
        for cat, keywords in CATEGORY_KEYWORDS.items()
        if cat != "conference"
    ]
    scored = [(score, cat) for score, cat in scored if score > 0]
    scored.sort(reverse=True, key=lambda pair: pair[0])
    top = [cat for _, cat in scored[:2]]
    return top or ["general"]


def extract_keywords(title: str, url: str) -> list[str]:
    text = f"{title} {url}".lower()
    words = re.findall(r"[a-z]+(?:\s+[a-z]+){0,2}", text)
    meaningful = [w for w in words if w not in STOPWORDS and len(w) > 3]
    counter = Counter(meaningful)
    return [term for term, _ in counter.most_common(5)]


def generate_executive_summary(items: list[dict], assessments: list[dict]) -> str:
    categories: dict[str, list[dict]] = {}
    for a, item in zip(assessments, items):
        if not a.get("relevant"):
            continue
        cat = a.get("category") or "general"
        categories.setdefault(cat, []).append(item)

    relevant = sum(1 for a in assessments if a.get("relevant"))
    top_cats = list(categories.keys())[:3]
    p1 = (
        f"Across {len(items)} detected updates, {relevant} items were assessed "
        f"relevant to climate and actuarial risk, concentrated on "
        f"{', '.join(cat.replace('_', ' ') for cat in top_cats) or 'no dominant categories'}."
    )
    parts = []
    for cat, cat_items in sorted(categories.items()):
        titles = ", ".join(i.get("title", "")[:40] for i in cat_items[:2])
        parts.append(f"{cat.replace('_', ' ').title()} ({len(cat_items)}): {titles}")
    p2 = "Category analysis: " + ("; ".join(parts) + "." if parts else "no categorized items.")

    implication_cats = [c for c in top_cats if c in IMPLICATIONS] or ["general"]
    p3 = "Actuarial implications include: " + "; ".join(IMPLICATIONS[c] for c in implication_cats) + "."
    p4 = "Working-group follow-ups: " + ", ".join(
        f"review {c.replace('_', ' ')} items" for c in implication_cats[:3]
    ) + "."
    return f"{p1}\n\n{p2}\n\n{p3}\n\n{p4}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic fallback article assessments")
    parser.add_argument("--date", required=True)
    parser.add_argument("--force", action="store_true", help="overwrite existing assessments")
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

    agg_path = REPORTS / f"aggregated_{args.date}.json"
    if not agg_path.exists():
        print(f"ERROR: aggregated file not found: {agg_path}")
        return 1
    out_path = REPORTS / f"hermes_assessments_{args.date}.json"
    if out_path.exists() and not args.force:
        print(f"SKIP: {out_path.name} already exists (LLM assessments present); use --force to overwrite")
        return 0

    items = json.loads(agg_path.read_text()).get("items", [])
    conf_path = REPORTS / f"conferences_{args.date}.json"
    conf_urls = set()
    if conf_path.exists():
        for c in json.loads(conf_path.read_text()).get("conferences", []):
            conf_urls.add(c.get("url", ""))

    assessments = []
    for i, item in enumerate(items):
        title = item.get("title", "")
        url = item.get("url", "")
        categories = ["conference"] if url in conf_urls else classify_article(title, url)
        assessments.append(
            {
                "id": i,
                "url": url,
                "relevant": is_relevant(title, url),
                "category": categories[0],
                "summary": "",
                "keywords": extract_keywords(title, url),
            }
        )

    executive_summary = generate_executive_summary(items, assessments)
    out_path.write_text(
        json.dumps({"assessments": assessments, "executive_summary": executive_summary}, ensure_ascii=False, indent=2)
    )
    relevant = sum(1 for a in assessments if a["relevant"])
    print(f"OK: {len(assessments)} deterministic assessments ({relevant} relevant), summaries left empty by design → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
