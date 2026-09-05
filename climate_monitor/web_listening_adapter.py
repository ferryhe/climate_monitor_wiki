from __future__ import annotations

import hashlib
import json
import os
import sys
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .dedupe import canonical_url
from .models import CandidateItem, MonitorSource, SiteScope


_ACTIONABLE_MANIFEST_STATUSES = {"changed", "downloaded", "new", "updated"}
_BROWSER_FETCH_CONFIG = {"user_agent_profile": "browser"}
_BAD_FINAL_URL_MARKERS = (
    "/404",
    "/error/",
    "error_404",
    "not-found",
    "redirect_captcha",
)
_BLOCKED_CONTENT_MARKERS = (
    "access denied",
    "checking if the site connection is secure",
    "performing security verification",
    "please complete the security check",
    "request unsuccessful",
    "security verification",
)
_CHECKPOINT_STAGE_VERSION = "web-listening-checkpoint-stage.v1"
_CHECKPOINT_STAGE_SUFFIX = ".pending-run.json"


def read_manifest_items(path: str | Path) -> list[CandidateItem]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    manifests = payload if isinstance(payload, list) else [payload]
    items: list[CandidateItem] = []
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        source = manifest.get("source", {}) or {}
        source_name = str(source.get("site_name") or source.get("source_id") or "Website")
        assets_by_source_item_id = _assets_by_source_item_id(manifest.get("downloaded_assets", []) or [])
        for raw in manifest.get("discovered_items", []) or []:
            if not isinstance(raw, dict):
                continue
            if not _manifest_item_is_actionable(raw):
                continue
            url = str(raw.get("url", "")).strip()
            if not url:
                continue
            title = str(raw.get("title") or _title_from_url(url))
            item_id = str(raw.get("item_id", ""))
            item_type = str(raw.get("item_type", ""))
            lane = "document" if item_type == "file_link" else "website"
            asset = assets_by_source_item_id.get(item_id) if lane == "document" else None
            items.append(
                CandidateItem(
                    title=title,
                    url=url,
                    summary=str(raw.get("summary") or _manifest_summary(source_name, title, lane=lane)),
                    source_name=source_name,
                    lane=lane,
                    detected_at=str(raw.get("observed_at", "")),
                    content_hash=_content_hash(raw),
                    source_item_id=item_id,
                    semantics=_manifest_semantics(raw),
                    **_asset_fields(asset, raw=raw),
                )
            )
    return items


