#!/usr/bin/env python3
"""Step 3: Aggregate + Dedup.

Reads Pillar A (article_changes_<DATE>.json) and Pillar B (pillar_b_<DATE>.json),
merges by canonical URL, and outputs one candidate with complete origins per URL.

This is a deterministic script — no LLM, no randomness.
"""
import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.article_candidate_contract import CandidateContractError
from climate_monitor.candidate_aggregation import (
    combine_current_artifacts,
    commit_combined_candidates,
    combined_candidates_path,
    serialize_combined_candidates,
    staged_combined_candidates_path,
    validate_combined_candidates,
)
from climate_monitor.seen_state import (
    SeenStateError,
    load_legacy_seen_urls,
    load_pending_seen_url_transaction,
    pending_seen_url_delta_path,
    prepare_legacy_seen_url_delta,
)
from scripts.step2_save_state import validate_final_report_bundle
from climate_registry.classification import classify_document
from climate_registry.errors import RegistryInputError
from climate_registry.selection import (
    MAX_SUMMARY,
    MAX_TITLE,
    MAX_URL,
    _validate_public_http_url,
)

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))
STATE_FILE = Path(
    os.environ.get("CLIMATE_WL_STATE", "/home/ubuntu/web_listening/data/article_state.json")
)

PILLAR_A_ITEM_FIELDS = frozenset({"title", "url", "categories"})
PILLAR_B_ITEM_FIELDS = frozenset({"title", "url", "source", "summary"})


def _row_reasons(item: object, *, allowed_fields: frozenset[str]) -> list[str]:
    if not isinstance(item, dict):
        return ["invalid_record_type"]

    reasons = []
    if set(item) - allowed_fields:
        reasons.append("unexpected_fields")

    title = item.get("title")
    if not isinstance(title, str):
        reasons.append("missing_title")
    elif title != "" and (not title.strip() or len(title) > MAX_TITLE):
        reasons.append("invalid_title")

    url = item.get("url")
    if not isinstance(url, str) or not url.strip():
        reasons.append("missing_url")
    elif len(url) > MAX_URL:
        reasons.append("invalid_url")
    else:
        try:
            _validate_public_http_url(url)
        except RegistryInputError:
            reasons.append("invalid_url")
        else:
            if not classify_document(url).publication_eligible:
                reasons.append("publication_ineligible_url")

    summary = item.get("summary", "")
    if not isinstance(summary, str) or len(summary) > MAX_SUMMARY:
        reasons.append("invalid_summary")
    if "categories" in allowed_fields or "categories" in item:
        categories = item.get("categories")
        if (
            not isinstance(categories, list)
            or not categories
            or any(not isinstance(value, str) or not value.strip() for value in categories)
        ):
            reasons.append("invalid_categories")
    return reasons


def _record_errors(
    errors: dict[str, Counter[str]], artifact: str, reasons: list[str]
) -> None:
    if reasons:
        errors.setdefault(artifact, Counter()).update(reasons)
        errors[artifact]["__rows__"] += 1


def _load_pillar_a(
    path: Path, *, report_date: str, errors: dict[str, Counter[str]]
) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Pillar A artifact must be a JSON object")
    if payload.get("date") != report_date:
        raise ValueError("Pillar A artifact date does not match --date")
    if payload.get("pillar") != "A":
        raise ValueError("Pillar A artifact must declare pillar A")
    groups = payload.get("articles")
    if not isinstance(groups, list):
        raise ValueError("Pillar A artifact articles must be an array")

    for group_index, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            _record_errors(errors, path.name, ["invalid_org_group"])
            continue
        org = group.get("org")
        org_valid = isinstance(org, str) and bool(org.strip())
        for item_index, item in enumerate(group["items"]):
            reasons = _row_reasons(item, allowed_fields=PILLAR_A_ITEM_FIELDS)
            if not org_valid:
                reasons.append("invalid_org")
            _record_errors(errors, path.name, reasons)
    return payload


def _load_pillar_b(path: Path, *, errors: dict[str, Counter[str]]) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Pillar B artifact must be a JSON array")

    for item_index, item in enumerate(payload):
        reasons = _row_reasons(item, allowed_fields=PILLAR_B_ITEM_FIELDS)
        if isinstance(item, dict):
            if set(item) != PILLAR_B_ITEM_FIELDS:
                if "unexpected_fields" not in reasons:
                    reasons.append("unexpected_fields")
            if item.get("source") != "web":
                reasons.append("invalid_source")
        _record_errors(errors, path.name, reasons)
    return payload


