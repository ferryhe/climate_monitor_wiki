#!/usr/bin/env python3
"""Step 1: Pillar A — extract articles with real titles, URLs, and summaries from changes."""
import argparse
import json
import os
import re
import string
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))
WL_REPO = Path(os.environ.get("CLIMATE_WL_REPO", "/home/ubuntu/web_listening"))
SITE_DB = WL_REPO / "data" / "web_listening.db"
STATE_FILE = WL_REPO / "data" / "article_state.json"

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

NAV_TITLES = {
    'home', 'about', 'contact', 'login', 'logout', 'register', 'search',
    'menu', 'navigation', 'footer', 'header', 'sidebar', 'privacy', 'terms',
    'sitemap', 'accessibility', 'skip to', 'more info', 'read more',
    'learn more', 'click here', 'latest news', 'upcoming events',
    'related links', 'share this', 'print', 'email', 'facebook', 'twitter',
    'linkedin', 'youtube', 'rss', 'newsletter', 'subscribe', 'donate',
    'faq', 'help', 'careers', 'press room', 'what we do', 'who we are',
    'our work', 'topics', 'regions', 'countries', 'data', 'publications',
    'events', 'news', 'stories', 'feed', 'embed', 'dashboard',
    'arabic', 'français', 'chinese', 'español', 'português', 'deutsch',
    'comments', 'page', 'next', 'previous', 'load more', 'favicon.ico',
    'site.webmanifest', 'robots.txt', 'sitemap.xml', 'manifest.json',
    'structure', 'history', '30 years of unep fi', 'view preferences',
    'manage services', 'manage', 'leadership council', 'global steering',
    'banking board', 'insurance board', 'pension board', 'investment board',
    'cookie policy', 'privacy statement', 'terms of use', 'accessibility',
    'modern slavery', 'russian', 'spanish', 'french', 'german', 'italian',
    'japanese', 'korean', 'dutch', 'swedish', 'norwegian', 'danish',
    'finnish', 'polish', 'czech', 'hungarian', 'romanian', 'bulgarian',
    'croatian', 'serbian', 'slovenian', 'slovak', 'estonian', 'latvian',
    'lithuanian', 'ukrainian', 'belarusian', 'moldovan', 'albanian',
    'macedonian', 'bosnian', 'montenegrin', 'icelandic', 'irish', 'maltese',
    'synthesis report', 'working groups', 'annual report', 'strategic plan',
    'work programme', 'press releases', 'newsroom', 'media centre',
    'interactives', 'data portal', 'resources', 'un agency service',
    'the taskforce', 'the secretariat', 'the board', 'why nature',
    'environment assembly', 'next generation', 'peacebuilders',
    'senior advisers', 'stewardship council', 'knowledge partners',
    'banking principles', 'principles', 'academy', 'secretariat',
    'taskforce', 'psi board', 'emission factor database',
    'keynote address', 'ipcc chair', 'advanced search.html',
}

NON_ARTICLE_URL_PATTERNS = [
    r'/page/\d+',
    r'/languages?/.*(arabic|français|chinese|español|deutsch)',
    r'/wp-(json|content|includes)/',
    r'/feed$',
    r'/rss$',
    r'/atom$',
    r'/embed',
    r'/oembed',
    r'embed\?url=',
    r'/comments/feed',
    r'/wp-json/',
    r'/cookie',
    r'/privacy',
    r'/terms',
    r'/accessibility',
    r'/sitemap',
    r'/robots\.txt',
]


