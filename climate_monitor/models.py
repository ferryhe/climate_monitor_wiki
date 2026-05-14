from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal


DOCUMENT_JSON_FIELDS = (
    "source_item_id",
    "asset_id",
    "asset_tracked_path",
    "asset_filename",
    "asset_media_type",
    "asset_bytes",
    "asset_checksum_algorithm",
    "asset_checksum_value",
)


@dataclass(frozen=True)
class MonitorSource:
    key: str
    abbreviation: str
    full_name: str
    url: str
    high_priority: bool = False
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SiteScope:
    source_key: str
    seed_urls: tuple[str, ...]
    include_patterns: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    notes: str = ""


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
    lane: Literal["website", "research", "document"]
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
    source_item_id: str = ""
    asset_id: str = ""
    asset_local_path: str = ""
    asset_canonical_blob_path: str = ""
    asset_tracked_path: str = ""
    asset_filename: str = ""
    asset_media_type: str = ""
    asset_bytes: int | None = None
    asset_checksum_algorithm: str = ""
    asset_checksum_value: str = ""
    asset_metadata: dict[str, Any] | None = None
    topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class MonitorRunResult:
    report_date: date
    report_path: str | None
    items: tuple[CandidateItem, ...] = ()
    dedup_notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    synced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_date": self.report_date.isoformat(),
            "report_path": self.report_path,
            "synced": self.synced,
            "item_count": len(self.items),
            "items": [_candidate_item_to_dict(item) for item in self.items],
            "dedup_notes": list(self.dedup_notes),
            "warnings": list(self.warnings),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"


def _candidate_item_to_dict(item: CandidateItem) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "lane": item.lane,
        "source": item.source_name,
        "title": item.title,
        "url": item.url,
        "summary": item.summary,
        "published": item.published,
        "detected": item.detected_at,
        "content_hash": item.content_hash,
        "relevance": {
            "reason": item.relevance_reason,
            "confidence": item.confidence,
        },
        "climate": {
            "related": item.climate_related,
            "signal": item.climate_signal,
        },
        "actuarial": {
            "related": item.actuarial_related,
            "signal": item.actuarial_signal,
        },
        "topics": list(item.topics),
    }
    if item.lane == "document":
        payload.update(_document_metadata(item))
    return payload


def _document_metadata(item: CandidateItem) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for name in DOCUMENT_JSON_FIELDS:
        value = getattr(item, name)
        if _has_json_value(value):
            metadata[name] = _json_value(value)
    return metadata


def _has_json_value(value: Any) -> bool:
    return value is not None and value != "" and value != () and value != [] and value != {}


def _json_value(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(value[key]) for key in sorted(value)}
    return value
