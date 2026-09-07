from __future__ import annotations

import re
from typing import TypeVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
ASCII_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

CandidateT = TypeVar("CandidateT")


def canonical_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_KEYS and not key.lower().startswith(TRACKING_PREFIXES)
    ]
    # RFC 3986 unreserved path escapes have the same URL identity as their
    # literal characters. Leave reserved escapes, query handling and authority
    # unchanged; strict candidate validation still owns URL safety.
    path = re.sub(
        r"%([0-9a-fA-F]{2})",
        lambda match: chr(int(match[1], 16))
        if chr(int(match[1], 16)) in ASCII_UNRESERVED else match[0],
        parsed.path,
    )
    normalized_path = path.rstrip("/") or path
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), normalized_path, "", urlencode(query), ""))


def canonical_title(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "").strip().casefold())


def dedupe_items(
    items: list[CandidateT],
    *,
    seen_urls: set[str],
) -> tuple[list[CandidateT], list[str]]:
    kept: list[CandidateT] = []
    notes: list[str] = []
    local_urls: set[str] = set()

    for item in items:
        item_title = str(getattr(item, "title", ""))
        url_key = canonical_url(str(getattr(item, "url", "")))
        if url_key in seen_urls or url_key in local_urls:
            notes.append(f"{item_title} ({url_key}) already in URL history - skipped")
            continue
        kept.append(item)
        local_urls.add(url_key)

    return kept, notes
