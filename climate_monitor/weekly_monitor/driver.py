from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..dedupe import canonical_url
from ..models import CandidateItem
from ..taxonomy import load_article_taxonomy
from ..orchestrator import run_monitor
from .authoring_contract import (
    AUTHORING_CONTRACT_VERSION,
    AUTHORING_CONTRACT_VERSION_V2,
    AUTHORING_REQUEST_SCHEMA_VERSION_V2,
    AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
    AuthoringContractError,
    _validate_v2_stats_shape,
    build_authoring_request,
    load_authoring_response,
    validate_authoring_response,
)
from .prompt_loader import load_weekly_monitor_prompt


DRIVER_VERSION = "weekly-monitor-driver.v1"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SECRET_WORDS = ("api_key", "credential", "password", "secret", "token")


def run_weekly_monitor(
    *,
    source_config_path: str | Path = "monitoring/supranational_sources.yaml",
    run_config_path: str | Path = "monitoring/run_config.yaml",
    report_date: date | None = None,
    manifest_fixture_path: str | Path | None = None,
    research_fixture_path: str | Path | None = None,
    article_changes_artifact_path: str | Path | None = None,
    pillar_b_artifact_path: str | Path | None = None,
    site_scopes_path: str | Path = "monitoring/site_scopes.yaml",
    state_dir: str | Path = "monitoring/state",
    source_dir: str | Path | None = None,
    wiki_dir: str | Path | None = None,
    sync: bool = True,
    update_seen_state: bool = True,
    authoring_response_path: str | Path | None = None,
    prompt_path: str | Path | None = None,
    repository_commit_sha: str | None = None,
    model_provider: str = "",
    model: str = "",
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    # Issue #93 evidence-based authoring: optional ``article_evidence`` and
    # ``stats`` make the driver the single caller of the v2 request emitter.
    # ``article_evidence`` may be a mapping with a ``records`` key (the #92
    # ``article-evidence.v1`` envelope) or a raw sequence of records.
    article_evidence: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    stats: Mapping[str, Any] | None = None,
):
    if authoring_response_path is None:
        raise ValueError("production weekly driver requires an authoring response file")
    prompt = load_weekly_monitor_prompt(prompt_path) if prompt_path else load_weekly_monitor_prompt()
    commit_sha = repository_commit_sha or _repository_commit_sha(Path.cwd())
    if not _GIT_SHA.fullmatch(commit_sha):
        raise ValueError("repository commit SHA must be a 40-character lowercase hex digest")
    response = load_authoring_response(authoring_response_path)

    # v2 evidence path: when the caller supplies the article-evidence and
    # stats that the orchestrator just staged, the driver emits the v2
    # request and validates the response before delegating to the
    # orchestrator. If neither is supplied, the driver falls back to v1
    # validation through the orchestrator (unchanged for legacy callers).
    pre_request = _emit_authoring_request(
        article_evidence=article_evidence,
        stats=stats,
        report_date=report_date,
        prompt=prompt,
    )
    validated_v2_stats: Mapping[str, int] | None = None
    if pre_request is not None:
        response_schema = response.get("schema_version")
        if response_schema != AUTHORING_RESPONSE_SCHEMA_VERSION_V2:
            raise AuthoringContractError(
                "v2 evidence path requires a v2 authoring response, "
                f"got {response_schema!r}"
            )
        # Issue #87 AC-2: validate the canonical 6-key stats shape and the
        # deterministic mapping ``total == updated + unchanged + blocked +
        # failed + unresolved`` on the v2 response before the orchestrator
        # writes any artifact. The driver pins the validated mapping on
        # ``MonitorRunResult.stats`` so downstream consumers (Hermes
        # wrappers, 09:00 climate_delivery, AC-5 dry-run) can report the
        # 57/42/15 split without re-validating.
        validated_v2_stats = _validate_v2_stats_shape(response.get("stats"))
        # The strict v2 contract binds the response to the emitted request
        # identity, deterministic stats, and summary_basis rules. The driver
        # performs the validation here so AC-6 (production driver actually
        # calls the request emitter and the response validator/apply) holds
        # even when the orchestrator later re-validates against the same
        # articles for the final render.
        validate_authoring_response(
            _candidate_items_from_evidence(pre_request, article_evidence),
            response,
            taxonomy=load_article_taxonomy(),
            request=pre_request,
        )
    return run_monitor(
        source_config_path=source_config_path,
        run_config_path=run_config_path,
        report_date=report_date,
        manifest_fixture_path=manifest_fixture_path,
        research_fixture_path=research_fixture_path,
        article_changes_artifact_path=article_changes_artifact_path,
        pillar_b_artifact_path=pillar_b_artifact_path,
        site_scopes_path=site_scopes_path,
        state_dir=state_dir,
        source_dir=source_dir,
        wiki_dir=wiki_dir,
        sync=sync,
        update_seen_state=update_seen_state,
        authoring_response=response,
        authoring_request=pre_request,
        prompt_provenance={
            "id": prompt.prompt_id,
            "version": prompt.version,
            "sha256": prompt.sha256,
        },
        driver_version=DRIVER_VERSION,
        contract_version=(
            AUTHORING_CONTRACT_VERSION_V2 if pre_request is not None
            else AUTHORING_CONTRACT_VERSION
        ),
        repository_commit_sha=commit_sha,
        model_metadata=_model_metadata(
            provider=model_provider,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        ),
        stats=validated_v2_stats,
    )


