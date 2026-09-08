"""Production search evidence, before the legacy URL candidate adapter."""
from __future__ import annotations

from datetime import date
from typing import Any

from climate_monitor.dedupe import canonical_url
from .prompt_loader import pillar_b_window_start, pillar_b_search_queries

SCHEMA_VERSION = "pillar-b-discovery.v1"
ARTICLE_FIELDS = frozenset({"title", "url", "source", "summary"})


def validate_discovery(payload: Any, *, report_date: date) -> list[dict]:
    """Require completed searches and dated evidence; never turn failure into [].

    Dates are producer assertions backed by retained excerpts, not inferred
    from URL paths or discovery time. The original envelope remains the hashed
    input artifact, so each candidate can point back to its date evidence.
    """
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "report_date", "searches", "articles"
    } or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("production Pillar B requires pillar-b-discovery.v1")
    if payload["report_date"] != report_date.isoformat():
        raise ValueError("Pillar B report_date mismatch")
    searches = payload["searches"]
    if not isinstance(searches, list) or any(
        not isinstance(item, dict) or set(item) != {"query", "status"}
        or not isinstance(item["query"], str) or item["status"] != "completed"
        for item in searches
    ):
        raise ValueError("Pillar B searches must all be completed")
    queries = [item["query"] for item in searches]
    if sorted(queries) != sorted(pillar_b_search_queries(report_date)):
        raise ValueError("Pillar B searches do not match the required query set")
    articles = payload["articles"]
    if not isinstance(articles, list):
        raise ValueError("Pillar B articles must be a list")
    start = pillar_b_window_start(report_date)
    for item in articles:
        if not isinstance(item, dict) or set(item) != ARTICLE_FIELDS | {"published_date", "date_evidence"}:
            raise ValueError("Pillar B article fields are invalid")
        try:
            published = date.fromisoformat(item["published_date"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Pillar B requires an exact publication date") from exc
        if published.isoformat() != item["published_date"] or not start <= published <= report_date:
            raise ValueError("Pillar B publication date is outside the report window")
        evidence = item["date_evidence"]
        if (not isinstance(evidence, dict) or set(evidence) != {"url", "text"}
                or not isinstance(evidence["text"], str) or not evidence["text"].strip()
                or not isinstance(evidence["url"], str)
                or not isinstance(item["url"], str)
                or not canonical_url(item["url"])
                or canonical_url(evidence["url"]) != canonical_url(item["url"])):
            raise ValueError("Pillar B date evidence must identify this article")
    return articles
