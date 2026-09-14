from __future__ import annotations

import hashlib
import json
import os
import time
import tempfile
import uuid
from contextlib import contextmanager
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
    RequestBudget,
    RequestBudgetError,
)


_ACTIONABLE_MANIFEST_STATUSES = {"changed", "downloaded", "new", "updated"}
_CHECKPOINT_STAGE_VERSION = "web-listening-new-checkpoint-stage.v1"
_CHECKPOINT_STAGE_SUFFIX = ".pending-run.json"
_UPSTREAM_REVISION = "ac2343f89bc7939736d85f049ebe2beac571034a"


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
    """Freeze the public Runtime request policy for the selected inventory."""
    seeds = tuple(dict.fromkeys(
        url for source in sources for url in _seed_urls(source, scopes.get(source.key))
    ))
    config: dict[str, Any] = {
        "schema_version": "climate-web-listening-new.v1",
        "upstream_revision": _UPSTREAM_REVISION,
        "seed_urls": list(seeds),
        "explore_all_tools": True,
        "max_requests_per_target": 12,
        "max_bytes_per_target": 8 * 1024 * 1024,
        "max_runtime_seconds_per_target": 60,
        "max_tool_attempts_per_target": 4,
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


def _runtime_service_type():
    from web_listening.runtime.service import RuntimeService

    return RuntimeService


@contextmanager
def _open_governed_runtime(sources, scopes, state_dir, config=None, budget=None):
    """Open the one pinned public Runtime and the existing durable run budget."""
    service = None
    try:
        if os.getenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING") != "1":
            raise RuntimeError("live web_listening collection requires CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING=1")
        expected = gateway_configuration(
            sources, scopes, budget_limit=config["budget_limit"] if config is not None else DEFAULT_FETCH_ATTEMPTS,
        )
        if config is not None and config != expected:
            raise ValueError("frozen gateway authority/scope/identity configuration differs")
        runtime_root = Path(
            os.environ.get("CLIMATE_WEB_LISTENING_DATA_DIR")
            or Path(state_dir) / ".web-listening-runtime"
        )
        service = _runtime_service_type().open(runtime_root)
        if budget is None:
            temporary = tempfile.TemporaryDirectory(prefix="climate-seed-budget-")
            budget = RequestBudget(Path(temporary.name) / "ledger.json", {
                "run_id": expected["authority_sha256"], "attempt": 1,
                "budgets": {"fetch_attempts": expected["budget_limit"],
                            "search_attempts": DEFAULT_SEARCH_ATTEMPTS,
                            "search_results": DEFAULT_SEARCH_RESULTS,
                            "retries_per_item": 2, "runtime_seconds": 3600},
            })
        else:
            temporary = None
    except Exception as exc:
        if service is not None:
            service.close()
        if "temporary" in locals() and temporary is not None:
            temporary.cleanup()
        raise RuntimeError(
            f"[tool] governed runtime preflight failed before bulk acquisition: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        yield service, expected, budget
    finally:
        service.close()
        if temporary is not None:
            temporary.cleanup()


def collect_source_items(
    *, source: MonitorSource, state_dir: Path, fetch_mode: str = "http",
    scope: SiteScope | None = None, stage_checkpoint: bool = False,
    update_checkpoint: bool = True, _runtime: Any | None = None,
    seed_outcomes: dict[str, Any] | None = None,
) -> tuple[list[CandidateItem], list[str]]:
    if _runtime is None:
        with _open_governed_runtime(
            [source], {source.key: scope} if scope else {}, state_dir
        ) as runtime:
            return collect_source_items(source=source, state_dir=state_dir, fetch_mode=fetch_mode,
                scope=scope, stage_checkpoint=stage_checkpoint, update_checkpoint=update_checkpoint,
                _runtime=runtime, seed_outcomes=seed_outcomes)
    service, gateway_config, budget = _runtime
    items, warnings = [], []
    seed_outcomes = seed_outcomes if seed_outcomes is not None else {}
    for seed_url in _seed_urls(source, scope):
        key = f"seed:{source.key}:{seed_url}"
        receipt = budget.receipt(key) if stage_checkpoint else None
        if receipt is None:
            systemic = budget.systemic_read_failure()
            if systemic["stopped"]:
                receipt = {
                    "status": "incomplete", "event_kind": "precheck",
                    "error": f"systemic acquisition stop: {systemic['root_error']}",
                }
                budget.note("precheck", seed_url, receipt["error"])
                seed_outcomes[seed_url] = receipt
                warnings.append(f"{source.key} seed {seed_url}: {receipt['error']}")
                continue
            try:
                receipt = _run_site_seed(
                    service, source, scope, seed_url, state_dir, gateway_config, budget
                )
            except RequestBudgetError as exc:
                receipt = {"status": "incomplete", "event_kind": "precheck", "error": str(exc)}
                budget.note("precheck", seed_url, exc)
            except Exception as exc:
                typed_error = f"{type(exc).__name__}: {exc}"
                receipt = {"status": "incomplete", "event_kind": "network", "error": typed_error}
                budget.note(receipt["event_kind"], seed_url, receipt["error"])
            if stage_checkpoint and receipt["status"] in {"success", "rejected"}:
                budget.save_receipt(key, receipt)
        seed_outcomes[seed_url] = receipt
        if receipt["status"] == "success":
            items.extend(CandidateItem(**item) for item in receipt["candidates"])
            _save_checkpoint(_state_path(state_dir, source, seed_url), receipt["checkpoint"],
                candidate_urls=receipt["candidate_urls"], staged=stage_checkpoint, update=update_checkpoint)
        else:
            warnings.append(f"{source.key} seed {seed_url}: {receipt['error']}")
    return items, warnings


def _update_source_systemic_failure(budget, seed_outcomes):
    """Count identical transport failure once per independent source."""
    current = budget.systemic_read_failure()
    if current["stopped"]:
        return current
    outcomes = list(seed_outcomes.values())
    if any(row.get("status") in {"success", "rejected"} for row in outcomes):
        return budget.reset_systemic_read_failure()
    errors = {
        row.get("error") for row in outcomes
        if row.get("status") == "incomplete" and row.get("event_kind") == "network"
        and isinstance(row.get("error"), str) and row["error"]
    }
    if outcomes and len(errors) == 1 and all(
        row.get("status") == "incomplete" and row.get("event_kind") == "network"
        for row in outcomes
    ):
        return budget.record_systemic_read_failure(errors.pop(), threshold=3)
    return budget.reset_systemic_read_failure()


def _run_site_seed(service, source, scope, seed_url, state_dir, config, budget):
    from web_listening.request.model import Budgets, ContentType, Request, Scope
    from web_listening.request.site_refresh import SiteRefreshRequest

    remaining = budget.limits["fetch_attempts"] - budget.usage()["fetch_attempts"]
    reserved_units = min(config["max_requests_per_target"], remaining)
    attempt_limit = min(config["max_tool_attempts_per_target"], reserved_units)
    if reserved_units < 1:
        raise RequestBudgetError("fetch budget exhausted before site operation")
    budgets = Budgets(
        reserved_units,
        config["max_bytes_per_target"],
        min(config["max_runtime_seconds_per_target"], max(1, int(budget.remaining_seconds()))),
        attempt_limit,
    )
    request_scope = Scope(
        (seed_url,),
        tuple(sorted({_origin(url) for url in _seed_urls(source, scope)})),
        _upstream_include_paths(seed_url, scope),
        (ContentType.HTML, ContentType.FILE),
    )
    checkpoint = _load_refresh_checkpoint(_state_path(state_dir, source, seed_url), source, seed_url)
    call_id = uuid.uuid4().hex
    budget.claim(
        "web_listening_site", seed_url, call_id=call_id, units=reserved_units,
        retry_key=f"site:{source.key}:{seed_url}",
    )
    try:
        if checkpoint is None:
            result = service.explore_site(Request(request_scope, None, True, budgets))
            phase = "first"
        else:
            result = service.refresh_site(SiteRefreshRequest(
                request_scope, checkpoint["site_skill"], checkpoint["site_state"], True, budgets,
            ))
            phase = "refresh"
        raw = result.to_dict()
    except Exception as exc:
        compact = {"phase": "first" if checkpoint is None else "refresh",
                   "error": f"{type(exc).__name__}: {exc}"}
        # No measured result exists, so keep the conservative reservation spent.
        budget.complete_tool(call_id, compact, "error")
        raise
    actual_requests = raw["usage"]["requests"]
    compact = {
        "phase": phase, "status": raw["status"], "stop_reason": raw["stop_reason"],
        "usage": raw["usage"], "errors": raw["errors"],
    }
    budget.complete_tool(
        call_id, compact, "ok" if raw["status"] in {"completed", "partial"} else "error",
        actual_units=actual_requests,
    )
    errors = _result_error_codes(raw)
    rejected = raw["status"] == "rejected" or any(_policy_error(code) for code in errors)
    if phase == "first":
        usable = raw["status"] == "completed" and raw.get("site_skill_candidate") is not None
        next_skill = raw.get("site_skill_candidate")
        next_state = raw.get("site_state")
        changes = [
            {"url": page["canonical_url"], "current": {
                "digest": page["content_digest"], "artifact_id": page["artifact_id"],
            }}
            for page in (next_state or {}).get("pages", [])
        ]
    else:
        usable = raw.get("refresh_complete") is True and raw["status"] in {"completed", "partial"}
        update = raw.get("site_skill_update")
        next_skill = update.get("candidate") if isinstance(update, dict) else checkpoint["mapping"]["site_skill"]
        next_state = raw.get("current_state")
        changes = list(raw.get("added", [])) + list(raw.get("changed", []))
    if not usable:
        return {
            "status": "rejected" if rejected else "incomplete",
            "event_kind": "policy" if rejected else "source",
            "error": "; ".join(errors) or raw.get("stop_reason") or "site acquisition incomplete",
            "phase": phase, "result": raw, "attempts": raw.get("attempts", []),
            "candidates": [], "candidate_urls": [],
        }
    checkpoint_mapping = {
        "schema_version": "climate-web-listening-refresh-context.v1",
        "upstream_revision": _UPSTREAM_REVISION,
        "source_key": source.key,
        "seed_url": seed_url,
        "site_skill": next_skill,
        "site_state": next_state,
    }
    candidates = []
    for change in changes:
        url = change["url"]
        current = change.get("current")
        if url == seed_url or not isinstance(current, dict) or not _url_allowed(url, scope):
            continue
        lane = "document" if _is_document_url(url) else "website"
        title = _title_from_url(url)
        candidates.append(asdict(CandidateItem(
            title=title, url=url,
            summary=f"{source.abbreviation} published or changed a {lane}: {title}.",
            source_name=source.abbreviation, lane=lane,
            detected_at=next_state["generated_at"],
            content_hash=current["digest"].removeprefix("sha256:"),
            evidence_text=f"{url} {title}", source_item_id=current["artifact_id"],
        )))
    return {
        "status": "success", "event_kind": "source", "phase": phase,
        "candidates": candidates, "candidate_urls": [item["url"] for item in candidates],
        "checkpoint": checkpoint_mapping, "observed_at": next_state["generated_at"],
        "result": raw, "attempts": raw.get("attempts", []),
        "change_counts": {name: len(raw.get(name, [])) for name in
                          ("added", "changed", "unchanged", "missing", "failed", "unresolved")},
    }


def _load_refresh_checkpoint(path, source, seed_url):
    payload = _load_state(path)
    if not payload or payload.get("schema_version") != "climate-web-listening-refresh-context.v1":
        return None
    if (payload.get("upstream_revision"), payload.get("source_key"), payload.get("seed_url")) != (
        _UPSTREAM_REVISION, source.key, seed_url,
    ):
        raise ValueError("web_listening_new checkpoint authority differs")
    from web_listening.artifact.site_state import site_state_from_mapping
    from web_listening.site_skill.validate import site_skill_from_mapping

    return {"mapping": payload, "site_skill": site_skill_from_mapping(payload["site_skill"]),
            "site_state": site_state_from_mapping(payload["site_state"])}


def _origin(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _upstream_include_paths(seed_url, scope):
    seed_path = urlparse(seed_url).path or "/"
    paths = {seed_path}
    if seed_path != "/":
        paths.add(seed_path.rstrip("/") + "/**")
    if scope:
        for pattern in scope.include_patterns:
            path = urlparse(pattern).path if "://" in pattern else pattern
            if not path.startswith("/") or any(token in path for token in "?["):
                continue
            if "*" in path:
                paths.add(path)
            elif path.endswith("/"):
                paths.add(path + "**")
            else:
                paths.add(path)
                paths.add(path.rstrip("/") + "/**")
    return tuple(sorted(paths))


def _result_error_codes(raw):
    codes = [item.get("code", "") for item in raw.get("errors", [])]
    codes.extend(
        attempt.get("error", {}).get("code", "")
        for attempt in raw.get("attempts", []) if isinstance(attempt.get("error"), dict)
    )
    return sorted({code for code in codes if code})


def _policy_error(code):
    return code.startswith((
        "scope.", "robots.", "security.", "policy.", "gateway.", "eligibility.",
    )) or code == "runtime.site_identity_mismatch"


def _is_document_url(url):
    return Path(urlparse(url).path).suffix.lower() in {
        ".csv", ".doc", ".docx", ".pdf", ".ppt", ".pptx", ".xls", ".xlsx", ".xml",
    }


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
    with _open_governed_runtime(sources, scope_by_key, state_dir) as runtime:
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
            seed_outcomes = {}
            kwargs = {"source": source, "state_dir": state_dir, "scope": scope,
                      "_runtime": runtime, "seed_outcomes": seed_outcomes}
            if stage_checkpoints:
                kwargs["stage_checkpoint"] = True
            if not update_checkpoints:
                kwargs["update_checkpoint"] = False
            source_items, source_warnings = collect_source_items(**kwargs)
            _update_source_systemic_failure(runtime[2], seed_outcomes)
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
    with _open_governed_runtime(sources, scopes, state_dir, gateway_config, budget) as runtime:
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
        _update_source_systemic_failure(runtime[2], seed_outcomes)
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
                "engine": "web_listening_new",
                "requested_engine": _scope_fetch_mode("http", scope),
                "effective_engine": [
                    row.get("tool_id")
                    for row in seed_outcomes.get(seed, {}).get("attempts", [])
                    if row.get("tool_id", "").startswith("acquisition.")
                    and row.get("outcome") != "skipped"
                ],
                "status": "success" if valid else "failed",
                "attempted_at": observed_at, "requested_url": seed,
                "error": None if valid else ("; ".join(related) or "no valid snapshot was stored"),
                "checkpoint_sha256": checkpoint_sha256,
            })
        succeeded_seeds = [
            seed for seed in seeds if seed_outcomes.get(seed, {}).get("status") == "success"
        ]
        complete = bool(seeds) and len(succeeded_seeds) == len(seeds) and not warnings
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
        rejected = not succeeded_seeds and bool(seed_outcomes) and any(row["status"] == "rejected" for row in seed_outcomes.values()) and all(
            row["status"] in {"success", "rejected"} for row in seed_outcomes.values())
        coverage_status = "success" if complete else ("rejected" if rejected else "incomplete")
        manifest_body = {
            "schema_version": "web-listening-manifest.v1",
            "run": {"run_id": f"run-{parent_run_id}", "parent_run_id": parent_run_id},
            "source": {"source_id": source.key, "tree_seed_url": source.url},
            "discovered_items": rows, "snapshot_evidence": snapshots,
            "seed_outcomes": seed_outcomes, "coverage_status": coverage_status,
        }
        # Persist and compare the same JSON value. Candidate dataclasses carry
        # tuple fields, which JSON represents as arrays.
        manifest_body = json.loads(json.dumps(manifest_body, ensure_ascii=False))
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
        disposition = (("updated" if rows else "unchanged") if succeeded_seeds
                       else ("blocked" if rejected else "failed"))
        source_disposition = {
            "task_id": source.key, "site_key": source.key,
            "requested_url": manifest["source"]["tree_seed_url"],
            "disposition": disposition,
            "reason": (
                "scope.completed" if complete else
                "scope.partial" if succeeded_seeds else
                "scope.policy_rejected" if rejected else
                "scope.acquisition_failed"
            ),
            "artifact_id": artifact_id if succeeded_seeds else None,
        }
        dispositions = [source_disposition]
        counts_by_disposition = {
            name: sum(row["disposition"] == name for row in dispositions)
            for name in ("updated", "unchanged", "blocked", "failed", "unresolved")
        }
        succeeded_count = counts_by_disposition["updated"] + counts_by_disposition["unchanged"]
        outcome = {
            "schema_version": "acquisition-batch-result.v2",
            "run_id": f"scope-run-{parent_run_id}",
            "authoritative_status": "completed" if complete or rejected else "partial",
            "status": "succeeded" if complete else "partial" if succeeded_count else "failed",
            "full_success": complete,
            "counts": {"requested": len(dispositions), **counts_by_disposition,
                       "valid_snapshots": succeeded_count,
                       "failed_evidence": counts_by_disposition["blocked"] + counts_by_disposition["failed"],
                       "succeeded": succeeded_count},
            "dispositions": dispositions,
            "summary": {"checked": len(dispositions), "succeeded": succeeded_count,
                        "failed": len(dispositions) - succeeded_count},
        }
        candidates = [
            {"url": item.url, "title": item.title, "summary": item.summary,
             "source": source.key, "discovery_ref": item.source_item_id or item.url,
             "observed_at": item.detected_at or observed_at}
            for item in items
        ]
        source_results.append({
            "source": source.key,
            "status": "succeeded" if complete else "partial" if succeeded_count else "failed",
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
    systemic = runtime[2].systemic_read_failure()
    evidence = {"status": "completed", "full_success": bool(source_results) and all(
        row["status"] == "succeeded" for row in source_results
    ), "source_results": source_results, "systemic_error": (
        systemic["root_error"] if systemic["stopped"] else None
    )}
    return all_items, all_warnings, evidence


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
            or not _valid_refresh_checkpoint_mapping(payload["checkpoint"])
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


def _valid_refresh_checkpoint_mapping(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "upstream_revision", "source_key", "seed_url",
        "site_skill", "site_state",
    }:
        return False
    if (
        value["schema_version"] != "climate-web-listening-refresh-context.v1"
        or value["upstream_revision"] != _UPSTREAM_REVISION
        or not isinstance(value["source_key"], str)
        or not isinstance(value["seed_url"], str)
    ):
        return False
    try:
        from web_listening.artifact.site_state import site_state_from_mapping
        from web_listening.site_skill.validate import site_skill_from_mapping

        skill = site_skill_from_mapping(value["site_skill"])
        state = site_state_from_mapping(value["site_state"])
    except (ImportError, KeyError, TypeError, ValueError):
        return False
    return (
        state.site_key == skill.site_key
        and urlparse(value["seed_url"]).hostname == skill.site_key
        and state.site_skill_digest == skill.digest
    )


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
        # A host-only HTTP URL requests "/". The governed gateway requires
        # that path explicitly; normalize before binding profiles and receipts.
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.path:
            url = parsed._replace(path="/").geturl()
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def _scope_fetch_mode(default_fetch_mode: str, scope: SiteScope | None) -> str:
    if scope and scope.fetch_mode:
        return scope.fetch_mode
    return default_fetch_mode


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