def normalize_title(title):
    """Normalize article title to proper title case (APA/Chicago style)."""
    if not title:
        return title

    # Known acronyms that should stay uppercase
    acronyms = {
        'c3s': 'C3S', 'who': 'WHO', 'undrr': 'UNDRR', 'wef': 'WEF',
        'ipcc': 'IPCC', 'iais': 'IAIS', 'ifrs': 'IFRS', 'issb': 'ISSB',
        'tcfd': 'TCFD', 'csrd': 'CSRD', 'tnfd': 'TNFD', 'oca': 'OCA',
        'csfa': 'CSFA', 'wri': 'WRI', 'ngfs': 'NGFS', 'wb': 'WB',
        'oecd': 'OECD', 'unctad': 'UNCTAD', 'unep': 'UNEP',
        'afdb': 'AfDB', 'adb': 'ADB', 'ebrd': 'EBRD', 'iadb': 'IDB',
        'g20': 'G20', 'g7': 'G7', 'eu': 'EU', 'us': 'US',
        'cdr': 'CDR', 'ccs': 'CCS', 'cat': 'CAT',
        'sar': 'SAR', 'ngo': 'NGO', 'ngos': 'NGOs',
    }

    # Lowercase words that should remain lowercase in title case
    minor_words = {
        'a', 'an', 'the', 'and', 'but', 'or', 'for', 'nor', 'so', 'yet',
        'at', 'by', 'in', 'of', 'on', 'to', 'up', 'via', 'as', 'if',
        'it', 'its', 'be', 'is', 'are', 'was', 'were', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
        'shall', 'should', 'may', 'might', 'can', 'could',
    }

    # If title is all lowercase or all uppercase, convert to title case
    if title.islower() or title.isupper():
        words = title.lower().split()
        if not words:
            return title

        result = []
        for i, word in enumerate(words):
            # Clean word for acronym lookup
            clean = re.sub(r'[^a-zA-Z0-9]', '', word).lower()

            # Always capitalize first and last word
            if i == 0 or i == len(words) - 1:
                if clean in acronyms:
                    result.append(acronyms[clean])
                else:
                    result.append(word.capitalize())
            # Capitalize if not a minor word
            elif clean not in minor_words:
                if clean in acronyms:
                    result.append(acronyms[clean])
                else:
                    result.append(word.capitalize())
            # Lowercase minor words
            else:
                result.append(word.lower())

        return ' '.join(result)

    return title


def classify_article(title, url):
    """Classify article into categories."""
    text = f"{title} {url}".lower()
    categories = []
    for cat, keywords in CATEGORY_MAP.items():
        if any(kw in text for kw in keywords):
            categories.append(cat)
    if not categories:
        categories.append("general")
    return categories


