from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from scripts.sync_source_wiki import sync_source_wiki

from .ai_filter import classify_candidate
from .article_candidate_contract import validate_candidate
from .candidate_aggregation import (
    combine_current_artifacts,
    combine_runtime_items,
    commit_combined_candidates,
    combined_candidates_path,
    items_from_current_candidates,
    items_from_merged_candidates_with_carry,
    serialize_combined_candidates,
    validate_combined_candidates,
)
from .candidate_snapshot import (
    build_candidate_item_snapshot,
    candidate_item_snapshot_path,
    verify_candidate_item_snapshot,
)
from .config import load_run_config, load_site_scopes, load_sources
from .dedupe import canonical_url
from .models import CandidateItem, MonitorRunResult, RunConfig
from .report_writer import render_report
from .research_search import search_recent_research
from .seen_state import (
    SeenStateError,
    commit_seen_url_delta,
    discard_pending_seen_url_delta_if_base_matches,
    load_pending_seen_url_additions,
    load_pending_seen_url_snapshot_sha256,
    load_pending_seen_url_transaction,
    load_seen_urls,
    pending_seen_url_delta_path,
    prepare_seen_url_delta,
)
from .semantic_bundle import (
    commit_report_with_semantics,
    recover_pending_commit,
    select_semantic_articles,
    semantic_sidecar_path,
    verify_semantic_sidecar,
)
from .web_listening_adapter import (
    collect_website_items,
    commit_staged_source_checkpoints,
)
from .weekly_monitor.authoring_contract import validate_authoring_response
from .weekly_monitor.provenance import build_run_provenance, require_complete_provenance_inputs


DEFAULT_STATE_DIR = Path("monitoring/state")


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


