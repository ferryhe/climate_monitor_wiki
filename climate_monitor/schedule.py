"""Application schedule; ET means America/New_York, including DST.

The legacy policy remains readable for historical snapshots and rehearsals.
Production selects biweekly-et explicitly; the library's daily cadence is
unrelated to this dispatch policy.
"""
from datetime import date, datetime, timedelta, timezone
import os
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ANCHOR = date(2026, 9, 14)
SCHEMA = "biweekly-job-status.v1"
SLOTS = {"monitor": (8, 0), "email": (9, 0), "publisher": (10, 0), "registry": (10, 30)}
PIPELINE_SCHEMA = "independent-job-status.v1"
PIPELINE_SLOTS = {
    "T1": ("daily", 5), "T2": ("daily", 6), "T3": ("sunday", 8),
    "T4": ("biweekly", 8), "T5": ("quarter_hour", None), "T6": ("quarter_hour", None),
    "T7": ("five_minutes", None), "T8": ("monday_utc", 0), "T9": ("daily", 4),
    "T10": ("quarter_hour", None),
}


def biweekly() -> bool:
    value = os.environ.get("CLIMATE_SCHEDULE", "weekly-utc")
    if value not in {"weekly-utc", "biweekly-et", "independent-et"}:
        raise ValueError("unsupported CLIMATE_SCHEDULE")
    return value in {"biweekly-et", "independent-et"}


def is_run_date(day: date) -> bool:
    return day >= ANCHOR and (day - ANCHOR).days % 14 == 0


def period_date(now: datetime, *, eastern: bool | None = None) -> date:
    eastern = biweekly() if eastern is None else eastern
    local = now.astimezone(ET if eastern else timezone.utc)
    if eastern:
        return ANCHOR + timedelta(days=max(0, (local.date() - ANCHOR).days // 14) * 14)
    return local.date() - timedelta(days=local.weekday())


def occurrence(day: date | str, slot: str, *, eastern: bool | None = None) -> datetime:
    eastern = biweekly() if eastern is None else eastern
    day = date.fromisoformat(day) if isinstance(day, str) else day
    if day.weekday() != 0 or (eastern and not is_run_date(day)):
        raise ValueError("date is outside the configured schedule")
    hour, minute = SLOTS[slot]
    return datetime(day.year, day.month, day.day, hour, minute,
                    tzinfo=ET if eastern else timezone.utc).astimezone(timezone.utc)


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def due(slot: str, now: datetime) -> bool:
    """Match a guarded Hermes tick within five minutes of the ET slot."""
    day = now.astimezone(ET).date()
    return is_run_date(day) and timedelta(0) <= now - occurrence(day, slot, eastern=True) < timedelta(minutes=5)


def pipeline_occurrence(slot: str, now: datetime) -> datetime:
    """Each role owns its own occurrence; local midnight boundaries follow DST."""
    if now.tzinfo is None:
        raise ValueError("schedule clock must be aware")
    cadence, hour = PIPELINE_SLOTS[slot]
    local = now.astimezone(timezone.utc if cadence == "monday_utc" else ET)
    if cadence in {"quarter_hour", "five_minutes"}:
        interval = 15 if cadence == "quarter_hour" else 5
        return local.replace(minute=local.minute // interval * interval, second=0, microsecond=0).astimezone(timezone.utc)
    day = local.date()
    if cadence == "sunday":
        day -= timedelta(days=(day.weekday() + 1) % 7)
    elif cadence == "biweekly":
        day = ANCHOR + timedelta(days=((day - ANCHOR).days // 14) * 14)
    elif cadence == "monday_utc":
        day -= timedelta(days=day.weekday())
    return datetime.combine(day, datetime.min.time(), local.tzinfo).replace(hour=hour).astimezone(timezone.utc)


def pipeline_due(slot: str, now: datetime) -> bool:
    if slot == "T4" and now.astimezone(ET).date() < ANCHOR:
        return False
    return timedelta(0) <= now - pipeline_occurrence(slot, now) < timedelta(minutes=5)
