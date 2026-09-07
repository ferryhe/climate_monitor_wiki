from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.orchestrator import run_monitor
from climate_monitor.weekly_monitor.driver import run_weekly_monitor
from climate_monitor.weekly_monitor.authoring_contract import (
    AUTHORING_REQUEST_SCHEMA_VERSION_V2,
    AuthoringContractError,
    build_authoring_request,
    load_authoring_response,
    validate_authoring_response,
    _validate_v2_stats_shape,
)
from climate_monitor.weekly_monitor.prompt_loader import load_weekly_monitor_prompt
from climate_monitor.weekly_monitor.driver import _candidate_items_from_evidence
from climate_monitor.taxonomy import load_article_taxonomy
from climate_monitor.candidate_aggregation import (
    combine_current_artifacts,
    serialize_combined_candidates,
    items_from_merged_candidates_with_carry,
)
from climate_monitor.article_content_adapter import (
    build_article_evidence_artifact,
    write_article_evidence_artifact,
)
from climate_monitor.dedupe import canonical_url
from climate_monitor.candidate_snapshot import (
    candidate_item_snapshot_path,
    build_candidate_item_snapshot,
    serialize_candidate_item_snapshot as _serialize_candidate_snapshot,
    validate_candidate_item_snapshot,
)
from climate_monitor.models import CandidateItem


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


def _read_outcome(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"acquisition outcome {path} must be an object")
    if payload.get("schema_version") != "acquisition-batch-result.v2":
        raise SystemExit(
            f"acquisition outcome schema_version must be acquisition-batch-result.v2, "
            f"got {payload.get('schema_version')!r}"
        )
    run = payload.get("run") or {}
    if not isinstance(run, dict) or not run.get("run_id") or not run.get("source_id"):
        raise SystemExit("acquisition outcome run must declare run_id and source_id")
    counts = payload.get("counts") or {}
    required = ("requested", "updated", "unchanged", "blocked", "failed", "unresolved")
    for key in required:
        if key not in counts:
            raise SystemExit(f"acquisition outcome counts missing {key!r}")
        value = counts[key]
        if type(value) is not int or value < 0:
            raise SystemExit(f"acquisition outcome counts.{key} must be a non-negative integer")
    records = payload.get("records")
    if not isinstance(records, list):
        raise SystemExit("acquisition outcome records must be a list")
    return payload


def _read_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"web-listening manifest {path} must be an object")
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
    return payload


def _read_pillar_b(path: Path) -> list:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit(f"pillar-b artifact {path} must be a list")
    for entry in payload:
        if not isinstance(entry, dict) or not entry.get("url"):
            raise SystemExit("pillar-b artifact entries must be objects with a url")
    return payload


def _canonical_digest(payload: dict | list) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_bytes(payload: dict | list) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False,
                       sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_same_run_identity(outcome: dict, manifest: dict) -> None:
    """Enforce that the public outcome and manifest come from the same upstream run.

    Cross-run or missing identity binding is the canonical leak path for
    the migration windows the Issue describes (bootstrap-no-miss,
    same-week re-run, duplicate scan). Fail closed before the staging
    bundle is written.
    """
    outcome_run = (outcome.get("run") or {}).get("run_id")
    manifest_run = (manifest.get("run") or {}).get("run_id")
    outcome_source = (outcome.get("run") or {}).get("source_id")
    manifest_source_id = ((manifest.get("source") or {}).get("source_id"))
    if not outcome_run or not manifest_run:
        raise SystemExit("same-run identity requires both run_id values")
    if outcome_run != manifest_run:
        raise SystemExit(
            f"cross-run identity: outcome run_id={outcome_run!r} != manifest run_id={manifest_run!r}"
        )
    if not outcome_source:
        raise SystemExit("acquisition outcome must declare run.source_id for same-run identity")
    if manifest_source_id and outcome_source != manifest_source_id:
        raise SystemExit(
            f"cross-run source identity: outcome source_id={outcome_source!r} != manifest source_id={manifest_source_id!r}"
        )


