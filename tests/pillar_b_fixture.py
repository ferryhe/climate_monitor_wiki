"""Explicit synthetic search evidence for production contract tests."""


def discovery_fixture(articles=(), day="2026-09-07"):
    frozen_at = f"{day}T00:00:00Z"
    return {
        "schema_version": "pillar-b-discovery.v2",
        "report_date": day,
        "date_policy": {
            "mode": "unlimited", "start": None, "end": None, "days": None,
            "anchor_date": day, "frozen_at": frozen_at,
        },
        "search_decision": {"status": "attempted", "reason": None},
        "searches": [{
            "search_ref": "fixture-search-1",
            "query": "fixture agent-chosen climate query",
            "engine": "fixture-search",
            "status": "success",
            "attempted_at": frozen_at,
            "result_refs": ["fixture://pillar-b/results"],
            "budget": {"max_results": 100, "used_results": len(tuple(articles))},
            "error": None,
        }],
        "articles": [dict(
            item,
            published_date=day,
            date_evidence={
                "url": item["url"], "text": f"Test publication: {day}",
                "kind": "publisher",
            },
            search_ref="fixture-search-1",
            result_ref="fixture://pillar-b/results",
        ) for item in articles],
    }
