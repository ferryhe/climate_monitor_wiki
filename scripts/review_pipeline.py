#!/usr/bin/env python3
"""Business queue commands for native Hermes agent cron and script-only sending."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from climate_registry import acquisition_review as acquisition
from climate_delivery import report_review as reports


def pending(kind: str, root: Path):
    pattern = "*/acquisition-review/state.json" if kind == "acquisition" else "reports/*/state.json"
    for path in sorted(root.glob(pattern)):
        state = json.loads(path.read_text())
        recovery_pending = kind == "acquisition" and state["status"] == "reviewed_partial" and any(
            value.get("status") == "recovering" for value in state.get("sources", {}).values())
        if state["status"] not in {"pending_review", "reviewing"} and not recovery_pending:
            continue
        if kind == "acquisition":
            result_path = Path(state["packet"]["attempt_result_path"])
            if not result_path.is_file():
                continue
        claim_path = path.parent / "claim.json" if kind == "acquisition" else path.parent / "revisions" / f"{state['revision']:04d}" / "claim.json"
        if claim_path.exists():
            claim = json.loads(claim_path.read_text())
            if not claim.get("released_at") and not acquisition.owner_finished(claim):
                continue  # Native owner completion, not elapsed time, permits reconciliation.
        if kind == "acquisition":
            from climate_registry.publication import resolve_database, require_review_information
            import sqlite3
            with sqlite3.connect(f"{resolve_database(frozen=state['packet']['registry_database']).as_uri()}?mode=ro",uri=True) as connection:
                from climate_registry.item_lifecycle import partition_items
                current = dict(connection.execute("SELECT entity_id,latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article'"))
                original = state["packet"]["candidates"] + state.get("excluded_candidates", [])
                shas = [current[item["identity"]["article_id"]] for item in original if item["identity"]["article_id"] in current]
                ready = {item["candidate_sha256"] for item in partition_items(connection, root, shas)["ready"]}
                frozen = {item.get("registry_candidate_sha256") for item in state["packet"]["candidates"]}
                if ready and ready != frozen:
                    return path.parent, {**state, "preparation_required": True, "ready_candidate_sha256s": sorted(ready)}
                try:
                    require_review_information(connection,state["packet"])
                    from climate_registry.item_lifecycle import require_item_readiness
                    require_item_readiness(connection, root, state["packet"])
                    if not state["packet"]["candidates"] and state.get("excluded_candidates"):
                        continue
                except ValueError:
                    continue
        return path.parent, state
    if kind == "acquisition":
        packet_path = root / "registry-review" / "packet.json"
        if packet_path.is_file():
            packet = json.loads(packet_path.read_text())
            from climate_registry.publication import resolve_database
            from climate_registry.item_lifecycle import partition_items
            import sqlite3
            with sqlite3.connect(f"{resolve_database(frozen=packet['registry_database']).as_uri()}?mode=ro", uri=True) as connection:
                shas = [row[0] for row in connection.execute("""SELECT c.candidate_sha256 FROM registry_publication p
                    JOIN registry_candidates c ON c.candidate_sha256=p.latest_candidate_sha256
                    WHERE p.published_candidate_sha256 IS NOT p.latest_candidate_sha256""")]
                pending_hashes = {item["candidate_sha256"] for item in partition_items(connection, root, shas)["ready"]}
            if pending_hashes:
                claim_path = packet_path.parent / "claim.json"
                if not claim_path.exists() or (claim := json.loads(claim_path.read_text())).get("released_at") or acquisition.owner_finished(claim):
                    return packet_path.parent, {"packet": packet,
                        "preparation_required": pending_hashes != {item["candidate_sha256"] for item in packet["candidates"]},
                        "ready_candidate_sha256s": sorted(pending_hashes)}
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("peek", "claim", "submit", "correct", "recover", "activate", "regenerate", "send",
        "status", "export", "hold", "import-decisions", "resume", "prepare-owner"))
    parser.add_argument("--kind", choices=("acquisition", "report"), default="report")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--target", help="run id for acquisition, occurrence date for reports")
    # Native local terminal injects the current ContextVar identity per command.
    # Never discover a session by selecting the latest row in state.db.
    session = os.environ.get("HERMES_SESSION_ID", "")
    parser.add_argument("--session-id", default=session or None)
    parser.add_argument("--execution-id", default=session or None)
    parser.add_argument("--reviewer", default="hermes-native:" + session.split("_")[1] if session.startswith("cron_") else None)
    parser.add_argument("--hermes-database", type=Path, default=Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / "state.db")
    parser.add_argument("--timeout-seconds", type=int, default=4500)
    parser.add_argument("--token")
    parser.add_argument("--result", type=Path)
    parser.add_argument("--reason")
    parser.add_argument("--queue-dir", type=Path, default=os.getenv("CLIMATE_INTAKE_QUEUE_DIR") or os.getenv("CLIMATE_PDF_INTAKE_QUEUE_DIR"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--item", action="append", help="exact Registry candidate SHA for an item handoff")
    parser.add_argument("--decision-id", help="exact imported human reply to resume")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--runtime-wiki-dir", type=Path)
    parser.add_argument("--send", action="store_true", help="Enable actual delivery after the separate production cutover")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_absolute() or root.is_relative_to(ROOT):
        parser.error("business state must be external to the checkout")
    if args.command in {"status", "export", "hold", "import-decisions", "resume", "prepare-owner"}:
        if args.kind != "acquisition":
            parser.error("item handoffs require --kind acquisition")
        from climate_registry import item_lifecycle as items
        database = args.database or os.getenv("CLIMATE_REGISTRY_DB")
        if not database:
            parser.error("item handoffs require an exact --database")
        selection = dict(candidate_sha256s=args.item, queue_dir=args.queue_dir, runtime_wiki_dir=args.runtime_wiki_dir)
        if args.command in {"status", "export"}:
            result = (items.item_status if args.command == "status" else items.export_items)(root, database, **selection)
        elif args.command == "resume":
            result = items.resume_items(root, database, decision_id=args.decision_id, backup_dir=args.backup_dir, **selection)
        else:
            if not args.result:
                parser.error("a precise --result JSON input is required")
            value = json.loads(args.result.read_text(encoding="utf-8"))
            if args.command == "import-decisions":
                result = items.import_decisions(root, database, value)
            elif args.command == "hold":
                result = items.record_dispute(root, database, **value)
            else:
                from climate_registry.owner_checks import prepare_owner_checks
                result = prepare_owner_checks(database, value, root=root)
        print(json.dumps(result, sort_keys=True))
        return 0
    verification_report = None
    def refresh_verification_report():
        if args.kind != "acquisition":
            return None
        from climate_registry.verification_report import write_verification_report
        database = args.database or os.getenv("CLIMATE_REGISTRY_DB")
        if not database:
            for path in [root / "registry-review/packet.json", *sorted(root.glob("*/acquisition-review/state.json"))]:
                if path.is_file():
                    value = json.loads(path.read_text())
                    database = value.get("registry_database") or value.get("packet", {}).get("registry_database")
                    if database:
                        break
        return write_verification_report(database, root) if database else {"status": "unavailable", "reason": "Registry is not configured"}
    if args.command == "peek":
        verification_report = refresh_verification_report()
        selected = pending(args.kind, root)
        resolved = {}
        if selected and not selected[1].get("preparation_required"):
            target_root, state = selected
            packet = state["packet"]
            if args.kind == "report":
                revision_root = target_root / "revisions" / f"{state['revision']:04d}"
                resolved = {"pdf": str((revision_root / packet["pdf_path"]).resolve()),
                    "source": str((revision_root / "report-source.json").resolve()),
                    "snapshot": str((target_root / "snapshot.json").resolve()),
                    "previous_source": str((target_root / "revisions" / f"{state['revision'] - 1:04d}" / "report-source.json").resolve()) if state["revision"] > 1 else None,
                    "text": (packet.get("readable_text") or {}).get("path") or str((revision_root / packet["text_path"]).resolve()),
                    "pages": [str((revision_root / page["path"]).resolve()) for page in packet["pages"]]}
            else:
                resolved = {"registry_snapshots": [c.get("registry_snapshot_path") or c.get("snapshot_path") for c in packet["candidates"] if c.get("registry_snapshot_path") or c.get("snapshot_path")]}
                if packet["schema_version"] != "registry-final-review.v1":
                    resolved["candidate_bodies"] = [(c.get("readable_text") or {}).get("path") or str((target_root / c["body_path"]).resolve()) for c in packet["candidates"]]
        print(json.dumps({"wakeAgent": bool(selected), "kind": args.kind, "verification_report": verification_report,
            **({"target": selected[1].get("occurrence") or selected[1]["packet"]["run_id"],
                **{key: selected[1][key] for key in ("preparation_required", "ready_candidate_sha256s") if key in selected[1]},
                "packet": selected[1]["packet"], "resolved_paths": resolved} if selected else {})}, sort_keys=True))
        return 0
    if args.command == "send" and not args.target:
        from datetime import datetime, timedelta, timezone
        results = []
        for state in reports.report_states(root):
            if state["status"] != "approved" or datetime.now(timezone.utc) < acquisition.timestamp(state["approval"]["approved_at"]) + timedelta(minutes=60):
                continue
            try:
                results.append(reports.send_approved(root, state["occurrence"], no_send=not args.send, config_path=args.config))
            except Exception as exc:
                results.append({"occurrence": state["occurrence"], "status": "blocked", "error": type(exc).__name__})
        print(json.dumps(results, sort_keys=True))
        return int(any(value.get("status") == "blocked" for value in results))
    if not args.target:
        parser.error("a precise --target is required")
    acquisition_root = root / args.target / "acquisition-review"
    registry_review = args.kind == "acquisition" and args.target == "registry-review"
    if registry_review:
        acquisition_root = root / "registry-review"
    if args.command == "regenerate":
        if args.kind != "report" or not args.reason:
            parser.error("report regeneration requires an explicit repair reason")
        result = reports.regenerate_report(root, args.target, reason=args.reason)
    elif args.command == "claim":
        if not all((args.session_id, args.execution_id, args.reviewer, args.hermes_database)):
            parser.error("native session/execution/reviewer/database identity is required")
        context = dict(session_id=args.session_id, execution_id=args.execution_id, reviewer=args.reviewer,
            hermes_database=args.hermes_database, timeout_seconds=args.timeout_seconds)
        if registry_review:
            from climate_registry.publication import claim_registry_review
            result = claim_registry_review(acquisition_root, **context)
        else:
            result = (acquisition.claim_acquisition(acquisition_root, **context) if args.kind == "acquisition"
            else reports.claim_report(root, args.target, **context))
    elif args.command == "submit":
        if not args.result or not args.token:
            parser.error("--result and --token are required")
        value = json.loads(args.result.read_text())
        if registry_review:
            from climate_registry.publication import review_candidates
            result = review_candidates(acquisition_root, args.token, value)
        else:
            result = (acquisition.review_acquisition(acquisition_root, args.token, value)
                if args.kind == "acquisition" else reports.submit_report_review(root, args.target, args.token, **value))
    elif args.command == "correct":
        if args.kind != "acquisition" or not args.result:
            parser.error("candidate corrections require acquisition --result")
        value = json.loads(args.result.read_text())
        result = acquisition.correct_candidate(acquisition_root, value["item_id"], value["changes"], reason=value["reason"],
            session_id=args.session_id, execution_id=args.execution_id, hermes_database=args.hermes_database)
    elif args.command == "recover":
        if args.kind != "acquisition" or not args.token:
            parser.error("recovery requires an acquisition review token")
        from climate_monitor.management import ManagementService
        result = acquisition.recover_acquisition(acquisition_root, args.token, ManagementService.from_environment())
    elif args.command == "activate":
        if args.kind != "acquisition" or not args.queue_dir:
            parser.error("activation requires the acquisition writer queue")
        if registry_review:
            from climate_registry.publication import review_activation_status
            result = review_activation_status(acquisition_root, database=args.database)
        else:
            database = args.database or Path(json.loads((acquisition_root / "state.json").read_text())["packet"]["registry_database"])
            result = acquisition.activate_approved(acquisition_root, queue_dir=args.queue_dir,
                database=database, repository_root=ROOT)
    else:
        result = reports.send_approved(root, args.target, no_send=not args.send, config_path=args.config)
    if args.command in {"submit", "activate"} and args.kind == "acquisition":
        result["verification_report"] = refresh_verification_report()
    print(json.dumps(result, sort_keys=True))
    if registry_review and args.command == "activate":
        return int(result["approved_count"] == 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
