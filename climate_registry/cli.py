from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .audit import build_audit_registry
from .errors import RegistryBuildError, RegistryInputError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="climate-registry")
    subcommands = parser.add_subparsers(dest="command", required=True)
    audit = subcommands.add_parser("audit-history")
    audit.add_argument("--source-dir", required=True, type=Path)
    audit.add_argument("--database", required=True, type=Path)
    audit.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = build_audit_registry(args.source_dir, args.database, args.output_dir)
        code = 0
    except RegistryInputError as exc:
        result, code = {"status": "error", "kind": "input", "message": str(exc)}, 2
    except RegistryBuildError as exc:
        result, code = {"status": "error", "kind": "build", "message": str(exc)}, 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return code