def _collect_same_run_records(outcome: dict, manifest: dict) -> list[dict]:
    """Project every manifest ``discovered_item`` to a same-run evidence record.

    Only items that the manifest bound to the same upstream run as the
    acquisition outcome survive; missing URLs or duplicate canonical URLs
    fail closed. Records are deterministically ordered by canonical URL so
    bundle digests are stable across re-prepares.
    """
    by_canonical: dict[str, dict] = {}
    for raw in manifest.get("discovered_items") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        if not url:
            continue
        canonical = canonical_url(url)
        if not canonical:
            raise SystemExit(f"manifest item url has no canonical form: {url!r}")
        if canonical in by_canonical:
            raise SystemExit(f"manifest duplicate canonical url: {canonical}")
        final_url = str(raw.get("final_url") or url)
        by_canonical[canonical] = {
            "final_url": final_url,
            "requested_url": url,
            "title": raw.get("title") or "",
            "summary": raw.get("summary") or "",
            "summary_basis": raw.get("summary_basis") or "page",
            "title_basis": raw.get("title_basis") or "upstream_artifact",
            "display_pillar": raw.get("display_pillar") or "A",
            "origins": raw.get("origins") or [],
            "content_hash": raw.get("content_hash") or "",
        }
    ordered = sorted(by_canonical.values(), key=lambda r: canonical_url(r["final_url"]))
    return ordered


def _attach_outcome_disposition(records: list[dict], outcome: dict) -> list[dict]:
    """Cross-reference outcome dispositions with manifest records.

    A manifest record with no matching outcome disposition is a cross-run
    leak; an outcome record with no manifest entry is the reverse. Both
    fail closed. ``acquisition_unresolved`` and ``failed`` are merged per
    Issue #87 AC-3 so the consumer can render them as a single
    failure bucket.
    """
    by_canonical: dict[str, dict] = {}
    for record in outcome.get("records") or []:
        if not isinstance(record, dict):
            continue
        url = str(record.get("final_url") or record.get("requested_url") or "").strip()
        if not url:
            continue
        canonical = canonical_url(url)
        if not canonical:
            continue
        if canonical in by_canonical:
            raise SystemExit(f"outcome duplicate canonical url: {canonical}")
        by_canonical[canonical] = record
    enriched: list[dict] = []
    for record in records:
        canonical = canonical_url(record["final_url"])
        outcome_record = by_canonical.get(canonical)
        if outcome_record is None:
            raise SystemExit(
                f"manifest record has no matching outcome disposition: {record['final_url']!r}"
            )
        disposition = str(outcome_record.get("disposition") or "").strip().lower()
        if not disposition:
            raise SystemExit(
                f"outcome record {record['final_url']!r} is missing disposition"
            )
        if disposition not in {"updated", "unchanged", "blocked", "failed"}:
            raise SystemExit(
                f"outcome record {record['final_url']!r} has unsupported disposition {disposition!r}"
            )
        merged = dict(record)
        merged["disposition"] = disposition
        error_code = outcome_record.get("error_code")
        if error_code:
            merged["error_code"] = str(error_code)
        if outcome_record.get("acquisition_unresolved"):
            merged["acquisition_unresolved"] = True
        enriched.append(merged)
    return enriched


