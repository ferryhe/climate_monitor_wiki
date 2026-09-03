#!/usr/bin/env python3
"""Step 8: Sync report + articles to Registry DB (direct insert, no capture)."""
import argparse
import hashlib
import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

REPORTS = Path("/home/ubuntu/climate_monitor_wiki/data/reports")
DB = Path("/home/ubuntu/climate_monitor_data/registry/article-registry.sqlite3")


def last_monday():
    today = date.today()
    if today.weekday() == 0:
        return today
    return today - timedelta(days=today.weekday())


def extract_report_title(md_text):
    m = re.search(r'^#\s+(.+?)\s*$', md_text, re.MULTILINE)
    return m.group(1).strip() if m else "Weekly Climate & Actuarial Monitor"


def extract_report_stats(md_text):
    checked = succeeded = failed = 0
    m = re.search(
        r'Sites checked:\s*\*\*(\d+)\*\*,\s*succeeded:\s*\*\*(\d+)\*\*,\s*failed:\s*\*\*(\d+)\*\*',
        md_text,
    )
    if m:
        checked, succeeded, failed = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return checked, succeeded, failed


def parse_articles_from_md(md_text):
    """Parse articles from MD into structured data.

    MD format per article:
    - **Title**
      - *Categories:* cat
      - Summary text
      - *Keywords:* kw1, kw2
      🔗 URL
    """
    articles = []
    lines = md_text.split('\n')

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Article title: starts with "- **" but NOT metadata lines
        if not stripped.startswith('- **'):
            continue
        if '**Categories:**' in stripped or '**Keywords:**' in stripped:
            continue

        # Extract title
        title = stripped[3:]  # Remove "- "
        while title.startswith('*'):
            title = title[1:]
        while title.endswith('*'):
            title = title[:-1]
        title = title.strip()
        if not title:
            continue

        article = {
            'title': title,
            'url': '',
            'summary': '',
            'categories': [],
            'keywords': [],
            'pillar': 'A' if 'Pillar B' not in md_text[max(0, md_text.find(line) - 500):md_text.find(line)] else 'B',
        }

        # Look ahead for metadata
        for j in range(i + 1, min(i + 10, len(lines))):
            future = lines[j].strip()

            # URL (may have 🔗 prefix)
            if not article['url']:
                if '🔗' in future:
                    url = future.split('🔗')[1].strip()
                    if url.startswith('http'):
                        article['url'] = url.split()[0]
                        continue
                elif future.startswith('http'):
                    article['url'] = future.split()[0]
                    continue

            # Categories
            if '**Categories:**' in future:
                cats = future.split('**Categories:**')[1]
                article['categories'] = [c.strip() for c in cats.split(',')]
                continue

            # Keywords
            if '**Keywords:**' in future:
                kws = future.split('**Keywords:**')[1]
                article['keywords'] = [k.strip() for k in kws.split(',')]
                continue

            # Stop at next article or heading
            if future.startswith('- **') or future.startswith('### ') or future.startswith('---'):
                break

            # Summary
            if (future and not future.startswith('*') and
                    '**Categories:**' not in future and
                    '**Keywords:**' not in future):
                if not article['summary']:
                    article['summary'] = future

        if article['url'] and article['url'].startswith('http'):
            articles.append(article)

    return articles


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=last_monday().isoformat())
    args = parser.parse_args()

    md_path = REPORTS / f"climate-monitor-{args.date}.md"
    if not md_path.exists():
        print(f"ERROR: markdown not found: {md_path}")
        return

    data = md_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    md_text = data.decode("utf-8")

    title = extract_report_title(md_text)
    checked, succeeded, failed = extract_report_stats(md_text)
    articles = parse_articles_from_md(md_text)
    print(f"Parsed {len(articles)} articles from MD")

    c = sqlite3.connect(str(DB))
    source_id = c.execute("SELECT source_id FROM sources LIMIT 1").fetchone()[0]

    # Upsert report
    existing = c.execute("SELECT report_id FROM reports WHERE report_date=?", (args.date,)).fetchone()
    if existing:
        c.execute(
            "UPDATE reports SET report_sha256=?, report_title=?, sites_checked=?, sites_succeeded=?, sites_failed=? WHERE report_date=?",
            (sha, title, checked, succeeded, failed, args.date),
        )
    else:
        report_id = f"report-{args.date}"
        c.execute(
            """INSERT INTO reports (report_id, report_date, filename, report_title,
               report_sha256, cadence, report_format, sites_checked, sites_succeeded,
               sites_failed, parse_warnings_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (report_id, args.date, f"climate-monitor-{args.date}.md", title, sha,
             "weekly", "weekly-pillars-v1", checked, succeeded, failed, "[]"),
        )
    c.commit()

    # Sync articles
    report_id = c.execute("SELECT report_id FROM reports WHERE report_date=?", (args.date,)).fetchone()[0]
    c.execute("DELETE FROM report_appearances WHERE report_id=?", (report_id,))
    c.execute("DELETE FROM discoveries WHERE report_id=?", (report_id,))

    inserted = 0
    for i, article in enumerate(articles):
        url = article.get('url', '')
        if not url or not url.startswith('http'):
            continue

        article_id = f"article-{hashlib.sha256(url.encode()).hexdigest()[:16]}"

        # Insert article
        c.execute(
            "INSERT OR IGNORE INTO articles (article_id, canonical_url, source_id, first_seen, last_seen, document_kind) VALUES (?, ?, ?, ?, ?, ?)",
            (article_id, url, source_id, args.date, args.date, 'article'),
        )

        # Insert article version
        fp = hashlib.sha256(
            (url + article.get('title', '')).encode()
        ).hexdigest()
        ver_id = f"ver-{fp[:16]}"
        c.execute(
            "INSERT OR IGNORE INTO article_versions (version_id, article_id, observed_title, canonical_title, observed_summary, content_fingerprint, content_basis, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ver_id, article_id, article.get('title', ''), article.get('title', ''),
             article.get('summary', ''), fp, 'report-title-summary', args.date, args.date),
        )

        # Update article current version
        c.execute("UPDATE articles SET current_version_id=? WHERE article_id=?", (ver_id, article_id))

        # Insert discovery
        c.execute(
            "INSERT OR IGNORE INTO discoveries (discovery_id, report_id, ordinal, section, pillar, article_id, version_id, raw_url, observed_title, observed_summary, selected) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (article_id, report_id, i + 1, 'body', article.get('pillar', 'A'),
             article_id, ver_id, url, article.get('title', ''),
             article.get('summary', ''), 1),
        )

        # Insert appearance
        c.execute(
            "INSERT INTO report_appearances (report_id, article_id, version_id, discovery_id, section, pillar, ordinal, disposition) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (report_id, article_id, ver_id, article_id, 'body', article.get('pillar', 'A'), i + 1, 'new'),
        )
        inserted += 1

    c.commit()

    count = c.execute("SELECT count(*) FROM reports").fetchone()[0]
    latest = c.execute("SELECT max(report_date) FROM reports").fetchone()[0]
    print(f"OK: Registry has {count} reports, latest: {latest}, articles inserted: {inserted}")


if __name__ == "__main__":
    main()
