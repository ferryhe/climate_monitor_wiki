from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import CandidateItem, RunConfig


class ResearchSearchItem(BaseModel):
    title: str
    url: str
    summary: str
    source_name: str = "Research search"
    published: str = ""


class ResearchSearchPayload(BaseModel):
    items: list[ResearchSearchItem]


def parse_openai_research_payload(payload: dict[str, Any]) -> list[CandidateItem]:
    parsed = ResearchSearchPayload.model_validate(payload)
    return [
        CandidateItem(
            title=item.title,
            url=item.url,
            summary=item.summary,
            source_name=item.source_name,
            lane="research",
            published=item.published,
        )
        for item in parsed.items
    ]


def filter_recent_items(
    items: list[CandidateItem],
    *,
    today: date | None = None,
    lookback_days: int = 30,
) -> list[CandidateItem]:
    current = today or date.today()
    earliest = current - timedelta(days=lookback_days)
    recent: list[CandidateItem] = []
    for item in items:
        published = _parse_date(item.published)
        if published is None:
            continue
        if earliest <= published <= current:
            recent.append(item)
    return recent


def read_research_fixture(path: str | Path) -> list[CandidateItem]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        payload = {"items": payload}
    if not isinstance(payload, dict):
        raise ValueError("Research fixture must be a list or object with an 'items' list")
    return parse_openai_research_payload(payload)


def search_recent_research(
    config: RunConfig,
    *,
    fixture_path: str | Path | None = None,
    openai_client: Any | None = None,
    today: date | None = None,
) -> list[CandidateItem]:
    if fixture_path:
        return filter_recent_items(
            read_research_fixture(fixture_path),
            today=today,
            lookback_days=config.research_lookback_days,
        )
    if os.getenv("CLIMATE_MONITOR_ENABLE_LIVE_RESEARCH") != "1" or not os.getenv("OPENAI_API_KEY"):
        return []

    client = openai_client or _build_openai_client()
    response = client.responses.parse(
        model=os.getenv("CLIMATE_MONITOR_SEARCH_MODEL", "gpt-5.2"),
        tools=[{"type": "web_search"}],
        input=[
            {
                "role": "system",
                "content": (
                    f"Find climate-related research papers and institutional reports published in the last "
                    f"{config.research_lookback_days} days. Prefer insurance, actuarial, risk management, "
                    "solvency, supervision, disclosure, and catastrophe modeling relevance. Return only "
                    "source-backed results."
                ),
            },
            {"role": "user", "content": "\n".join(config.research_queries)},
        ],
        text_format=ResearchSearchPayload,
    )
    return filter_recent_items(
        parse_openai_research_payload(response.output_parsed.model_dump()),
        today=today,
        lookback_days=config.research_lookback_days,
    )


def _build_openai_client() -> Any:
    from openai import OpenAI

    return OpenAI()


def _parse_date(value: str) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        pass
    for fmt in ("%Y-%m", "%B %d, %Y", "%b %d, %Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.date()
    return None
