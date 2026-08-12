from pathlib import Path
from typing import Any

from .io import atomic_write_json
from .report import WeeklyReport


def format_scope_line(summary: dict[str, Any]) -> str:
    sites = summary["report"].get("sites", {})
    if all(isinstance(sites.get(key), int) for key in ("checked", "succeeded", "failed")):
        return (
            f"{sites['checked']} sites checked - "
            f"{sites['succeeded']} succeeded - {sites['failed']} failed"
        )
    return "Weekly report"


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