def _manifest_semantics(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Return semantics attached by the single existing authoring pass.

    ``web_listening`` manifests may carry a ``semantics`` object produced in the
    same Hermes pass that discovered the item. It is passed through untouched;
    ``climate_monitor.semantic_bundle`` validates it fail-closed later, only for
    articles that survive final selection.
    """

    semantics = raw.get("semantics")
    return dict(semantics) if isinstance(semantics, dict) else None


def _manifest_item_is_actionable(raw: dict[str, Any]) -> bool:
    status = raw.get("status")
    if status is None:
        return True
    status_text = str(status).strip().lower()
    if not status_text:
        return True
    return status_text in _ACTIONABLE_MANIFEST_STATUSES


def collect_source_items(
    *,
    source: MonitorSource,
    state_dir: Path,
    fetch_mode: str = "http",
    scope: SiteScope | None = None,
    stage_checkpoint: bool = False,
    update_checkpoint: bool = True,
) -> tuple[list[CandidateItem], list[str]]:
    if os.getenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING") != "1":
        raise RuntimeError("live web_listening collection requires CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING=1")

    _extend_web_listening_path()
    Crawler, diff = _load_web_listening()
    items: list[CandidateItem] = []
    warnings: list[str] = []
    resolved_fetch_mode = _scope_fetch_mode(fetch_mode, scope)
    fetch_config = _scope_fetch_config(scope)
    for seed_url in _seed_urls(source, scope):
        try:
            state_file = _state_path(state_dir, source, seed_url)
            previous = _load_state(state_file)
            with Crawler(fetch_mode=resolved_fetch_mode) as crawler:
                page = crawler.fetch_page(
                    seed_url,
                    fetch_mode=resolved_fetch_mode,
                    fetch_config_json=fetch_config,
                )

            final_url = getattr(page, "final_url", "") or seed_url
            failure_reason = _fetch_failure_reason(page, final_url=final_url)
            if failure_reason:
                warnings.append(f"{source.key} seed {seed_url}: {failure_reason}")
                continue
            compare_text = diff["select_compare_text"](
                fit_markdown=getattr(page, "fit_markdown", ""),
                markdown=getattr(page, "markdown", ""),
                content_text=getattr(page, "content_text", ""),
            )
            content_hash = diff["compute_hash"](compare_text)
            current_links = list((getattr(page, "metadata_json", {}) or {}).get("links", []))
            if not current_links and diff.get("extract_links"):
                current_links = diff["extract_links"](getattr(page, "raw_html", ""), final_url)

            eligible_links = [link for link in current_links if _url_allowed(link, scope)]
            new_links = diff["find_new_links"](previous.get("links", []), eligible_links)
            doc_links = diff["find_document_links"](new_links)
            if not previous.get("content_hash"):
                _save_checkpoint(
                    state_file,
                    {"content_hash": content_hash, "links": eligible_links},
                    candidate_urls=(),
                    staged=stage_checkpoint,
                    update=update_checkpoint,
                )
                continue

            doc_link_set = set(doc_links)
            for link in doc_links + [link for link in new_links if link not in doc_link_set]:
                lane = "document" if link in doc_link_set else "website"
                link_title = _title_from_url(link)
                items.append(
                    CandidateItem(
                        title=link_title,
                        url=link,
                        summary=f"{source.abbreviation} added a new {lane} link. Link text: {link_title}.",
                        source_name=source.abbreviation,
                        lane=lane,
                        evidence_text=f"{link} {link_title}",
                    )
                )
            _save_checkpoint(
                state_file,
                {"content_hash": content_hash, "links": eligible_links},
                candidate_urls=new_links,
                staged=stage_checkpoint,
                update=update_checkpoint,
            )
        except Exception as exc:
            warnings.append(f"{source.key} seed {seed_url}: {exc}")
    return items, warnings


def collect_website_items(
    sources: list[MonitorSource],
    *,
    state_dir: Path,
    manifest_fixture_path: str | Path | None = None,
    site_scopes: dict[str, SiteScope] | list[SiteScope] | tuple[SiteScope, ...] | None = None,
    stage_checkpoints: bool = False,
    update_checkpoints: bool = True,
) -> tuple[list[CandidateItem], list[str]]:
    if manifest_fixture_path:
        return read_manifest_items(manifest_fixture_path), []
    if stage_checkpoints:
        discard_staged_source_checkpoints(state_dir)
    scope_by_key = _scope_by_source_key(site_scopes)
    items: list[CandidateItem] = []
    warnings: list[str] = []
    for source in sources:
        scope = scope_by_key.get(source.key)
        try:
            kwargs = {"source": source, "state_dir": state_dir, "scope": scope}
            if stage_checkpoints:
                kwargs["stage_checkpoint"] = True
            if not update_checkpoints:
                kwargs["update_checkpoint"] = False
            source_items, source_warnings = collect_source_items(**kwargs)
            if source_warnings and not source_items and len(source_warnings) >= len(_seed_urls(source, scope)):
                warnings.append(f"Source failure for {source.key}: all monitored seeds failed.")
            warnings.extend(source_warnings)
            items.extend(source_items)
        except Exception as exc:
            warnings.append(f"Source failure for {source.key}: {exc}")
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


def _assets_by_source_item_id(raw_assets: list[Any]) -> dict[str, dict[str, Any]]:
    assets: dict[str, dict[str, Any]] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            continue
        source_item_id = str(raw_asset.get("source_item_id", ""))
        if not source_item_id or source_item_id in assets:
            continue
        assets[source_item_id] = raw_asset
    return assets


def _asset_fields(asset: dict[str, Any] | None, *, raw: dict[str, Any]) -> dict[str, Any]:
    checksum = asset.get("checksum", {}) if isinstance(asset, dict) else {}
    if not isinstance(checksum, dict):
        checksum = {}
    return {
        "asset_id": str(asset.get("asset_id", "")) if asset else "",
        "asset_local_path": str(asset.get("local_path", "")) if asset else "",
        "asset_canonical_blob_path": str(asset.get("canonical_blob_path", "")) if asset else "",
        "asset_tracked_path": str(asset.get("tracked_path", "")) if asset else "",
        "asset_filename": str(asset.get("filename", "")) if asset else "",
        "asset_media_type": str(asset.get("media_type") or raw.get("content_type") or "") if asset else str(raw.get("content_type", "")),
        "asset_bytes": _int_or_none(asset.get("bytes")) if asset else None,
        "asset_checksum_algorithm": str(checksum.get("algorithm", "")),
        "asset_checksum_value": str(checksum.get("value", "")),
        "asset_metadata": dict(asset) if asset else None,
    }


def _content_hash(raw: dict[str, Any]) -> str:
    content_hash = raw.get("content_hash")
    if content_hash:
        return str(content_hash)
    checksum = raw.get("checksum")
    if isinstance(checksum, dict):
        return str(checksum.get("value") or "")
    return ""


def _manifest_summary(source_name: str, title: str, *, lane: str) -> str:
    if lane == "document":
        return f"{source_name} published or changed a document/report file: {title}."
    return f"{source_name} published or changed: {title}."


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _checkpoint_stage_path(path: Path) -> Path:
    return path.with_name(path.name + _CHECKPOINT_STAGE_SUFFIX)


def _save_checkpoint(
    path: Path,
    payload: dict[str, Any],
    *,
    candidate_urls: list[str] | tuple[str, ...],
    staged: bool,
    update: bool,
) -> None:
    if not update:
        return
    if not staged:
        _save_state(path, payload)
        return
    identities = sorted({canonical_url(url) or url for url in candidate_urls})
    _save_state(
        _checkpoint_stage_path(path),
        {
            "schema_version": _CHECKPOINT_STAGE_VERSION,
            "state_filename": path.name,
            "candidate_urls": identities,
            "checkpoint": payload,
        },
    )


def discard_staged_source_checkpoints(state_dir: str | Path) -> None:
    """Remove abandoned per-run checkpoint stages without touching canonical state."""

    for path in Path(state_dir).glob(f"*{_CHECKPOINT_STAGE_SUFFIX}"):
        path.unlink(missing_ok=True)


def commit_staged_source_checkpoints(
    state_dir: str | Path,
    *,
    committed_urls: set[str],
) -> int:
    """Commit stages whose discovered candidates are all in canonical URL state."""

    committed = {canonical_url(url) for url in committed_urls}
    applied = 0
    for staged_path in sorted(Path(state_dir).glob(f"*{_CHECKPOINT_STAGE_SUFFIX}")):
        payload = _load_state(staged_path)
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"schema_version", "state_filename", "candidate_urls", "checkpoint"}
            or payload["schema_version"] != _CHECKPOINT_STAGE_VERSION
            or not isinstance(payload["state_filename"], str)
            or Path(payload["state_filename"]).name != payload["state_filename"]
            or staged_path.name
            != payload["state_filename"] + _CHECKPOINT_STAGE_SUFFIX
            or not isinstance(payload["candidate_urls"], list)
            or any(not isinstance(url, str) or not url for url in payload["candidate_urls"])
            or payload["candidate_urls"] != sorted(set(payload["candidate_urls"]))
            or not isinstance(payload["checkpoint"], dict)
            or set(payload["checkpoint"]) != {"content_hash", "links"}
            or not isinstance(payload["checkpoint"]["content_hash"], str)
            or not isinstance(payload["checkpoint"]["links"], list)
            or any(not isinstance(url, str) for url in payload["checkpoint"]["links"])
        ):
            raise ValueError(f"invalid staged web-listening checkpoint: {staged_path.name}")
        candidate_urls = set(payload["candidate_urls"])
        if candidate_urls.issubset(committed):
            _save_state(
                staged_path.with_name(payload["state_filename"]),
                payload["checkpoint"],
            )
            applied += 1
        staged_path.unlink()
    return applied


def _state_path(state_dir: Path, source: MonitorSource, seed_url: str | None = None) -> Path:
    digest = hashlib.sha256((seed_url or source.url).encode("utf-8")).hexdigest()[:12]
    return state_dir / f"{source.key}-{digest}.json"


def _seed_urls(source: MonitorSource, scope: SiteScope | None) -> list[str]:
    urls = [source.url] if scope is None or scope.include_source_url else []
    if scope:
        urls.extend(scope.seed_urls)
    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def _scope_fetch_mode(default_fetch_mode: str, scope: SiteScope | None) -> str:
    if scope and scope.fetch_mode:
        return scope.fetch_mode
    return default_fetch_mode


def _scope_fetch_config(scope: SiteScope | None) -> dict[str, Any]:
    if scope and scope.fetch_config_json is not None:
        return dict(scope.fetch_config_json)
    return dict(_BROWSER_FETCH_CONFIG)


def _fetch_failure_reason(page: Any, *, final_url: str) -> str:
    status_code = getattr(page, "status_code", None)
    if isinstance(status_code, int) and status_code >= 400:
        return f"HTTP {status_code} at {final_url}"
    lowered_final_url = final_url.lower()
    if any(marker in lowered_final_url for marker in _BAD_FINAL_URL_MARKERS):
        return f"resolved to a likely error page: {final_url}"
    metadata = getattr(page, "metadata_json", {}) or {}
    text = _best_page_text(page)
    blocked_marker = _blocked_content_marker(text)
    if blocked_marker:
        return f"blocked or rejected content marker `{blocked_marker}` at {final_url}"
    word_count = _metadata_int(metadata, "word_count", default=len(text.split()))
    link_count = _page_link_count(metadata)
    source_kind = str(metadata.get("source_kind", "html") or "html")
    item_count = _metadata_int(metadata, "item_count")
    if source_kind == "xml_feed" and item_count == 0 and link_count == 0:
        return f"empty feed (status={status_code or 'unknown'}, final_url={final_url})"
    if source_kind == "xml_sitemap" and link_count == 0:
        return f"empty sitemap (status={status_code or 'unknown'}, final_url={final_url})"
    if word_count == 0 and link_count == 0:
        return (
            "no usable information "
            f"(status={status_code or 'unknown'}, words={word_count}, links={link_count}, "
            f"source_kind={source_kind}, final_url={final_url})"
        )
    return ""


def _best_page_text(page: Any) -> str:
    return (
        str(getattr(page, "fit_markdown", "") or "")
        or str(getattr(page, "markdown", "") or "")
        or str(getattr(page, "content_text", "") or "")
    )


def _metadata_int(metadata: dict[str, Any], key: str, *, default: int = 0) -> int:
    try:
        return int(metadata.get(key, default))
    except (TypeError, ValueError):
        return default


def _page_link_count(metadata: dict[str, Any]) -> int:
    links = metadata.get("links")
    if isinstance(links, list):
        return len({str(link) for link in links if str(link).strip()})
    return _metadata_int(metadata, "link_count")


def _blocked_content_marker(text: str) -> str:
    lowered = text.lower()
    for marker in _BLOCKED_CONTENT_MARKERS:
        if marker in lowered:
            return marker
    return ""


def _scope_by_source_key(
    site_scopes: dict[str, SiteScope] | list[SiteScope] | tuple[SiteScope, ...] | None,
) -> dict[str, SiteScope]:
    if site_scopes is None:
        return {}
    if isinstance(site_scopes, dict):
        return site_scopes
    return {scope.source_key: scope for scope in site_scopes}


def _url_allowed(url: str, scope: SiteScope | None) -> bool:
    if _globally_excluded(url):
        return False
    if scope is None:
        return True
    if scope.exclude_patterns and _matches_any(url, scope.exclude_patterns):
        return False
    if not scope.include_patterns:
        return True
    return _matches_any(url, scope.include_patterns)


def _globally_excluded(url: str) -> bool:
    lowered = url.lower()
    return any(
        token in lowered
        for token in (
            "_wp_link_placeholder",
            "/wp-admin/",
            "/wp-login",
            "mailto:",
            "javascript:",
        )
    )


def _matches_any(url: str, patterns: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    candidates = (url, parsed.path or "/", unquote(parsed.path or "/"))
    for pattern in patterns:
        lowered_pattern = pattern.lower()
        glob_pattern = lowered_pattern if any(token in lowered_pattern for token in "*?[]") else f"*{lowered_pattern}*"
        for candidate in candidates:
            lowered_candidate = candidate.lower()
            if lowered_pattern in lowered_candidate or fnmatch(lowered_candidate, glob_pattern):
                return True
    return False


def _title_from_url(url: str) -> str:
    path = unquote(urlparse(url).path).strip("/")
    if not path:
        return url
    stem = Path(path).name.rsplit(".", 1)[0]
    return stem.replace("-", " ").replace("_", " ").strip().title() or url

