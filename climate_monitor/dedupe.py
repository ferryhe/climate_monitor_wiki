from __future__ import annotations

import re
from typing import TypeVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}

CandidateT = TypeVar("CandidateT")


def canonical_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_KEYS and not key.lower().startswith(TRACKING_PREFIXES)
    ]
    normalized_path = parsed.path.rstrip("/") or parsed.path
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), normalized_path, "", urlencode(query), ""))


def canonical_title(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "").strip().casefold())


def dedupe_items(
    items: list[CandidateT],
    *,
    seen_urls: set[str],
    seen_titles: set[str],
) -> tuple[list[CandidateT], list[str]]:
    kept: list[CandidateT] = []
    notes: list[str] = []
    local_urls: set[str] = set()
    local_titles: set[str] = set()

    for item in items:
        item_title = str(getattr(item, "title", ""))
        url_key = canonical_url(str(getattr(item, "url", "")))
        title_key = canonical_title(item_title)
        if title_key and (title_key in seen_titles or title_key in local_titles):
            notes.append(f"{item_title} duplicate title - skipped")
            continue
        if url_key in seen_urls or url_key in local_urls:
            notes.append(f"{item_title} ({url_key}) already in URL history - skipped")
            continue
        kept.append(item)
        local_urls.add(url_key)
        if title_key:
            local_titles.add(title_key)

    return kept, notes
