from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

from .models import MonitorSource, RunConfig, SiteScope


_ALLOWED_FETCH_MODES = {"", "http", "browser", "auto"}


def _load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML object.")
    return payload


def _normalize_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Source URL is required.")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid source URL: {value}")
    return raw


def load_sources(path: str | Path) -> list[MonitorSource]:
    payload = _load_yaml(Path(path))
    sources = payload.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("sources must be a list.")
    result: list[MonitorSource] = []
    seen_keys: set[str] = set()
    for item in sources:
        if not isinstance(item, dict):
            raise ValueError("Each source must be a YAML object.")
        key = str(item.get("key", "")).strip().lower()
        if not key:
            raise ValueError("Each source needs a key.")
        if key in seen_keys:
            raise ValueError(f"Duplicate source key: {key}")
        seen_keys.add(key)
        tags = tuple(str(tag).strip() for tag in item.get("tags", []) if str(tag).strip())
        result.append(
            MonitorSource(
                key=key,
                abbreviation=str(item.get("abbreviation", "")).strip(),
                full_name=str(item.get("full_name", "")).strip(),
                url=_normalize_url(str(item.get("url", ""))),
                high_priority=bool(item.get("high_priority", False)),
                tags=tags,
            )
        )
    return result


def _string_tuple(values: object, field_name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list):
        raise ValueError(f"{field_name} must be a list.")
    return tuple(str(value).strip() for value in values if str(value).strip())


def _fetch_mode(value: object, *, source_key: str) -> str:
    mode = str(value or "").strip().lower()
    if mode not in _ALLOWED_FETCH_MODES:
        allowed = ", ".join(sorted(mode for mode in _ALLOWED_FETCH_MODES if mode))
        raise ValueError(f"{source_key}.fetch_mode must be one of: {allowed}.")
    return mode


def _optional_mapping(value: object, field_name: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a YAML object.")
    return dict(value)


def load_site_scopes(path: str | Path) -> list[SiteScope]:
    payload = _load_yaml(Path(path))
    scopes = payload.get("site_scopes", [])
    if not isinstance(scopes, list):
        raise ValueError("site_scopes must be a list.")
    result: list[SiteScope] = []
    seen_keys: set[str] = set()
    for item in scopes:
        if not isinstance(item, dict):
            raise ValueError("Each site scope must be a YAML object.")
        source_key = str(item.get("source_key", "")).strip().lower()
        if not source_key:
            raise ValueError("Each site scope needs a source_key.")
        if source_key in seen_keys:
            raise ValueError(f"Duplicate site scope source_key: {source_key}")
        seen_keys.add(source_key)
        seed_urls = tuple(_normalize_url(url) for url in _string_tuple(item.get("seed_urls", []), "seed_urls"))
        result.append(
            SiteScope(
                source_key=source_key,
                seed_urls=seed_urls,
                include_patterns=_string_tuple(item.get("include_patterns", []), "include_patterns"),
                exclude_patterns=_string_tuple(item.get("exclude_patterns", []), "exclude_patterns"),
                include_source_url=bool(item.get("include_source_url", True)),
                fetch_mode=_fetch_mode(item.get("fetch_mode", ""), source_key=source_key),
                fetch_config_json=_optional_mapping(item.get("fetch_config_json"), "fetch_config_json"),
                notes=str(item.get("notes", "")).strip(),
            )
        )
    return result


def load_run_config(path: str | Path) -> RunConfig:
    payload = _load_yaml(Path(path))
    research = payload.get("research_lane", {}) or {}
    output = payload.get("output", {}) or {}
    dedupe = payload.get("dedupe", {}) or {}
    return RunConfig(
        report_title=str(payload.get("report_title", "Daily Climate & Actuarial Monitor")).strip(),
        climate_keywords=tuple(
            str(value).strip().lower()
            for value in payload.get("climate_keywords", [])
            if str(value).strip()
        ),
        actuarial_keywords=tuple(
            str(value).strip().lower()
            for value in payload.get("actuarial_keywords", [])
            if str(value).strip()
        ),
        research_queries=tuple(
            str(value).strip() for value in research.get("queries", []) if str(value).strip()
        ),
        research_lookback_days=int(research.get("lookback_days", 30)),
        max_items_per_report=int(payload.get("max_items_per_report", 12)),
        source_dir=str(output.get("source_dir", "sources")),
        wiki_dir=str(output.get("wiki_dir", "wiki")),
        write_empty_report=bool(output.get("write_empty_report", False)),
        seen_urls_path=str(dedupe.get("url_tracking_path", "monitoring/state/seen_urls.json")),
        seen_titles_path=str(dedupe.get("title_tracking_path", "monitoring/state/seen_titles.json")),
    )
