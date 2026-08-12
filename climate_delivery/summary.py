from pathlib import Path
from typing import Any

from .io import atomic_write_json
from .report import WeeklyReport


def build_summary(report: WeeklyReport) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "report": {
            "date": report.report_date,
            "title": report.title,
            "sha256": report.sha256,
            "sites": {
                "checked": report.checked,
                "succeeded": report.succeeded,
                "failed": report.failed,
            },
        },
        "executive_summary": list(report.executive_summary),
        "highlights": [
            {"pillar": item.pillar, "title": item.title, "summary": item.summary, "url": item.url}
            for item in report.highlights
        ],
        "original_links": list(report.original_links),
    }


def write_summary(summary: dict[str, Any], output: Path) -> None:
    atomic_write_json(Path(output), summary)
