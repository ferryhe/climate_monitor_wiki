from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.orchestrator import run_monitor, read_candidate_history, resolve_seen_urls_path
from climate_monitor.config import load_run_config
from climate_monitor.weekly_monitor.driver import run_weekly_monitor
from climate_monitor.weekly_monitor.authoring_contract import (
    AUTHORING_REQUEST_SCHEMA_VERSION_V2,
    AUTHORING_CONTRACT_VERSION_V2,
    AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
    AuthoringContractError,
    build_authoring_request,
    load_authoring_response,
    validate_authoring_response,
    _validate_v2_stats_shape,
)
from climate_monitor.weekly_monitor.prompt_loader import (
    LoadedPrompt, load_weekly_monitor_prompt, load_article_relevance_rules, load_pillar_b_search_prompt,
)
from climate_monitor.article_title import extract_page_title
from climate_monitor.weekly_monitor.driver import _candidate_items_from_evidence
from climate_monitor.taxonomy import ArticleTaxonomy, load_article_taxonomy
from climate_monitor.candidate_aggregation import (
    combine_current_artifacts,
    serialize_combined_candidates,
    items_from_merged_candidates_with_carry,
)
from climate_monitor.article_content_adapter import (
    build_article_evidence_artifact,
    validate_retained_article_evidence,
    write_article_evidence_artifact,
)
from climate_registry.acquisition import (
    freeze_acquisition_for_report,
    load_acquisition_batch,
    store_acquisition_batch,
)
from climate_monitor.dedupe import canonical_url
from climate_monitor.candidate_snapshot import (
    candidate_item_snapshot_path,
    build_candidate_item_snapshot,
    serialize_candidate_item_snapshot as _serialize_candidate_snapshot,
    validate_candidate_item_snapshot,
)
from climate_monitor.models import CandidateItem, MonitorRunResult
from climate_monitor.seen_state import _write_atomic, pending_seen_url_delta_path


def _bound_taxonomy(binding: dict) -> ArticleTaxonomy:
    """Resolve and validate the taxonomy reference frozen into a binding."""
    reference = (binding.get("definition") or {}).get("taxonomy") or {}
    if (reference.get("schema_version") != "article-taxonomy-ref.v1"
            or reference.get("version") != "v1"):
        raise SystemExit("bound taxonomy version/contract mismatch")
    configured = Path(str(reference.get("path", "")))
    taxonomy_root = (ROOT / "monitoring" / "taxonomies").resolve()
    try:
        path = (configured if configured.is_absolute() else ROOT / configured).resolve(strict=True)
    except OSError as exc:
        raise SystemExit("bound taxonomy reference cannot be resolved") from exc
    if path.parent != taxonomy_root or not path.is_file():
        raise SystemExit("bound taxonomy reference is outside monitoring/taxonomies")
    actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_sha256 != binding.get("taxonomy_sha256"):
        raise SystemExit("bound taxonomy sha256 drifted from the immutable task binding")
    try:
        taxonomy = load_article_taxonomy(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"bound taxonomy contract validation failed: {exc}") from exc
    return taxonomy


def _load_task_binding_with_taxonomy(
    path_value: str,
) -> tuple[dict, Path, ArticleTaxonomy]:
    """Load and cryptographically revalidate one immutable management binding."""
    from climate_monitor.management import (
        BINDING_SCHEMA, PROMPT_NAMES, _sha, _text_sha, managed_report_inputs,
    )

    path = Path(path_value)
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_file():
        raise SystemExit("--task-binding must be an existing canonical absolute regular file")
    binding = json.loads(path.read_text(encoding="utf-8"))
    if binding.get("schema_version") != BINDING_SCHEMA:
        raise SystemExit("task binding schema is unsupported")
    definition = binding.get("definition") or {}
    parameters = definition.get("parameters") or {}
    prompts = definition.get("prompts") or {}
    expected_prompt_hashes = {name: _text_sha(prompts[name]["text"]) for name in PROMPT_NAMES}
    expected_prompt_versions = {name: prompts[name]["version"] for name in PROMPT_NAMES}
    taxonomy = _bound_taxonomy(binding)
    inventory = binding.get("source_inventory") or {}
    if inventory.get("sha256") != hashlib.sha256(_canonical_bytes(inventory.get("records"))).hexdigest():
        raise SystemExit("task binding source inventory hash mismatch")
    effective = {
        "task_id": definition.get("task_id"), "parameters": parameters,
        "runtime": definition.get("runtime"), "taxonomy": definition.get("taxonomy"),
        "taxonomy_sha256": taxonomy.sha256,
        "prompt_versions": expected_prompt_versions, "prompt_hashes": expected_prompt_hashes,
    }
    expected = {
        "prompt_hashes": expected_prompt_hashes,
        "prompt_versions": expected_prompt_versions,
        "effective_sha256": _sha(effective),
        "provider": parameters.get("provider"), "model": parameters.get("model"),
        "budgets": parameters.get("budgets"), "source_keys": parameters.get("source_keys"),
        "registry_database": (definition.get("runtime") or {}).get("registry_database"),
    }
    expected_checkpoint = Path(definition["runtime"]["run_root"]) / binding["run_id"] / "checkpoint"
    expected_lineage_id = f"acq-{binding['run_id']}"
    expected.update({
        "acquisition_lineage_id": expected_lineage_id,
        "acquisition_batch_id": f"{expected_lineage_id}-attempt-1",
        "checkpoint_dir": str(expected_checkpoint),
        "frozen_report_input": str(expected_checkpoint.parent / "frozen-report-input.json"),
        "report_inputs": managed_report_inputs(definition, binding["run_id"]),
    })
    for key, value in expected.items():
        if _canonical_bytes(binding.get(key)) != _canonical_bytes(value):
            raise SystemExit(f"task binding field failed immutable validation: {key}")
    expected_dir = Path(binding["checkpoint_dir"]).parent
    if path.parent != expected_dir:
        raise SystemExit("task binding is outside its frozen checkpoint directory")
    return binding, path, taxonomy


def _load_task_binding(path_value: str) -> tuple[dict, Path]:
    binding, path, _ = _load_task_binding_with_taxonomy(path_value)
    return binding, path


def _bound_prompt(binding: dict, name: str, binding_path: Path) -> LoadedPrompt:
    component = binding["definition"]["prompts"][name]
    raw = component["text"].replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != binding["prompt_hashes"][name]:
        raise SystemExit(f"bound prompt hash mismatch: {name}")
    return LoadedPrompt(prompt_id=name, version=component["version"], path=binding_path,
                        raw_bytes=raw, sha256=digest)


_PRODUCTION_FIXTURE_FORBIDDEN_REASON = (
    "production monitor refuses fixture paths under tests/fixtures; "
    "supply the real #67 outcome + manifest and the Pillar B artifact instead"
)


def _parse_loopback_provider(spec: str) -> tuple:
    """Parse a ``module:callable`` loopback spec into a tuple of providers.

    Test/CI seam only — used by ``--article-evidence-loopback``. Empty spec
    yields an empty tuple so the orchestrator falls back to its default
    providers (or the honest ``unavailable`` records when none are wired).
    """

    spec = (spec or "").strip()
    if not spec:
        return ()
    if ":" not in spec:
        raise SystemExit(
            "--article-evidence-loopback requires 'module:callable' form"
        )
    module_name, _, attr = spec.partition(":")
    module_name = module_name.strip()
    attr = attr.strip()
    if not module_name or not attr:
        raise SystemExit(
            "--article-evidence-loopback requires non-empty module and callable"
        )
    module = importlib.import_module(module_name)
    provider = getattr(module, attr, None)
    if not callable(provider):
        raise SystemExit(
            f"--article-evidence-loopback target {spec!r} is not callable"
        )
    return (provider,)


def _resolve_required_path(env_name: str, *, must_be_file: bool = True,
                           must_be_absolute: bool = True) -> Path:
    value = os.environ.get(env_name, "")
    if not value:
        raise SystemExit(f"missing required env var: {env_name}")
    path = Path(value)
    if must_be_absolute and (not path.is_absolute() or path.resolve() != path):
        raise SystemExit(f"env var {env_name} must be an absolute canonical path: {value!r}")
    if must_be_file and not path.is_file():
        raise SystemExit(f"env var {env_name} points to a missing or non-file path: {value!r}")
    return path


def _production_path_is_fixture(path: Path) -> bool:
    """Return True when *path* lives under the test fixtures tree.

    The production monitor refuses any path that lives under
    ``tests/fixtures``; the dry-run path is the only context in which the
    approved fixture set is allowed to substitute for the real public
    outcome + manifest + pillar B artifacts.
    """
    try:
        path = path.resolve()
    except OSError:
        return False
    fixtures_root = (ROOT / "tests" / "fixtures").resolve()
    try:
        path.relative_to(fixtures_root)
    except ValueError:
        return False
    return True


def _enforce_production_paths(paths: list[Path], *, dry_run: bool) -> None:
    if dry_run:
        return
    for path in paths:
        if _production_path_is_fixture(path):
            raise SystemExit(_PRODUCTION_FIXTURE_FORBIDDEN_REASON)


def _store_restart_and_freeze_registry_acquisition(
    *, database: str, input_path: str, batch_id: str, report_date: str,
    manifest: dict | list[dict] | None = None, pillar_b: dict | None = None,
) -> tuple[dict, dict]:
    """Durably ingest, reopen, and freeze one exact pre-report batch."""
    try:
        path = Path(input_path).resolve(strict=True)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Registry acquisition input must be a JSON object")
        if payload.get("batch_id") != batch_id:
            raise ValueError("Registry acquisition input batch_id does not match the requested batch")
        if payload.get("report_date") != report_date:
            raise ValueError("Registry acquisition input report_date does not match the report")
        completeness = None
        if manifest is not None or pillar_b is not None:
            if manifest is None or pillar_b is None:
                raise ValueError("complete upstream acquisition manifest inputs must be paired")
            completeness = _validate_registry_acquisition_completeness(
                payload, manifest, pillar_b
            )
        stored = store_acquisition_batch(database, payload)
        # These reads reopen SQLite and prove authoring uses durable state.
        load_acquisition_batch(database, batch_id)
        frozen = freeze_acquisition_for_report(database, batch_id, report_date=report_date)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Registry store-before-freeze failed: {exc}") from exc
    identity = {
        "input_path": str(path),
        "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "store_summary": stored,
    }
    if completeness is not None:
        identity["completeness"] = completeness
    return frozen, identity


