from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal


@dataclass(frozen=True)
class MonitorSource:
    key: str
    abbreviation: str
    full_name: str
    url: str
    high_priority: bool = False
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunConfig:
    report_title: str
    climate_keywords: tuple[str, ...]
    actuarial_keywords: tuple[str, ...]
    research_queries: tuple[str, ...]
    research_lookback_days: int
    max_items_per_report: int
    source_dir: str
    wiki_dir: str
    write_empty_report: bool
    seen_urls_path: str = "monitoring/state/seen_urls.json"
    seen_titles_path: str = "monitoring/state/seen_titles.json"


@dataclass(frozen=True)
class CandidateItem:
    title: str
    url: str
    summary: str
    source_name: str
    lane: Literal["website", "research"]
    published: str = ""
    detected_at: str = ""
    content_hash: str = ""
    evidence_text: str = ""
    climate_related: bool = False
    actuarial_related: bool = False
    relevance_reason: str = ""
    climate_signal: str = "none"
    actuarial_signal: str = "none"
    confidence: float = 0.0
    evidence_snippet: str = ""
    topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class MonitorRunResult:
    report_date: date
    report_path: str | None
    items: tuple[CandidateItem, ...] = ()
    dedup_notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    synced: bool = False
