#!/usr/bin/env python3
"""Step 3: Aggregate + Dedup.

Reads Pillar A (article_changes_<DATE>.json) and Pillar B (pillar_b_<DATE>.json),
deduplicates by URL and near-identical titles, and outputs a unified candidate list.

This is a deterministic script — no LLM, no randomness.
"""
import argparse
import json
import os
import re
from pathlib import Path
from datetime import date

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))


def normalize_url(url: str) -> str:
    """Normalize URL for dedup."""
    url = url.strip().lower()
    url = re.sub(r"^https?://(www\.)?", "", url)
    url = re.sub(r"[?#].*$", "", url)
    return url.rstrip("/")


def normalize_title(title: str) -> str:
    """Normalize title for near-duplicate detection."""
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t


def dedup_items(items: list[dict]) -> list[dict]:
    """Deduplicate by URL and near-identical titles."""
    seen_urls = set()
    seen_titles = set()
    result = []
    
    for item in items:
        url = normalize_url(item.get("url", ""))
        title = normalize_title(item.get("title", ""))
        
        if url and url in seen_urls:
            continue
        if title and title in seen_titles:
            continue
        
        if url:
            seen_urls.add(url)
        if title:
            seen_titles.add(title)
        result.append(item)
    
    return result


def main():
    parser = argparse.ArgumentParser(description="Aggregate + Dedup")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    # Read Pillar A
    pa_path = REPORTS / f"article_changes_{args.date}.json"
    if pa_path.exists():
        pa_data = json.loads(pa_path.read_text())
        pa_items = []
        for org in pa_data.get("articles", []):
            for item in org.get("items", []):
                pa_items.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "source": org.get("org", ""),
                    "pillar": "A",
                    "summary": item.get("summary", ""),
                    "org": org.get("org", ""),
                })
    else:
        pa_items = []
        print(f"WARNING: Pillar A file not found: {pa_path}")

    # Read Pillar B
    pb_path = REPORTS / f"pillar_b_{args.date}.json"
    if pb_path.exists():
        pb_items = json.loads(pb_path.read_text())
        for item in pb_items:
            item["pillar"] = "B"
    else:
        pb_items = []
        print(f"WARNING: Pillar B file not found: {pb_path}")

    # Aggregate
    all_items = pa_items + pb_items
    print(f"Aggregate: {len(pa_items)} Pillar A + {len(pb_items)} Pillar B = {len(all_items)} total")

    # Dedup
    deduped = dedup_items(all_items)
    print(f"After dedup: {len(deduped)} items ({len(all_items) - len(deduped)} removed)")

    output = {
        "date": args.date,
        "pillar_a_count": len(pa_items),
        "pillar_b_count": len(pb_items),
        "total_before_dedup": len(all_items),
        "total_after_dedup": len(deduped),
        "items": deduped
    }

    out_path = args.output or (REPORTS / f"aggregated_{args.date}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"OK: {out_path}")


if __name__ == "__main__":
    main()