def _enforce_production_env_fixture(env: os._Environ | dict) -> None:
    if os.environ.get("CLIMATE_DRY_RUN") == "1":
        return
    forbidden = (
        "AUTHORING_RESPONSE", "ARTICLE_EVIDENCE", "CLIMATE_STATS_PATH",
        "CLIMATE_DRY_RUN_FIXTURE_DIR",
    )
    for name in forbidden:
        if os.environ.get(name):
            raise SystemExit(
                f"production monitor refuses preset {name!r}; the same-run "
                "chain must build its own bundle from #67 outcome + manifest"
            )


def _outcome_fixture_root(path: Path | None = None) -> Path | None:
    """An explicit dependency-free fixture seam, confined to a temporary dry run."""
    if not os.environ.get("CLIMATE_DRY_RUN_OUTCOME_FIXTURE"):
        return None
    import tempfile
    raw_root = os.environ.get("CLIMATE_DRY_RUN_ROOT", "")
    root = Path(raw_root).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if (os.environ.get("CLIMATE_DRY_RUN_OUTCOME_FIXTURE") != "1"
            or os.environ.get("CLIMATE_DRY_RUN") != "1"
            or not raw_root or not Path(raw_root).is_absolute()
            or root == temp or not root.is_relative_to(temp)
            or (path is not None and not path.resolve().is_relative_to(root))):
        raise SystemExit("outcome fixture requires an isolated temporary dry run")
    return root


def _read_outcomes(path: Path) -> list[dict]:
    fixture_root = _outcome_fixture_root(path)
    try:
        try:
            from web_listening.contracts.acquisition_batch import AcquisitionBatchResultV2
        except ModuleNotFoundError as exc:
            if exc.name != "web_listening" or fixture_root is None:
                raise
            # No schema substitute: the test-only helper accepts two exact saved
            # public payloads. Installed upstream always owns validation above.
            import runpy
            validate = runpy.run_path(str(ROOT / "tests/issue87_outcome_fixture.py"))["validate_fixture"]
            return [validate(path.read_text(encoding="utf-8"))]
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload if isinstance(payload, list) else [payload]
        if not entries:
            raise ValueError("acquisition batch must contain at least one outcome")
        return [AcquisitionBatchResultV2.model_validate_json(
            json.dumps(entry)).model_dump(mode="json") for entry in entries]
    except ValueError as exc:
        raise SystemExit("invalid public acquisition-batch-result.v2") from exc


def _aggregate_outcomes(outcomes: list[dict]) -> dict:
    if len(outcomes) == 1:
        return outcomes[0]
    from web_listening.contracts.acquisition_batch import aggregate_batch_result_v2
    tasks = [item["task_id"] for result in outcomes for item in result["dispositions"]]
    if len(tasks) != len(set(tasks)):
        raise SystemExit("acquisition collection contains duplicate task identities")
    return aggregate_batch_result_v2(outcomes)


def _read_outcome(path: Path) -> dict:
    return _aggregate_outcomes(_read_outcomes(path))


def _read_manifest(path: Path) -> dict | list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload if isinstance(payload, list) else [payload]:
        _validate_manifest(entry)
    return payload


def _validate_manifest(payload) -> None:
    if not isinstance(payload, dict):
        raise SystemExit("web-listening manifest must be an object")
    if payload.get("schema_version") != "web-listening-manifest.v1":
        raise SystemExit(
            f"web-listening manifest schema_version must be web-listening-manifest.v1, "
            f"got {payload.get('schema_version')!r}"
        )
    run = payload.get("run") or {}
    if not isinstance(run, dict) or not run.get("run_id"):
        raise SystemExit("web-listening manifest run must declare run_id")
    discovered = payload.get("discovered_items")
    if not isinstance(discovered, list):
        raise SystemExit("web-listening manifest discovered_items must be a list")


def _read_pillar_b(
    path: Path, *, report_date: str | None = None, allow_incomplete: bool = False
) -> list | dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if report_date is not None or isinstance(payload, dict):
        from climate_monitor.weekly_monitor.pillar_b_discovery import validate_discovery
        try:
            entries = validate_discovery(
                payload,
                report_date=date.fromisoformat(
                    report_date if report_date is not None else payload.get("report_date", "")
                ),
                allow_incomplete=allow_incomplete,
                retain_ineligible=allow_incomplete,
            )
        except (ValueError, TypeError) as exc:
            raise SystemExit(str(exc)) from exc
    elif isinstance(payload, list):
        entries = payload
    else:
        raise SystemExit(f"pillar-b artifact {path} must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("url"):
            raise SystemExit("pillar-b artifact entries must be objects with a url")
        # Preflight enforces the same Pillar B source contract as the real
        # consumer (``adapt_pillar_b``): ``source`` is the producer's
        # institution/website name and must be a non-empty, trimmed string.
        # This closes the mismatch where preflight accepted an institution
        # ``source`` while the consumer rejected it (or vice versa).
        source = entry.get("source")
        if not isinstance(source, str) or not source or source != source.strip():
            raise SystemExit(
                "pillar-b artifact entries must declare a non-empty, trimmed source"
            )
    return payload


def _canonical_digest(payload: dict | list) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_bytes(payload: dict | list) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False,
                       sort_keys=True, separators=(",", ":")).encode("utf-8")


def _registry_occurrence_key(
    *, kind: object, discovery_ref: object, search_ref: object,
    source: object, url: object,
) -> tuple[str, str, str | None, str, str]:
    if kind not in {"site", "search"}:
        raise ValueError("complete upstream acquisition manifest has an invalid discovery kind")
    if (not isinstance(discovery_ref, str) or not discovery_ref.strip()
            or not isinstance(source, str) or not source.strip()
            or not isinstance(url, str) or not url.strip()):
        raise ValueError("complete upstream acquisition manifest has an invalid occurrence identity")
    normalized = canonical_url(url)
    if not normalized:
        raise ValueError("complete upstream acquisition manifest has an invalid occurrence URL")
    if search_ref is not None and (not isinstance(search_ref, str) or not search_ref.strip()):
        raise ValueError("complete upstream acquisition manifest has an invalid search identity")
    return str(kind), discovery_ref, search_ref, source, normalized


def _expected_registry_occurrences(manifest: dict | list[dict], pillar_b: dict) -> Counter:
    manifests = manifest if isinstance(manifest, list) else [manifest]
    expected: Counter = Counter()
    for export in manifests:
        source_id = (export.get("source") or {}).get("source_id")
        for item in export.get("discovered_items") or []:
            if not isinstance(item, dict):
                raise ValueError("complete upstream acquisition manifest contains a non-object site item")
            item_id = item.get("item_id")
            origins = item.get("origins") or [{"source": source_id}]
            if not isinstance(origins, list) or not origins:
                raise ValueError("complete upstream acquisition manifest has no site origin")
            for index, origin in enumerate(origins):
                if not isinstance(origin, dict):
                    raise ValueError("complete upstream acquisition manifest has an invalid site origin")
                ref = item_id if index == 0 else f"{item_id}#origin-{index + 1}"
                expected[_registry_occurrence_key(
                    kind="site", discovery_ref=ref, search_ref=None,
                    source=origin.get("source") or source_id, url=item.get("url"),
                )] += 1
    for article in pillar_b.get("articles") or []:
        expected[_registry_occurrence_key(
            kind="search", discovery_ref=article.get("result_ref"),
            search_ref=article.get("search_ref"), source=article.get("source"),
            url=article.get("url"),
        )] += 1
    return expected


def _validate_registry_acquisition_completeness(
    payload: dict, manifest: dict | list[dict], pillar_b: dict,
) -> dict:
    """Bind every public discovery/search occurrence to one fetch observation."""
    if not isinstance(payload, dict) or not isinstance(pillar_b, dict):
        raise ValueError("complete upstream acquisition manifest requires object payloads")
    if (payload.get("report_date") != pillar_b.get("report_date")
            or payload.get("date_policy") != pillar_b.get("date_policy")
            or payload.get("search_decision") != pillar_b.get("search_decision")
            or payload.get("searches") != pillar_b.get("searches")):
        raise ValueError(
            "Registry input does not match the complete upstream acquisition manifest searches/date policy"
        )
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("complete upstream acquisition manifest requires Registry items")
    actual: Counter = Counter()
    observations: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("complete upstream acquisition manifest contains a non-object Registry item")
        actual[_registry_occurrence_key(
            kind=item.get("discovery_kind"), discovery_ref=item.get("discovery_ref"),
            search_ref=item.get("discovery_search_ref"), source=item.get("source"),
            url=item.get("url"),
        )] += 1
        evidence = item.get("evidence") or {}
        observations.append({
            "occurrence": list(_registry_occurrence_key(
                kind=item.get("discovery_kind"), discovery_ref=item.get("discovery_ref"),
                search_ref=item.get("discovery_search_ref"), source=item.get("source"),
                url=item.get("url"),
            )),
            "selected": item.get("selected"),
            "fetch_status": evidence.get("status"),
            "material_status": evidence.get("classification"),
            "processing_status": item.get("processing_status"),
            "content_hash": evidence.get("content_hash"),
        })
    expected = _expected_registry_occurrences(manifest, pillar_b)
    if actual != expected:
        missing = list((expected - actual).elements())
        extra = list((actual - expected).elements())
        raise ValueError(
            "Registry input does not match the complete upstream acquisition manifest "
            f"(missing={len(missing)}, extra={len(extra)})"
        )
    identity = {
        "schema_version": "registry-acquisition-completeness.v1",
        "site_occurrence_count": sum(count for key, count in expected.items() if key[0] == "site"),
        "search_occurrence_count": sum(count for key, count in expected.items() if key[0] == "search"),
        "search_attempt_count": len(payload.get("searches") or []),
        "fetch_processing_observation_count": len(observations),
        "observations": sorted(observations, key=_canonical_bytes),
    }
    identity["manifest_sha256"] = _canonical_digest(identity)
    return identity


def _verify_same_run_identity(outcome: dict, manifest: dict) -> None:
    """Enforce that the public outcome and manifest come from the same upstream run.

    Cross-run or missing identity binding is the canonical leak path for
    the migration windows the Issue describes (bootstrap-no-miss,
    same-week re-run, duplicate scan). Fail closed before the staging
    bundle is written.
    """
    outcome_run = outcome.get("run_id")
    run = manifest.get("run") or {}
    parent = run.get("parent_run_id")
    source = (manifest.get("source") or {}).get("source_id")
    if (not parent or not source or run.get("run_id") != f"run-{parent}"
            or outcome_run != f"scope-run-{parent}"):
        raise SystemExit("cross-run identity: export parent must identify the outcome scope run")
    matches = [item for item in outcome["dispositions"] if item["site_key"] == source]
    if len(matches) != 1:
        raise SystemExit("cross-run source identity: export source must identify one disposition")
    artifact_id = matches[0].get("artifact_id")
    if not artifact_id or artifact_id != manifest.get("manifest_id"):
        raise SystemExit("cross-run export identity: manifest must match disposition artifact_id")
    seed = (manifest.get("source") or {}).get("tree_seed_url")
    if (not isinstance(seed, str) or not seed.strip()
            or canonical_url(seed) != canonical_url(matches[0]["requested_url"])):
        raise SystemExit("cross-run source identity: scope seed differs")


