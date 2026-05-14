from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .models import CandidateItem, MonitorSource


def read_manifest_items(path: str | Path) -> list[CandidateItem]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    manifests = payload if isinstance(payload, list) else [payload]
    items: list[CandidateItem] = []
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        source = manifest.get("source", {}) or {}
        source_name = str(source.get("site_name") or source.get("source_id") or "Website")
        for raw in manifest.get("discovered_items", []) or []:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url", "")).strip()
            if not url:
                continue
            title = str(raw.get("title") or _title_from_url(url))
            items.append(
                CandidateItem(
                    title=title,
                    url=url,
                    summary=str(raw.get("summary") or f"{source_name} published or changed: {title}."),
                    source_name=source_name,
                    lane="website",
                    detected_at=str(raw.get("observed_at", "")),
                    content_hash=str(raw.get("content_hash", "")),
                )
            )
    return items


def collect_source_items(
    *,
    source: MonitorSource,
    state_dir: Path,
    fetch_mode: str = "http",
) -> list[CandidateItem]:
    if os.getenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING") != "1":
        raise RuntimeError("live web_listening collection requires CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING=1")

    _extend_web_listening_path()
    Crawler, diff = _load_web_listening()
    state_file = _state_path(state_dir, source)
    previous = _load_state(state_file)
    with Crawler(fetch_mode=fetch_mode) as crawler:
        page = crawler.fetch_page(source.url, fetch_mode=fetch_mode)

    compare_text = diff["select_compare_text"](
        fit_markdown=getattr(page, "fit_markdown", ""),
        markdown=getattr(page, "markdown", ""),
        content_text=getattr(page, "content_text", ""),
    )
    content_hash = diff["compute_hash"](compare_text)
    current_links = list((getattr(page, "metadata_json", {}) or {}).get("links", []))
    if not current_links and diff.get("extract_links"):
        current_links = diff["extract_links"](getattr(page, "raw_html", ""), getattr(page, "final_url", "") or source.url)

    new_links = diff["find_new_links"](previous.get("links", []), current_links)
    doc_links = diff["find_document_links"](new_links)
    items: list[CandidateItem] = []
    if not previous.get("content_hash"):
        _save_state(state_file, {"content_hash": content_hash, "links": current_links})
        return items

    if previous.get("content_hash") and previous.get("content_hash") != content_hash:
        items.append(
            CandidateItem(
                title=f"{source.abbreviation} website content changed",
                url=getattr(page, "final_url", "") or source.url,
                summary=_summary_from_text(compare_text, fallback=f"{source.full_name} homepage or monitored landing page changed."),
                source_name=source.abbreviation,
                lane="website",
                content_hash=content_hash,
                evidence_text=compare_text[:5000],
            )
        )
    for link in doc_links + [link for link in new_links if link not in doc_links]:
        items.append(
            CandidateItem(
                title=_title_from_url(link),
                url=link,
                summary=f"{source.abbreviation} added a new link observed from {source.url}. Link text: {_title_from_url(link)}.",
                source_name=source.abbreviation,
                lane="website",
                evidence_text=" ".join([link, _title_from_url(link), compare_text[:1000]]),
            )
        )
    _save_state(state_file, {"content_hash": content_hash, "links": current_links})
    return items


def collect_website_items(
    sources: list[MonitorSource],
    *,
    state_dir: Path,
    manifest_fixture_path: str | Path | None = None,
) -> tuple[list[CandidateItem], list[str]]:
    if manifest_fixture_path:
        return read_manifest_items(manifest_fixture_path), []
    items: list[CandidateItem] = []
    warnings: list[str] = []
    for source in sources:
        try:
            items.extend(collect_source_items(source=source, state_dir=state_dir))
        except Exception as exc:
            warnings.append(f"{source.key}: {exc}")
    return items, warnings


def _extend_web_listening_path() -> None:
    project_path = os.getenv("WEB_LISTENING_PROJECT_PATH")
    if project_path:
        resolved = str(Path(project_path).resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)


def _load_web_listening() -> tuple[Any, dict[str, Any]]:
    try:
        from web_listening.blocks.crawler import Crawler
        from web_listening.blocks.diff import (
            compute_hash,
            extract_links,
            find_document_links,
            find_new_links,
            select_compare_text,
        )
    except Exception as exc:
        raise RuntimeError(
            "web_listening is required for live website monitoring. Install it or set "
            "WEB_LISTENING_PROJECT_PATH to a local checkout."
        ) from exc
    return Crawler, {
        "compute_hash": compute_hash,
        "extract_links": extract_links,
        "find_document_links": find_document_links,
        "find_new_links": find_new_links,
        "select_compare_text": select_compare_text,
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _state_path(state_dir: Path, source: MonitorSource) -> Path:
    digest = hashlib.sha256(source.url.encode("utf-8")).hexdigest()[:12]
    return state_dir / f"{source.key}-{digest}.json"


def _title_from_url(url: str) -> str:
    path = unquote(urlparse(url).path).strip("/")
    if not path:
        return url
    stem = Path(path).name.rsplit(".", 1)[0]
    return stem.replace("-", " ").replace("_", " ").strip().title() or url


def _summary_from_text(text: str, *, fallback: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return fallback
    return cleaned[:500] + ("..." if len(cleaned) > 500 else "")