def _runtime_artifact_identity(
    items: list[CandidateItem], *, pillar: str, report_date: date
) -> tuple[str, str]:
    payload = json.dumps(
        [asdict(item) for item in items],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return (
        f"runtime-pillar-{pillar.lower()}_{report_date.isoformat()}.json",
        hashlib.sha256(payload).hexdigest(),
    )


def _history_notes(candidates: tuple[Any, ...]) -> list[str]:
    notes: list[str] = []
    for candidate in candidates:
        origin = next(
            origin for origin in candidate.origins if origin.pillar == candidate.display_pillar
        )
        title = candidate.title or origin.original_title or "(untitled)"
        notes.append(
            f"{title} ({candidate.canonical_url}) already in URL history - skipped"
        )
    return notes


def _verified_pending_seen_bundle(
    *,
    report_path: Path,
    candidate_path: Path,
    snapshot_path: Path,
    seen_urls_path: Path,
    report_date: date,
    update_seen_state: bool,
    recovery_result: str,
) -> dict[str, Any] | None:
    """Validate a committed bundle before recovering its pending URL delta."""

    pending = pending_seen_url_delta_path(seen_urls_path)
    if not pending.exists():
        return None
    try:
        additions = load_pending_seen_url_additions(seen_urls_path)
        (
            pending_date,
            pending_combined_sha256,
            pending_report_sha256,
        ) = load_pending_seen_url_transaction(seen_urls_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SeenStateError(
            "pending seen-URL delta is invalid"
        ) from exc

    report_exists = report_path.exists()
    sidecar_exists = semantic_sidecar_path(report_path).exists()
    try:
        pending_snapshot_sha256 = load_pending_seen_url_snapshot_sha256(
            seen_urls_path
        )
        sidecar, candidates, snapshot_items = _validated_committed_report_bundle(
            report_path=report_path,
            candidate_path=candidate_path,
            snapshot_path=snapshot_path,
            report_date=report_date,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        if (
            update_seen_state
            and recovery_result in {"clean", "discarded"}
            and not report_exists
            and not sidecar_exists
        ):
            discard_pending_seen_url_delta_if_base_matches(seen_urls_path)
            return None
        raise SeenStateError(
            "pending seen-URL delta has no complete matching committed report bundle"
        ) from exc

    rendered_urls = {article["canonical_url"] for article in sidecar["articles"]}
    candidate_sha256 = hashlib.sha256(serialize_combined_candidates(candidates)).hexdigest()
    if pending_date != report_date.isoformat():
        raise SeenStateError(
            "pending seen-URL delta does not match the committed report bundle"
        )
    if pending_combined_sha256 != candidate_sha256:
        if update_seen_state and recovery_result in {"clean", "discarded"}:
            discard_pending_seen_url_delta_if_base_matches(seen_urls_path)
            return None
        raise SeenStateError(
            "pending seen-URL delta does not match the committed report bundle"
        )
    if pending_report_sha256 != sidecar["report"]["sha256"]:
        if update_seen_state and recovery_result in {"clean", "discarded"}:
            discard_pending_seen_url_delta_if_base_matches(seen_urls_path)
            return None
        raise SeenStateError(
            "pending seen-URL delta does not match the committed report bundle"
        )
    if additions != rendered_urls:
        raise SeenStateError(
            "pending seen-URL delta does not match the committed report bundle"
        )
    if pending_snapshot_sha256 is not None:
        if snapshot_items is None or not snapshot_path.exists():
            raise SeenStateError(
                "pending seen-URL delta has no complete matching committed report bundle"
            )
        if hashlib.sha256(snapshot_path.read_bytes()).hexdigest() != pending_snapshot_sha256:
            raise SeenStateError(
                "pending seen-URL delta does not match the committed report bundle"
            )
    return sidecar


def _validated_committed_report_bundle(
    *,
    report_path: Path,
    candidate_path: Path,
    snapshot_path: Path,
    report_date: date,
) -> tuple[dict[str, Any], dict[str, Any], tuple[CandidateItem, ...] | None]:
    sidecar = verify_semantic_sidecar(report_path)
    candidate_bytes = candidate_path.read_bytes()
    candidates = validate_combined_candidates(json.loads(candidate_bytes.decode("utf-8")))
    if serialize_combined_candidates(candidates) != candidate_bytes:
        raise ValueError("combined candidate evidence is not canonical")
    rendered_urls = {article["canonical_url"] for article in sidecar["articles"]}
    retained_urls = {item["canonical_url"] for item in candidates["items"]}
    if (
        sidecar["report"]["date"] != report_date.isoformat()
        or candidates["report_date"] != report_date.isoformat()
        or not rendered_urls.issubset(retained_urls)
    ):
        raise ValueError("committed report bundle identities do not match")
    snapshot_items = None
    if snapshot_path.exists():
        snapshot_items = verify_candidate_item_snapshot(
            snapshot_path,
            combined_path=candidate_path,
            report_path=report_path,
            report_date=report_date,
        )
    return sidecar, candidates, snapshot_items


def _same_date_report_context(
    *,
    report_path: Path,
    candidate_path: Path,
    snapshot_path: Path,
    report_date: date,
) -> tuple[
    dict[str, Any], dict[str, Any], tuple[CandidateItem, ...] | None
] | None:
    """Return one complete existing same-date report bundle, if present."""

    sidecar_path = semantic_sidecar_path(report_path)
    if not report_path.exists() and not sidecar_path.exists():
        return None
    if not report_path.exists() or not sidecar_path.exists() or not candidate_path.exists():
        raise SeenStateError("same-date report bundle is incomplete or inconsistent")
    try:
        return _validated_committed_report_bundle(
            report_path=report_path,
            candidate_path=candidate_path,
            snapshot_path=snapshot_path,
            report_date=report_date,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise SeenStateError(
            "same-date report bundle is incomplete or inconsistent"
        ) from exc


def _items_from_recovered_sidecar(payload: Mapping[str, Any]) -> tuple[CandidateItem, ...]:
    items: list[CandidateItem] = []
    for article in payload["articles"]:
        semantics = dict(article["semantics"])
        keywords = tuple(semantics["keywords"])
        items.append(
            CandidateItem(
                title=article["title"],
                url=article["url"],
                summary=semantics["summary"],
                source_name=article["source"],
                lane=article["lane"],
                content_hash=article["content_hash"],
                climate_related=True,
                actuarial_related=True,
                topics=keywords,
                categories=tuple(semantics["categories"]),
                keywords=keywords,
                semantics=semantics,
            )
        )
    return tuple(items)


def _carry_forward_runtime_items(
    sidecar: Mapping[str, Any],
    candidates: Mapping[str, Any],
    snapshot_items: tuple[CandidateItem, ...] | None,
) -> tuple[CandidateItem, ...]:
    """Preserve rendered order/semantics and append retained non-rendered items."""

    if snapshot_items is not None:
        return snapshot_items
    rendered = _items_from_recovered_sidecar(sidecar)
    rendered_urls = {article["canonical_url"] for article in sidecar["articles"]}
    retained = tuple(validate_candidate(item) for item in candidates["items"])
    remaining = tuple(
        candidate
        for candidate in retained
        if candidate.canonical_url not in rendered_urls
    )
    return (*rendered, *items_from_current_candidates(remaining))


def _rendered_items_from_snapshot(
    sidecar: Mapping[str, Any],
    snapshot_items: tuple[CandidateItem, ...] | None,
) -> tuple[CandidateItem, ...]:
    if snapshot_items is None:
        return _items_from_recovered_sidecar(sidecar)
    by_canonical = {canonical_url(item.url): item for item in snapshot_items}
    return tuple(by_canonical[article["canonical_url"]] for article in sidecar["articles"])


def _finish_source_checkpoints(
    *,
    checkpoint_dir: Path,
    seen_urls_path: Path,
    update_seen_state: bool,
) -> None:
    if not update_seen_state:
        return
    commit_staged_source_checkpoints(
        checkpoint_dir,
        committed_urls=load_seen_urls(seen_urls_path),
    )


def run_monitor(
    *,
    source_config_path: str | Path = "monitoring/supranational_sources.yaml",
    run_config_path: str | Path = "monitoring/run_config.yaml",
    report_date: date | None = None,
    manifest_fixture_path: str | Path | None = None,
    research_fixture_path: str | Path | None = None,
    article_changes_artifact_path: str | Path | None = None,
    pillar_b_artifact_path: str | Path | None = None,
    site_scopes_path: str | Path | None = "monitoring/site_scopes.yaml",
    state_dir: str | Path = "monitoring/state",
    source_dir: str | Path | None = None,
    wiki_dir: str | Path | None = None,
    sync: bool = True,
    update_seen_state: bool = True,
    authoring_response: Mapping[str, Any] | None = None,
    prompt_provenance: Mapping[str, str] | None = None,
    driver_version: str = "",
    contract_version: str = "",
    repository_commit_sha: str = "",
    model_metadata: Mapping[str, Any] | None = None,
) -> MonitorRunResult:
    day = report_date or date.today()
    repo_root = Path.cwd()
    emit_provenance = require_complete_provenance_inputs(
        prompt_provenance=prompt_provenance,
        driver_version=driver_version,
        contract_version=contract_version,
        repository_commit_sha=repository_commit_sha,
        model_metadata=model_metadata,
    )
    sources = load_sources(source_config_path)
    config = _with_output_overrides(load_run_config(run_config_path), source_dir=source_dir, wiki_dir=wiki_dir)
    state_root = _resolve_path(state_dir, root=repo_root)
    source_checkpoint_dir = state_root / "websites"
    output_source_dir = _resolve_path(config.source_dir, root=repo_root)
    output_wiki_dir = _resolve_path(config.wiki_dir, root=repo_root)
    output_path = _source_file_path(output_source_dir, day)
    candidate_path = combined_candidates_path(output_source_dir, day)
    snapshot_path = candidate_item_snapshot_path(output_source_dir, day)
    seen_urls_path = _resolve_path(config.seen_urls_path, root=repo_root)
    if state_root != _resolve_path(DEFAULT_STATE_DIR, root=repo_root):
        seen_urls_path = state_root / "seen_urls.json"
    if (article_changes_artifact_path is None) != (pillar_b_artifact_path is None):
        raise ValueError(
            "article_changes_artifact_path and pillar_b_artifact_path must be supplied together"
        )
    if article_changes_artifact_path is not None and (
        manifest_fixture_path is not None or research_fixture_path is not None
    ):
        raise ValueError("current Pillar artifacts cannot be combined with manifest/research fixtures")

    pending_path = pending_seen_url_delta_path(seen_urls_path)
    pending_day: date | None = None
    pending_report_path: Path | None = None
    recovered_sidecar = None
    if pending_path.exists():
        pending_date, _, _ = load_pending_seen_url_transaction(seen_urls_path)
        pending_day = date.fromisoformat(pending_date)
        pending_report_path = _source_file_path(output_source_dir, pending_day)
        pending_candidate_path = combined_candidates_path(output_source_dir, pending_day)
        pending_snapshot_path = candidate_item_snapshot_path(output_source_dir, pending_day)
        if update_seen_state:
            recovery_result = recover_pending_commit(pending_report_path)
            recovered_sidecar = _verified_pending_seen_bundle(
                report_path=pending_report_path,
                candidate_path=pending_candidate_path,
                snapshot_path=pending_snapshot_path,
                seen_urls_path=seen_urls_path,
                report_date=pending_day,
                update_seen_state=True,
                recovery_result=recovery_result,
            )
            if recovered_sidecar is not None:
                commit_seen_url_delta(seen_urls_path)
                _finish_source_checkpoints(
                    checkpoint_dir=source_checkpoint_dir,
                    seen_urls_path=seen_urls_path,
                    update_seen_state=True,
                )
        elif pending_day == day:
            recovered_sidecar = _verified_pending_seen_bundle(
                report_path=pending_report_path,
                candidate_path=pending_candidate_path,
                snapshot_path=pending_snapshot_path,
                seen_urls_path=seen_urls_path,
                report_date=pending_day,
                update_seen_state=False,
                recovery_result="clean",
            )

    if recovered_sidecar is not None:
        if pending_day != day or pending_report_path is None:
            recovered_sidecar = None
        else:
            sidecar_path = semantic_sidecar_path(pending_report_path)
            recovered_commit = {
                "report_path": pending_report_path,
                "report_sha256": recovered_sidecar["report"]["sha256"],
                "sidecar_path": sidecar_path,
                "payload": recovered_sidecar,
            }
            provenance = None
            if emit_provenance:
                provenance = build_run_provenance(
                    commit=recovered_commit,
                    prompt_provenance=prompt_provenance,
                    driver_version=driver_version,
                    contract_version=contract_version,
                    repository_commit_sha=repository_commit_sha,
                    model_metadata=model_metadata,
                )
            _, _, recovered_snapshot_items = _validated_committed_report_bundle(
                report_path=pending_report_path,
                candidate_path=pending_candidate_path,
                snapshot_path=pending_snapshot_path,
                report_date=pending_day,
            )
            recovered_items = _rendered_items_from_snapshot(
                recovered_sidecar,
                recovered_snapshot_items,
            )
            synced = False
            if sync:
                sync_source_wiki(source_dir=output_source_dir, wiki_dir=output_wiki_dir)
                synced = True
            return MonitorRunResult(
                report_date=day,
                report_path=str(pending_report_path),
                items=recovered_items,
                synced=synced,
                report_sha256=recovered_commit["report_sha256"],
                semantics_path=str(sidecar_path),
                provenance=provenance,
            )

    if pending_day != day or not update_seen_state:
        recover_pending_commit(output_path)

    same_date_context = _same_date_report_context(
        report_path=output_path,
        candidate_path=candidate_path,
        snapshot_path=snapshot_path,
        report_date=day,
    )
    has_same_date_report = same_date_context is not None
    carry_forward_candidates = ()
    carry_forward_items = ()
    same_date_snapshot_items = None
    same_date_urls: set[str] = set()
    if same_date_context is not None:
        (
            same_date_sidecar,
            same_date_candidates,
            same_date_snapshot_items,
        ) = same_date_context
        carry_forward_candidates = tuple(
            validate_candidate(item) for item in same_date_candidates["items"]
        )
        carry_forward_items = _carry_forward_runtime_items(
            same_date_sidecar,
            same_date_candidates,
            same_date_snapshot_items,
        )
        same_date_urls = {
            article["canonical_url"] for article in same_date_sidecar["articles"]
        }
    if not has_same_date_report:
        candidate_path.unlink(missing_ok=True)
        snapshot_path.unlink(missing_ok=True)
    site_scopes = None
    if manifest_fixture_path is None and site_scopes_path is not None:
        resolved_site_scopes_path = _resolve_path(site_scopes_path, root=repo_root)
        if resolved_site_scopes_path.exists():
            site_scopes = {scope.source_key: scope for scope in load_site_scopes(resolved_site_scopes_path)}

    canonical_seen_urls = load_seen_urls(seen_urls_path)
    seen_urls = canonical_seen_urls - same_date_urls

    source_checkpoints_active = False
    current_input_count: int | None = None
    if article_changes_artifact_path is not None and pillar_b_artifact_path is not None:
        article_changes_path = Path(article_changes_artifact_path)
        pillar_b_path = Path(pillar_b_artifact_path)
        article_changes_bytes = article_changes_path.read_bytes()
        pillar_b_bytes = pillar_b_path.read_bytes()
        article_changes_payload = json.loads(article_changes_bytes.decode("utf-8"))
        pillar_b_payload = json.loads(pillar_b_bytes.decode("utf-8"))
        combined = combine_current_artifacts(
            article_changes_payload,
            pillar_b_payload,
            report_date=day.isoformat(),
            pillar_a_artifact_id=article_changes_path.name,
            pillar_a_artifact_sha256=hashlib.sha256(article_changes_bytes).hexdigest(),
            pillar_b_artifact_id=pillar_b_path.name,
            pillar_b_artifact_sha256=hashlib.sha256(pillar_b_bytes).hexdigest(),
            pillar_b_discovered_at=f"{day.isoformat()}T00:00:00Z",
            seen_urls=seen_urls,
            carry_forward_candidates=carry_forward_candidates,
        )
        current_input_count = sum(
            len(group["items"])
            for group in article_changes_payload["articles"]
        ) + len(pillar_b_payload)
        merged_items = list(
            items_from_merged_candidates_with_carry(
                combined.candidates,
                carry_forward_candidates=carry_forward_candidates,
                carry_forward_items=carry_forward_items,
            )
        )
        website_warnings: list[str] = []
        invalid_notes: list[str] = []
    else:
        source_checkpoints_active = manifest_fixture_path is None
        website_items, website_warnings = collect_website_items(
            sources,
            state_dir=source_checkpoint_dir,
            manifest_fixture_path=manifest_fixture_path,
            site_scopes=site_scopes,
            stage_checkpoints=update_seen_state,
            update_checkpoints=update_seen_state,
        )
        if (
            manifest_fixture_path is None
            and os.getenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING") == "1"
            and sources
            and _failed_source_count(website_warnings, {source.key for source in sources})
            >= len(sources)
        ):
            raise RuntimeError("Live website monitoring failed for every configured source.")
        research_items = search_recent_research(
            config, fixture_path=research_fixture_path, today=day
        )
        current_input_count = len(website_items) + len(research_items)
        # These are identities for the exact CandidateItem rows supplied to
        # the shared runtime adapter. Fixture files can contain a wider source
        # shape, so hashing the adapted rows keeps each JSON pointer truthful.
        pillar_a_artifact_id, pillar_a_sha256 = _runtime_artifact_identity(
            website_items, pillar="A", report_date=day
        )
        pillar_b_artifact_id, pillar_b_sha256 = _runtime_artifact_identity(
            research_items, pillar="B", report_date=day
        )
        runtime = combine_runtime_items(
            website_items,
            research_items,
            report_date=day.isoformat(),
            pillar_a_artifact_id=pillar_a_artifact_id,
            pillar_a_artifact_sha256=pillar_a_sha256,
            pillar_b_artifact_id=pillar_b_artifact_id,
            pillar_b_artifact_sha256=pillar_b_sha256,
            discovered_at=f"{day.isoformat()}T00:00:00Z",
            seen_urls=seen_urls,
            carry_forward_candidates=carry_forward_candidates,
            carry_forward_items=carry_forward_items,
        )
        combined = runtime.combined
        merged_items = list(runtime.items)
        invalid_notes = list(runtime.invalid_notes)

    if (
        same_date_context is not None
        and same_date_snapshot_items is None
        and current_input_count
    ):
        raise SeenStateError(
            "same-date candidate item snapshot is missing; incremental replacement "
            "would lose retained item state"
        )

    if (
        same_date_context is not None
        and current_input_count == 0
        and same_date_urls.issubset(canonical_seen_urls)
    ):
        if source_checkpoints_active:
            _finish_source_checkpoints(
                checkpoint_dir=source_checkpoint_dir,
                seen_urls_path=seen_urls_path,
                update_seen_state=update_seen_state,
            )
        existing_commit = {
            "report_path": output_path,
            "report_sha256": same_date_sidecar["report"]["sha256"],
            "sidecar_path": semantic_sidecar_path(output_path),
            "payload": same_date_sidecar,
        }
        provenance = None
        if emit_provenance:
            provenance = build_run_provenance(
                commit=existing_commit,
                prompt_provenance=prompt_provenance,
                driver_version=driver_version,
                contract_version=contract_version,
                repository_commit_sha=repository_commit_sha,
                model_metadata=model_metadata,
            )
        synced = False
        if sync:
            sync_source_wiki(source_dir=output_source_dir, wiki_dir=output_wiki_dir)
            synced = True
        return MonitorRunResult(
            report_date=day,
            report_path=str(output_path),
            items=_rendered_items_from_snapshot(
                same_date_sidecar,
                same_date_snapshot_items,
            ),
            warnings=tuple(website_warnings),
            synced=synced,
            report_sha256=existing_commit["report_sha256"],
            semantics_path=str(existing_commit["sidecar_path"]),
            provenance=provenance,
        )

    dedup_notes = [*_history_notes(combined.history_skips), *invalid_notes]
    classified = [classify_candidate(item, config) for item in merged_items]
    relevant = [item for item in classified if item.climate_related and item.actuarial_related]
    kept = relevant[: config.max_items_per_report]

    if authoring_response is not None:
        authored = validate_authoring_response(kept, authoring_response)
        kept_semantic = list(authored.items)
    else:
        # Drop benign per-item oddities (blank URL, sparse/unvalidatable bundle)
        # before the Markdown and sidecar are built over the *same* item set.
        # Strict production authoring bypasses this fallback and fails closed
        # above when the supplied response is incomplete or invalid.
        kept_semantic, drop_notes = select_semantic_articles(kept)
        dedup_notes.extend(drop_notes)

    if not kept_semantic and not config.write_empty_report:
        if has_same_date_report:
            raise SeenStateError(
                "same-date report bundle cannot be replaced by a no-report outcome"
            )
        commit_combined_candidates(candidate_path, combined.artifact_bytes)
        if source_checkpoints_active:
            _finish_source_checkpoints(
                checkpoint_dir=source_checkpoint_dir,
                seen_urls_path=seen_urls_path,
                update_seen_state=update_seen_state,
            )
        return MonitorRunResult(
            report_date=day,
            report_path=None,
            items=(),
            dedup_notes=tuple(dedup_notes),
            warnings=tuple(website_warnings),
            synced=False,
        )

    output_source_dir.mkdir(parents=True, exist_ok=True)
    output_wiki_dir.mkdir(parents=True, exist_ok=True)
    report_text = render_report(
        report_date=day,
        title=config.report_title,
        items=kept_semantic,
        dedup_notes=dedup_notes,
        sites_monitored=len(sources),
        warnings=website_warnings,
    )
    snapshot_by_canonical = {
        canonical_url(item.url): item for item in classified
    }
    snapshot_by_canonical.update(
        {canonical_url(item.url): item for item in kept_semantic}
    )
    snapshot_items = tuple(
        snapshot_by_canonical[candidate.canonical_url]
        for candidate in combined.candidates
    )
    _, snapshot_bytes = build_candidate_item_snapshot(
        report_date=day,
        combined_bytes=combined.artifact_bytes,
        items=snapshot_items,
        report_sha256=hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
    )
    # One atomic commit: the canonical Markdown and its semantic sidecar are
    # validated first and then published together, so neither can be updated
    # without the other. Semantics come from the single existing authoring
    # pass; this step never calls a model and never touches the network.
    if update_seen_state:
        prepare_seen_url_delta(
            seen_urls_path,
            [item.url for item in kept_semantic],
            report_date=day.isoformat(),
            combined_sha256=hashlib.sha256(combined.artifact_bytes).hexdigest(),
            report_sha256=hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
            snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
        )
    commit = commit_report_with_semantics(
        report_path=output_path,
        report_date=day,
        report_text=report_text,
        items=kept_semantic,
        evidence_artifacts={
            candidate_path: combined.artifact_bytes,
            snapshot_path: snapshot_bytes,
        },
    )
    sidecar_path = Path(commit["sidecar_path"])
    provenance = None
    if emit_provenance:
        provenance = build_run_provenance(
            commit=commit,
            prompt_provenance=prompt_provenance,
            driver_version=driver_version,
            contract_version=contract_version,
            repository_commit_sha=repository_commit_sha,
            model_metadata=model_metadata,
        )

    if update_seen_state:
        commit_seen_url_delta(seen_urls_path)
    if source_checkpoints_active:
        _finish_source_checkpoints(
            checkpoint_dir=source_checkpoint_dir,
            seen_urls_path=seen_urls_path,
            update_seen_state=update_seen_state,
        )

    synced = False
    if sync:
        sync_source_wiki(source_dir=output_source_dir, wiki_dir=output_wiki_dir)
        synced = True

    return MonitorRunResult(
        report_date=day,
        report_path=str(output_path),
        items=tuple(kept_semantic),
        dedup_notes=tuple(dedup_notes),
        warnings=tuple(website_warnings),
        synced=synced,
        report_sha256=commit["report_sha256"],
        semantics_path=str(sidecar_path),
        provenance=provenance,
    )
