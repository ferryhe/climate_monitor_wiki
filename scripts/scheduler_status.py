#!/usr/bin/env python3
"""CLI wrapper around ``climate_monitor.scheduler_status.update_slot``.

The Hermеs cron wrappers in ``scripts/hermes_job_*.sh`` shell out to this
script after each runnable step so ``GET /api/job-status`` reflects the
real scheduler state instead of fabricating a value.

The script intentionally does not own any secrets, does not push to git,
and does not reload the API server.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.scheduler_status import (
    SchedulerStatusError,
    update_slot,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update one slot of the weekly job-status snapshot.",
    )
    parser.add_argument(
        "--name",
        required=True,
        choices=("monitor", "email", "publisher", "registry"),
        help="Job slot to update.",
    )
    parser.add_argument(
        "--state",
        required=True,
        choices=("scheduled", "running", "completed", "failed",
                 "unknown", "not_dispatched"),
        help="Slot state.",
    )
    parser.add_argument(
        "--scheduled-for",
        default="",
        help="Strict UTC ISO-8601 Monday timestamp matching the slot schedule.",
    )
    parser.add_argument(
        "--claimed-at",
        default="",
        help="Strict UTC ISO-8601 timestamp the slot was claimed by a runner.",
    )
    parser.add_argument(
        "--started-at",
        default="",
        help="Strict UTC ISO-8601 timestamp the slot's work began.",
    )
    parser.add_argument(
        "--finished-at",
        default="",
        help="Strict UTC ISO-8601 timestamp the slot's work ended.",
    )
    parser.add_argument(
        "--result-code",
        default="",
        help="Optional structured failure token (alnum/_-, up to 64 chars).",
    )
    parser.add_argument(
        "--error-reason",
        default="",
        help="Optional free-form reason; never written to disk.",
    )
    parser.add_argument(
        "--status-dir",
        default="",
        help="Override CLIMATE_JOB_STATUS_DIR for this call.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        target = update_slot(
            args.name,
            args.state,
            scheduled_for=args.scheduled_for,
            claimed_at=args.claimed_at or None,
            started_at=args.started_at or None,
            finished_at=args.finished_at or None,
            result_code=args.result_code or None,
            error_reason=args.error_reason or None,
            status_dir=args.status_dir or None,
        )
    except SchedulerStatusError as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"status": "ok", "path": str(target)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())