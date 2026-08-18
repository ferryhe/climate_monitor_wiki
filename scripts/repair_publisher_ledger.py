#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.ledger_repair import (  # noqa: E402
    RepairLockConflict,
    RepairPreflightError,
    RepairValidationError,
    repair_publisher_ledger,
)
from climate_monitor.run_ledger import LedgerError  # noqa: E402


def _error(status: str, message: str, exit_code: int) -> int:
    print(
        json.dumps(
            {"status": status, "message": message, "exit_code": exit_code},
            sort_keys=True,
        )
    )
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate or repair one exact legacy Publisher ledger attempt."
    )
    parser.add_argument("--date", required=True, help="Exact Monday in YYYY-MM-DD form.")
    parser.add_argument("--ledger-dir", required=True, type=Path)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--registry-database", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument(
        "--lock-file",
        required=True,
        type=Path,
        help="The same flock path used by the 10:00 Publisher wrapper.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Append the repair overlay; the default is a read-only dry-run.",
    )
    args = parser.parse_args()
    try:
        result = repair_publisher_ledger(
            target_date=args.date,
            source_dir=args.source_dir,
            database=args.registry_database,
            artifact_root=args.artifact_root,
            ledger_dir=args.ledger_dir,
            lock_file=args.lock_file,
            apply=args.apply,
        )
    except RepairLockConflict:
        return _error("lock_conflict", "Publisher lock is held", 4)
    except RepairPreflightError as exc:
        return _error("preflight_failed", str(exc), 7)
    except (RepairValidationError, LedgerError, OSError):
        return _error("validation_failed", "repair validation failed", 8)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
