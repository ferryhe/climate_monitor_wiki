"""Current eleven-role observations retain exact historical schedule meaning."""
from datetime import datetime, timezone
import json
import os
import sqlite3
import sys

import pytest

from climate_monitor import schedule
from climate_monitor.job_status import validate_snapshot
from scripts.export_scheduler_status import project_independent


def test_five_minute_information_hourly_publisher_and_historical_ten_roles():
    now = datetime(2026, 10, 17, 16, 27, tzinfo=timezone.utc)
    assert schedule.stamp(schedule.pipeline_occurrence("T1", now)) == "2026-10-17T16:25:00Z"
    assert schedule.stamp(schedule.pipeline_occurrence("T11", now)) == "2026-10-17T16:00:00Z"
    assert schedule.pipeline_due("T1", now)
    assert not schedule.pipeline_due("T11", now)
    assert schedule.pipeline_due("T11", now.replace(minute=2))
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE executions(job_id,status,claimed_at,started_at,finished_at)")
    for historical, slots in ((False, schedule.PIPELINE_SLOTS), (True, schedule.LEGACY_PIPELINE_SLOTS)):
        identities = {role: "actual-" + role for role in slots}
        snapshot = project_independent(db, identities, now=now, business={},
            definitions=[{"id": identity, "enabled": False} for identity in identities.values()])
        assert validate_snapshot(snapshot, now=now) == snapshot
        assert snapshot["schema_version"] == (schedule.LEGACY_PIPELINE_SCHEMA if historical else schedule.PIPELINE_SCHEMA)
        assert len(snapshot["jobs"]) == (10 if historical else 11)
        expected_t1 = "2026-10-17T09:00:00Z" if historical else "2026-10-17T16:25:00Z"
        assert snapshot["jobs"]["T1"]["scheduled_for"] == expected_t1


def _profile_inputs(tmp_path):
    now = datetime(2026, 10, 17, 16, 27, tzinfo=timezone.utc)
    identities = {role: "actual-" + role for role in schedule.PIPELINE_SLOTS}
    definitions = [[{"id": identity, "enabled": True, "no_agent": role not in {"T5", "T10"}}
        for role, identity in identities.items() if (role in {"T5", "T10"}) == review]
        for review in (False, True)]
    definitions[0].append({"id": "host-health-outside-application", "enabled": True})
    databases, job_files = [], []
    for index, observed in enumerate(definitions):
        database = tmp_path / f"profile-{index}.sqlite3"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE executions(execution_id,job_id,status,claimed_at,started_at,finished_at)")
            for item in observed:
                role = next((role for role, identity in identities.items() if identity == item["id"]), None)
                if role is None: continue
                claimed = schedule.stamp(schedule.pipeline_occurrence(role, now))
                db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?)",
                    ("execution-" + role, item["id"], "failed" if role == "T5" else "completed", claimed, claimed, claimed))
        path = tmp_path / f"profile-{index}-jobs.json"
        path.write_text(json.dumps({"jobs": observed} if index == 0 else observed))
        databases.append(database); job_files.append(path)
    mapping = tmp_path / "map.json"; mapping.write_text(json.dumps(identities))
    return now, databases, job_files, mapping


@pytest.mark.skipif(os.name == "nt", reason="snapshot publication uses POSIX locks")
def test_exporter_combines_two_real_profiles_read_only_and_preserves_business_receipts(tmp_path, monkeypatch):
    from scripts import export_scheduler_status as exporter
    now, databases, job_files, mapping = _profile_inputs(tmp_path)
    checks = tmp_path / "checks"; checks.mkdir()
    (checks / "articles-daily-check.json").write_text(json.dumps({"status": "partial", "run_id": "actual-information-run"}))
    publisher = tmp_path / "publisher"; publisher.mkdir()
    (publisher / "public-snapshot.json").write_text(json.dumps({"status": "failed", "result_code": "publish_failed"}))
    before = [path.read_bytes() for path in databases]
    project = exporter.project_independent
    def inspect_connection(db, *args, **kwargs):
        assert {row[1] for row in db.execute("PRAGMA database_list")} >= {"profile0", "profile1"}
        db.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("INSERT INTO profile1.executions(job_id) VALUES('fabricated')")
        db.execute("PRAGMA query_only=ON")
        return project(db, *args, **kwargs)
    monkeypatch.setattr(exporter, "project_independent", inspect_connection)
    path = exporter.export_snapshot(databases, mapping, tmp_path / "status", jobs_file=job_files,
        clock=lambda: now, checks_root=checks, publisher_root=publisher)
    snapshot = validate_snapshot(json.loads(path.read_text()), now=now)
    assert len(snapshot["jobs"]) == 11
    assert snapshot["jobs"]["T5"]["state"] == "failed"
    assert snapshot["jobs"]["T10"]["state"] == "completed"
    assert snapshot["jobs"]["T1"]["state"] == "completed"
    assert snapshot["business"]["T1"]["articles"]["run_id"] == "actual-information-run"
    assert snapshot["business"]["T11"]["status"] == "failed"
    assert [path.read_bytes() for path in databases] == before