def _read_prepare_inputs(
    outcome_path, manifest_path, pillar_b_path, *, report_date=None,
    allow_incomplete_pillar_b=False, bound_managed=False,
):
    """Read-only validation shared by prepare and Hermes preflight."""
    outcomes = _read_outcomes(outcome_path)
    outcome = _aggregate_outcomes(outcomes)
    manifest = _read_manifest(manifest_path)
    if isinstance(manifest, list):
        _verify_collection_identity(outcomes, manifest)
    else:
        _verify_same_run_identity(outcome, manifest)
    pillar_b = _read_pillar_b(
        pillar_b_path, report_date=report_date,
        allow_incomplete=allow_incomplete_pillar_b,
    )
    if report_date is not None and not isinstance(pillar_b, dict):
        raise SystemExit("production Pillar B input must use the v2 envelope")
    records = _attach_outcome_disposition(_collect_same_run_records(outcome, manifest), outcome)
    return outcome, manifest, pillar_b, records, _derive_stats(records, outcome)


def _verify_collection_identity(outcomes: list[dict], manifests: list[dict]) -> None:
    """Bind every retained scope export before aggregating its discoveries.

    The collection contains original public payloads, never a synthetic export
    with an invented common parent. Terminal failures need no article export.
    """
    successful = [(result, item) for result in outcomes for item in result["dispositions"]
                  if item["disposition"] in {"updated", "unchanged"}]
    expected = [item.get("artifact_id") for _, item in successful]
    actual = [manifest.get("manifest_id") for manifest in manifests]
    if (any(not item for item in expected + actual)
            or len(set(expected)) != len(expected) or len(set(actual)) != len(actual)
            or set(expected) != set(actual)):
        raise SystemExit("cross-run export identity: collection must contain every successful export exactly once")
    by_artifact = {item["artifact_id"]: result for result, item in successful}
    for manifest in manifests:
        _verify_same_run_identity(by_artifact[manifest["manifest_id"]], manifest)


def _manifest_identity(manifest: dict | list[dict]) -> dict:
    if isinstance(manifest, list):
        return {"scope_runs": [_manifest_identity(item) for item in manifest]}
    return {"run_id": manifest["run"]["run_id"],
            "parent_run_id": manifest["run"]["parent_run_id"],
            "source_id": manifest["source"]["source_id"]}


def _collect_same_run_records(outcome: dict, manifest: dict | list[dict]) -> list[dict]:
    """Project every manifest ``discovered_item`` to a same-run evidence record.

    Keep discovery occurrences until adapt_article_changes/merge_candidates
    merges their URL identities, retaining every origin. Stable ordering also
    makes the projected artifact row identities independent of export order.
    """
    if isinstance(manifest, list):
        return sorted([record for item in manifest
                       for record in _collect_same_run_records(outcome, item)],
                      key=lambda r: (canonical_url(r["final_url"]), _canonical_bytes(r)))
    records: list[dict] = []
    for raw in manifest.get("discovered_items") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        if not url:
            continue
        canonical = canonical_url(url)
        if not canonical:
            raise SystemExit(f"manifest item url has no canonical form: {url!r}")
        final_url = str(raw.get("final_url") or url)
        display_pillar = raw.get("display_pillar") or "A"
        origins = raw.get("origins") or [{"pillar": display_pillar, "source": manifest["source"]["source_id"], "url": url}]
        origins = [dict(origin) for origin in origins]
        for origin in origins:
            for field, value in (
                ("original_title", raw.get("title")),
                ("title_basis", (raw.get("title_basis") or "upstream_artifact") if raw.get("title") else None),
                ("original_summary", raw.get("summary")),
                ("summary_basis", (raw.get("summary_basis") or "page") if raw.get("summary") else None),
                ("source_item_id", raw.get("item_id")), ("discovered_at", raw.get("observed_at")),
                ("provenance", raw.get("provenance")), ("metadata", raw.get("metadata")),
                ("content_hash", raw.get("content_hash")),
            ):
                if value:
                    origin.setdefault(field, value)
        records.append({
            "source_id": manifest["source"]["source_id"],
            "manifest_id": manifest.get("manifest_id"),
            "final_url": final_url,
            "requested_url": url,
            "title": raw.get("title") or "",
            "summary": raw.get("summary") or "",
            "summary_basis": raw.get("summary_basis") or "page",
            "title_basis": raw.get("title_basis") or "upstream_artifact",
            "display_pillar": display_pillar,
            "origins": sorted(origins, key=_canonical_bytes),
            "content_hash": raw.get("content_hash") or "",
        })
    return sorted(records, key=lambda r: (canonical_url(r["final_url"]), _canonical_bytes(r)))


def _attach_outcome_disposition(records: list[dict], outcome: dict) -> list[dict]:
    """Attach the monitored source outcome to its discovered articles.

    One source may discover many articles; discovered URLs are not fetch counts.
    """
    by_source = {item["site_key"]: item for item in outcome["dispositions"]}
    by_artifact = {item["artifact_id"]: item for item in outcome["dispositions"]
                   if item.get("artifact_id")}
    enriched = []
    for record in records:
        item = by_artifact.get(record.get("manifest_id")) or by_source[record["source_id"]]
        unresolved = item["disposition"] == "unresolved"
        enriched.append({**record,
            "disposition": "failed" if unresolved else item["disposition"],
            "acquisition_unresolved": unresolved,
            "error_code": "acquisition_unresolved" if unresolved else item["reason"],
        })
    return enriched


def _derive_stats(records: list[dict], outcome: dict) -> dict:
    """Map validated source dispositions, never discovered article counts."""
    counts = outcome.get("counts") or {}
    names = ("updated", "unchanged", "blocked", "failed", "unresolved")
    for key in ("requested", *names):
        if type(counts.get(key)) is not int or counts[key] < 0:
            raise SystemExit(f"acquisition outcome counts.{key} must be a non-negative integer")
    observed = {key: 0 for key in names}
    for item in outcome.get("dispositions", []):
        observed[item["disposition"]] += 1
    if any(observed[key] != counts[key] for key in names) or sum(observed.values()) != counts["requested"]:
        raise SystemExit("outcome counts inconsistent with source dispositions")
    return {
        "total": counts["requested"], "updated": counts["updated"],
        "unchanged": counts["unchanged"], "blocked": counts["blocked"],
        "failed": counts["failed"], "unresolved": counts["unresolved"],
    }


def _outcome_to_article_changes(outcome: dict, manifest: dict,
                                records: list[dict], report_date: str) -> dict:
    """Project the public ``acquisition-batch-result.v2`` outcome into the
    Pillar-A ``article_changes_DATE.json`` shape the existing #91 transaction
    consumes.

    The Pillar A adapter does not yet know about ``acquisition-batch-result.v2``;
    rather than widen that adapter, this projection keeps the upstream contract
    (``schema_version: acquisition-batch-result.v2``) stable while emitting the
    legacy shape the transaction's invariants expect. The single source of
    truth remains the public outcome; the projection is computed in-process
    before any staging file is written.
    """

    by_disposition: dict[str, list[dict]] = {"updated": [], "unchanged": [],
                                              "blocked": [], "failed": []}
    for record in records:
        by_disposition.setdefault(record["disposition"], []).append(record)
    kept = by_disposition["updated"] + by_disposition["unchanged"]
    groups: dict[str, list[dict]] = {}
    for record in kept:
        url = record.get("final_url") or record.get("requested_url")
        parsed = urlparse(url)
        path = urlparse(canonical_url(url)).path
        if parsed.path.endswith("/") and not path.endswith("/"):
            path += "/"
        # Use the shared canonical path, retaining transport query/fragment bytes
        # and the trailing separator. Raw discovery URLs remain in origins.
        url = parsed._replace(path=path).geturl()
        for origin in record.get("origins") or [{}]:
            source = origin.get("source") or "unknown"
            groups.setdefault(source, []).append({
                "title": origin.get("original_title") or record.get("title") or "",
                "url": url,
                "categories": ["climate_supervision"],
            })
    articles: list[dict] = []
    for org, items in sorted(groups.items()):
        articles.append({"org": org, "items": items})
    return {
        "date": report_date,
        "pillar": "A",
        "sites_with_changes": len(groups),
        "orgs_with_articles": len(groups),
        "baseline_urls": 0,
        "new_articles": sum(len(items) for items in groups.values()),
        "seen_before": 0,
        "generated_at": f"{report_date}T08:05:00Z",
        "articles": articles,
    }


def _build_evidence_payload(records: list[dict], candidates, *, data_root=None, providers=(), report_date="", page_titles=True) -> dict:
    """Build the ``article-evidence.v1`` envelope the driver expects.

    Only successful / retained records are surfaced; failed+blocked entries
    remain in the staging bundle for audit but never carry article evidence.
    The envelope is built by calling the same public builder the existing
    orchestrator uses, so AC-2 (the existing public adapter) is honoured
    without a parallel implementation.
    """
    by_url: dict[str, list[dict]] = {}
    for record in sorted(records, key=_canonical_bytes):
        by_url.setdefault(canonical_url(record["final_url"]), []).append(record)
    inputs: list[dict] = []
    for candidate in candidates:
        sources = by_url.get(candidate.canonical_url, [])
        source = next(iter(sources), {})
        title_source = next((item for item in sources if item.get("title")), {})
        snippet = next((item["summary"] for item in sources if item.get("summary")), "") or next(
            (origin.original_snippet or origin.original_summary for origin in candidate.origins
             if origin.original_snippet or (origin.summary_basis == "search_result" and origin.original_summary)), "")
        inputs.append({
            "article_id": candidate.canonical_url,
            "source_id": source.get("source_id"),
            "url": candidate.url,
            "title": candidate.title or title_source.get("title", ""),
            "title_basis": candidate.title_basis if candidate.title else title_source.get("title_basis"),
            # Canonical candidate lineage plus the original discovery metadata:
            # no last-record-wins loss of alternate titles, summaries or sources.
            "origins": [origin.model_dump(mode="json", exclude_none=True) for origin in candidate.origins]
                       + [origin for item in sources for origin in item["origins"]],
            "display_pillar": candidate.display_pillar,
            "search_snippet": snippet,
        })
    return build_article_evidence_artifact(
        inputs, providers=providers, report_date=report_date, data_root=data_root,
        include_verified_content=True,
        title_extractor=extract_page_title if page_titles else None)


