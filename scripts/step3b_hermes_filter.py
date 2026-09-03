#!/usr/bin/env python3
"""Step 3b helper: write the Hermes LLM assessment prompt to a JSON file.

This script does NOT call any LLM. It emits hermes_prompt_<date>.json, which
the LLM cron job (PIPELINE_CONFIG.md § Step 3b) reads and answers into
hermes_assessments_<date>.json. The deterministic keyword fallback lives in
step3b_generate_assessments.py and refuses to overwrite LLM output.
"""
import argparse
import json
import os
from datetime import date
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))


PROMPT_TEMPLATE = """You are a climate & actuarial intelligence analyst. Assess each article for relevance to climate change and actuarial risk.

For each article, provide:
1. relevant: true/false (is this about climate change AND actuarial/insurance risk?)
2. category: one of [climate_disclosure, scenario_analysis, catastrophe_natcat, adaptation_resilience, mitigation_energy, parametric_insurance, financial_risk, health_mortality, regulation_standards, biodiversity_nature, general]
3. summary: "" (empty string) unless you actually fetched the article content and the summary is grounded in it. You may fetch articles with web tools. NEVER invent content from title/URL alone; if you did not fetch an article, its summary MUST be the empty string.
4. keywords: 3-5 specific keywords derived from the title (or from the fetched content)

Articles to assess:
{articles}

Respond in JSON format:
{{"assessments": [
  {{"id": 0, "relevant": true/false, "category": "...", "summary": "...", "keywords": [...]}},
  ...
]}}

Integrity rules (non-negotiable):
- A non-empty summary requires fetched article evidence; otherwise it must be "".
- relevance and category must be derivable from title/URL alone.
- Do not claim facts about an article you have not fetched.
"""


def main():
    parser = argparse.ArgumentParser(description="Hermes LLM relevance filter")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prompt-file", type=Path, help="Custom prompt template")
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

    # Load aggregated articles
    in_path = args.input or (REPORTS / f"aggregated_{args.date}.json")
    if not in_path.exists():
        print(f"ERROR: {in_path} not found")
        return 1
    
    data = json.loads(in_path.read_text())
    items = data.get("items", [])
    
    if not items:
        print("ERROR: no articles to assess")
        return 1
    
    # Build prompt
    prompt = PROMPT_TEMPLATE.format(
        articles=json.dumps([{
            "id": i,
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "source": item.get("source", ""),
        } for i, item in enumerate(items)], ensure_ascii=False, indent=2)
    )
    
    # Save prompt for Hermes to process
    prompt_path = REPORTS / f"hermes_prompt_{args.date}.json"
    prompt_path.write_text(json.dumps({
        "prompt": prompt,
        "output_schema": {
            "type": "object",
            "properties": {
                "assessments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "relevant": {"type": "boolean"},
                            "category": {"type": "string"},
                            "summary": {"type": "string"},
                            "keywords": {"type": "array", "items": {"type": "string"}}
                        }
                    }
                }
            }
        }
    }, ensure_ascii=False, indent=2))
    
    print(f"Prompt generated: {prompt_path}")
    print(f"Articles to assess: {len(items)}")


if __name__ == "__main__":
    raise SystemExit(main())