def _emit_authoring_request(
    *,
    article_evidence: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    stats: Mapping[str, Any] | None,
    report_date: date | None,
    prompt: Any,
) -> Mapping[str, Any] | None:
    if article_evidence is None:
        return None
    if not article_evidence:
        # An empty (but not None) evidence payload is a misuse: the caller
        # asked for v2 but provided no records. Fail closed rather than
        # silently fall back to v1.
        raise ValueError(
            "v2 authoring path received empty article_evidence; "
            "supply records or omit the article_evidence argument to "
            "use the v1 path"
        )
    if stats is None:
        raise ValueError(
            "v2 authoring path requires the canonical 6-key stats shape "
            "(total/updated/unchanged/blocked/failed/unresolved)"
        )
    if report_date is None:
        raise ValueError("v2 authoring request requires an explicit report_date")
    # Issue #87 AC-2: the v2 authoring path requires the canonical 6-key
    # stats dict (total/updated/unchanged/blocked/failed/unresolved). The
    # driver validates it once here so the emitted request carries the same
    # mapping the response must match.
    _validate_v2_stats_shape(stats)
    request = build_authoring_request(
        report_date=report_date,
        items=_candidate_items_from_evidence(None, article_evidence),
        prompt=prompt,
        article_evidence=article_evidence,
        stats=stats,
    )
    if request["schema_version"] != AUTHORING_REQUEST_SCHEMA_VERSION_V2:
        raise ValueError("v2 driver path requires v2 request schema")
    return request


def _candidate_items_from_evidence(
    request: Mapping[str, Any] | None,
    article_evidence: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> tuple[CandidateItem, ...]:
    """Construct lightweight CandidateItem shells aligned with the evidence.

    The orchestrator performs full classification later; the driver only needs
    one shell per evidence URL so ``build_authoring_request`` /
    ``validate_authoring_response`` can attach the article identity. The
    shells carry the canonical URL and an explicit ``display_pillar`` when
    the evidence record provides one.
    """
    if article_evidence is None:
        return ()
    if isinstance(article_evidence, Mapping):
        records = article_evidence.get("records")
    else:
        records = article_evidence
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return ()
    shells: list[CandidateItem] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        url = str(record.get("final_url") or record.get("requested_url") or "").strip()
        canonical = canonical_url(url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        origins_raw = record.get("origins")
        has_b_origin = isinstance(origins_raw, list) and any(
            isinstance(origin, Mapping) and origin.get("pillar") == "B"
            for origin in origins_raw
        )
        lane = "research" if has_b_origin else "website"
        record_title = record.get("title")
        shell_title = str(record_title) if record_title else ""
        shell = CandidateItem(
            title=shell_title,
            url=url,
            summary="",
            source_name="",
            lane=lane,
            content_hash=str(record.get("content_hash") or ""),
        )
        shells.append(shell)
    return tuple(shells)


def _repository_commit_sha(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _model_metadata(
    *,
    provider: str,
    model: str,
    temperature: float | None,
    max_output_tokens: int | None,
) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    if temperature is not None:
        if not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        settings["temperature"] = float(temperature)
    if max_output_tokens is not None:
        if isinstance(max_output_tokens, bool) or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        settings["max_output_tokens"] = int(max_output_tokens)
    return {
        "provider": _safe_public_label(provider, field="model provider"),
        "model": _safe_public_label(model, field="model"),
        "settings": {name: settings[name] for name in sorted(settings)},
    }


def _safe_public_label(value: str, *, field: str) -> str:
    cleaned = " ".join(str(value or "").split())
    lowered = cleaned.casefold()
    if len(cleaned) > 120 or any(word in lowered for word in _SECRET_WORDS) or "sk-" in lowered:
        raise ValueError(f"{field} is not safe public metadata")
    if any(marker in cleaned for marker in ("\\", "/", ":", "\n", "\r", "\t")):
        raise ValueError(f"{field} must not contain paths or control characters")
    return cleaned