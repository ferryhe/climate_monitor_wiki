from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from climate_monitor.run_ledger import (
    LedgerConflictError,
    LedgerContractError,
    LedgerLocationError,
    LedgerUnavailableError,
    MAX_ATTEMPT_BYTES,
    append_attempt,
    decode_attempt_json,
    read_bounded_file,
)


ROOT = Path(__file__).resolve().parents[1]


def _error(reason: str) -> int:
    print(json.dumps({"status": "error", "reason": reason}), file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Append one sanitized weekly run attempt.")
    parser.add_argument("--ledger-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True, help="Attempt JSON file.")
    args = parser.parse_args()
    try:
        payload = decode_attempt_json(
            read_bounded_file(args.input, max_bytes=MAX_ATTEMPT_BYTES)
        )
        result = append_attempt(args.ledger_dir, payload, repository_root=ROOT)
    except (json.JSONDecodeError, UnicodeDecodeError, LedgerContractError):
        return _error("invalid_attempt")
    except LedgerLocationError:
        return _error("invalid_location")
    except LedgerConflictError:
        return _error("attempt_conflict")
    except (OSError, LedgerUnavailableError):
        return _error("ledger_unavailable")
    print(
        json.dumps(
            {"attempt_id": payload["attempt_id"], "status": result},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
