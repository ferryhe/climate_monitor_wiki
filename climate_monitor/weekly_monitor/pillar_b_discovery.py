"""Pillar B v2 native-search evidence contract.

This module validates evidence produced by the Agent's installed search tools.
It deliberately contains no search client and no prescribed query list.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from climate_monitor.dedupe import canonical_url
from climate_registry.acquisition import PublicationDatePolicy

SCHEMA_VERSION = "pillar-b-discovery.v2"
LEGACY_SCHEMA_VERSION = "pillar-b-discovery.v1"
ARTICLE_FIELDS = frozenset(
    {"title", "url", "source", "summary", "published_date", "date_evidence", "search_ref", "result_ref"}
)
SEARCH_FIELDS = frozenset(
    {"search_ref", "query", "engine", "status", "attempted_at", "result_refs", "budget", "error"}
)


class PillarBIncompleteError(ValueError):
    """A real search failed, so report authoring must not claim zero updates."""


def _validate_timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"Pillar B {field} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Pillar B {field} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Pillar B {field} must include a timezone")


def validate_discovery(
    payload: Any,
    *,
    report_date: date,
    allow_incomplete: bool = False,
    retain_ineligible: bool = False,
) -> list[dict]:
    """Validate v2 evidence and return storage or report-selection candidates.

    ``retain_ineligible=True`` returns old/out-of-window and unknown-date rows for
    durable acquisition storage.  The default returns only report-eligible rows.
    Unlimited policy returns every valid row, including unknown and old dates.
    Search failures remain represented and reject report handoff unless the
    caller is explicitly performing the storage phase.
    """
    if not isinstance(payload, dict):
        raise ValueError("Pillar B discovery must be an object")
    if payload.get("schema_version") == LEGACY_SCHEMA_VERSION:
        raise ValueError(
            "pillar-b-discovery.v1 is incompatible with v2: rerun without fabricated "
            "fixed-query/date completion"
        )
    if set(payload) != {
        "schema_version", "report_date", "date_policy", "search_decision", "searches", "articles"
    } or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("production Pillar B requires pillar-b-discovery.v2")
    if payload["report_date"] != report_date.isoformat():
        raise ValueError("Pillar B report_date mismatch")
    policy = PublicationDatePolicy.from_dict(payload["date_policy"])
    if policy.anchor_date != report_date:
        raise ValueError("Pillar B date policy anchor mismatch")

    decision = payload["search_decision"]
    if not isinstance(decision, Mapping) or set(decision) != {"status", "reason"}:
        raise ValueError("Pillar B search_decision fields are invalid")
    searches = payload["searches"]
    if not isinstance(searches, list):
        raise ValueError("Pillar B searches must be a list")
    if decision["status"] == "no_search":
        if searches or not isinstance(decision["reason"], str) or not decision["reason"].strip():
            raise ValueError("Pillar B no_search requires evidence-backed reason and no attempts")
    elif decision["status"] == "attempted":
        if decision["reason"] is not None or not searches:
            raise ValueError("Pillar B attempted search requires true attempts")
    else:
        raise ValueError("Pillar B search decision is invalid")

    failed = []
    seen_search_refs: set[str] = set()
    successful_results: dict[str, set[str]] = {}
    for item in searches:
        if not isinstance(item, dict) or set(item) != SEARCH_FIELDS:
            raise ValueError("Pillar B search attempt fields are invalid")
        if (not isinstance(item["search_ref"], str) or not item["search_ref"].strip()
                or item["search_ref"] in seen_search_refs):
            raise ValueError("Pillar B search_ref must be non-empty and unique")
        seen_search_refs.add(item["search_ref"])
        if not isinstance(item["query"], str) or not item["query"].strip():
            raise ValueError("Pillar B executed query is missing")
        if not isinstance(item["engine"], str) or not item["engine"].strip():
            raise ValueError("Pillar B attempted engine is missing")
        _validate_timestamp(item["attempted_at"], "search attempted_at")
        if item["status"] not in {"success", "failed"}:
            raise ValueError("Pillar B search status must be success or failed")
        refs = item["result_refs"]
        if (not isinstance(refs, list)
                or any(not isinstance(ref, str) or not ref or ref != ref.strip() for ref in refs)
                or len(refs) != len(set(refs))):
            raise ValueError("Pillar B search result_refs must be unique trimmed strings")
        budget = item["budget"]
        if (not isinstance(budget, dict) or not budget
                or any(not isinstance(key, str) or not key or key != key.strip()
                       or type(value) is not int or value < 0
                       for key, value in budget.items())):
            raise ValueError("Pillar B search budget must contain non-negative integer limits/usage")
        if item["status"] == "failed":
            if not isinstance(item["error"], str) or not item["error"].strip():
                raise ValueError("Pillar B failed search requires an error")
            failed.append(item)
        else:
            if item["error"] is not None:
                raise ValueError("Pillar B successful search cannot have an error")
            successful_results[item["search_ref"]] = set(item["result_refs"])
    if failed and not allow_incomplete:
        raise PillarBIncompleteError("Pillar B has failed search attempts; not zero results")

    articles = payload["articles"]
    if not isinstance(articles, list):
        raise ValueError("Pillar B articles must be a list")
    accepted: list[dict] = []
    for item in articles:
        if not isinstance(item, dict) or set(item) != ARTICLE_FIELDS:
            raise ValueError("Pillar B article fields are invalid")
        if any(not isinstance(item[field], str) for field in ("title", "url", "source", "summary")):
            raise ValueError("Pillar B article text fields are invalid")
        normalized = canonical_url(item["url"])
        if not normalized:
            raise ValueError("Pillar B article URL is invalid")
        evidence = item["date_evidence"]
        published = item["published_date"]
        parsed = None
        if published is None:
            if evidence is not None:
                raise ValueError("unknown publication date cannot carry date evidence")
        else:
            try:
                parsed = date.fromisoformat(published)
            except (TypeError, ValueError) as exc:
                raise ValueError("Pillar B publication date must be exact or null") from exc
            if parsed.isoformat() != published:
                raise ValueError("Pillar B publication date must be exact or null")
            if (not isinstance(evidence, dict)
                    or set(evidence) != {"kind", "url", "text"}
                    or evidence["kind"] not in {"publisher", "search_result"}
                    or not isinstance(evidence["text"], str) or not evidence["text"].strip()
                    or canonical_url(str(evidence["url"])) != normalized):
                raise ValueError("Pillar B date evidence must identify this article")
        if (not isinstance(item["search_ref"], str)
                or not isinstance(item["result_ref"], str)
                or not item["result_ref"].strip()
                or item["result_ref"] not in successful_results.get(item["search_ref"], set())):
            raise ValueError("Pillar B article must link to a result from its successful search attempt")
        if retain_ineligible or policy.selects(parsed):
            accepted.append(item)
    return accepted
