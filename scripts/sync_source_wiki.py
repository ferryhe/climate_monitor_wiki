from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = REPO_ROOT / "sources"
DEFAULT_WIKI_DIR = REPO_ROOT / "wiki"
DAILY_FILE_RE = re.compile(r"^climate-monitor-(\d{4}-\d{2}-\d{2})\.md$")
LAST_UPDATED_RE = re.compile(r"^_Last updated: .+_$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")


@dataclass(frozen=True)
class SyncResult:
    latest_date: str
    topic_pages: int
    daily_pages: int
    source_days: int
    missing_days: list[str]
    created_pages: list[str]
    updated_pages: list[str]
    unchanged_pages: list[str]
    warnings: list[str]


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _daily_date_from_name(name: str) -> str | None:
    match = DAILY_FILE_RE.fullmatch(name)
    return match.group(1) if match else None


def _discover_daily_dates(directory: Path) -> set[str]:
    dates: set[str] = set()
    if not directory.exists():
        return dates
    for path in directory.glob("climate-monitor-*.md"):
        daily_date = _daily_date_from_name(path.name)
        if daily_date:
            dates.add(daily_date)
    return dates


def _iter_dates(start: str, end: str) -> list[str]:
    current = date.fromisoformat(start)
    final = date.fromisoformat(end)
    days: list[str] = []
    while current <= final:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _strip_markdown(text: str) -> str:
    cleaned = text
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = WIKILINK_RE.sub(lambda match: match.group(2) or match.group(1), cleaned)
    cleaned = re.sub(r"^[#>*+\-\s]+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"_([^_]+)_", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _extract_report_date(markdown: str) -> str:
    match = re.search(r"Report Date:\**\s*(\d{4}-\d{2}-\d{2})", markdown, re.IGNORECASE)
    return match.group(1) if match else ""


def _section_body(markdown: str, heading_fragment: str) -> str:
    pattern = re.compile(
        rf"^##\s+[^\n]*{re.escape(heading_fragment)}[^\n]*\n(?P<body>.*?)(?=^##\s+|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(markdown)
    return match.group("body").strip() if match else ""


def extract_summary(markdown: str) -> str:
    executive = _section_body(markdown, "Executive Summary")
    if executive:
        return _strip_markdown(executive)

    summary = _section_body(markdown, "Summary")
    if summary:
        return _strip_markdown(summary)

    lines: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "Report Date" in line:
            continue
        lines.append(line)
        if len(lines) >= 3:
            break
    return _strip_markdown(" ".join(lines))


def render_daily_page(day: str, *, summary: str, has_source: bool) -> str:
    source_line = (
        f"Source: [[sources/climate-monitor-{day}]]"
        if has_source
        else "Source: missing"
    )
    body = summary if has_source else "No report - source file missing for this date."
    return "\n".join(
        [
            f"# Climate Monitor - {day}",
            "",
            f"**Report Date:** {day}",
            source_line,
            "",
            "## Summary",
            "",
            body,
            "",
            "## Tags",
            f"#climate-monitor #daily-report #{day}",
            "",
        ]
    )


def _read_topic_pages(wiki_dir: Path) -> list[Path]:
    pages: list[Path] = []
    if not wiki_dir.exists():
        return pages
    for path in sorted(wiki_dir.glob("*.md")):
        if path.name in {"index.md", "log.md"}:
            continue
        if _daily_date_from_name(path.name):
            continue
        pages.append(path)
    return pages


def _preserved_index_tail(index_path: Path) -> str:
    if not index_path.exists():
        return ""

    existing = _normalize_text(index_path.read_text(encoding="utf-8"))
    marker_positions = [
        position
        for marker in ("\n## Entities\n", "\n## Concepts\n", "\n## Topics\n")
        if (position := existing.find(marker)) != -1
    ]
    if not marker_positions:
        return ""

    tail = existing[min(marker_positions):].strip()
    tail = LAST_UPDATED_RE.sub("", tail).strip()
    return tail


def build_index(
    *,
    source_days: set[str],
    daily_days: list[str],
    topic_pages: list[Path],
    index_tail: str,
) -> str:
    latest_date = daily_days[-1] if daily_days else ""
    rows = []
    for day in daily_days:
        status = "✅" if day in source_days else "⚠️ No report"
        rows.append(f"| {day} | [[climate-monitor-{day}]] | {status} |")

    blocks = [
        "# Wiki Index",
        "",
        f"_Last updated: {latest_date} - {len(topic_pages)} pages + {len(daily_days)} daily report pages_",
        "",
        "## Daily Reports",
        "",
        "| Date | Report | Status |",
        "|------|--------|--------|",
        *rows,
    ]
    if index_tail:
        blocks.extend(["", index_tail])
    if latest_date:
        blocks.extend(["", f"_Last updated: {latest_date}_"])
    blocks.append("")
    return "\n".join(blocks)


def _write_if_changed(path: Path, content: str) -> str:
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            return "unchanged"
        path.write_text(content, encoding="utf-8")
        return "updated"

    path.write_text(content, encoding="utf-8")
    return "created"


def sync_source_wiki(
    *,
    source_dir: Path = DEFAULT_SOURCE_DIR,
    wiki_dir: Path = DEFAULT_WIKI_DIR,
) -> SyncResult:
    source_dates = _discover_daily_dates(source_dir)
    existing_daily_dates = _discover_daily_dates(wiki_dir)
    known_dates = source_dates | existing_daily_dates
    if not known_dates:
        raise RuntimeError("No climate-monitor daily files were found in sources/ or wiki/.")

    daily_days = _iter_dates(min(known_dates), max(known_dates))
    topic_pages = _read_topic_pages(wiki_dir)
    index_tail = _preserved_index_tail(wiki_dir / "index.md")

    created_pages: list[str] = []
    updated_pages: list[str] = []
    unchanged_pages: list[str] = []
    warnings: list[str] = []

    for day in daily_days:
        source_path = source_dir / f"climate-monitor-{day}.md"
        has_source = source_path.exists()
        summary = ""
        if has_source:
            source_markdown = _normalize_text(source_path.read_text(encoding="utf-8"))
            report_date = _extract_report_date(source_markdown)
            if report_date and report_date != day:
                warnings.append(
                    f"Report date mismatch in {source_path.name}: expected {day}, found {report_date}. "
                    "Used the filename date."
                )
            summary = extract_summary(source_markdown)

        page_content = render_daily_page(day, summary=summary, has_source=has_source)
        target_path = wiki_dir / f"climate-monitor-{day}.md"
        write_state = _write_if_changed(target_path, page_content)
        if write_state == "created":
            created_pages.append(target_path.name)
        elif write_state == "updated":
            updated_pages.append(target_path.name)
        else:
            unchanged_pages.append(target_path.name)

    index_content = build_index(
        source_days=source_dates,
        daily_days=daily_days,
        topic_pages=topic_pages,
        index_tail=index_tail,
    )
    index_state = _write_if_changed(wiki_dir / "index.md", index_content)
    if index_state == "created":
        created_pages.append("index.md")
    elif index_state == "updated":
        updated_pages.append("index.md")
    else:
        unchanged_pages.append("index.md")

    return SyncResult(
        latest_date=daily_days[-1],
        topic_pages=len(topic_pages),
        daily_pages=len(daily_days),
        source_days=len(source_dates),
        missing_days=[day for day in daily_days if day not in source_dates],
        created_pages=created_pages,
        updated_pages=updated_pages,
        unchanged_pages=unchanged_pages,
        warnings=warnings,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate daily wiki pages from sources/ and rebuild wiki/index.md."
    )
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--wiki-dir", type=Path, default=DEFAULT_WIKI_DIR)
    args = parser.parse_args()

    result = sync_source_wiki(source_dir=args.source_dir, wiki_dir=args.wiki_dir)
    print(
        "Synced wiki:",
        f"latest_date={result.latest_date}",
        f"topic_pages={result.topic_pages}",
        f"daily_pages={result.daily_pages}",
        f"source_days={result.source_days}",
        f"missing_days={len(result.missing_days)}",
        f"created={len(result.created_pages)}",
        f"updated={len(result.updated_pages)}",
    )
    if result.created_pages:
        print("Created:", ", ".join(result.created_pages))
    if result.updated_pages:
        print("Updated:", ", ".join(result.updated_pages))
    if result.missing_days:
        print("No-report days:", ", ".join(result.missing_days))
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"- {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