def _format_errors(errors: dict[str, Counter[str]]) -> str:
    rows = sum(counts["__rows__"] for counts in errors.values())
    artifacts = []
    for artifact, counts in errors.items():
        reason_text = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(counts.items())
            if reason != "__rows__"
        )
        artifacts.append(f"{artifact}: {counts['__rows__']} ({reason_text})")
    return f"{rows} malformed article rows: " + "; ".join(artifacts)


def _primary_origin(candidate):
    return next(
        origin for origin in candidate.origins if origin.pillar == candidate.display_pillar
    )


def _legacy_record(pointer: str) -> str:
    if pointer.startswith("/articles/"):
        parts = pointer.strip("/").split("/")
        return f"articles[{parts[1]}].items[{parts[3]}]"
    return f"[{pointer.strip('/')}]"


def _legacy_item(candidate) -> dict:
    origin = _primary_origin(candidate)
    return {
        "article_id": candidate.article_id,
        "canonical_url": candidate.canonical_url,
        "title": candidate.title or origin.original_title or candidate.url,
        "url": candidate.url,
        "source": origin.source,
        "pillar": candidate.display_pillar,
        "summary": candidate.summary or origin.original_summary or "",
        "categories": list(candidate.categories or ()),
        "origins": [value.model_dump(mode="json") for value in candidate.origins],
        "provenance": {
            "artifact": origin.input_artifact.artifact_id,
            "record": _legacy_record(origin.row),
        },
    }


def _legacy_state_additions(candidates) -> dict[str, set[str]]:
    additions: dict[str, set[str]] = {}
    for candidate in candidates:
        for origin in candidate.origins:
            bucket = origin.source if origin.pillar == "A" else "__pillar_b__"
            additions.setdefault(bucket, set()).add(candidate.canonical_url)
    return additions


def _same_date_report_context(
    *, report_date: str, combined_path: Path
) -> tuple[set[str], tuple[dict, ...]]:
    report = REPORTS / f"climate-monitor-{report_date}.md"
    evidence = report.with_suffix(".json")
    if not report.exists() and not evidence.exists():
        return set(), ()
    if not report.exists() or not evidence.exists() or not combined_path.exists():
        raise ValueError("same-date report bundle is incomplete or inconsistent")
    try:
        candidate_urls = validate_final_report_bundle(
            report=report,
            evidence=evidence,
            combined=combined_path,
            report_date=report_date,
        )
        combined_bytes = combined_path.read_bytes()
        combined = validate_combined_candidates(
            json.loads(combined_bytes.decode("utf-8"))
        )
        if serialize_combined_candidates(combined) != combined_bytes:
            raise ValueError("combined candidate evidence is not canonical")
        return candidate_urls, tuple(combined["items"])
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("same-date report bundle is incomplete or inconsistent") from exc


