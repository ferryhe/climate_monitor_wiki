#!/usr/bin/env python3
"""Step 2: Pillar B — save results to article_state.json for dedup."""
import argparse
import json
import os
from datetime import date
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))
STATE_FILE = Path(os.environ.get("CLIMATE_WL_STATE", "/home/ubuntu/web_listening/data/article_state.json"))


def main():
    parser = argparse.ArgumentParser(description="Save Pillar B URLs to state")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    # Load pillar_b results
    pb_path = REPORTS / f"pillar_b_{args.date}.json"
    if not pb_path.exists():
        print(f"ERROR: {pb_path} not found")
        return
    
    pillar_b = json.loads(pb_path.read_text())
    
    # Load current state
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    else:
        state = {}
    
    # Add Pillar B URLs under "__pillar_b__" key
    pb_key = "__pillar_b__"
    if pb_key not in state:
        state[pb_key] = []
    
    existing = set(state[pb_key])
    new_urls = []
    for item in pillar_b:
        url = item.get("url", "").split('?')[0].rstrip('/')
        if url and url not in existing:
            state[pb_key].append(url)
            new_urls.append(url)
    
    # Save state
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    print(f"Added {len(new_urls)} Pillar B URLs to article_state.json")
    print(f"Total state URLs: {sum(len(v) for v in state.values())}")


if __name__ == "__main__":
    main()