def is_junk_url(url):
    """Filter out non-article URLs."""
    url_l = url.lower()
    parsed = url.split('?')[0]
    ext = '.' + parsed.rsplit('.', 1)[-1] if '.' in parsed else ''
    if ext in {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp', '.avif', '.bmp', '.tiff', '.webmanifest'}:
        return True
    if ext in {'.css', '.js', '.xml', '.json', '.woff', '.woff2', '.ttf', '.eot'}:
        return True
    for pattern in NON_ARTICLE_URL_PATTERNS:
        if re.search(pattern, url_l):
            return True
    if 'errors.edgesuite.net' in url_l:
        return True
    if 'you don\'t have permission' in url_l:
        return True
    return False


def is_junk_title(title):
    """Filter out non-article titles."""
    t = title.lower().strip()
    if len(t) < 8:
        return True
    if t.startswith(('http://', 'https://', 'www.')):
        return True
    if t in NAV_TITLES:
        return True
    if re.match(r'^(page|p)\s*\d+$', t):
        return True
    return False


def is_relevant(text):
    """Check if text is relevant to climate/actuarial topics."""
    t = (text or "").lower()
    return any(kw in t for kw in CLIMATE_KEYWORDS) or any(kw in t for kw in ACTUARIAL_KEYWORDS)


def extract_articles_from_changes(site_id, since_date):
    """Extract real article titles and URLs from the changes table diffs."""
    try:
        c = sqlite3.connect(str(SITE_DB))

        # Get new_content changes (these have #### [Title](URL) format)
        rows = c.execute(
            """SELECT diff_snippet FROM changes
               WHERE site_id = ? AND detected_at >= ? AND change_type = 'new_content'
               ORDER BY detected_at DESC""",
            (site_id, since_date)
        ).fetchall()

        articles = []
        for row in rows:
            diff = row[0] or ""
            lines = diff.split('\n')

            for line in lines:
                line = line.strip()
                if not line.startswith('+') or line.startswith('+++'):
                    continue
                line = line[1:].strip()

                # Pattern: #### [Title](URL)
                m = re.match(r'^#{2,6}\s+\[([^\]]+)\]\((https?://[^\)]+)\)', line)
                if m:
                    title = m.group(1).strip()
                    url = m.group(2).split('?')[0].rstrip('/')
                    if not is_junk_url(url) and not is_junk_title(title):
                        articles.append({"title": title[:120], "url": url})

        # Also get new_links changes (just URLs, extract title from URL)
        rows = c.execute(
            """SELECT diff_snippet FROM changes
               WHERE site_id = ? AND detected_at >= ? AND change_type = 'new_links'
               ORDER BY detected_at DESC""",
            (site_id, since_date)
        ).fetchall()

        for row in rows:
            diff = row[0] or ""
            for line in diff.split('\n'):
                line = line.strip()
                if line.startswith('+'):
                    line = line[1:].strip()
                if line.startswith('https://') or line.startswith('http://'):
                    url = line.split()[0].split('?')[0].rstrip('/')
                    if not is_junk_url(url):
                        # Extract title from URL path
                        path = url.split('/')[-1] if '/' in url else url
                        path = re.sub(r'[-_]+', ' ', path)
                        path = re.sub(r'\.(pdf|html?|aspx?|jsp)$', '', path)
                        if len(path) >= 10:
                            path = normalize_title(path)
                            articles.append({"title": path[:80], "url": url})

        c.close()
        return articles
    except Exception as e:
        print(f"WARNING: extract_articles failed for site {site_id}: {e}", file=sys.stderr)
        return []


def main():
    parser = argparse.ArgumentParser(description="Pillar A site check")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--since-days", type=int, default=7)
    args = parser.parse_args()

    # Load baseline state (Pillar A URLs from article_state.json).
    # The "__pillar_b__" key tracks web-search URLs and must not be treated
    # as a monitored-org baseline.
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    baseline_urls = set()
    for org, urls in state.items():
        if org == "__pillar_b__":
            continue
        for url in urls:
            baseline_urls.add(url.split('?')[0].rstrip('/'))

    print(f"Baseline (Pillar A): {len(baseline_urls)} URLs in article_state.json")

    # Query changes from last N days
    window_anchor = date.fromisoformat(args.date)
    since = (window_anchor - timedelta(days=args.since_days)).isoformat()

    c = sqlite3.connect(str(SITE_DB))

    # Get distinct sites with changes
    sites_with_changes = c.execute(
        """SELECT DISTINCT s.id, s.name, s.url
           FROM changes c
           JOIN sites s ON s.id = c.site_id
           WHERE c.detected_at >= ?
           ORDER BY s.id""",
        (since,)
    ).fetchall()

    print(f"Sites with changes (last {args.since_days} days): {len(sites_with_changes)}")

    articles = []
    new_baseline_urls: dict[str, set[str]] = {}
    total_new = 0
    total_seen_before = 0

    for site_id, name, url in sites_with_changes:
        # Extract articles from changes table
        found = extract_articles_from_changes(site_id, since)

        if not found:
            continue

        items = []
        for art in found:
            title = art["title"]
            art_url = art["url"]

            # Filter junk
            if is_junk_url(art_url) or is_junk_title(title):
                continue

            # Check baseline (Pillar A only)
            if art_url in baseline_urls:
                total_seen_before += 1
                continue

            # Filter irrelevant
            if not is_relevant(title + " " + art_url):
                continue

            # Classify
            categories = classify_article(title, art_url)

            items.append({
                "title": title[:120],
                "url": art_url,
                "categories": categories,
            })
            new_baseline_urls.setdefault(name, set()).add(art_url)
            total_new += 1

        if items:
            articles.append({
                "org": name,
                "items": items,
            })

    c.close()

    # Append newly discovered Pillar A URLs to the baseline so the same
    # article is not reported again next week. Failure is non-fatal: the
    # report stays valid, at the cost of repeats next run.
    if new_baseline_urls:
        try:
            updated = 0
            for org, urls in new_baseline_urls.items():
                bucket = state.setdefault(org, [])
                existing = set(bucket)
                for url in sorted(urls):
                    if url not in existing:
                        bucket.append(url)
                        existing.add(url)
                        updated += 1
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp_state = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
            tmp_state.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            os.replace(tmp_state, STATE_FILE)
            print(f"Baseline updated: +{updated} Pillar A URLs in article_state.json")
        except OSError as exc:
            print(f"WARNING: could not update article_state.json: {exc}", file=sys.stderr)

    output = {
        "date": args.date,
        "pillar": "A",
        "sites_with_changes": len(sites_with_changes),
        "orgs_with_articles": len(articles),
        "baseline_urls": len(baseline_urls),
        "new_articles": total_new,
        "seen_before": total_seen_before,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "articles": articles,
    }

    out_path = args.output or (REPORTS / f"article_changes_{args.date}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"OK: {len(articles)} orgs, {total_new} new articles ({total_seen_before} already in baseline) → {out_path}")


if __name__ == "__main__":
    raise SystemExit(main())
