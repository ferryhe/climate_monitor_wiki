from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import date
from pathlib import Path

from scripts.sync_source_wiki import sync_source_wiki

from .ai_filter import classify_candidate
from .config import load_run_config, load_site_scopes, load_sources
from .dedupe import canonical_title, canonical_url, dedupe_items
from .models import CandidateItem, MonitorRunResult, RunConfig
from .report_writer import render_report
from .research_search import search_recent_research
from .semantic_bundle import commit_report_with_semantics, recover_pending_commit
from .web_listening_adapter import collect_website_items


DEFAULT_STATE_DIR = Path("monitoring/state")


def _load_string_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return set()
    return {str(item) for item in payload if str(item).strip()}


def _save_string_set(path: Path, values: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(values), indent=2) + "\n", encoding="utf-8")


def _source_file_path(source_dir: Path, report_date: date) -> Path:
    return source_dir / f"climate-monitor-{report_date.isoformat()}.md"


def _resolve_path(value: str | Path, *, root: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def _with_output_overrides(config: RunConfig, *, source_dir: str | Path | None, wiki_dir: str | Path | None) -> RunConfig:
    if source_dir is None and wiki_dir is None:
        return config
    return replace(
        config,
        source_dir=str(source_dir) if source_dir is not None else config.source_dir,
        wiki_dir=str(wiki_dir) if wiki_dir is not None else config.wiki_dir,
    )


def _failed_source_count(warnings: list[str], source_keys: set[str]) -> int:
    failed: set[str] = set()
    for warning in warnings:
        if warning.startswith("Source failure for "):
            key = warning.removeprefix("Source failure for ").split(":", 1)[0].strip()
            if key in source_keys:
                failed.add(key)
            continue
        key = warning.split(":", 1)[0].strip()
        if key in source_keys and " seed " not in warning:
            failed.add(key)
    return len(failed)


def run_monitor(
    *,
    source_config_path: str | Path = "monitoring/supranational_sources.yaml",
    run_config_path: str | Path = "monitoring/run_config.yaml",
    report_date: date | None = None,
    manifest_fixture_path: str | Path | None = None,
    research_fixture_path: str | Path | None = None,
    site_scopes_path: str | Path | None = "monitoring/site_scopes.yaml",
    state_dir: str | Path = "monitoring/state",
    source_dir: str | Path | None = None,
    wiki_dir: str | Path | None = None,
    sync: bool = True,
    update_seen_state: bool = True,
) -> MonitorRunResult:
    day = report_date or date.today()
    repo_root = Path.cwd()
    sources = load_sources(source_config_path)
    config = _with_output_overrides(load_run_config(run_config_path), source_dir=source_dir, wiki_dir=wiki_dir)
    state_root = _resolve_path(state_dir, root=repo_root)
    site_scopes = None
    if manifest_fixture_path is None and site_scopes_path is not None:
        resolved_site_scopes_path = _resolve_path(site_scopes_path, root=repo_root)
        if resolved_site_scopes_path.exists():
            site_scopes = {scope.source_key: scope for scope in load_site_scopes(resolved_site_scopes_path)}

    website_items, website_warnings = collect_website_items(
        sources,
        state_dir=state_root / "websites",
        manifest_fixture_path=manifest_fixture_path,
        site_scopes=site_scopes,
    )
    if (
        manifest_fixture_path is None
        and os.getenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING") == "1"
        and sources
        and _failed_source_count(website_warnings, {source.key for source in sources}) >= len(sources)
    ):
        raise RuntimeError("Live website monitoring failed for every configured source.")

    research_items = search_recent_research(config, fixture_path=research_fixture_path, today=day)
    classified = [classify_candidate(item, config) for item in website_items + research_items]
    relevant = [item for item in classified if item.climate_related and item.actuarial_related]

    seen_urls_path = _resolve_path(config.seen_urls_path, root=repo_root)
    seen_titles_path = _resolve_path(config.seen_titles_path, root=repo_root)
    if state_root != _resolve_path(DEFAULT_STATE_DIR, root=repo_root):
        seen_urls_path = state_root / "seen_urls.json"
        seen_titles_path = state_root / "seen_titles.json"
    seen_urls = _load_string_set(seen_urls_path)
    seen_titles = _load_string_set(seen_titles_path)
    kept, dedup_notes = dedupe_items(relevant, seen_urls=seen_urls, seen_titles=seen_titles)
    kept = kept[: config.max_items_per_report]

    if not kept and not config.write_empty_report:
        return MonitorRunResult(
            report_date=day,
            report_path=None,
            items=(),
            dedup_notes=tuple(dedup_notes),
            warnings=tuple(website_warnings),
            synced=False,
        )

    output_source_dir = _resolve_path(config.source_dir, root=repo_root)
    output_wiki_dir = _resolve_path(config.wiki_dir, root=repo_root)
    output_source_dir.mkdir(parents=True, exist_ok=True)
    output_wiki_dir.mkdir(parents=True, exist_ok=True)
    output_path = _source_file_path(output_source_dir, day)
    recover_pending_commit(output_path)
    report_text = render_report(
        report_date=day,
        title=config.report_title,
        items=kept,
        dedup_notes=dedup_notes,
        sites_monitored=len(sources),
        warnings=website_warnings,
    )
    # One atomic commit: the canonical Markdown and its semantic sidecar are
    # validated first and then published together, so neither can be updated
    # without the other. Semantics come from the single existing authoring
    # pass; this step never calls a model and never touches the network.
    commit = commit_report_with_semantics(
        report_path=output_path,
        report_date=day,
        report_text=report_text,
        items=kept,
    )

    if update_seen_state:
        for item in kept:
            seen_urls.add(canonical_url(item.url))
            title_key = canonical_title(item.title)
            if title_key:
                seen_titles.add(title_key)
        _save_string_set(seen_urls_path, seen_urls)
        _save_string_set(seen_titles_path, seen_titles)

    synced = False
    if sync:
        sync_source_wiki(source_dir=output_source_dir, wiki_dir=output_wiki_dir)
        synced = True

    return MonitorRunResult(
        report_date=day,
        report_path=str(output_path),
        items=tuple(kept),
        dedup_notes=tuple(dedup_notes),
        warnings=tuple(website_warnings),
        synced=synced,
        report_sha256=commit["report_sha256"],
        semantics_path=str(commit["sidecar_path"]),
    )
