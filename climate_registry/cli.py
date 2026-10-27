from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from climate_monitor.semantic_bundle import SemanticBundleError

from .audit import build_audit_registry
from .capture import MAX_BATCH, capture_enrich_registry
from .errors import RegistryBuildError, RegistryInputError, RegistryLockError
from .fetch import DEFAULT_TIMEOUT
from .persistent import initialize_registry, plan_registry_update, update_registry
from .semantic_import import import_report_semantics
from .selection import load_selection_input, plan_registry_selection
from .weekly import (
    WeeklyPartialError,
    WeeklyPreflightError,
    WeeklyValidationError,
    restore_registry_backup,
    weekly_sync,
)


WEEKLY_NO_OP_EXIT = 6
WEEKLY_PREFLIGHT_EXIT = 7
WEEKLY_VALIDATION_EXIT = 8


class _RegistryArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "required" in message:
            sanitized = "required CLI arguments are missing"
        elif "invalid choice" in message:
            sanitized = "CLI argument value is invalid"
        elif "unrecognized arguments" in message:
            sanitized = "CLI argument is not recognized"
        else:
            sanitized = "CLI arguments are invalid"
        raise RegistryInputError(sanitized)


def _parser() -> argparse.ArgumentParser:
    parser = _RegistryArgumentParser(prog="climate-registry")
    subcommands = parser.add_subparsers(
        dest="command", required=True, parser_class=_RegistryArgumentParser
    )
    audit = subcommands.add_parser("audit-history")
    audit.add_argument("--source-dir", required=True, type=Path)
    audit.add_argument("--database", required=True, type=Path)
    audit.add_argument("--output-dir", required=True, type=Path)

    plan = subcommands.add_parser("plan-update")
    plan.add_argument("--source-dir", required=True, type=Path)
    plan.add_argument("--database", required=True, type=Path)
    plan.add_argument(
        "--allow-offcycle",
        action="store_true",
        help="Allow non-Monday report dates (manual re-runs). Off by default.",
    )

    update = subcommands.add_parser("update")
    update.add_argument("--source-dir", required=True, type=Path)
    update.add_argument("--database", required=True, type=Path)
    update.add_argument(
        "--allow-offcycle",
        action="store_true",
        help="Allow non-Monday report dates (manual re-runs). Off by default.",
    )
    update.add_argument("--backup-dir", required=True, type=Path)

    selection = subcommands.add_parser("plan-selection")
    selection.add_argument("--database", required=True, type=Path)
    selection.add_argument("--source-dir", required=True, type=Path)
    selection.add_argument("--input", required=True, type=Path)
    selection.add_argument(
        "--allow-offcycle",
        action="store_true",
        help="Allow non-Monday report dates (manual re-runs). Off by default.",
    )

    capture = subcommands.add_parser("capture-enrich")
    capture.add_argument("--database", required=True, type=Path)
    capture.add_argument("--backup-dir", required=True, type=Path)
    capture.add_argument("--article-id", action="append", default=[])
    capture.add_argument("--limit", type=int)
    capture.add_argument("--refresh", action="store_true")

    weekly = subcommands.add_parser("weekly-sync")
    weekly.add_argument("--date", required=True)
    weekly.add_argument("--source-dir", required=True, type=Path)
    weekly.add_argument("--database", required=True, type=Path)
    weekly.add_argument("--artifact-root", required=True, type=Path)
    weekly.add_argument("--backup-dir", required=True, type=Path)
    weekly.add_argument("--lock-file", required=True, type=Path)
    weekly.add_argument("--publisher-ledger-dir", required=True, type=Path)
    weekly.add_argument("--metadata-dir", type=Path)
    weekly.add_argument("--expected-report-sha256")
    weekly.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    weekly.add_argument("--dry-run", action="store_true")
    weekly.add_argument(
        "--allow-offcycle",
        action="store_true",
        help="Allow non-Monday target dates (manual re-runs). Off by default.",
    )

    semantic = subcommands.add_parser("semantic-import")
    semantic.add_argument("--report", required=True, type=Path)
    semantic.add_argument("--expected-report-sha256", required=True)
    semantic.add_argument("--database", type=Path)
    semantic.add_argument("--backup-dir", type=Path)
    semantic.add_argument("--apply", action="store_true")

    categories = subcommands.add_parser("correct-categories")
    categories.add_argument("--database", required=True, type=Path)
    categories.add_argument("--manifest", required=True, type=Path)
    categories.add_argument("--backup-dir", type=Path)
    categories.add_argument("--apply", action="store_true")

    restore = subcommands.add_parser("restore-backup")
    restore.add_argument("--database", required=True, type=Path)
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--expected-sha256", required=True)
    restore.add_argument("--backup-dir", required=True, type=Path)
    restore.add_argument("--lock-file", required=True, type=Path)

    init = subcommands.add_parser("init")
    init.add_argument("--database", required=True, type=Path)
    migrate = subcommands.add_parser("migrate-publication")
    migrate.add_argument("--database", type=Path)
    migrate.add_argument("--backup-dir", required=True, type=Path)
    migrate.add_argument("--apply", action="store_true")
    visibility = subcommands.add_parser("set-visibility")
    visibility.add_argument("--database", type=Path)
    visibility.add_argument("--kind", required=True, choices=["article", "pdf_article", "meeting", "pdf_meeting"])
    visibility.add_argument("--id", required=True)
    visibility.add_argument("--is-visible", required=True, choices=["true", "false"])
    export = subcommands.add_parser("export-public-snapshot")
    export.add_argument("--database", type=Path)
    export.add_argument("--output", type=Path, required=True)
    adoption = subcommands.add_parser("adopt-pdf-summary")
    adoption.add_argument("--database", type=Path)
    adoption.add_argument("--document-sha256", required=True)
    adoption.add_argument("--expected-summary-sha256", required=True)
    adoption.add_argument("--actor", required=True)
    adoption.add_argument("--reason", required=True)
    adoption.add_argument("--backup-dir", required=True, type=Path)
    adoption.add_argument("--apply", action="store_true")
    for name in ("plan-owner-check", "plan-owner-review", "ingest-owner-check", "approve-owner-check"):
        owner = subcommands.add_parser(name)
        owner.add_argument("--database", required=True, type=Path)
        if name == "plan-owner-check":
            owner.add_argument("--kind", required=True, choices=["article", "pdf_article", "meeting", "pdf_meeting"])
            owner.add_argument("--id", required=True)
            owner.add_argument("--identity-correction", action="store_true",
                help="Plan an exact semantic identity, URL, disposition and body/event relation correction")
            owner.add_argument("--identity-participant", action="append", default=[], metavar="KIND:ID",
                help="Freeze an alias or Dates participant in the identity scope; repeat as needed")
            owner.add_argument("--with-editorial-metadata", action="store_true",
                help="Plan an explicitly versioned v2 article metadata scope")
            owner.add_argument("--publication-date-only", action="store_true",
                help="Inherit approved article facts and independently review only a source publication date")
            owner.add_argument("--date-field", action="append", choices=["publication_date", "updated_date", "revision_date"], default=[],
                help="Inherit approved article facts and selectively review one date field; repeat for multiple fields")
            owner.add_argument("--activity-dates-only", action="store_true",
                help="Inherit approved article facts and independently review source-backed activity dates")
            owner.add_argument("--human-date-override", action="store_true",
                help="Use explicit human date authorization with --date-field or the dedicated --activity-dates-only scope")
        elif name == "plan-owner-review":
            owner.add_argument("--candidate-sha256", required=True)
        else:
            owner.add_argument("--input", required=True, type=Path)
            owner.add_argument("--queue-dir", required=True, type=Path)
            if name == "approve-owner-check":
                owner.add_argument("--lifecycle-root", type=Path,
                    help="Exact external lifecycle root from prepare-owner; required for source-fact approval")
            owner.add_argument("--backup-dir", type=Path)
            owner.add_argument("--apply", action="store_true")
    date_inventory = subcommands.add_parser("inventory-date-overlays")
    date_inventory.add_argument("--database", required=True, type=Path)
    subcommands.add_parser("enrichment-capabilities", help="Read the versioned source/entity/scope capability matrix")
    enrichment = subcommands.add_parser("inspect-enrichment-contract", help="Read the exact frozen candidate contract and publication references")
    enrichment.add_argument("--database", required=True, type=Path)
    enrichment.add_argument("--candidate-sha256", required=True)
    validation = subcommands.add_parser("validate-enrichment-contract", help="Validate a declared scope on an existing candidate snapshot without staging or writing")
    validation.add_argument("--database", required=True, type=Path)
    validation.add_argument("--input", required=True, type=Path)
    validation.add_argument("--scope", required=True, choices=["complete_article_content_update", "initial_article_content"])
    acceptance = subcommands.add_parser("accept-publication", help="Read-only exact Registry and separate consumer-content acceptance")
    acceptance.add_argument("--database", required=True, type=Path)
    acceptance.add_argument("--selection", type=Path)
    acceptance.add_argument("--captures", type=Path)
    acceptance.add_argument("--runtime-dir", type=Path)
    acceptance.add_argument("--public-export", type=Path)
    acceptance.add_argument("--report", required=True, type=Path)
    return parser


