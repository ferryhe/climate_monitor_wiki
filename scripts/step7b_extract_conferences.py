#!/usr/bin/env python3
"""Step 7b: Extract conference/meeting info into JSON for downstream use."""
import argparse
import json
import os
import re
from datetime import date
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))

# Only these URL patterns indicate actual events
EVENT_URL_PATTERNS = [
    r'/event/', r'/events/', r'/meeting/', r'/conference/',
    r'/workshop/', r'/seminar/', r'/webinar/', r'/summit/',
]

# Title keywords that indicate events. Multi-word phrases are matched
# verbatim; single words use word-boundary matching to avoid false positives
# such as "congressional briefing" or "meeting the targets".
EVENT_TITLE_KEYWORDS = [
    'conference', 'meeting', 'workshop', 'seminar', 'webinar',
    'summit', 'symposium', 'congress', 'registration open',
    'call for papers', 'agenda', 'programme',
]


def is_conference_article(title, url):
    """Check if article is a conference/meeting notice."""
    url_lower = url.lower()
    for pattern in EVENT_URL_PATTERNS:
        if pattern in url_lower:
            return True
    title_lower = title.lower()
    for keyword in EVENT_TITLE_KEYWORDS:
        if " " in keyword:
            if keyword in title_lower:
                return True
        elif re.search(rf'\b{re.escape(keyword)}\b', title_lower):
            return True
    return False


def extract_conference_info(item):
    """Extract conference metadata from an article."""
    title = item.get("title", "")
    url = item.get("url", "")
    org = item.get("org", "")
    summary = item.get("summary", "")

    conf_info = {
        "title": title,
        "url": url,
        "organization": org,
        "source": item.get("source", ""),
        "category": "conference",
        "keywords": item.get("keywords", []),
    }

    # Try to extract date from title or summary
    date_match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4}', title + " " + summary, re.I)
    if date_match:
        conf_info["date"] = date_match.group(0)

    # Location is deliberately not extracted: naive "in /at " scanning
    # produced misleading values from ordinary prose. If a structured
    # location field is ever needed, derive it from fetched content only.

    # Try to extract organizers
    if "hosted by" in (title + summary).lower():
        org_match = re.search(r'hosted by ([^.]+)', title + " " + summary, re.I)
        if org_match:
            conf_info["hosted_by"] = org_match.group(1).strip()

    return conf_info


def main():
    parser = argparse.ArgumentParser(description="Extract conference info")
    parser.add_argument("--date", default=date.today().isoformat())
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

    # Load aggregated articles. Conferences are extracted from the full
    # candidate set so this step does not depend on the assessments/filter
    # stage (which runs afterwards and consumes this output).
    agg_path = REPORTS / f"aggregated_{args.date}.json"
    if not agg_path.exists():
        print(f"ERROR: {agg_path} not found")
        return 1

    items = json.loads(agg_path.read_text()).get("items", [])

    # Find conference articles
    conferences = []
    for item in items:
        if is_conference_article(item.get("title", ""), item.get("url", "")):
            conf_info = extract_conference_info(item)
            conferences.append(conf_info)

    # Save conferences JSON
    conf_path = REPORTS / f"conferences_{args.date}.json"
    conf_path.write_text(json.dumps({
        "date": args.date,
        "total_conferences": len(conferences),
        "conferences": conferences,
    }, ensure_ascii=False, indent=2))

    print(f"Found {len(conferences)} conference/meeting articles")
    print(f"OK: {conf_path}")


if __name__ == "__main__":
    raise SystemExit(main())