def main():
    parser = argparse.ArgumentParser(
        description="Merge current Pillar A/B artifacts by canonical URL before processing."
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--combined-output",
        type=Path,
        help=(
            "Canonical combined-candidates.v1 path; when it belongs to a complete "
            "same-date report, write the next evidence to its .next sibling for Step 5."
        ),
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=STATE_FILE,
        help=(
            "Legacy URL history used for canonical-URL exclusion and a date-bound "
            "pending delta; commit an older pending date before a new state-updating run."
        ),
    )
    parser.add_argument(
        "--no-update-seen-state",
        action="store_true",
        help="Do not stage or overwrite a pending seen-URL transaction.",
    )
    args = parser.parse_args()

    try:
        parsed_date = date.fromisoformat(args.date)
    except ValueError:
        print(f"ERROR: invalid --date {args.date!r} (expected YYYY-MM-DD)")
        return 1
    if parsed_date.isoformat() != args.date:
        print(f"ERROR: invalid --date {args.date!r} (expected YYYY-MM-DD)")
        return 1
    args.date = parsed_date.isoformat()

    pending_path = pending_seen_url_delta_path(args.state_file)
    if pending_path.exists():
        try:
            pending_date, _, _ = load_pending_seen_url_transaction(args.state_file)
        except (OSError, SeenStateError, UnicodeError, ValueError) as exc:
            print(f"ERROR: pending seen-URL delta is invalid: {exc}")
            return 1
        if not args.no_update_seen_state or pending_date == args.date:
            print(
                f"ERROR: pending seen-URL delta belongs to {pending_date}; "
                "run step2_save_state.py --commit-pending for that date before "
                "another state-updating aggregate"
            )
            return 1

    out_path = args.output or (REPORTS / f"aggregated_{args.date}.json")
    combined_path = args.combined_output or combined_candidates_path(REPORTS, args.date)
    staged_combined_path = staged_combined_candidates_path(combined_path)
    pa_path = REPORTS / f"article_changes_{args.date}.json"
    pb_path = REPORTS / f"pillar_b_{args.date}.json"
    try:
        input_paths = {pa_path.resolve(), pb_path.resolve()}
        if out_path.resolve() in input_paths or combined_path.resolve() in input_paths:
            print("ERROR: aggregate output must not overwrite an input artifact")
            return 1
        if out_path.resolve() == combined_path.resolve():
            print("ERROR: aggregate and combined outputs must be different files")
            return 1
        same_date_urls, carry_forward_candidates = _same_date_report_context(
            report_date=args.date,
            combined_path=combined_path,
        )
        has_same_date_report = (REPORTS / f"climate-monitor-{args.date}.md").exists()
        out_path.unlink(missing_ok=True)
        staged_combined_path.unlink(missing_ok=True)
        if not has_same_date_report:
            combined_path.unlink(missing_ok=True)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: cannot invalidate stale aggregate/combined output: {exc}")
        return 1

    errors: dict[str, Counter[str]] = {}

    # Read Pillar A
    if pa_path.exists():
        try:
            pillar_a = _load_pillar_a(
                pa_path, report_date=args.date, errors=errors
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"ERROR: invalid {pa_path.name}: {exc}")
            return 1
    else:
        pillar_a = {
            "date": args.date,
            "pillar": "A",
            "sites_with_changes": 0,
            "orgs_with_articles": 0,
            "baseline_urls": 0,
            "new_articles": 0,
            "seen_before": 0,
            "generated_at": f"{args.date}T00:00:00Z",
            "articles": [],
        }
        print(f"WARNING: Pillar A file not found: {pa_path}")

    # Read Pillar B
    if pb_path.exists():
        try:
            pillar_b = _load_pillar_b(pb_path, errors=errors)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"ERROR: invalid {pb_path.name}: {exc}")
            return 1
    else:
        pillar_b = []
        print(f"WARNING: Pillar B file not found: {pb_path}")

    if errors:
        print(f"ERROR: {_format_errors(errors)}")
        return 1

    try:
        seen_urls = load_legacy_seen_urls(args.state_file) - same_date_urls
        combined = combine_current_artifacts(
            pillar_a,
            pillar_b,
            report_date=args.date,
            pillar_a_artifact_id=pa_path.name,
            pillar_a_artifact_sha256=hashlib.sha256(
                pa_path.read_bytes() if pa_path.exists() else json.dumps(pillar_a).encode("utf-8")
            ).hexdigest(),
            pillar_b_artifact_id=pb_path.name,
            pillar_b_artifact_sha256=hashlib.sha256(
                pb_path.read_bytes() if pb_path.exists() else b"[]"
            ).hexdigest(),
            pillar_b_discovered_at=f"{args.date}T00:00:00Z",
            seen_urls=seen_urls,
            carry_forward_candidates=carry_forward_candidates,
        )
    except (CandidateContractError, SeenStateError, OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: invalid current article artifacts/state: {exc}")
        return 1

    pillar_a_rows = combined.artifact["counts"]["pillar_a_rows"]
    pillar_b_rows = combined.artifact["counts"]["pillar_b_rows"]
    items = [_legacy_item(candidate) for candidate in combined.candidates]
    print(
        f"Aggregate: {pillar_a_rows} Pillar A + {pillar_b_rows} Pillar B; "
        f"{combined.artifact['counts']['unique_urls']} unique canonical URLs"
    )
    print(
        f"After history: {len(items)} items "
        f"({combined.artifact['counts']['history_skips']} skipped)"
    )

    output = {
        "date": args.date,
        "pillar_a_count": pillar_a_rows,
        "pillar_b_count": pillar_b_rows,
        "total_before_dedup": pillar_a_rows + pillar_b_rows,
        "total_after_dedup": len(items),
        "items": items,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    combined_destination = staged_combined_path if has_same_date_report else combined_path
    commit_combined_candidates(combined_destination, combined.artifact_bytes)
    if not args.no_update_seen_state:
        prepare_legacy_seen_url_delta(
            args.state_file,
            _legacy_state_additions(combined.candidates),
            report_date=args.date,
            combined_sha256=hashlib.sha256(combined.artifact_bytes).hexdigest(),
        )
    print(f"OK: {out_path} + {combined_destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