def _weekly_failure(args: argparse.Namespace, *, kind: str, message: str) -> dict:
    return {
        "status": "failed",
        "kind": kind,
        "message": message,
        "date": args.date,
        "report_sha256": None,
        "dry_run": bool(args.dry_run),
        "reports_added": 0,
        "articles_added": 0,
        "articles_captured": 0,
        "articles_failed": 0,
        "articles_fallback": 0,
        "articles_unresolved": 0,
        "fallback_failure_classes": {},
        "promotion_with_fallback": False,
        "coverage_status": "blocked_unresolved",
        "would_add_reports": 0,
        "would_add_articles": 0,
        "would_capture_article_ids": [],
        "would_capture_count": 0,
        "would_fallback_count": 0,
        "would_promote": False,
        "promotion": "blocked",
        "reload_required": False,
        "database_sha256_before": None,
        "database_sha256_after": None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "accept-publication":
            from .acceptance import write_acceptance_report
            result = write_acceptance_report(args.database, args.report, selection_path=args.selection,
                captures_path=args.captures, runtime_dir=args.runtime_dir, public_export=args.public_export)
        elif args.command == "enrichment-capabilities":
            from .enrichment_contract import capability_matrix
            result = capability_matrix()
        elif args.command == "inspect-enrichment-contract":
            from .enrichment_contract import inspect_candidate
            try:
                result = inspect_candidate(args.database, args.candidate_sha256)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RegistryInputError(str(exc)) from exc
        elif args.command == "validate-enrichment-contract":
            from .enrichment_contract import validate_input
            try:
                result = validate_input(args.database, json.loads(args.input.read_text(encoding="utf-8")), args.scope)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RegistryInputError(str(exc)) from exc
        elif args.command == "inventory-date-overlays":
            from .owner_dates import inventory_date_overlays
            result = inventory_date_overlays(args.database)
        elif args.command in {"plan-owner-check", "plan-owner-review", "ingest-owner-check", "approve-owner-check"}:
            from .owner_checks import plan_owner_check, owner_review_template, ingest_owner_check, approve_owner_check
            try:
                if args.command == "plan-owner-check":
                    result = plan_owner_check(args.database, args.kind, args.id,
                        identity_correction=args.identity_correction, identity_participants=[value.split(":", 1) for value in args.identity_participant],
                        with_editorial_metadata=args.with_editorial_metadata,
                        publication_date_only=args.publication_date_only, date_fields=args.date_field,
                        activity_dates_only=args.activity_dates_only, human_date_override=args.human_date_override)
                elif args.command == "plan-owner-review":
                    result = owner_review_template(args.database, args.candidate_sha256)
                else:
                    operation = ingest_owner_check if args.command == "ingest-owner-check" else approve_owner_check
                    result = operation(args.database, json.loads(args.input.read_text(encoding="utf-8")),
                        **({"lifecycle_root": args.lifecycle_root} if args.command == "approve-owner-check" else {}),
                        queue_dir=args.queue_dir, backup_dir=args.backup_dir, apply=args.apply)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RegistryInputError(str(exc)) from exc
        elif args.command == "adopt-pdf-summary":
            from .pdf_summary import adopt_pdf_summary
            result = adopt_pdf_summary(args.database, args.document_sha256, args.expected_summary_sha256,
                actor=args.actor, reason=args.reason, backup_dir=args.backup_dir, apply=args.apply)
        elif args.command == "migrate-publication":
            from .publication import migrate_publication
            result = migrate_publication(args.database, args.backup_dir, apply=args.apply)
        elif args.command == "set-visibility":
            from .publication import set_visibility
            set_visibility(args.database, args.kind, args.id, args.is_visible == "true")
            result = {"status": "updated", "is_visible": args.is_visible == "true"}
        elif args.command == "export-public-snapshot":
            from .publication import export_public_snapshot
            result = export_public_snapshot(args.database, args.output)
        elif args.command == "audit-history":
            result = build_audit_registry(args.source_dir, args.database, args.output_dir)
        elif args.command == "plan-update":
            result = plan_registry_update(
                args.source_dir, args.database, allow_offcycle=args.allow_offcycle
            )
        elif args.command == "update":
            result = update_registry(
                args.source_dir,
                args.database,
                args.backup_dir,
                allow_offcycle=args.allow_offcycle,
            )
        elif args.command == "plan-selection":
            payload = load_selection_input(
                args.input, allow_offcycle=args.allow_offcycle
            )
            result = plan_registry_selection(
                args.database,
                args.source_dir,
                payload,
                allow_offcycle=args.allow_offcycle,
            )
        elif args.command == "capture-enrich":
            if args.limit is not None and not 1 <= args.limit <= MAX_BATCH:
                raise RegistryInputError(f"limit must be between 1 and {MAX_BATCH}")
            result = capture_enrich_registry(
                args.database,
                args.backup_dir,
                article_ids=args.article_id,
                limit=args.limit,
                refresh=args.refresh,
            )
        elif args.command == "weekly-sync":
            result = weekly_sync(
                target_date=args.date,
                source_dir=args.source_dir,
                database=args.database,
                artifact_root=args.artifact_root,
                backup_dir=args.backup_dir,
                lock_file=args.lock_file,
                publisher_ledger_dir=args.publisher_ledger_dir,
                metadata_dir=args.metadata_dir,
                expected_report_sha256=args.expected_report_sha256,
                timeout=args.timeout,
                dry_run=args.dry_run,
                allow_offcycle=args.allow_offcycle,
            )
        elif args.command == "semantic-import":
            result = import_report_semantics(
                report_path=args.report,
                expected_report_sha256=args.expected_report_sha256,
                dry_run=not args.apply,
                database=args.database,
                backup_dir=args.backup_dir,
            )
        elif args.command == "correct-categories":
            from .category_correction import correct_article_categories
            result = correct_article_categories(database=args.database, manifest=args.manifest,
                apply=args.apply, backup_dir=args.backup_dir)
        elif args.command == "init":
            result = initialize_registry(args.database)
        else:
            result = restore_registry_backup(
                database=args.database,
                backup=args.backup,
                expected_sha256=args.expected_sha256,
                backup_dir=args.backup_dir,
                lock_file=args.lock_file,
            )
        if args.command == "accept-publication":
            code = 0 if result.get("status") == "pass" else 1
        elif args.command == "weekly-sync" and result.get("status") == "no-op":
            code = WEEKLY_NO_OP_EXIT
        else:
            code = 5 if result.get("status") == "partial" else 0
    except WeeklyPreflightError as exc:
        if args.command == "weekly-sync":
            result, code = _weekly_failure(
                args, kind="preflight", message=str(exc)
            ), WEEKLY_PREFLIGHT_EXIT
        else:
            result, code = {
                "status": "failed",
                "kind": "preflight",
                "message": str(exc),
                "promotion": "blocked",
                "reload_required": False,
            }, 2
    except WeeklyPartialError as exc:
        result = exc.result or _weekly_failure(
            args, kind="partial", message=str(exc)
        )
        result.update(kind="partial", message=str(exc))
        code = 5
    except WeeklyValidationError as exc:
        if args.command == "weekly-sync":
            result, code = _weekly_failure(
                args, kind="validation", message=str(exc)
            ), WEEKLY_VALIDATION_EXIT
        else:
            result, code = {
                "status": "failed",
                "kind": "validation",
                "message": str(exc),
                "promotion": "blocked",
                "reload_required": False,
            }, 3
    except RegistryInputError as exc:
        result, code = {"status": "error", "kind": "input", "message": str(exc)}, 2
    except SemanticBundleError as exc:
        result, code = {
            "status": "error",
            "kind": "semantic_bundle",
            "message": str(exc),
        }, 2
    except RegistryBuildError as exc:
        result, code = {"status": "error", "kind": "build", "message": str(exc)}, 3
    except RegistryLockError as exc:
        if "args" in locals() and args.command == "weekly-sync":
            result = _weekly_failure(args, kind="lock", message=str(exc))
        else:
            result = {"status": "error", "kind": "lock", "message": str(exc)}
        code = 4
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return code
