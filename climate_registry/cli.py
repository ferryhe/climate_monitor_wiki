from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .audit import build_audit_registry
from .errors import RegistryBuildError, RegistryInputError, RegistryLockError
from .persistent import plan_registry_update, update_registry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="climate-registry")
    subcommands = parser.add_subparsers(dest="command", required=True)
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "audit-history":
            result = build_audit_registry(args.source_dir, args.database, args.output_dir)
        elif args.command == "plan-update":
            result = plan_registry_update(args.source_dir, args.database)
        else:
            result = update_registry(args.source_dir, args.database, args.backup_dir)
        code = 0
    except RegistryInputError as exc:
        result, code = {"status": "error", "kind": "input", "message": str(exc)}, 2
    except RegistryBuildError as exc:
        result, code = {"status": "error", "kind": "build", "message": str(exc)}, 3
    except RegistryLockError as exc:
        result, code = {"status": "error", "kind": "lock", "message": str(exc)}, 4
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return code