def _derive_stats(records: list[dict], outcome: dict) -> dict:
    """Map the outcome counts to the canonical 6-key v2 stats shape.

    Per Issue #87 AC-3: ``failed = failed + unresolved`` and ``total =
    updated + unchanged + blocked + failed + unresolved``. The recorded
    per-URL dispositions must agree with the high-level counts.
    """
    counts = (outcome.get("counts") or {})
    derived: dict[str, int] = {
        "total": int(counts["requested"]),
        "updated": int(counts["updated"]),
        "unchanged": int(counts["unchanged"]),
        "blocked": int(counts["blocked"]),
        "failed": int(counts["failed"]) + int(counts["unresolved"]),
        "unresolved": int(counts["unresolved"]),
    }
    expected_total = (
        derived["updated"] + derived["unchanged"] + derived["blocked"]
        + derived["failed"] + derived["unresolved"]
    )
    if derived["total"] != expected_total:
        raise SystemExit(
            f"outcome counts inconsistent: requested={derived['total']} "
            f"vs components sum={expected_total}"
        )
    observed: dict[str, int] = {"updated": 0, "unchanged": 0, "blocked": 0, "failed": 0}
    for record in records:
        observed[record["disposition"]] += 1
    if observed["updated"] != derived["updated"]:
        raise SystemExit(
            f"manifest updated count {observed['updated']} != outcome updated {derived['updated']}"
        )
    if observed["unchanged"] != derived["unchanged"]:
        raise SystemExit(
            f"manifest unchanged count {observed['unchanged']} != outcome unchanged {derived['unchanged']}"
        )
    if observed["blocked"] != derived["blocked"]:
        raise SystemExit(
            f"manifest blocked count {observed['blocked']} != outcome blocked {derived['blocked']}"
        )
    if (observed["failed"]) != derived["failed"]:
        raise SystemExit(
            f"manifest failed count {observed['failed']} != outcome failed+unresolved {derived['failed']}"
        )
    return derived


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
        source = ((record.get("origins") or [{}])[0] or {}).get("source") or "unknown"
        groups.setdefault(source, []).append({
            "title": record.get("title") or "",
            "url": record.get("final_url") or record.get("requested_url"),
            "categories": ["climate_supervision"],
        })
    articles: list[dict] = []
    for org, items in sorted(groups.items()):
        articles.append({"org": org, "items": items})
    if not articles:
        articles = [{"org": "no_retained_records",
                     "items": [{"title": "", "url": "https://www.iais.org/no-retained-records",
                                 "categories": ["climate_supervision"]}]}]
    return {
        "date": report_date,
        "pillar": "A",
        "sites_with_changes": len(groups),
        "orgs_with_articles": len(groups),
        "baseline_urls": 0,
        "new_articles": len(kept),
        "seen_before": 0,
        "generated_at": f"{report_date}T08:05:00Z",
        "articles": articles,
    }


def _build_evidence_payload(records: list[dict]) -> dict:
    """Build the ``article-evidence.v1`` envelope the driver expects.

    Only successful / retained records are surfaced; failed+blocked entries
    remain in the staging bundle for audit but never carry article evidence.
    The envelope is built by calling the same public builder the existing
    orchestrator uses, so AC-2 (the existing public adapter) is honoured
    without a parallel implementation.
    """
    kept = [r for r in records if r["disposition"] in {"updated", "unchanged"}]
    inputs: list[dict] = []
    for record in kept:
        inputs.append({
            "article_id": canonical_url(record["final_url"]),
            "url": record["final_url"],
            "title": record.get("title") or "",
            "search_snippet": record.get("summary") or "",
        })
    return build_article_evidence_artifact(inputs, providers=(), report_date="")