@pytest.mark.skipif(os.name == "nt", reason="snapshot publication uses POSIX locks")
@pytest.mark.parametrize("failure", ["missing_database", "missing_jobs", "duplicate_database", "duplicate_jobs_file",
    "unpaired_jobs", "duplicate_job_id", "conflicting_id", "missing_mapping_id", "invalid_schema", "legacy_multiple"])
def test_exporter_invalid_profile_inputs_preserve_last_snapshot(tmp_path, failure):
    from scripts import export_scheduler_status as exporter
    now, databases, job_files, mapping = _profile_inputs(tmp_path)
    path = exporter.export_snapshot(databases, mapping, tmp_path / "status", jobs_file=job_files, clock=lambda: now)
    previous = path.read_bytes()
    if failure == "missing_database": databases[1] = tmp_path / "missing.sqlite3"
    elif failure == "missing_jobs": job_files[1] = tmp_path / "missing-jobs.json"
    elif failure == "duplicate_database": databases[1] = databases[0]
    elif failure == "duplicate_jobs_file": job_files[1] = job_files[0]
    elif failure == "unpaired_jobs": job_files.pop()
    elif failure == "duplicate_job_id":
        jobs = json.loads(job_files[1].read_text()); jobs.append(json.loads(job_files[0].read_text())["jobs"][0])
        job_files[1].write_text(json.dumps(jobs))
    elif failure == "conflicting_id":
        jobs = json.loads(job_files[1].read_text()); jobs[0]["job_id"] = "different-id"
        job_files[1].write_text(json.dumps(jobs))
    elif failure == "missing_mapping_id": job_files[1].write_text("[]")
    elif failure == "invalid_schema":
        with sqlite3.connect(databases[1]) as db: db.execute("DROP TABLE executions")
    else:
        identities = json.loads(mapping.read_text()); identities.pop("T11"); mapping.write_text(json.dumps(identities))
    with pytest.raises((ValueError, OSError, sqlite3.Error)):
        exporter.export_snapshot(databases, mapping, tmp_path / "status", jobs_file=job_files, clock=lambda: now)
    assert path.read_bytes() == previous


@pytest.mark.skipif(os.name == "nt", reason="snapshot publication uses POSIX locks")
@pytest.mark.parametrize("slots", [schedule.SLOTS, schedule.LEGACY_PIPELINE_SLOTS, schedule.PIPELINE_SLOTS])
def test_exporter_single_path_keeps_historical_and_current_contracts(tmp_path, slots):
    from scripts import export_scheduler_status as exporter
    now = datetime(2026, 10, 17, 16, 27, tzinfo=timezone.utc)
    database = tmp_path / "single.sqlite3"
    with sqlite3.connect(database) as db: db.execute("CREATE TABLE executions(job_id,status,claimed_at,started_at,finished_at)")
    identities = {role: "single-" + role for role in slots}
    mapping = tmp_path / "map.json"; mapping.write_text(json.dumps(identities))
    jobs = tmp_path / "jobs.json"; jobs.write_text(json.dumps([{"id": identity} for identity in identities.values()]))
    path = exporter.export_snapshot(database, mapping, tmp_path / "status", clock=lambda: now,
        jobs_file=jobs if slots != schedule.SLOTS else None)
    assert len(validate_snapshot(json.loads(path.read_text()), now=now)["jobs"]) == len(slots)


@pytest.mark.skipif(os.name == "nt", reason="snapshot publication uses POSIX locks")
def test_exporter_cli_accepts_repeated_existing_profile_flags(tmp_path, monkeypatch):
    from scripts import export_scheduler_status as exporter
    now, databases, job_files, mapping = _profile_inputs(tmp_path)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return now
    monkeypatch.setattr(exporter, "datetime", Clock)
    monkeypatch.setattr(sys, "argv", ["export_scheduler_status", "--executions-db", str(databases[0]),
        "--executions-db", str(databases[1]), "--jobs-file", str(job_files[0]), "--jobs-file", str(job_files[1]),
        "--jobs-map", str(mapping), "--status-dir", str(tmp_path / "status")])
    assert exporter.main() == 0
    snapshot = json.loads((tmp_path / "status/scheduler-status.json").read_text())
    assert snapshot["jobs"]["T5"]["state"] == "failed" and snapshot["jobs"]["T10"]["state"] == "completed"