def _select_authoring_candidates(args, report_date, outcome_path, manifest_path,
                                 pillar_b_path, outcome, manifest, pillar_b, records):
    seen_path = resolve_seen_urls_path(load_run_config(args.run_config), args.state_dir)
    _, carried, carried_items, same_date_urls, seen = read_candidate_history(
        Path(args.source_dir).resolve(), report_date, seen_path, retain_all_same_date=True)
    combined = combine_current_artifacts(
        _outcome_to_article_changes(outcome, manifest, records, report_date.isoformat()),
        pillar_b, report_date=report_date.isoformat(),
        pillar_a_artifact_id=outcome_path.name,
        pillar_a_artifact_sha256=hashlib.sha256(outcome_path.read_bytes()).hexdigest(),
        pillar_b_artifact_id=pillar_b_path.name,
        pillar_b_artifact_sha256=hashlib.sha256(pillar_b_path.read_bytes()).hexdigest(),
        pillar_b_discovered_at=f"{report_date.isoformat()}T00:00:00Z",
        seen_urls=seen - same_date_urls, carry_forward_candidates=carried,
        pillar_b_allow_incomplete=bool(args.registry_database),
    )
    items = items_from_merged_candidates_with_carry(
        combined.candidates, carry_forward_candidates=carried,
        carry_forward_items=carried_items)
    selection = {"seen_urls_path": str(seen_path.resolve()),
                 "candidate_urls": sorted(c.canonical_url for c in combined.candidates)}
    return combined, items, selection


def _verify_candidate_selection(args, bundle):
    """Reject history changes that alter the frozen selection, before inference."""
    expected = bundle.get("history_selection")
    if not isinstance(expected, dict):
        raise SystemExit("prepared history selection missing; use fresh staging")
    seen_path = resolve_seen_urls_path(load_run_config(args.run_config), args.state_dir)
    if str(seen_path.resolve()) != expected.get("seen_urls_path"):
        raise SystemExit("prepared history path changed; use fresh staging")
    if pending_seen_url_delta_path(seen_path).exists():
        # The existing finalizer owns interrupted report/state transactions.
        # Do not reject its intermediate history before that recovery runs.
        return
    paths = [Path(bundle["public_artifacts"][key]["path"]) for key in
             ("acquisition_batch", "web_listening_manifest", "pillar_b_artifact")]
    outcome, manifest, pillar_b, records, _ = _read_prepare_inputs(
        *paths, report_date=bundle["report_date"],
        allow_incomplete_pillar_b=bool(args.registry_database),
    )
    _, _, current = _select_authoring_candidates(
        args, date.fromisoformat(bundle["report_date"]), *paths,
        outcome, manifest, pillar_b, records)
    if current != expected:
        raise SystemExit("prepared history selection changed; use fresh staging")


