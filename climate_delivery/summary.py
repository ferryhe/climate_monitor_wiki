from pathlib import Path
from typing import Any

from .io import atomic_write_json
from .report import WeeklyReport


THEMES = (
    (
        "climate disclosure and reporting",
        "disclosure quality and reporting controls",
        ("disclos", "reporting", "ifrs s2", "issb", "isap 8"),
    ),
    (
        "scenario analysis and actuarial assumptions",
        "scenario calibration and actuarial assumption setting",
        ("scenario", "orsa", "assumption", "stress test"),
    ),
    (
        "resilience and parametric insurance",
        "pricing, product design and protection-gap analysis",
        ("resilien", "parametric", "protection gap", "adaptation"),
    ),
    (
        "natural catastrophe and physical risk",
        "catastrophe modelling, hazard trends and loss assumptions",
        ("natural catastrophe", "nat cat", "wildfire", "storm", "flood", "drought", "hazard"),
    ),
    (
        "climate finance and investment",
        "investment classification and capital allocation",
        ("investment", "finance", "capital", "taxonomy", "taxonomies"),
    ),
    (
        "standards and regulation",
        "implementation timelines and professional standards",
        ("standard", "regulat", "supervis", "compliance"),
    ),
    (
        "climate, health and migration",
        "mortality, morbidity and migration assumptions",
        ("health", "mortality", "morbidity", "migration"),
    ),
)


def format_scope_line(summary: dict[str, Any]) -> str:
    sites = summary["report"].get("sites", {})
    if all(isinstance(sites.get(key), int) for key in ("checked", "succeeded", "failed")):
        return (
            f"{sites['checked']} sites checked - "
            f"{sites['succeeded']} succeeded - {sites['failed']} failed"
        )
    return "Weekly report"


def _joined(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _content_executive_summary(report: WeeklyReport) -> list[str]:
    pillar_a = [item for item in report.highlights if item.pillar == "A"]
    pillar_b = [item for item in report.highlights if item.pillar == "B"]
    total = len(report.highlights)
    scored = []
    for order, (label, implication, keywords) in enumerate(THEMES):
        count = sum(
            any(keyword in f"{item.title} {item.summary}".casefold() for keyword in keywords)
            for item in report.highlights
        )
        if count:
            scored.append((count, -order, label, implication))
    scored.sort(reverse=True)
    leading = scored[:3]

    if leading:
        first = (
            f"Across {total} updates, this week's evidence concentrated on "
            f"{_joined([item[2] for item in leading])}."
        )
    else:
        first = (
            f"This week's report contains {total} climate and actuarial updates: "
            f"{len(pillar_a)} newly detected site {'change' if len(pillar_a) == 1 else 'changes'} and "
            f"{len(pillar_b)} wider intelligence {'item' if len(pillar_b) == 1 else 'items'}."
        )

    sentences = [first]
    if pillar_a:
        sentences.append(
            f"New monitored-site developments include {_joined([item.title for item in pillar_a[:2]])}."
        )
    else:
        sentences.append("No newly detected site change met the report's Pillar A inclusion criteria this week.")
    if pillar_b:
        if leading:
            sentences.append(
                f"Notable wider-intelligence items include {_joined([item.title for item in pillar_b[:2]])}."
            )
        else:
            sentences.append(f"The wider intelligence set includes {_joined([item.title for item in pillar_b[:2]])}.")
    else:
        sentences.append("No wider-intelligence item met the report's Pillar B inclusion criteria this week.")
    if leading:
        sentences.append(
            f"Across the evidence, recurring actuarial implications include "
            f"{_joined([item[3] for item in leading])}."
        )
    return sentences


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
        "executive_summary": _content_executive_summary(report),
        "monitoring_notes": [
            item for item in report.monitoring_notes if not item.casefold().startswith("sites checked:")
        ],
        "highlights": [
            {"pillar": item.pillar, "title": item.title, "summary": item.summary, "url": item.url}
            for item in report.highlights
        ],
        "original_links": list(report.original_links),
    }


def write_summary(summary: dict[str, Any], output: Path) -> None:
    atomic_write_json(Path(output), summary)
