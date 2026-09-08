"""Explicit synthetic search evidence for production contract tests."""
from datetime import date

from climate_monitor.weekly_monitor.prompt_loader import pillar_b_search_queries


def discovery_fixture(articles=(), day="2026-09-07"):
    return {
        "schema_version": "pillar-b-discovery.v1", "report_date": day,
        "searches": [{"query": query, "status": "completed"}
                     for query in pillar_b_search_queries(date.fromisoformat(day))],
        "articles": [dict(item, published_date=day,
                          date_evidence={"url": item["url"], "text": f"Test publication: {day}"})
                     for item in articles],
    }
