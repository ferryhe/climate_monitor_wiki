from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .dedupe import canonical_url
from .models import CandidateItem, MonitorSource, SiteScope
from .request_budget import (
    DEFAULT_FETCH_ATTEMPTS,
    DEFAULT_SEARCH_ATTEMPTS,
    DEFAULT_SEARCH_RESULTS,
    GuardedGateway,
    RequestBudget,
    RequestBudgetError,
)


_ACTIONABLE_MANIFEST_STATUSES = {"changed", "downloaded", "new", "updated"}
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
_SYSTEMIC_READ_FAILURE_THRESHOLD = 3


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


def gateway_configuration(
    sources: list[MonitorSource], scopes: dict[str, SiteScope], *, budget_limit: int = DEFAULT_FETCH_ATTEMPTS,
) -> dict[str, Any]:
    """Bind the public gateway to the selected inventory and supported HTTP identity.

    This is the upstream gateway cap, not a durable climate run request ledger.
    """
    seeds = tuple(dict.fromkeys(
        url for source in sources for url in _seed_urls(source, scopes.get(source.key))
    ))
    config = {
        "seed_urls": list(seeds),
        "allowed_domains": sorted({urlparse(url).hostname for url in seeds}),
        "user_agent": "web-listening-bot/1.0",
        "max_body_bytes": 4 * 1024 * 1024,
        "timeout_seconds": 30.0,
        "budget_limit": budget_limit,
    }
    authority = {"sources": [asdict(source) for source in sources],
                 "scopes": [asdict(scopes[source.key]) for source in sources
                            if source.key in scopes], "gateway": config}
    config["authority_sha256"] = hashlib.sha256(
        json.dumps(authority, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode()
    ).hexdigest()
    return config


def _load_gateway_builder() -> Any:
    from web_listening.blocks.governed_read import build_runtime_read_gateway
    return build_runtime_read_gateway


@contextmanager
def _open_governed_runtime(sources, scopes, config=None, budget=None):
    """Assemble once before bulk work; cleanup never replaces the root error."""
    stack = ExitStack()
    try:
        if os.getenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING") != "1":
            raise RuntimeError("live web_listening collection requires CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING=1")
        _extend_web_listening_path()
        Crawler, diff = _load_web_listening()
        builder = _load_gateway_builder()
        expected = gateway_configuration(
            sources, scopes, budget_limit=config["budget_limit"] if config is not None else DEFAULT_FETCH_ATTEMPTS,
        )
        if config is not None and config != expected:
            raise ValueError("frozen gateway authority/scope/identity configuration differs")
        kwargs = {**expected, "seed_urls": tuple(expected["seed_urls"]),
                  "allowed_domains": tuple(expected["allowed_domains"])}
        gateway = builder(**kwargs)
        stack.callback(gateway.close)
        if not callable(gateway.read) or gateway.user_agent != expected["user_agent"]:
            raise ValueError("incompatible governed read gateway or mismatched User-Agent")
        import inspect
        parameters = inspect.signature(gateway.read).parameters
        if not ({"before_target_request", "timeout_seconds"}.issubset(parameters)
                or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())):
            raise ValueError("governed gateway read lacks before_target_request/timeout_seconds")
        if budget is None:
            temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="climate-seed-budget-"))
            budget = RequestBudget(Path(temporary) / "ledger.json", {
                "run_id": expected["authority_sha256"], "attempt": 1,
                "budgets": {"fetch_attempts": expected["budget_limit"],
                            "search_attempts": DEFAULT_SEARCH_ATTEMPTS,
                            "search_results": DEFAULT_SEARCH_RESULTS,
                            "retries_per_item": 2, "runtime_seconds": 3600},
            })
        guarded = GuardedGateway(
            gateway, budget, "seed",
            transport_timeout_seconds=expected["timeout_seconds"],
        )
        crawler = stack.enter_context(Crawler(fetch_mode="http", read_gateway=guarded))
        crawler.climate_budget = budget
        crawler.climate_gateway = guarded
    except Exception as exc:
        try:
            stack.close()
        except Exception as cleanup:
            exc.add_note(f"gateway cleanup also failed: {type(cleanup).__name__}: {cleanup}")
        raise RuntimeError(
            f"[tool] governed gateway preflight failed before bulk acquisition: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        yield crawler, diff, expected
    except BaseException as exc:
        try:
            stack.close()
        except Exception as cleanup:
            exc.add_note(f"gateway cleanup also failed: {type(cleanup).__name__}: {cleanup}")
        raise
    else:
        stack.close()


def collect_source_items(
    *, source: MonitorSource, state_dir: Path, fetch_mode: str = "http",
    scope: SiteScope | None = None, stage_checkpoint: bool = False,
    update_checkpoint: bool = True, _runtime: Any | None = None,
    seed_outcomes: dict[str, Any] | None = None,
) -> tuple[list[CandidateItem], list[str]]:
    if _runtime is None:
        with _open_governed_runtime([source], {source.key: scope} if scope else {}) as runtime:
            return collect_source_items(source=source, state_dir=state_dir, fetch_mode=fetch_mode,
                scope=scope, stage_checkpoint=stage_checkpoint, update_checkpoint=update_checkpoint,
                _runtime=runtime, seed_outcomes=seed_outcomes)
    crawler, diff, gateway_config = _runtime
    budget = crawler.climate_budget
    # Collection is serial. The same URL under two selected institutions is
    # two source slots; only subsequent reads of the same slot are retries.
    crawler.climate_gateway.lane = f"seed:{source.key}"
    items, warnings = [], []
    seed_outcomes = seed_outcomes if seed_outcomes is not None else {}
    for seed_url in _seed_urls(source, scope):
        key = f"seed:{source.key}:{seed_url}"
        receipt = budget.receipt(key, reset_systemic=True) if stage_checkpoint else None
        systemic = budget.systemic_read_failure()
        if receipt is None:
            if systemic["stopped"]:
                error = (
                    "systemic controlled-read stop after "
                    f"{_SYSTEMIC_READ_FAILURE_THRESHOLD} consecutive identical "
                    f"post-send failures; root cause: {systemic['root_error']}"
                )
                receipt = {
                    "status": "incomplete", "event_kind": "precheck", "error": error,
                }
                budget.note("precheck", seed_url, error)
                seed_outcomes[seed_url] = receipt
                warnings.append(f"{source.key} seed {seed_url}: {receipt['error']}")
                continue
            sends_before = budget.usage()["target_send_reservations"]
            try:
                budget.remaining_seconds()
                if budget.usage()["fetch_attempts"] >= budget.limits["fetch_attempts"]:
                    raise RequestBudgetError("fetch budget exhausted before seed operation")
                state_file = _state_path(state_dir, source, seed_url)
                previous = _load_state(state_file)
                page = crawler.fetch_page(seed_url, fetch_mode="http",
                    fetch_config_json={"user_agent": gateway_config["user_agent"]})
                final_url = getattr(page, "final_url", "") or seed_url
                reason = _fetch_failure_reason(page, final_url=final_url)
                if reason:
                    raise ValueError(reason)
                compare_text = diff["select_compare_text"](
                    fit_markdown=getattr(page, "fit_markdown", ""),
                    markdown=getattr(page, "markdown", ""), content_text=getattr(page, "content_text", ""))
                links = list((getattr(page, "metadata_json", {}) or {}).get("links", []))
                if not links and diff.get("extract_links"):
                    links = diff["extract_links"](getattr(page, "raw_html", ""), final_url)
                eligible = [link for link in links if _url_allowed(link, scope)]
                new_links = diff["find_new_links"](previous.get("links", []), eligible)
                docs = diff["find_document_links"](new_links)
                candidates = []
                if previous.get("content_hash"):
                    for link in docs + [link for link in new_links if link not in set(docs)]:
                        lane = "document" if link in docs else "website"
                        title = _title_from_url(link)
                        candidates.append(asdict(CandidateItem(title=title, url=link,
                            summary=f"{source.abbreviation} added a new {lane} link. Link text: {title}.",
                            source_name=source.abbreviation, lane=lane, evidence_text=f"{link} {title}")))
                receipt = {"status": "success", "event_kind": "source", "candidates": candidates,
                    "checkpoint": {"content_hash": diff["compute_hash"](compare_text), "links": eligible},
                    "candidate_urls": new_links if previous.get("content_hash") else [],
                    "observed_at": datetime.now(timezone.utc).isoformat()}
                # Commit the reusable evidence before materializing the report
                # stage. A crash after this commit never requires another send.
                if stage_checkpoint:
                    budget.save_receipt(key, receipt)
                systemic = budget.reset_systemic_read_failure()
            except RequestBudgetError as exc:
                receipt = {"status": "incomplete", "event_kind": "precheck", "error": str(exc)}
                budget.note("precheck", seed_url, exc)
                systemic = budget.reset_systemic_read_failure()
            except Exception as exc:
                envelope = getattr(exc, "envelope", None)
                rejected = envelope is not None
                typed_error = f"{type(exc).__name__}: {exc}"
                receipt = {"status": "rejected" if rejected else "incomplete",
                    "event_kind": "policy" if rejected else "network",
                    "error": typed_error}
                if envelope is not None:
                    receipt["rejection"] = envelope.model_dump(mode="json")
                    if stage_checkpoint:
                        budget.save_receipt(key, receipt)
                sent = budget.usage()["target_send_reservations"] > sends_before
                if sent and not rejected:
                    signature = " ".join(typed_error.split())
                    systemic = budget.record_systemic_read_failure(
                        signature, threshold=_SYSTEMIC_READ_FAILURE_THRESHOLD,
                    )
                else:
                    systemic = budget.reset_systemic_read_failure()
                budget.note(receipt["event_kind"], seed_url, receipt["error"])
        seed_outcomes[seed_url] = receipt
        if receipt["status"] == "success":
            items.extend(CandidateItem(**item) for item in receipt["candidates"])
            _save_checkpoint(_state_path(state_dir, source, seed_url), receipt["checkpoint"],
                candidate_urls=receipt["candidate_urls"], staged=stage_checkpoint, update=update_checkpoint)
        else:
            warnings.append(f"{source.key} seed {seed_url}: {receipt['error']}")
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
    scope_by_key = _scope_by_source_key(site_scopes)
    with _open_governed_runtime(sources, scope_by_key) as runtime:
        return _collect_website_items(
            sources, state_dir=state_dir, scope_by_key=scope_by_key,
            stage_checkpoints=stage_checkpoints, update_checkpoints=update_checkpoints,
            runtime=runtime,
        )


def _collect_website_items(sources, *, state_dir, scope_by_key,
                           stage_checkpoints, update_checkpoints, runtime):
    if stage_checkpoints:
        discard_staged_source_checkpoints(state_dir)
    items: list[CandidateItem] = []
    warnings: list[str] = []
    for source in sources:
        scope = scope_by_key.get(source.key)
        try:
            kwargs = {"source": source, "state_dir": state_dir, "scope": scope, "_runtime": runtime}
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


def collect_website_items_with_evidence(
    sources: list[MonitorSource],
    *,
    state_dir: Path,
    site_scopes: dict[str, SiteScope] | list[SiteScope] | tuple[SiteScope, ...] | None = None,
    gateway_config: dict[str, Any] | None = None,
    budget: RequestBudget | None = None,
) -> tuple[list[CandidateItem], list[str], dict[str, Any]]:
    """Collect sites and expose the adapter's stored, hash-bound evidence.

    The legacy two-value API intentionally remains unchanged. Managed runs use
    this API so snapshot validity, disposition, and artifact identity originate
    at the acquisition adapter rather than being guessed from candidate rows.
    """
    state_dir = Path(state_dir)
    scopes = _scope_by_source_key(site_scopes)
    with _open_governed_runtime(sources, scopes, gateway_config, budget) as runtime:
        return _collect_website_evidence(sources, state_dir=state_dir, scopes=scopes, runtime=runtime)


def _collect_website_evidence(sources, *, state_dir, scopes, runtime):
    artifact_dir = state_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    all_items: list[CandidateItem] = []
    all_warnings: list[str] = []
    source_results: list[dict[str, Any]] = []
    discard_staged_source_checkpoints(state_dir)
    for source in sources:
        started = time.monotonic()
        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        scope = scopes.get(source.key)
        seeds = _seed_urls(source, scope)
        seed_outcomes = {}
        items, warnings = collect_source_items(
            source=source, state_dir=state_dir, scope=scope,
            stage_checkpoint=True, update_checkpoint=True, _runtime=runtime, seed_outcomes=seed_outcomes,
        )
        snapshots: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        for seed in seeds:
            checkpoint_path = _checkpoint_stage_path(_state_path(state_dir, source, seed))
            valid = checkpoint_path.is_file()
            checkpoint_sha256 = None
            if valid:
                checkpoint_bytes = checkpoint_path.read_bytes()
                checkpoint_sha256 = hashlib.sha256(checkpoint_bytes).hexdigest()
                snapshots.append({
                    "seed_url": seed, "path": str(checkpoint_path.resolve()),
                    "sha256": checkpoint_sha256,
                    "checkpoint": json.loads(checkpoint_bytes),
                })
            related = [warning for warning in warnings if seed in warning]
            attempts.append({
                "event_kind": "source",
                "source_outcome": seed_outcomes.get(seed, {}),
                "engine": "governed_http",
                "requested_engine": _scope_fetch_mode("http", scope),
                "effective_engine": "governed_http",
                "status": "success" if valid else "failed",
                "attempted_at": observed_at, "requested_url": seed,
                "error": None if valid else ("; ".join(related) or "no valid snapshot was stored"),
                "checkpoint_sha256": checkpoint_sha256,
            })
        complete = bool(seeds) and len(snapshots) == len(seeds) and not warnings
        rows = [
            {"item_id": item.source_item_id or item.url, "item_type": "page",
             "url": item.url, "title": item.title, "summary": item.summary,
             "status": "new", "observed_at": item.detected_at or observed_at}
            for item in items
        ]
        parent_run_id = "managed-" + hashlib.sha256(
            json.dumps({"source": source.key, "observed_at": observed_at,
                        "snapshots": snapshots}, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32]
        rejected = bool(seed_outcomes) and any(row["status"] == "rejected" for row in seed_outcomes.values()) and all(
            row["status"] in {"success", "rejected"} for row in seed_outcomes.values())
        coverage_status = "success" if complete else ("rejected" if rejected else "incomplete")
        manifest_body = {
            "schema_version": "web-listening-manifest.v1",
            "run": {"run_id": f"run-{parent_run_id}", "parent_run_id": parent_run_id},
            "source": {"source_id": source.key, "tree_seed_url": source.url},
            "discovered_items": rows, "snapshot_evidence": snapshots,
            "seed_outcomes": seed_outcomes, "coverage_status": coverage_status,
        }
        artifact_id = "wl-" + hashlib.sha256(
            json.dumps(manifest_body, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        manifest = {**manifest_body, "manifest_id": artifact_id}
        artifact_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                     indent=2) + "\n").encode("utf-8")
        artifact_path = artifact_dir / f"{artifact_id}.json"
        temporary = artifact_path.with_suffix(".tmp")
        temporary.write_bytes(artifact_bytes)
        os.replace(temporary, artifact_path)
        disposition = ("updated" if rows else "unchanged") if complete else ("blocked" if rejected else "failed")
        outcome = {
            "schema_version": "acquisition-batch-result.v2",
            "run_id": f"scope-run-{parent_run_id}",
            "authoritative_status": "completed",
            "status": "succeeded" if complete else "failed", "full_success": complete,
            "counts": {"requested": 1, "updated": int(disposition == "updated"),
                       "unchanged": int(disposition == "unchanged"), "blocked": int(disposition == "blocked"),
                       "failed": int(disposition == "failed"), "unresolved": 0,
                       "valid_snapshots": int(complete), "failed_evidence": int(not complete),
                       "succeeded": int(complete)},
            "dispositions": [{"task_id": source.key, "site_key": source.key,
                              "requested_url": source.url, "disposition": disposition,
                              "reason": "scope.completed" if complete else "scope.acquisition_failed",
                              "artifact_id": artifact_id if complete else None}],
            "summary": {"checked": 1, "succeeded": int(complete), "failed": int(not complete)},
        }
        candidates = [
            {"url": item.url, "title": item.title, "summary": item.summary,
             "source": source.key, "discovery_ref": item.source_item_id or item.url,
             "observed_at": item.detected_at or observed_at}
            for item in items
        ]
        source_results.append({
            "source": source.key, "status": "succeeded" if complete else "failed",
            "coverage_status": coverage_status,
            "disposition": disposition, "artifact_id": artifact_id,
            "artifact_path": str(artifact_path.resolve()),
            "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "manifest": manifest, "outcome": outcome, "attempts": attempts,
            "warnings": list(warnings), "runtime_seconds": time.monotonic() - started,
            "candidates": candidates,
        })
        all_items.extend(items)
        all_warnings.extend(warnings)
    systemic = runtime[0].climate_budget.systemic_read_failure()
    evidence = {"status": "completed", "full_success": bool(source_results) and all(
        row["status"] == "succeeded" for row in source_results
    ), "source_results": source_results,
        "systemic_error": systemic["root_error"] if systemic["stopped"] else None}
    return all_items, all_warnings, evidence


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
            f"WEB_LISTENING_PROJECT_PATH to the pinned checkout. {type(exc).__name__}: {exc}"
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

