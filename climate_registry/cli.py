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

    update = subcommands.add_parser("update")
    update.add_argument("--source-dir", required=True, type=Path)
    update.add_argument("--database", required=True, type=Path)
    update.add_argument("--backup-dir", required=True, type=Path)

    selection = subcommands.add_parser("plan-selection")
    selection.add_argument("--database", required=True, type=Path)
    selection.add_argument("--source-dir", required=True, type=Path)
    selection.add_argument("--input", required=True, type=Path)

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

    semantic = subcommands.add_parser("semantic-import")
    semantic.add_argument("--report", required=True, type=Path)
    semantic.add_argument("--expected-report-sha256", required=True)
    semantic.add_argument("--database", type=Path)
    semantic.add_argument("--backup-dir", type=Path)
    semantic.add_argument("--apply", action="store_true")

    restore = subcommands.add_parser("restore-backup")
    restore.add_argument("--database", required=True, type=Path)
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--expected-sha256", required=True)
    restore.add_argument("--backup-dir", required=True, type=Path)
    restore.add_argument("--lock-file", required=True, type=Path)

    init = subcommands.add_parser("init")
    init.add_argument("--database", required=True, type=Path)
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
        if args.command == "audit-history":
            result = build_audit_registry(args.source_dir, args.database, args.output_dir)
        elif args.command == "plan-update":
            result = plan_registry_update(args.source_dir, args.database)
        elif args.command == "update":
            result = update_registry(args.source_dir, args.database, args.backup_dir)
        elif args.command == "plan-selection":
            payload = load_selection_input(args.input)
            result = plan_registry_selection(args.database, args.source_dir, payload)
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
            )
        elif args.command == "semantic-import":
            result = import_report_semantics(
                report_path=args.report,
                expected_report_sha256=args.expected_report_sha256,
                dry_run=not args.apply,
                database=args.database,
                backup_dir=args.backup_dir,
            )
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
        if args.command == "weekly-sync" and result.get("status") == "no-op":
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
