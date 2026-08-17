from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .audit import build_audit_registry
from .capture import MAX_BATCH, capture_enrich_registry
from .errors import RegistryBuildError, RegistryInputError, RegistryLockError
from .persistent import plan_registry_update, update_registry
from .selection import load_selection_input, plan_registry_selection


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
    return parser


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
        else:
            if args.limit is not None and not 1 <= args.limit <= MAX_BATCH:
                raise RegistryInputError(f"limit must be between 1 and {MAX_BATCH}")
            result = capture_enrich_registry(
                args.database,
                args.backup_dir,
                article_ids=args.article_id,
                limit=args.limit,
                refresh=args.refresh,
            )
        code = 5 if result.get("status") == "partial" else 0
    except RegistryInputError as exc:
        result, code = {"status": "error", "kind": "input", "message": str(exc)}, 2
    except RegistryBuildError as exc:
        result, code = {"status": "error", "kind": "build", "message": str(exc)}, 3
    except RegistryLockError as exc:
        result, code = {"status": "error", "kind": "lock", "message": str(exc)}, 4
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return code