def _run_prepare(args, parser) -> int:
    task_binding = None
    task_binding_path = None
    task_taxonomy = None
    if getattr(args, "task_binding", ""):
        task_binding, task_binding_path, task_taxonomy = _load_task_binding_with_taxonomy(
            args.task_binding
        )
        if args.report_date and args.report_date != task_binding["report_date"]:
            raise SystemExit("--report-date differs from the immutable task binding")
        args.report_date = task_binding["report_date"]
        args.registry_database = task_binding["registry_database"]
        args.registry_acquisition_batch_id = task_binding["acquisition_batch_id"]
    outcome_path = Path(args.acquisition_batch).resolve()
    manifest_path = Path(args.web_listening_manifest).resolve()
    pillar_b_path = Path(args.pillar_b_artifact).resolve()
    staging_dir = Path(args.staging_dir).resolve()
    if not staging_dir.is_absolute() or staging_dir.resolve() != staging_dir:
        parser.error("--staging-dir must be an absolute canonical path")
    if task_binding is not None:
        supplied = {
            "acquisition_batch": str(outcome_path), "web_listening_manifest": str(manifest_path),
            "pillar_b_artifact": str(pillar_b_path), "staging_dir": str(staging_dir),
            "state_dir": str(Path(args.state_dir).resolve()),
            "source_dir": str(Path(args.source_dir).resolve()),
            "wiki_dir": str(Path(args.wiki_dir).resolve()),
        }
        if supplied != task_binding["report_inputs"]:
            raise SystemExit("report inputs differ from the immutable task binding")
    dry_run = os.environ.get("CLIMATE_DRY_RUN") == "1"
    _enforce_production_paths(
        [outcome_path, manifest_path, pillar_b_path, staging_dir],
        dry_run=dry_run,
    )
    outcome, manifest, pillar_b, records, stats = _read_prepare_inputs(
        outcome_path, manifest_path, pillar_b_path, report_date=args.report_date,
        allow_incomplete_pillar_b=bool(args.registry_database or task_binding),
        bound_managed=task_binding is not None,
    )
    if not isinstance(pillar_b, dict):
        raise SystemExit("production Pillar B input must use the v2 envelope")
    staging_dir.mkdir(parents=True, exist_ok=True)
    import yaml
    config = yaml.safe_load(Path(args.run_config).read_text(encoding="utf-8")) or {}
    configured_root = os.environ.get("WL_DATA_DIR")
    if not configured_root:
        configured = (config.get("web_listening") or {}).get("data_dir")
        # A relative climate scratch path does not identify the upstream data
        # root. In that case let the public reader use its runtime configuration.
        if configured and Path(configured).is_absolute():
            configured_root = configured
    data_root = Path(configured_root).resolve() if configured_root else None

    report_date = date.fromisoformat(args.report_date) if args.report_date else date.today()
    source_dir = Path(args.source_dir).resolve()
    if not source_dir.is_absolute():
        parser.error("--source-dir must be an absolute path")
    state_dir = Path(args.state_dir).resolve()
    if not state_dir.is_absolute():
        parser.error("--state-dir must be an absolute path")
    output_source_dir = source_dir
    source_dir.mkdir(parents=True, exist_ok=True)

    combined, snapshot_items, history_selection = _select_authoring_candidates(
        args, report_date, outcome_path, manifest_path, pillar_b_path,
        outcome, manifest, pillar_b, records)
    registry_identity = None
    if task_binding is not None:
        try:
            frozen_path = Path(task_binding["frozen_report_input"])
            if not frozen_path.is_file():
                raise ValueError("bound frozen report input is missing")
            evidence_payload = json.loads(frozen_path.read_text(encoding="utf-8"))
            durable = load_acquisition_batch(task_binding["registry_database"], task_binding["acquisition_batch_id"])
            regenerated = freeze_acquisition_for_report(
                task_binding["registry_database"], task_binding["acquisition_batch_id"],
                report_date=task_binding["report_date"],
            )
            if _canonical_bytes(regenerated) != _canonical_bytes(evidence_payload):
                raise ValueError("bound frozen report input differs from durable Registry evidence")
            expected_urls = {canonical_url(candidate.canonical_url) for candidate in combined.candidates}
            actual_urls = {canonical_url(record["requested_url"]) for record in evidence_payload["records"]}
            if expected_urls != actual_urls:
                raise ValueError("bound acquisition selection does not match the frozen candidate set")
            validate_retained_article_evidence(evidence_payload, report_date=report_date.isoformat(), urls=expected_urls)
            registry_identity = {
                "database": task_binding["registry_database"], "batch_id": task_binding["acquisition_batch_id"],
                "content_version_ids": sorted(record["content_version_id"] for record in evidence_payload["records"]),
                "ingestion": {"status": "frozen-binding", "payload_sha256": durable["payload_sha256"]},
                "task_binding": {
                    "path": str(task_binding_path), "sha256": hashlib.sha256(task_binding_path.read_bytes()).hexdigest(),
                    "task_version": task_binding["task_version"], "effective_sha256": task_binding["effective_sha256"],
                    "provider": task_binding["provider"], "model": task_binding["model"],
                    "checkpoint_dir": task_binding["checkpoint_dir"],
                },
            }
        except (OSError, ValueError) as exc:
            raise SystemExit(f"immutable Registry acquisition handoff failed: {exc}") from exc
    elif args.registry_database:
        try:
            evidence_payload, ingestion_identity = _store_restart_and_freeze_registry_acquisition(
                database=args.registry_database,
                input_path=args.registry_acquisition_input,
                batch_id=args.registry_acquisition_batch_id,
                report_date=report_date.isoformat(),
                manifest=manifest,
                pillar_b=pillar_b,
            )
            expected_urls = {canonical_url(candidate.canonical_url) for candidate in combined.candidates}
            actual_urls = {canonical_url(record["requested_url"])
                           for record in evidence_payload["records"]}
            if expected_urls != actual_urls:
                raise ValueError(
                    "Registry acquisition selection does not match the frozen candidate set"
                )
            validate_retained_article_evidence(
                evidence_payload,
                report_date=report_date.isoformat(),
                urls=expected_urls,
            )
            registry_identity = {
                "database": str(Path(args.registry_database).resolve()),
                "batch_id": args.registry_acquisition_batch_id,
                "content_version_ids": sorted(
                    record["content_version_id"] for record in evidence_payload["records"]
                ),
                "ingestion": ingestion_identity,
            }
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Registry acquisition handoff failed: {exc}") from exc
    else:
        evidence_payload = _build_evidence_payload(
            records, combined.candidates, data_root=data_root,
            providers=_parse_loopback_provider(args.article_evidence_loopback),
            report_date=args.report_date, page_titles=not args.no_page_titles)
    combined_bytes = serialize_combined_candidates(combined.artifact)

    # Per Issue #87 spec: prepare writes ONLY to the staging dir; the
    # orchestrator's #91 transaction materialises source artifacts during
    # finalize, not prepare. The snapshot is copied into
    # ``staging_dir/candidate_item_snapshot.json`` below for the
    # finalize-side digest verification.
    snapshot_path = candidate_item_snapshot_path(staging_dir, report_date)
    combined_sha = hashlib.sha256(combined_bytes).hexdigest()
    snapshot_payload, snapshot_bytes = build_candidate_item_snapshot(
        report_date=report_date,
        combined_bytes=combined_bytes,
        items=snapshot_items,
        report_sha256=combined_sha,
    )
    validate_candidate_item_snapshot(
        snapshot_payload,
        combined_bytes=combined_bytes,
        report_date=report_date.isoformat(),
        report_sha256=combined_sha,
    )
    snapshot_path.write_bytes(snapshot_bytes)

    candidate_items = _candidate_items_from_evidence(None, evidence_payload)
    authoring_prompt = (_bound_prompt(task_binding, "article_summary", Path(str(task_binding_path)))
                        if task_binding is not None else load_weekly_monitor_prompt())
    request = build_authoring_request(
        report_date=report_date,
        items=candidate_items,
        prompt=authoring_prompt,
        taxonomy=task_taxonomy,
        article_evidence=evidence_payload,
        stats=stats,
    )
    if request.get("schema_version") != AUTHORING_REQUEST_SCHEMA_VERSION_V2:
        raise SystemExit("prepare failed to emit a v2 authoring request")

    taxonomy = task_taxonomy or load_article_taxonomy()
    bundle_payload = {
        "schema_version": "climate-monitor-prepare-bundle.v1",
        "report_date": report_date.isoformat(),
        "staging_digest_inputs": [
            "combined", "snapshot", "evidence", "stats", "request", "identity", "history",
        ],
        "public_artifacts": {
            "acquisition_batch": {
                "path": str(outcome_path),
                "sha256": hashlib.sha256(outcome_path.read_bytes()).hexdigest(),
                "run_id": outcome["run_id"],
            },
            "web_listening_manifest": {
                "path": str(manifest_path),
                "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                **_manifest_identity(manifest),
            },
            "pillar_b_artifact": {
                "path": str(pillar_b_path),
                "sha256": hashlib.sha256(pillar_b_path.read_bytes()).hexdigest(),
                "count": len(pillar_b["articles"]),
            },
        },
        "prompt": {
            "id": request["prompt"]["id"],
            "version": request["prompt"]["version"],
            "sha256": request["prompt"]["sha256"],
        },
        "taxonomy": {
            "schema_version": taxonomy.schema_version,
            "taxonomy_id": taxonomy.taxonomy_id,
            "sha256": taxonomy.sha256,
        },
        "stats": stats,
        "stats_sha256": _canonical_digest(stats),
        "page_titles": not args.no_page_titles,
        "history_selection": history_selection,
        "request_sha256": request["request_sha256"],
    }
    if task_binding is not None:
        bundle_payload["execution_binding"] = {
            "provider": task_binding["provider"], "model": task_binding["model"],
            "article_summary_sha256": task_binding["prompt_hashes"]["article_summary"],
            "executive_summary_sha256": task_binding["prompt_hashes"]["executive_summary"],
            "effective_sha256": task_binding["effective_sha256"],
        }
        bundle_payload["staging_digest_inputs"].append("execution_binding")
    if registry_identity is not None:
        bundle_payload["registry_acquisition"] = registry_identity
        bundle_payload["staging_digest_inputs"].append("registry_acquisition")
    digest_inputs: dict[str, bytes] = {
        "combined": combined_bytes,
        "snapshot": snapshot_path.read_bytes(),
        "evidence": _canonical_bytes(evidence_payload),
        "stats": _canonical_bytes(stats),
        "request": _canonical_bytes(request),
        "identity": _canonical_bytes(bundle_payload["public_artifacts"]),
        "history": _canonical_bytes(history_selection),
    }
    if registry_identity is not None:
        digest_inputs["registry_acquisition"] = _canonical_bytes(registry_identity)
    if task_binding is not None:
        digest_inputs["execution_binding"] = _canonical_bytes(bundle_payload["execution_binding"])
    digest = hashlib.sha256(
        b"".join(digest_inputs[key] for key in sorted(digest_inputs))
    ).hexdigest()
    bundle_payload["bundle_digest"] = digest
    bundle_payload["bundle_digest_algorithm"] = "sha256-ordered-keys"

    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "bundle.json").write_text(
        json.dumps(bundle_payload, ensure_ascii=False, allow_nan=False,
                    indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (staging_dir / "combined.json").write_bytes(combined_bytes)
    (staging_dir / "article_evidence.json").write_bytes(_canonical_bytes(evidence_payload))
    (staging_dir / "stats.json").write_bytes(_canonical_bytes(stats))
    (staging_dir / "v2_authoring_request.json").write_bytes(_canonical_bytes(request))
    snapshot_link = staging_dir / "candidate_item_snapshot.json"
    if snapshot_link.exists():
        snapshot_link.unlink()
    snapshot_link.write_bytes(snapshot_path.read_bytes())

    if args.json:
        print(json.dumps({
            "status": "prepare_ok",
            "staging_dir": str(staging_dir),
            "report_date": report_date.isoformat(),
            "stats": stats,
            "bundle_digest": digest,
            "request_sha256": request["request_sha256"],
        }, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Prepare: staging bundle written to {staging_dir}")
        print(f"Prepare: bundle digest {digest}")
        print(f"Prepare: stats {stats}")


def _read_staging_bundle(staging_dir: Path) -> dict:
    bundle_path = staging_dir / "bundle.json"
    if not bundle_path.is_file():
        raise SystemExit(f"staging bundle missing: {bundle_path}")
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"staging bundle is unreadable: {exc}")
    if not isinstance(bundle, dict):
        raise SystemExit("staging bundle must be an object")
    if bundle.get("schema_version") != "climate-monitor-prepare-bundle.v1":
        raise SystemExit(
            f"staging bundle schema_version must be climate-monitor-prepare-bundle.v1, "
            f"got {bundle.get('schema_version')!r}"
        )
    return bundle


def _verify_staging_digest(staging_dir: Path, bundle: dict) -> None:
    digest_keys = bundle.get("staging_digest_inputs") or []
    if not isinstance(digest_keys, list) or not digest_keys:
        raise SystemExit("staging bundle must declare staging_digest_inputs")
    parts: list[bytes] = []
    for key in sorted(digest_keys):
        if key == "combined":
            data = (staging_dir / "combined.json").read_bytes()
        elif key == "snapshot":
            data = (staging_dir / "candidate_item_snapshot.json").read_bytes()
        elif key == "evidence":
            data = (staging_dir / "article_evidence.json").read_bytes()
        elif key == "stats":
            data = (staging_dir / "stats.json").read_bytes()
        elif key == "request":
            data = (staging_dir / "v2_authoring_request.json").read_bytes()
        elif key == "identity":
            data = _canonical_bytes(bundle.get("public_artifacts") or {})
        elif key == "history":
            data = _canonical_bytes(bundle.get("history_selection") or {})
        elif key == "registry_acquisition":
            data = _canonical_bytes(bundle.get("registry_acquisition") or {})
        elif key == "execution_binding":
            data = _canonical_bytes(bundle.get("execution_binding") or {})
        else:
            raise SystemExit(f"unknown staging digest input: {key!r}")
        parts.append(data)
    expected = hashlib.sha256(b"".join(parts)).hexdigest()
    if expected != bundle.get("bundle_digest"):
        details = {k: hashlib.sha256(data).hexdigest() for k, data in zip(sorted(digest_keys), parts)}
        raise SystemExit(
            f"staging bundle digest mismatch: expected {expected}, got {bundle.get('bundle_digest')}; per-key {details}"
        )


def _run_finalize(args, parser) -> MonitorRunResult:
    """Consume the prepared bundle + exactly one authoring response.

    Every identity binding the prepare step established is re-checked here:
    bundle digest, public-artifact digests, prompt + taxonomy identity, the
    v2 request sha256, and the deterministic 6-key stats. A mismatch
    raises before any seen-state or report is written.
    """
    response_path = Path(args.authoring_response).resolve()
    staging_dir = Path(args.staging_dir).resolve()
    if not staging_dir.is_absolute():
        parser.error("--staging-dir must be an absolute path")
    if not response_path.is_file():
        parser.error(f"--authoring-response file is missing: {response_path}")
    dry_run = os.environ.get("CLIMATE_DRY_RUN") == "1"
    _enforce_production_paths([response_path, staging_dir], dry_run=dry_run)
    bundle = _read_staging_bundle(staging_dir)
    _verify_staging_digest(staging_dir, bundle)
    for label, entry in (bundle.get("public_artifacts") or {}).items():
        path = Path(entry["path"])
        if not path.is_file():
            raise SystemExit(f"staging public artifact missing on finalize: {label}={path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != entry.get("sha256"):
            raise SystemExit(
                f"staging public artifact sha256 changed since prepare: {label}"
            )
    request = json.loads((staging_dir / "v2_authoring_request.json").read_text(encoding="utf-8"))
    stats = json.loads((staging_dir / "stats.json").read_text(encoding="utf-8"))
    if stats != bundle.get("stats"):
        raise SystemExit("staging stats diverged from bundle stats")
    validated_stats = _validate_v2_stats_shape(stats)
    response = load_authoring_response(response_path)
    execution_binding = bundle.get("execution_binding")
    if execution_binding:
        reference = (bundle.get("registry_acquisition") or {}).get("task_binding") or {}
        binding_path = Path(str(reference.get("path", "")))
        if (not binding_path.is_file()
                or hashlib.sha256(binding_path.read_bytes()).hexdigest() != reference.get("sha256")):
            raise SystemExit("immutable task binding changed since prepare")
        binding, binding_path, taxonomy = _load_task_binding_with_taxonomy(
            str(binding_path)
        )
        prompt = _bound_prompt(binding, "article_summary", binding_path)
        if execution_binding.get("provider") != binding["provider"] or execution_binding.get("model") != binding["model"]:
            raise SystemExit("bound provider/model changed since prepare")
    else:
        taxonomy = load_article_taxonomy()
        prompt = load_weekly_monitor_prompt()
    if taxonomy.sha256 != (bundle.get("taxonomy") or {}).get("sha256"):
        raise SystemExit(
            f"taxonomy sha256 changed since prepare: "
            f"prepared={bundle['taxonomy']['sha256']} current={taxonomy.sha256}"
        )
    if prompt.sha256 != (bundle.get("prompt") or {}).get("sha256"):
        raise SystemExit(
            f"prompt sha256 changed since prepare: "
            f"prepared={bundle['prompt']['sha256']} current={prompt.sha256}"
        )
    evidence_payload = json.loads(
        (staging_dir / "article_evidence.json").read_text(encoding="utf-8")
    )
    _verify_candidate_selection(args, bundle)
    candidate_items = _candidate_items_from_evidence(None, evidence_payload)
    from climate_monitor.semantic_bundle import render_order
    ordered = render_order(candidate_items)
    try:
        validate_authoring_response(
            candidate_items,
            response,
            taxonomy=taxonomy,
            request=request,
        )
    except AuthoringContractError as exc:
        raise SystemExit(f"authoring response rejected: {exc}")
    except Exception as exc:
        raise SystemExit(f"authoring response rejected: {type(exc).__name__}: {exc}")

    # Materialise the Pillar A and Pillar B artifacts the orchestrator's
    # #91 transaction reads from disk. The same public outcome, manifest,
    # and pillar B inputs that produced the staging bundle re-emit these
    # files on the same run identity; passing them through makes the
    # orchestrator's transaction atomic rather than re-deriving inputs.
    report_date = date.fromisoformat(bundle["report_date"])
    outcome_path = Path(bundle["public_artifacts"]["acquisition_batch"]["path"])
    manifest_path = Path(bundle["public_artifacts"]["web_listening_manifest"]["path"])
    pillar_b_path = Path(bundle["public_artifacts"]["pillar_b_artifact"]["path"])
    outcome, manifest, pillar_b_payload, records, _ = _read_prepare_inputs(
        outcome_path, manifest_path, pillar_b_path, report_date=bundle["report_date"])
    pillar_a_payload = _outcome_to_article_changes(outcome, manifest, records, bundle["report_date"])
    source_dir = Path(args.source_dir).resolve()
    article_changes_artifact = source_dir / f"article_changes_{bundle['report_date']}.json"
    article_changes_artifact.write_bytes(_canonical_bytes(pillar_a_payload) + b"\n")
    article_changes_path = article_changes_artifact
    pillar_b_artifact = source_dir / f"pillar_b_{bundle['report_date']}.json"
    pillar_b_artifact.write_bytes(_canonical_bytes(pillar_b_payload) + b"\n")
    pillar_b_artifact_path = pillar_b_artifact

    # The bundle pins ``report_date`` at prepare time; finalize must use the
    # pinned date regardless of any CLI ``--report-date`` the operator passes
    # so the orchestrator always sees the prepare-blessed date. Stale or
    # missing CLI args must NOT silently swap the date.
    finalized_report_date = date.fromisoformat(bundle["report_date"])
    model, provider = _resolve_authoring_identity(args.model, args.model_provider)
    return run_weekly_monitor(
        model=model, model_provider=provider,
        source_config_path=Path(args.source_config),
        run_config_path=Path(args.run_config),
        report_date=finalized_report_date,
        site_scopes_path=Path(args.site_scopes) if args.site_scopes else "",
        state_dir=Path(args.state_dir),
        source_dir=Path(args.source_dir) if args.source_dir else None,
        wiki_dir=Path(args.wiki_dir) if args.wiki_dir else None,
        sync=not args.no_sync,
        update_seen_state=not args.no_update_seen_state,
        authoring_response_path=response_path,
        prompt_path=None,
        article_evidence=evidence_payload,
        stats=validated_stats,
        article_changes_artifact_path=article_changes_path,
        pillar_b_artifact_path=pillar_b_artifact_path,
        providers=_parse_loopback_provider(args.article_evidence_loopback),
    )


def _resolve_authoring_identity(model="", provider="") -> tuple[str, str]:
    """CLI flags override the existing Hermes inference environment identity."""
    model = (model or os.environ.get("HERMES_INFERENCE_MODEL", "")).strip()
    provider = (provider or os.environ.get("HERMES_INFERENCE_PROVIDER", "")).strip()
    if not model or not provider or provider == "auto":
        raise SystemExit("production authoring requires explicit model and provider")
    return model, provider


def _parse_hermes_quiet_response(stdout: str, stderr: str):
    """Validate quiet-mode diagnostics separately from the complete JSON payload."""
    import re
    if not re.fullmatch(r"session_id: [0-9]{8}_[0-9]{6}_[0-9a-f]{6}", stderr.strip()):
        raise ValueError("missing, malformed, or unexpected Hermes stderr session_id")
    warning = "Warning: Unknown toolsets: none\n"
    response = stdout.removeprefix(warning)
    # v2026.9.7 emits this fixed startup notice even in quiet mode.
    response = response.removeprefix(
        "  ⚠ tirith security scanner enabled but not available — "
        "command scanning will use pattern matching only\n"
    )
    # Some models wrap the entire JSON value in one Markdown code block even
    # when asked for JSON. Remove only that framing; never extract a substring
    # from surrounding prose or repair malformed/incomplete JSON.
    fenced = re.fullmatch(r"\s*```(?:json)?\s*\n([\s\S]*?)\n```\s*", response)
    if fenced:
        response = fenced.group(1)
    return json.loads(response)


def _hermes_authoring_invocation(
    help_stdout: str,
    instruction: str,
    *,
    model: str = "",
    provider: str = "",
) -> tuple[list[str], str]:
    """Return the argv-safe ``hermes chat`` command for one authoring turn.

    ``--query-file -`` explicitly reads stdin. Older Hermes versions interpret
    ``--query -`` as the literal message "-" and leave stdin unread, so they
    must fail before authoring rather than silently discard the evidence.
    """
    import re
    options = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", help_stdout))
    if "--query-file" not in options:
        raise SystemExit(
            "Hermes authoring requires --query-file support; use a verified compatible runtime"
        )
    if not {"--max-turns", "--reasoning", "--ignore-rules"}.issubset(options):
        raise SystemExit("Hermes authoring requires bounded-turn and reasoning controls")
    command = ["hermes", "chat", "--query-file", "-", "--quiet", "--toolsets", "none",
               "--max-turns", "1", "--reasoning", "none", "--ignore-rules"]
    if model:
        command += ["--model", model]
    if provider:
        command += ["--provider", provider]
    return command, instruction


def _authoring_evidence_view(evidence: dict) -> dict:
    """Derive model-readable text without modifying the verified source artifact."""
    records = []
    for record in evidence["records"]:
        body = record.get("content")
        text_view = None
        if isinstance(body, str) and body:
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != record.get("content_hash"):
                raise ValueError("authoring source content hash mismatch")
            media_type = str(record.get("content_type") or "").split(";", 1)[0].strip().lower()
            text, derivation = body, "source_text"
            if media_type in {"text/html", "application/xhtml+xml"}:
                from web_listening.blocks.normalizer import normalize_html
                text = normalize_html(
                    body, record.get("final_url") or record["requested_url"]
                ).markdown
                if not text.strip():
                    raise ValueError("HTML authoring normalization produced no readable text")
                derivation = "web_listening.blocks.normalizer.normalize_html.markdown"
            text_view = {
                "text": text,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "source_content_hash": record["content_hash"],
                "derivation": derivation,
            }
        records.append({
            "source_article_id": record["article_id"],
            "requested_url": record["requested_url"],
            "status": record["status"],
            "source_record_hash": record.get("record_hash"),
            "readable_content": text_view,
            "search_snippet": (record.get("extra") or {}).get("search_snippet"),
        })
    return {"source_artifact_digest": evidence.get("artifact_digest"), "records": records}


def _configured_business_prompt(name: str) -> str:
    """Load the same versioned business component used by acquisition bindings."""
    from climate_monitor.management import load_active_prompt

    return load_active_prompt(name)["text"].strip()


def _response_envelope(request, articles, executive_summary=""):
    return {"schema_version": AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
            "contract_version": AUTHORING_CONTRACT_VERSION_V2,
            "request_sha256": request["request_sha256"], "stats": request["stats"],
            "article_count": len(articles), "articles": articles,
            "executive_summary": executive_summary}


def _validate_url_authoring(raw, article, request, item, taxonomy):
    fields = {"climate_related", "actuarial_related", "summary", "summary_basis",
              "evidence_hash", "categories", "keywords"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValueError("URL authoring has unexpected or missing fields")
    if any(type(raw[key]) is not bool for key in ("climate_related", "actuarial_related")):
        raise ValueError("URL relevance decisions must be booleans")
    if raw["summary_basis"] not in {"article_content", "search_snippet", "none"}:
        raise ValueError("URL authoring has unsupported summary_basis")
    for key in ("categories", "keywords"):
        if not isinstance(raw[key], list) or any(not isinstance(v, str) for v in raw[key]):
            raise ValueError("URL categories and keywords must be string arrays")
    if any(v not in taxonomy.allowed_labels for v in raw["categories"]):
        raise ValueError("URL authoring has unknown category")
    authored = {**article, **{key: raw[key] for key in fields - {"climate_related", "actuarial_related"}},
                "relevant": raw["climate_related"] and raw["actuarial_related"]}
    # Reuse the production evidence/taxonomy validator on this exact article.
    subset = {**request, "articles": [article]}
    validate_authoring_response([item], _response_envelope(subset, [authored]),
                                taxonomy=taxonomy, request=subset)
    return authored


def _validate_executive_authoring(raw):
    if (not isinstance(raw, dict) or set(raw) != {"executive_summary"}
            or not isinstance(raw["executive_summary"], str)
            or not raw["executive_summary"].strip()):
        raise ValueError("executive summary must be a non-empty JSON string field")
    if re.search(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+|>|```|~~~)",
                 raw["executive_summary"], re.MULTILINE):
        raise ValueError("executive summary must use prose paragraphs, not Markdown blocks or lists")
    return raw["executive_summary"]


def _checkpointed_authoring(path, instruction, *, args, help_stdout, validate, retry_guidance=""):
    """One independent invocation; atomically retain validated work for resume."""
    import subprocess
    import time
    identity = _canonical_digest({"instruction": instruction, "model": args.model,
                                  "provider": args.model_provider})
    prior = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if prior and prior.get("input_sha256") != identity:
        raise SystemExit("authoring checkpoint input changed; use fresh staging")
    if prior.get("status") == "completed":
        # A saved success never bypasses the production validator.
        return validate(prior["response"])
    query = instruction
    if (prior.get("error_stage") in {"parse", "validate"}
            or prior.get("error_type") == "AuthoringContractError"):
        # Resume is still one fresh context for this item. Give the model its
        # previous validation error, without adding another article or a chat
        # history. The base task identity remains unchanged; pin the exact
        # attempt request separately below.
        query = ("Prior validation error for this same item (diagnostic data):\n"
                 + json.dumps({"error": prior["error"]}, ensure_ascii=False)
                 + "\nCorrect that output error while following the original task below.\n"
                 + retry_guidance + "\n\n"
                 + instruction)
    command, stdin = _hermes_authoring_invocation(help_stdout, query,
        model=args.model, provider=args.model_provider)
    record = {"input_sha256": identity, "status": "running",
              "attempt": prior.get("attempt", 0) + 1,
              "request_sha256": hashlib.sha256(stdin.encode("utf-8")).hexdigest()}
    _write_atomic(path.with_suffix(".query.txt"), stdin.encode("utf-8"))
    _write_atomic(path, _canonical_bytes(record))
    started = time.monotonic()
    phase = "invoke"
    try:
        completed = subprocess.run(command, input=stdin, text=True, encoding="utf-8",
            capture_output=True, cwd=ROOT, timeout=args.authoring_timeout,
            env={**os.environ, "HERMES_STREAM_RETRIES": "0"})
        diagnostic = {"returncode": completed.returncode, "stdout": completed.stdout,
                      "stderr": completed.stderr, "request_sha256": record["request_sha256"],
                      "seconds": round(time.monotonic() - started, 3)}
        _write_atomic(path.with_suffix(f".attempt-{record['attempt']}.json"), _canonical_bytes(diagnostic))
        if completed.returncode or not completed.stdout.strip():
            raise ValueError("Hermes authoring response absent or failed")
        phase = "parse"
        raw = _parse_hermes_quiet_response(completed.stdout, completed.stderr)
        phase = "validate"
        result = validate(raw)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError, KeyError) as exc:
        record.update(status="failed", error_stage=phase, error_type=type(exc).__name__, error=str(exc),
                      seconds=round(time.monotonic() - started, 3))
        _write_atomic(path, _canonical_bytes(record))
        raise ValueError(f"authoring item failed: {type(exc).__name__}") from exc
    record.update(status="completed", response=raw, seconds=round(time.monotonic() - started, 3))
    _write_atomic(path, _canonical_bytes(record))
    return result


def _verify_authoring_resume(args, staging, bundle):
    _verify_staging_digest(staging, bundle)
    if bundle.get("page_titles", False) != (not args.no_page_titles):
        raise SystemExit("prepared page-title policy changed; use fresh staging")
    for label, option in (("acquisition_batch", "acquisition_batch"),
                          ("web_listening_manifest", "web_listening_manifest"),
                          ("pillar_b_artifact", "pillar_b_artifact")):
        entry = bundle["public_artifacts"][label]
        path = Path(getattr(args, option)).resolve()
        if str(path) != entry["path"] or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise SystemExit("prepared authoring source changed; use fresh staging")
    if args.report_date and args.report_date != bundle["report_date"]:
        raise SystemExit("prepared authoring report date changed")
    if bundle.get("execution_binding"):
        reference = bundle["registry_acquisition"]["task_binding"]
        path = Path(reference["path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
            raise SystemExit("immutable task binding changed; use fresh staging")
        bound, bound_path, bound_taxonomy = _load_task_binding_with_taxonomy(str(path))
        prepared_prompt_sha = _bound_prompt(bound, "article_summary", bound_path).sha256
        prepared_taxonomy_sha = bound_taxonomy.sha256
        if args.model != bound["model"] or args.model_provider != bound["provider"]:
            raise SystemExit("bound provider/model changed; use fresh staging")
    else:
        prepared_prompt_sha = load_weekly_monitor_prompt().sha256
        prepared_taxonomy_sha = load_article_taxonomy().sha256
    if (prepared_prompt_sha != bundle["prompt"]["sha256"]
            or prepared_taxonomy_sha != bundle["taxonomy"]["sha256"]):
        raise SystemExit("prepared authoring prompt or taxonomy changed")
    identity = {"schema_version": "weekly-url-authoring-run.v1", "bundle_digest": bundle["bundle_digest"],
                "model": args.model, "provider": args.model_provider,
                "output_paths": {key: str(Path(getattr(args, key)).resolve())
                                 for key in ("state_dir", "source_dir", "wiki_dir")}}
    path = staging / "authoring_run.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != identity:
        raise SystemExit("authoring run identity changed; use fresh staging")
    _write_atomic(path, _canonical_bytes(identity))


def _run_authoring_sequence(args, parser) -> MonitorRunResult:
    """Prepare once, process/resume URLs serially, summarize, then finalize."""
    import subprocess
    from contextlib import redirect_stdout
    from dataclasses import asdict
    staging = Path(args.staging_dir).resolve()
    response_path = staging / "authoring_response.json"
    bound = None
    bound_path = None
    bound_taxonomy = None
    if getattr(args, "task_binding", ""):
        bound, bound_path, bound_taxonomy = _load_task_binding_with_taxonomy(
            args.task_binding
        )
        args.model = bound["model"]
        args.model_provider = bound["provider"]
    if response_path.exists() and not (staging / "authoring_run.json").exists():
        raise SystemExit("authoring response already exists without a resumable URL run")
    args.model, args.model_provider = _resolve_authoring_identity(args.model, args.model_provider)
    if not (staging / "bundle.json").exists():
        with redirect_stdout(sys.stderr):
            _run_prepare(args, parser)
    bundle = _read_staging_bundle(staging)
    _verify_authoring_resume(args, staging, bundle)
    _verify_candidate_selection(args, bundle)
    request = json.loads((staging / "v2_authoring_request.json").read_text(encoding="utf-8"))
    evidence = json.loads((staging / "article_evidence.json").read_text(encoding="utf-8"))
    items = _candidate_items_from_evidence(None, evidence)
    by_url = {canonical_url(item.url): item for item in items}
    views = {canonical_url(record["requested_url"]): record for record in _authoring_evidence_view(evidence)["records"]}
    taxonomy = bound_taxonomy or load_article_taxonomy()
    constraints = asdict(taxonomy.constraints)
    constraints["disallowed_keywords"] = sorted(constraints["disallowed_keywords"])
    try:
        help_result = subprocess.run(["hermes", "chat", "--help"], text=True,
                                     capture_output=True, cwd=ROOT, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit("Hermes authoring capabilities unavailable") from exc
    if help_result.returncode:
        raise SystemExit("Hermes authoring capabilities unavailable")
    _hermes_authoring_invocation(help_result.stdout, "", model=args.model, provider=args.model_provider)
    if bound is not None:
        article_component = _bound_prompt(bound, "article_summary", Path(str(bound_path))).raw_bytes.decode("utf-8")
        relevance_component = _bound_prompt(bound, "relevance", Path(str(bound_path))).raw_bytes.decode("utf-8")
        executive_component = _bound_prompt(bound, "executive_summary", Path(str(bound_path))).raw_bytes.decode("utf-8")
    else:
        article_component = _configured_business_prompt("article_summary")
        relevance_component = load_article_relevance_rules()
        executive_component = _configured_business_prompt("executive_summary")
    url_prompt = article_component + "\nRELEVANCE RULES:\n" + relevance_component
    authored, failures = [], []
    for index, article in enumerate(request["articles"]):
        url = canonical_url(article["url"])
        payload = {"task": "article", "article": {"url": article["url"], "title": article["title"]},
                   "evidence": views[url], "allowed_categories": sorted(taxonomy.allowed_labels),
                   "semantic_constraints": constraints}
        instruction = url_prompt + "\nINPUT_JSON:\n" + _canonical_bytes(payload).decode("utf-8")
        key = hashlib.sha256(article["article_id"].encode("utf-8")).hexdigest()
        retry_guidance = ""
        if not views[url]["readable_content"] and not views[url]["search_snippet"]:
            retry_guidance = (
                'This URL has NO body and NO search snippet. The only valid summary fields are '
                '{"summary":"","summary_basis":"none","evidence_hash":null}. '
                'The summary length minimum does not apply when evidence is absent. '
                'A title or URL is not a search snippet; do not infer a summary from it.'
            )
        try:
            result = _checkpointed_authoring(staging / "url_authoring" / f"{key}.json", instruction,
                args=args, help_stdout=help_result.stdout,
                retry_guidance=retry_guidance,
                validate=lambda raw: _validate_url_authoring(raw, article, request, by_url[url], taxonomy))
            authored.append(result)
            status = "completed"
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            failures.append({"article_id": article["article_id"], "url": article["url"], "error": str(exc)})
            status = "failed"
        print(json.dumps({"status": status, "article_id": article["article_id"],
                          "processed": index + 1, "total": len(request["articles"])}), file=sys.stderr, flush=True)
    _write_atomic(staging / "authoring_progress.json", _canonical_bytes(
        {"total": len(request["articles"]), "completed": len(authored), "failed": failures}))
    if failures:
        raise SystemExit(f"URL authoring incomplete: {len(failures)} failed; rerun with the same staging directory")
    qualified = [{key: article[key] for key in ("url", "title", "summary", "categories", "keywords")}
                 for article in authored if article["relevant"] and article["summary"]]
    executive = ""
    if qualified:
        payload = {"task": "executive_summary", "report_date": request["report_date"],
                   "articles": qualified}
        instruction = executive_component + "\nINPUT_JSON:\n" + _canonical_bytes(payload).decode("utf-8")
        try:
            executive = _checkpointed_authoring(staging / "executive_authoring.json", instruction,
                args=args, help_stdout=help_result.stdout, validate=_validate_executive_authoring)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise SystemExit("executive authoring incomplete; rerun with the same staging directory") from exc
    response = _response_envelope(request, authored, executive)
    validate_authoring_response(items, response, taxonomy=taxonomy, request=request)
    _write_atomic(response_path, _canonical_bytes(response))
    args.authoring_response = str(response_path)
    return _run_finalize(args, parser)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the climate web listening monitor.")
    parser.add_argument("--source-config", default="monitoring/supranational_sources.yaml")
    parser.add_argument("--run-config", default="monitoring/run_config.yaml")
    parser.add_argument("--date", default="")
    parser.add_argument("--report-date", default="",
                        help="Explicit report date for production-weekly prepare/finalize")
    parser.add_argument("--print-pillar-b-prompt", action="store_true",
                        help="Render the editable Hermes search task; requires --report-date and --pillar-b-artifact. No search or writes.")
    parser.add_argument("--manifest-fixture", default="")
    parser.add_argument("--research-fixture", default="")
    parser.add_argument(
        "--article-changes-artifact",
        default="",
        help="Current Step 1a article_changes JSON; requires --pillar-b-artifact.",
    )
    parser.add_argument(
        "--pillar-b-artifact",
        default="",
        help="Current Step 1b pillar_b JSON; requires --article-changes-artifact.",
    )
    parser.add_argument("--site-scopes", default="monitoring/site_scopes.yaml")
    parser.add_argument("--state-dir", default="monitoring/state")
    parser.add_argument("--source-dir", default="")
    parser.add_argument("--wiki-dir", default="")
    parser.add_argument("--no-sync", action="store_true")
    parser.add_argument(
        "--no-update-seen-state",
        action="store_true",
        help=(
            "Do not prepare or commit URL history or live source checkpoints. By "
            "default they commit only after Markdown, semantic sidecar, combined "
            "candidate evidence, full candidate-item snapshot, and canonical URL "
            "state succeed."
        ),
    )
    parser.add_argument(
        "--production-weekly",
        action="store_true",
        help="Use the repo-owned strict weekly driver and versioned prompt.",
    )
    parser.add_argument(
        "--authoring-mode",
        choices=("prepare", "finalize", "run"),
        default="",
        help="Production monitor mode. ``run`` prepares/resumes serial URL authoring, summarizes and finalizes. ``prepare`` materialises the "
             "staging bundle from #67 outcome + manifest + Pillar B; "
             "``finalize`` consumes that bundle plus exactly one authoring "
             "response and commits the #91 transaction. Only valid with "
             "``--production-weekly``.",
    )
    parser.add_argument("--acquisition-batch", default="",
                        help="Path to the public acquisition-batch-result.v2 artifact "
                             "emitted by the upstream #67 producer.")
    parser.add_argument("--task-binding", default="",
                        help="Canonical absolute immutable management binding; overrides report date, Registry batch, prompts, provider, and model.")
    parser.add_argument("--registry-database", default="",
        help="Registry SQLite database containing a completed pre-report acquisition batch.",
    )
    parser.add_argument(
        "--registry-acquisition-batch-id", default="",
        help="Exact immutable Registry acquisition batch to freeze for report evidence.",
    )
    parser.add_argument(
        "--registry-acquisition-input", default="",
        help="Complete pre-report-acquisition-batch.v1 JSON to store durably before freeze.",
    )
    parser.add_argument("--web-listening-manifest", default="",
                        help="Path to the public web-listening-manifest.v1 artifact "
                             "emitted by the upstream #67 producer.")
    parser.add_argument("--staging-dir", default="",
                        help="Absolute path the prepare mode writes the staging "
                             "bundle to and the finalize mode reads it from.")
    parser.add_argument("--authoring-response", default="")
    parser.add_argument("--no-page-titles", action="store_true",
                        help="Disable offline H1/title extraction during prepare; retain discovery titles.")
    parser.add_argument("--authoring-timeout", type=float, default=180,
                        help="Maximum seconds for each independent URL or executive-summary invocation.")
    parser.add_argument("--model-provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument(
        "--article-evidence-loopback", default="", metavar="MODULE:CALLABLE",
        help="Inject an article-content provider for test/CI evidence staging.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON for ai_interface.")
    parser.add_argument(
        "--article-evidence",
        default="",
        help=(
            "Optional path to a pre-staged article-evidence.v1 JSON envelope. "
            "When provided, --production-weekly invokes the v2 request emitter "
            "and response validator before the orchestrator runs. "
            "--stats must accompany this."
        ),
    )
    parser.add_argument(
        "--stats",
        default="",
        help=(
            "Deterministic stats JSON (object with checked, succeeded, "
            "failed keys) used to build the v2 authoring request. "
            "Required when --article-evidence is set."
        ),
    )
    args = parser.parse_args()
    if args.print_pillar_b_prompt:
        if args.production_weekly or args.authoring_mode:
            parser.error("--print-pillar-b-prompt cannot be combined with monitor execution")
        if not args.report_date or not args.pillar_b_artifact:
            parser.error("--print-pillar-b-prompt requires --report-date and --pillar-b-artifact")
        try:
            prompt = load_pillar_b_search_prompt(date.fromisoformat(args.report_date), args.pillar_b_artifact)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        text = prompt.raw_bytes.decode("utf-8")
        if args.json:
            print(json.dumps({"prompt_id": prompt.prompt_id, "version": prompt.version,
                              "path": str(prompt.path), "sha256": prompt.sha256,
                              "report_date": args.report_date, "prompt": text}, ensure_ascii=False))
        else:
            print(text, end="")
        return
    import math
    if not math.isfinite(args.authoring_timeout) or args.authoring_timeout <= 0:
        parser.error("--authoring-timeout must be a finite positive number")

    if args.authoring_mode and not args.production_weekly:
        parser.error("--authoring-mode requires --production-weekly")
    registry_values = (args.registry_database, args.registry_acquisition_batch_id,
                       args.registry_acquisition_input)
    if args.task_binding and any(registry_values):
        parser.error("--task-binding cannot be combined with mutable Registry handoff arguments")
    if any(registry_values) and not all(registry_values):
        parser.error("--registry-database, --registry-acquisition-batch-id, and "
                     "--registry-acquisition-input must be supplied together")
    if (args.registry_database or args.task_binding) and args.authoring_mode not in {"prepare", "run"}:
        parser.error("Registry acquisition handoff is only valid for prepare/run")
    if args.authoring_mode and args.article_evidence:
        parser.error("--authoring-mode supersedes --article-evidence / --stats; "
                     "the staging bundle carries the canonical evidence and stats")
    if args.authoring_mode and args.authoring_response and args.authoring_mode != "finalize":
        parser.error("--authoring-response is only valid with --authoring-mode finalize")
    if args.authoring_mode in {"prepare", "run"} and not (args.acquisition_batch
                                                  and args.web_listening_manifest
                                                  and args.pillar_b_artifact
                                                  and args.staging_dir
                                                  and (args.report_date or args.task_binding)):
        parser.error(
            "--authoring-mode prepare requires --acquisition-batch, "
            "--web-listening-manifest, --pillar-b-artifact, --staging-dir, --report-date"
        )
    if args.authoring_mode == "finalize" and not (args.staging_dir and args.authoring_response):
        parser.error(
            "--authoring-mode finalize requires --staging-dir and --authoring-response"
        )

    if args.production_weekly and not args.authoring_mode and not args.authoring_response:
        parser.error(
            "--production-weekly requires --authoring-mode {prepare,finalize}; "
            "the legacy --authoring-response/--article-evidence/--stats triple is no "
            "longer accepted because the same-run chain must build its own bundle"
        )
    if (args.production_weekly and not args.authoring_mode
            and bool(args.article_changes_artifact) != bool(args.pillar_b_artifact)):
        parser.error(
            "--article-changes-artifact and --pillar-b-artifact must be supplied together"
        )
    if args.article_changes_artifact and (args.manifest_fixture or args.research_fixture):
        parser.error("current Pillar artifacts cannot be combined with manifest/research fixtures")
    if args.production_weekly and not args.authoring_mode:
        if bool(args.article_evidence) != bool(args.stats):
            parser.error(
                "--article-evidence and --stats must be supplied together "
                "(both required for v2 evidence path)"
            )

    _enforce_production_env_fixture(os.environ)

    fixture_root = _outcome_fixture_root()
    if (args.authoring_mode in {"prepare", "run"} and fixture_root is None
            and not (all(registry_values) or args.task_binding)):
        parser.error("production prepare/run requires the Registry store-before-freeze "
                     "arguments: --registry-database, --registry-acquisition-batch-id, "
                     "--registry-acquisition-input")
    if fixture_root is not None:
        if (args.authoring_mode not in {"prepare", "finalize"} or not args.no_sync
                or args.article_evidence_loopback != "scripts.hermes_job:dry_run_unavailable_provider"):
            parser.error("outcome fixture is only for offline prepare/finalize tests")
        for value in (args.staging_dir, args.source_dir, args.state_dir, args.wiki_dir):
            if not value or not Path(value).resolve().is_relative_to(fixture_root):
                parser.error("outcome fixture output paths must stay inside the dry-run root")

    if args.authoring_mode == "run":
        result = _run_authoring_sequence(args, parser)
    elif args.authoring_mode == "prepare":
        return _run_prepare(args, parser)
    elif args.authoring_mode == "finalize":
        result = _run_finalize(args, parser)
    elif args.production_weekly:
        article_evidence_payload: dict | None = None
        stats_payload: dict | None = None
        if args.article_evidence:
            try:
                article_evidence_payload = json.loads(
                    Path(args.article_evidence).read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                parser.error(
                    f"--article-evidence file is not readable JSON: {exc}"
                )
            try:
                stats_payload = json.loads(args.stats)
            except ValueError as exc:
                parser.error(f"--stats is not valid JSON: {exc}")
            if not isinstance(stats_payload, dict):
                parser.error("--stats must be a JSON object")
        result = run_weekly_monitor(
            source_config_path=Path(args.source_config),
            run_config_path=Path(args.run_config),
            report_date=date.fromisoformat(args.date) if args.date else None,
            manifest_fixture_path=Path(args.manifest_fixture) if args.manifest_fixture else None,
            research_fixture_path=Path(args.research_fixture) if args.research_fixture else None,
            article_changes_artifact_path=(
                Path(args.article_changes_artifact) if args.article_changes_artifact else None
            ),
            pillar_b_artifact_path=Path(args.pillar_b_artifact) if args.pillar_b_artifact else None,
            site_scopes_path=Path(args.site_scopes) if args.site_scopes else None,
            state_dir=Path(args.state_dir),
            source_dir=Path(args.source_dir) if args.source_dir else None,
            wiki_dir=Path(args.wiki_dir) if args.wiki_dir else None,
            sync=not args.no_sync,
            update_seen_state=not args.no_update_seen_state,
            authoring_response_path=Path(args.authoring_response) if args.authoring_response else None,
            model_provider=args.model_provider,
            model=args.model,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
            providers=_parse_loopback_provider(args.article_evidence_loopback),
            article_evidence=article_evidence_payload,
            stats=stats_payload,
        )
    else:
        if args.authoring_response:
            parser.error("--authoring-response requires --production-weekly")
        providers = _parse_loopback_provider(args.article_evidence_loopback)
        common_kwargs = {
            "source_config_path": Path(args.source_config),
            "run_config_path": Path(args.run_config),
            "report_date": date.fromisoformat(args.date) if args.date else None,
            "manifest_fixture_path": Path(args.manifest_fixture) if args.manifest_fixture else None,
            "research_fixture_path": Path(args.research_fixture) if args.research_fixture else None,
            "article_changes_artifact_path": (
                Path(args.article_changes_artifact) if args.article_changes_artifact else None
            ),
            "pillar_b_artifact_path": Path(args.pillar_b_artifact) if args.pillar_b_artifact else None,
            "site_scopes_path": Path(args.site_scopes) if args.site_scopes else None,
            "state_dir": Path(args.state_dir),
            "source_dir": Path(args.source_dir) if args.source_dir else None,
            "wiki_dir": Path(args.wiki_dir) if args.wiki_dir else None,
            "sync": not args.no_sync,
            "update_seen_state": not args.no_update_seen_state,
        }
        if providers:
            common_kwargs["providers"] = providers
        result = run_monitor(**common_kwargs)

    if args.json:
        print(result.to_json(), end="")
        return

    if result.report_path:
        print(f"Report written: {result.report_path}")
        print(f"Items included: {len(result.items)}")
        print(f"Wiki synced: {'yes' if result.synced else 'no'}")
    else:
        print("No monitor-matching updates found; no report written.")
    # Surface the article-evidence.v1 artifact path so downstream consumers
    # (Issue #93) can locate it without scanning ``source_dir``. The
    # orchestrator writes one artifact per run that survives long enough
    # to reach this print, when at least one candidate was processed;
    # the path is computed the same way the writer does.
    if result.report_path:
        artifact_path = Path(result.report_path).parent / (
            f"article-evidence.v1_{result.report_date.isoformat()}.json"
        )
        print(f"Article evidence: {artifact_path}")
    for note in result.dedup_notes:
        print(f"Dedup: {note}")
    for warning in result.warnings:
        print(f"Warning: {warning}")


if __name__ == "__main__":
    main()