def _run_prepare(args, parser) -> int:
    outcome_path = Path(args.acquisition_batch).resolve()
    manifest_path = Path(args.web_listening_manifest).resolve()
    pillar_b_path = Path(args.pillar_b_artifact).resolve()
    staging_dir = Path(args.staging_dir).resolve()
    if not staging_dir.is_absolute() or staging_dir.resolve() != staging_dir:
        parser.error("--staging-dir must be an absolute canonical path")
    staging_dir.mkdir(parents=True, exist_ok=True)
    dry_run = os.environ.get("CLIMATE_DRY_RUN") == "1"
    _enforce_production_paths(
        [outcome_path, manifest_path, pillar_b_path, staging_dir],
        dry_run=dry_run,
    )
    outcome = _read_outcome(outcome_path)
    manifest = _read_manifest(manifest_path)
    pillar_b = _read_pillar_b(pillar_b_path)
    _verify_same_run_identity(outcome, manifest)
    records = _collect_same_run_records(outcome, manifest)
    records = _attach_outcome_disposition(records, outcome)
    stats = _derive_stats(records, outcome)
    evidence_payload = _build_evidence_payload(records)

    report_date = date.fromisoformat(args.report_date) if args.report_date else date.today()
    source_dir = Path(args.source_dir).resolve()
    if not source_dir.is_absolute():
        parser.error("--source-dir must be an absolute path")
    state_dir = Path(args.state_dir).resolve()
    if not state_dir.is_absolute():
        parser.error("--state-dir must be an absolute path")
    output_source_dir = source_dir
    source_dir.mkdir(parents=True, exist_ok=True)

    combined = combine_current_artifacts(
        _outcome_to_article_changes(outcome, manifest, records, report_date.isoformat()),
        pillar_b,
        report_date=report_date.isoformat(),
        pillar_a_artifact_id=outcome_path.name,
        pillar_a_artifact_sha256=hashlib.sha256(outcome_path.read_bytes()).hexdigest(),
        pillar_b_artifact_id=pillar_b_path.name,
        pillar_b_artifact_sha256=hashlib.sha256(pillar_b_path.read_bytes()).hexdigest(),
        pillar_b_discovered_at=f"{report_date.isoformat()}T00:00:00Z",
        seen_urls=set(),
    )
    combined_bytes = serialize_combined_candidates(combined.artifact)

    snapshot_items = items_from_merged_candidates_with_carry(
        combined.candidates, carry_forward_candidates=(), carry_forward_items=()
    )
    snapshot_path = candidate_item_snapshot_path(output_source_dir, report_date)
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
    request = build_authoring_request(
        report_date=report_date,
        items=candidate_items,
        prompt=load_weekly_monitor_prompt(),
        article_evidence=evidence_payload,
        stats=stats,
    )
    if request.get("schema_version") != AUTHORING_REQUEST_SCHEMA_VERSION_V2:
        raise SystemExit("prepare failed to emit a v2 authoring request")

    taxonomy = load_article_taxonomy()
    bundle_payload = {
        "schema_version": "climate-monitor-prepare-bundle.v1",
        "report_date": report_date.isoformat(),
        "staging_digest_inputs": [
            "combined", "snapshot", "evidence", "stats", "request", "identity",
        ],
        "public_artifacts": {
            "acquisition_batch": {
                "path": str(outcome_path),
                "sha256": hashlib.sha256(outcome_path.read_bytes()).hexdigest(),
                "run_id": (outcome.get("run") or {}).get("run_id"),
                "source_id": (outcome.get("run") or {}).get("source_id"),
            },
            "web_listening_manifest": {
                "path": str(manifest_path),
                "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "run_id": (manifest.get("run") or {}).get("run_id"),
                "source_id": ((manifest.get("source") or {}).get("source_id")),
            },
            "pillar_b_artifact": {
                "path": str(pillar_b_path),
                "sha256": hashlib.sha256(pillar_b_path.read_bytes()).hexdigest(),
                "count": len(pillar_b),
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
        "request_sha256": request["request_sha256"],
    }
    digest_inputs: dict[str, bytes] = {
        "combined": combined_bytes,
        "snapshot": snapshot_path.read_bytes(),
        "evidence": _canonical_bytes(evidence_payload),
        "stats": _canonical_bytes(stats),
        "request": _canonical_bytes(request),
        "identity": _canonical_bytes(bundle_payload["public_artifacts"]),
    }
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
        else:
            raise SystemExit(f"unknown staging digest input: {key!r}")
        parts.append(data)
    expected = hashlib.sha256(b"".join(parts)).hexdigest()
    if expected != bundle.get("bundle_digest"):
        details = {k: hashlib.sha256(data).hexdigest() for k, data in zip(sorted(digest_keys), parts)}
        raise SystemExit(
            f"staging bundle digest mismatch: expected {expected}, got {bundle.get('bundle_digest')}; per-key {details}"
        )


def _run_finalize(args, parser) -> int:
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
    taxonomy = load_article_taxonomy()
    if taxonomy.sha256 != (bundle.get("taxonomy") or {}).get("sha256"):
        raise SystemExit(
            f"taxonomy sha256 changed since prepare: "
            f"prepared={bundle['taxonomy']['sha256']} current={taxonomy.sha256}"
        )
    prompt = load_weekly_monitor_prompt()
    if prompt.sha256 != (bundle.get("prompt") or {}).get("sha256"):
        raise SystemExit(
            f"prompt sha256 changed since prepare: "
            f"prepared={bundle['prompt']['sha256']} current={prompt.sha256}"
        )
    evidence_payload = json.loads(
        (staging_dir / "article_evidence.json").read_text(encoding="utf-8")
    )
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
    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pillar_b_payload = json.loads(pillar_b_path.read_text(encoding="utf-8"))
    records = _collect_same_run_records(outcome, manifest)
    records = _attach_outcome_disposition(records, outcome)
    pillar_a_payload = _outcome_to_article_changes(outcome, manifest, records, bundle["report_date"])
    source_dir = Path(args.source_dir).resolve()
    article_changes_artifact = source_dir / f"article_changes_{bundle['report_date']}.json"
    article_changes_artifact.write_bytes(_canonical_bytes(pillar_a_payload) + b"\n")
    article_changes_path = article_changes_artifact
    pillar_b_artifact = source_dir / f"pillar_b_{bundle['report_date']}.json"
    pillar_b_artifact.write_bytes(_canonical_bytes(pillar_b_payload) + b"\n")
    pillar_b_artifact_path = pillar_b_artifact

    return run_weekly_monitor(
        source_config_path=Path(args.source_config),
        run_config_path=Path(args.run_config),
        report_date=date.fromisoformat(args.report_date) if args.report_date else None,
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the climate web listening monitor.")
    parser.add_argument("--source-config", default="monitoring/supranational_sources.yaml")
    parser.add_argument("--run-config", default="monitoring/run_config.yaml")
    parser.add_argument("--date", default="")
    parser.add_argument("--report-date", default="",
                        help="Explicit report date for production-weekly prepare/finalize")
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
        choices=("prepare", "finalize"),
        default="",
        help="Production monitor two-phase mode. ``prepare`` materialises the "
             "staging bundle from #67 outcome + manifest + Pillar B; "
             "``finalize`` consumes that bundle plus exactly one authoring "
             "response and commits the #91 transaction. Only valid with "
             "``--production-weekly``.",
    )
    parser.add_argument("--acquisition-batch", default="",
                        help="Path to the public acquisition-batch-result.v2 artifact "
                             "emitted by the upstream #67 producer.")
    parser.add_argument("--web-listening-manifest", default="",
                        help="Path to the public web-listening-manifest.v1 artifact "
                             "emitted by the upstream #67 producer.")
    parser.add_argument("--staging-dir", default="",
                        help="Absolute path the prepare mode writes the staging "
                             "bundle to and the finalize mode reads it from.")
    parser.add_argument("--authoring-response", default="")
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

    if args.authoring_mode and not args.production_weekly:
        parser.error("--authoring-mode requires --production-weekly")
    if args.authoring_mode and args.article_evidence:
        parser.error("--authoring-mode supersedes --article-evidence / --stats; "
                     "the staging bundle carries the canonical evidence and stats")
    if args.authoring_mode and args.authoring_response and args.authoring_mode != "finalize":
        parser.error("--authoring-response is only valid with --authoring-mode finalize")
    if args.authoring_mode == "prepare" and not (args.acquisition_batch
                                                  and args.web_listening_manifest
                                                  and args.pillar_b_artifact
                                                  and args.staging_dir
                                                  and args.report_date):
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

    if args.authoring_mode == "prepare":
        return _run_prepare(args, parser)
    if args.authoring_mode == "finalize":
        return _run_finalize(args, parser)
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
