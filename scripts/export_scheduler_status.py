"""Project actual Hermes executions into the public ET schedule snapshot."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from climate_monitor import schedule
from climate_monitor.job_status import JobStatusInvalidSnapshotError, validate_snapshot
from climate_monitor.scheduler_status import _atomic_write, snapshot_transaction


_DOMAIN_COMPLETION_CODES = {"no_eligible_information", "completed_with_gaps"}
_REGISTRY_PENDING_CODES = {
    "awaiting_human_merge_deploy", "registry_disabled",
    "registry_write_not_authorized",
}
_DELIVERY_NO_SEND_CODE = "delivery_no_send"


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _merge_observation(alias, observed, previous_block, *, previous_generated):
    """Keep the newest same-occurrence execution claim while producers converge."""
    if not isinstance(previous_block, dict) or (
        previous_block.get("scheduled_for") != observed.get("scheduled_for")
    ):
        return observed
    application_disposition = (
        previous_block.get("state") == "not_dispatched"
        and (
            (alias == "registry"
             and previous_block.get("result_code") in _REGISTRY_PENDING_CODES)
            or (alias == "email"
                and previous_block.get("result_code") == _DELIVERY_NO_SEND_CODE)
        )
    )
    observed_claim = _instant(observed.get("claimed_at"))
    prior_claim = _instant(previous_block.get("claimed_at"))
    if application_disposition and prior_claim is None:
        generated = _instant(previous_generated)
        return previous_block if observed_claim is None or (
            generated is not None and observed_claim <= generated
        ) else observed
    if observed_claim is not None and prior_claim is not None and observed_claim > prior_claim:
        return observed
    if observed_claim is not None and prior_claim is None:
        return observed
    if prior_claim is not None and (
        observed_claim is None or prior_claim > observed_claim
    ):
        return previous_block
    if application_disposition:
        return previous_block
    if observed.get("state") == "completed" and observed_claim == prior_claim:
        merged = dict(observed)
        if previous_block.get("result_code") in _DOMAIN_COMPLETION_CODES:
            merged["result_code"] = previous_block["result_code"]
        return merged
    if observed_claim == prior_claim:
        terminal_states = {"completed", "failed"}
        observed_terminal = observed.get("state") in terminal_states
        previous_terminal = previous_block.get("state") in terminal_states
        if observed_terminal and not previous_terminal:
            return observed
        if previous_terminal and not observed_terminal:
            return previous_block
        if observed_terminal and previous_terminal:
            observed_finished = _instant(observed.get("finished_at"))
            previous_finished = _instant(previous_block.get("finished_at"))
            if observed_finished is not None and (
                previous_finished is None or observed_finished > previous_finished
            ):
                return observed
            return previous_block
    return observed


def project(connection, job_ids, *, now, previous=None):
    if set(job_ids) != set(schedule.SLOTS) or len(set(job_ids.values())) != 4:
        raise ValueError("four distinct Hermes job IDs are required")
    day = schedule.period_date(now, eastern=True)
    jobs = {}
    for alias, job_id in job_ids.items():
        occurrence = schedule.occurrence(day, alias, eastern=True)
        rows = connection.execute(
            "SELECT status, claimed_at, started_at, finished_at FROM executions "
            "WHERE job_id=? AND julianday(claimed_at)>=julianday(?) "
            "AND julianday(claimed_at)<julianday(?) ORDER BY claimed_at DESC",
            (job_id, occurrence.isoformat(), (occurrence + timedelta(minutes=5)).isoformat()),
        ).fetchall()
        block = {"scheduled_for": schedule.stamp(occurrence), "state": "scheduled"}
        if not rows:
            if now >= occurrence + timedelta(minutes=5):
                block.update(state="not_dispatched", result_code="not_dispatched")
        else:
            state, claimed, started, finished = rows[0]
            for field, value in (("claimed_at", claimed), ("started_at", started), ("finished_at", finished)):
                if value:
                    block[field] = schedule.stamp(datetime.fromisoformat(value))
            if state in {"claimed", "running"}:
                block["state"] = "running"
            elif state == "completed":
                block["state"] = "completed"
            elif state == "failed":
                block.update(state="failed", result_code="execution_failed")
            else:
                block.update(state="unknown", result_code="execution_unknown")
        previous_block = (previous or {}).get("jobs", {}).get(alias, {})
        jobs[alias] = _merge_observation(
            alias, block, previous_block,
            previous_generated=(previous or {}).get("generated_at"),
        )
    return validate_snapshot({"schema_version": schedule.SCHEMA,
                              "generated_at": schedule.stamp(now), "jobs": jobs}, now=now)


def export_snapshot(executions_db, jobs_map, status_dir, *, clock=None):
    target = Path(status_dir).resolve()
    if target.is_relative_to(ROOT):
        raise ValueError("status output must be outside the production checkout")
    target.mkdir(parents=True, exist_ok=True)
    with snapshot_transaction(target) as snapshot:
        now = (clock or (lambda: datetime.now(timezone.utc)))()
        previous = None
        try:
            previous = validate_snapshot(json.loads(snapshot.read_text()), now=now)
        except (OSError, ValueError, JobStatusInvalidSnapshotError):
            pass
        with sqlite3.connect(Path(executions_db).resolve().as_uri() + "?mode=ro", uri=True) as db:
            payload = project(
                db, json.loads(Path(jobs_map).read_text()), now=now, previous=previous,
            )
        _atomic_write(snapshot, payload)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executions-db", type=Path, required=True)
    parser.add_argument("--jobs-map", type=Path, required=True)
    parser.add_argument("--status-dir", type=Path, required=True)
    args = parser.parse_args()
    export_snapshot(args.executions_db, args.jobs_map, args.status_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
