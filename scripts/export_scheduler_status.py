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


def business_status(*, report_root=None, acquisition_root=None, checks_root=None):
    from climate_delivery.report_review import report_states
    def observed(path):
        if not path or not path.is_file():
            return {"status": "not_configured"}
        value = json.loads(path.read_text())
        return {**value, "observed_at": schedule.stamp(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))}
    def run_order(binding, receipt):
        value = receipt.get("finished_at") or binding.get("created_at")
        stamp = schedule.stamp(datetime.fromisoformat(value.replace("Z", "+00:00"))) if value else ""
        occurrence = (binding.get("rotation") or {}).get("occurrence") or binding.get("report_date") or stamp[:10]
        return occurrence, stamp
    result = {"T1": {kind: observed(Path(checks_root) / f"{kind}-daily-check.json" if checks_root else None)
        for kind in ("articles", "meetings")}}
    for role, kind in (("T2", "website_rotation"), ("T3", "weekly_search")):
        runs = []
        for path in sorted(Path(acquisition_root).glob("*/binding.json")) if acquisition_root else []:
            binding = json.loads(path.read_text())
            if binding.get("acquisition_kind") != kind:
                continue
            attempts = sorted(path.parent.glob("attempt-*-result.json"), key=lambda p: int(p.name.split("-")[1]))
            receipt = observed(attempts[-1]) if attempts else {"status": "running_or_unknown"}
            runs.append((run_order(binding, receipt), {"run_id": binding["run_id"], "source_keys": binding["source_keys"], "rotation": binding.get("rotation"),
                "outcome": receipt.get("outcome"), "execution_complete": receipt.get("execution_complete"), "observed_at": receipt.get("observed_at")}))
        result[role] = {"status": "observed" if runs else "not_configured", "runs": [row for _, row in sorted(runs, key=lambda pair: pair[0])[-3:]]}
    reviews = []
    from climate_registry.acquisition_review import acquisition_state
    for path in sorted(Path(acquisition_root).glob("*/acquisition-review/state.json")) if acquisition_root else []:
        state = acquisition_state(path.parent)
        binding_path = path.parent.parent / "binding.json"
        binding = json.loads(binding_path.read_text()) if binding_path.is_file() else state["packet"]
        attempts = sorted(path.parent.parent.glob("attempt-*-result.json"), key=lambda p: int(p.name.split("-")[1]))
        receipt = observed(attempts[-1]) if attempts else {}
        reviews.append((run_order(binding, receipt), {"run_id": state["packet"]["run_id"], "status": state["status"],
            "packet_sha256": state["packet_sha256"], "item_statuses": {k: v["status"] for k, v in state["item_reviews"].items()},
            "source_statuses": {k: v["status"] for k, v in state.get("sources", {}).items()},
            "proposal_count": len(state.get("proposals", [])),
            "claim": observed(path.parent / "claim.json"),
            "claim_failures": [json.loads(p.read_text()) for p in sorted((path.parent / "claim-history").glob("*.json"))],
            "activation": observed(path.parent / "activation.json"), "writer_activations": state["writer_activations"]}))
    result["T10"] = {"status": "observed" if reviews else "not_configured", "reviews": [row for _, row in sorted(reviews, key=lambda pair: pair[0])[-3:]]}
    states = report_states(Path(report_root)) if report_root else []
    summaries = [{"occurrence": state["occurrence"], "revision": state["revision"], "status": state["status"],
        "snapshot_sha256": state["snapshot_sha256"], "approved_at": state.get("approval", {}).get("approved_at"),
        "claim_state": state.get("claim_state"),
        "updated_at": state.get("updated_at")} for state in states[:3]]
    result["T4"] = {"status": "observed" if states else "not_configured", "reports": summaries}
    result["T5"] = {"status": "observed" if states else "not_configured", "reviews": summaries}
    result["T6"] = {"status": "observed" if states else "not_configured", "delivery": [
        {"occurrence": state["occurrence"], **observed(Path(report_root) / "reports" / state["occurrence"] / "delivery.json"),
         "no_send": observed(Path(report_root) / "reports" / state["occurrence"] / "no-send.json")} for state in states[:3]]}
    for item, state in zip(result["T6"]["delivery"], states[:3]):
        item["recipients"] = [{"report_sha256": receipt.get("report_sha256"),
            "statuses": {key: value["status"] for key, value in receipt.get("recipients", {}).items()}}
            for receipt in state.get("recipient_receipts", [])]
    return result


def project_independent(connection, job_ids, *, now, business, definitions):
    if set(job_ids) != set(schedule.PIPELINE_SLOTS) or len(set(job_ids.values())) != 10:
        raise ValueError("ten distinct real Hermes job IDs are required")
    actual = {str(item.get("id") or item.get("job_id")): item for item in definitions}
    jobs = {}
    for role, job_id in job_ids.items():
        if job_id not in actual:
            raise ValueError("configured job ID was not observed in Hermes definitions")
        occurrence = schedule.pipeline_occurrence(role, now)
        row = connection.execute("SELECT status,claimed_at,started_at,finished_at FROM executions WHERE job_id=? "
            "AND julianday(claimed_at)>=julianday(?) AND julianday(claimed_at)<julianday(?) ORDER BY claimed_at DESC LIMIT 1",
            (job_id, occurrence.isoformat(), (occurrence + timedelta(minutes=5)).isoformat())).fetchone()
        state = "scheduled" if now < occurrence + timedelta(minutes=5) else "not_dispatched"
        block = {"job_id": job_id, "scheduled_for": schedule.stamp(occurrence), "state": state,
            "definition": {key: actual[job_id].get(key) for key in ("enabled", "no_agent", "next_run_at", "schedule")}}
        if row:
            block["state"] = {"claimed": "running", "running": "running", "completed": "completed", "failed": "failed"}.get(row[0], "unknown")
            for field, value in zip(("claimed_at", "started_at", "finished_at"), row[1:]):
                if value:
                    block[field] = schedule.stamp(datetime.fromisoformat(value))
        jobs[role] = block
    return validate_snapshot({"schema_version": schedule.PIPELINE_SCHEMA, "generated_at": schedule.stamp(now),
        "jobs": jobs, "business": business}, now=now)


def export_snapshot(executions_db, jobs_map, status_dir, *, clock=None, jobs_file=None,
    report_root=None, acquisition_root=None, checks_root=None):
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
            job_ids = json.loads(Path(jobs_map).read_text())
            if set(job_ids) == set(schedule.PIPELINE_SLOTS):
                if not jobs_file:
                    raise ValueError("independent observer requires the actual Hermes jobs file")
                definitions = json.loads(Path(jobs_file).read_text())
                if isinstance(definitions, dict):
                    definitions = definitions["jobs"]
                payload = project_independent(db, job_ids, now=now, definitions=definitions,
                    business=business_status(report_root=report_root, acquisition_root=acquisition_root, checks_root=checks_root))
            else:
                payload = project(db, job_ids, now=now, previous=previous)
        _atomic_write(snapshot, payload)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executions-db", type=Path, required=True)
    parser.add_argument("--jobs-map", type=Path, required=True)
    parser.add_argument("--status-dir", type=Path, required=True)
    parser.add_argument("--jobs-file", type=Path)
    parser.add_argument("--report-root", type=Path)
    parser.add_argument("--acquisition-root", type=Path)
    parser.add_argument("--checks-root", type=Path)
    args = parser.parse_args()
    export_snapshot(args.executions_db, args.jobs_map, args.status_dir, jobs_file=args.jobs_file,
        report_root=args.report_root, acquisition_root=args.acquisition_root, checks_root=args.checks_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
