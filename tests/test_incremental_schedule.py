"""Current eleven-role observations retain exact historical schedule meaning."""
from datetime import datetime, timezone
import sqlite3

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
