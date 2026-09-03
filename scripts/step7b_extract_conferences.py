#!/usr/bin/env python3
"""Step 7b: Extract conference/meeting info into JSON for downstream use."""
import argparse
import json
from datetime import date
from pathlib import Path

REPORTS = Path("/home/ubuntu/climate_monitor_wiki/data/reports")

# Only these URL patterns indicate actual events
EVENT_URL_PATTERNS = [
    r'/event/', r'/events/', r'/meeting/', r'/conference/',
    r'/workshop/', r'/seminar/', r'/webinar/', r'/summit/',
]

# Title keywords that indicate events (must be whole words)
EVENT_TITLE_KEYWORDS = [
    'conference', 'meeting', 'workshop', 'seminar', 'webinar',
    'summit', 'symposium', 'congress', 'session', 'registration open',
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
        if keyword in title_lower:
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
    import re
    date_match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4}', title + " " + summary, re.I)
    if date_match:
        conf_info["date"] = date_match.group(0)

    # Try to extract location
    location_keywords = ["in ", "at ", "hosted by"]
    for keyword in location_keywords:
        idx = (title + " " + summary).lower().find(keyword)
        if idx >= 0:
            location = (title + " " + summary)[idx:idx+100].split('.')[0].split(',')[0]
            conf_info["location"] = location
            break

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

    # Load filtered articles
    filtered_path = REPORTS / f"filtered_{args.date}.json"
    if not filtered_path.exists():
        print(f"ERROR: {filtered_path} not found")
        return

    filtered = json.loads(filtered_path.read_text())
    items = filtered.get("items", [])

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
    main()
