from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from climate_delivery.report import parse_weekly_report

REPORT_NAME = re.compile(r"^climate-monitor-(\d{4}-\d{2}-\d{2})\.md$")
HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
HTTP_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
NUMBERED_TITLE = re.compile(r"^\*\*(?:\d+\.\s*)?(.+?)\*\*(?:\s*\([^)]*\))?\s*$")
LIST_NUMBERED_TITLE = re.compile(r"^\d+\.\s+\*\*(.+?)\*\*\s*$")
ARROW_TITLE = re.compile(r"^[→➜]\s*\*\*(.+?)\*\*\s*$")
FIELD = re.compile(r"^\*\*(Title|Summary|URL|Source):\*\*\s*(.*?)(?:\s*<br>)?\s*$", re.IGNORECASE)
PLAIN_FIELD = re.compile(r"^(Summary|Source|URL):\s*(.*?)\s*$", re.IGNORECASE)
NUMBERED_H3 = re.compile(r"^\d+\.\s*(.+)$")


@dataclass(frozen=True)
class ParsedArticle:
    section: str
    pillar: str | None
    title: str
    summary: str
    url: str


@dataclass(frozen=True)
class ParsedReport:
    path: Path
    report_date: str
    title: str
    sha256: str
    cadence: str
    report_format: str
    sites_checked: int | None
    sites_succeeded: int | None
    sites_failed: int | None
    articles: tuple[ParsedArticle, ...]
    warnings: tuple[str, ...]


def _clean_markup(value: str) -> str:
    value = value.replace("<br>", " ").replace("  ", " ")
    value = re.sub(r"^[\-*>\s]+", "", value)
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", value)
    value = re.sub(r"`(.+?)`", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _clean_url(value: str) -> str:
    return value.rstrip(".,;:)]}\"")


def _section_kind(heading: str) -> tuple[str | None, str | None]:
    key = heading.casefold()
    if "pillar a" in key:
        return "pillar-a", "A"
    if "pillar b" in key:
        return "pillar-b", "B"
    if "website update" in key or "part 1" in key:
        return "legacy-website", None
    if "document" in key and "report" in key:
        return "legacy-document", None
    if "new research" in key or "part 2" in key:
        return "legacy-research", None
    return None, None


def _is_ignored_heading(heading: str) -> bool:
    key = heading.casefold()
    return any(
        marker in key
        for marker in ("executive summary", "source links", "original links", "dedup", "warning", "summary statistics")
    ) or key.strip(" 📊") == "summary"


def _parse_weekly(path: Path) -> ParsedReport:
    report = parse_weekly_report(path)
    return ParsedReport(
        path=path,
        report_date=report.report_date,
        title=report.title,
        sha256=report.sha256,
        cadence="weekly",
        report_format="weekly-pillars-v1",
        sites_checked=report.checked,
        sites_succeeded=report.succeeded,
        sites_failed=report.failed,
        articles=tuple(
            ParsedArticle(
                section=f"pillar-{highlight.pillar.casefold()}",
                pillar=highlight.pillar,
                title=highlight.title,
                summary=highlight.summary,
                url=highlight.url,
            )
            for highlight in report.highlights
        ),
        warnings=(),
    )


def _emit_legacy_item(
    output: list[ParsedArticle],
    *,
    section: str | None,
    pillar: str | None,
    title: str,
    summary_parts: list[str],
    url: str,
    fallback_title: str,
) -> None:
    if not section:
        return
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return
    cleaned_title = _clean_markup(title) or _clean_markup(fallback_title) or parsed.netloc
    cleaned_summary = _clean_markup(" ".join(summary_parts))
    output.append(
        ParsedArticle(
            section=section,
            pillar=pillar,
            title=cleaned_title,
            summary=cleaned_summary,
            url=_clean_url(url),
        )
    )


def _parse_legacy(path: Path, text: str, report_date: str) -> ParsedReport:
    lines = text.splitlines()
    title = next((_clean_markup(line[2:]) for line in lines if line.startswith("# ")), path.stem)
    section: str | None = None
    pillar: str | None = None
    group_title = ""
    item_title = ""
    summary_parts: list[str] = []
    output: list[ParsedArticle] = []
    warnings: list[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line == "---":
            continue
        heading = HEADING.match(line)
        if heading:
            level, heading_text = len(heading.group(1)), _clean_markup(heading.group(2))
            if level == 2:
                if _is_ignored_heading(heading_text):
                    section, pillar = None, None
                else:
                    section, pillar = _section_kind(heading_text)
                group_title = ""
                item_title = ""
                summary_parts = []
            elif level == 3 and section:
                numbered = NUMBERED_H3.match(heading_text)
                if numbered:
                    item_title = numbered.group(1)
                    summary_parts = []
                else:
                    group_title = heading_text
                    item_title = ""
                    summary_parts = []
            continue
        if not section:
            continue

        arrow = ARROW_TITLE.match(line)
        if arrow:
            item_title, summary_parts = arrow.group(1), []
            continue
        list_numbered_title = LIST_NUMBERED_TITLE.match(line)
        if list_numbered_title:
            item_title, summary_parts = list_numbered_title.group(1), []
            continue
        numbered_title = NUMBERED_TITLE.match(line)
        if numbered_title:
            item_title, summary_parts = numbered_title.group(1), []
            continue

        field = FIELD.match(line) or PLAIN_FIELD.match(line)
        if field:
            key, value = field.group(1).casefold(), field.group(2)
            if key == "title":
                item_title = value
                summary_parts = []
                continue
            if key == "summary":
                summary_parts.append(value)
                continue
            if key in {"url", "source"}:
                url_match = HTTP_URL.search(value)
                if url_match:
                    url = _clean_url(url_match.group(0))
                    _emit_legacy_item(
                        output,
                        section=section,
                        pillar=pillar,
                        title=item_title,
                        summary_parts=summary_parts,
                        url=url,
                        fallback_title=group_title,
                    )
                    item_title, summary_parts = "", []
                continue

        markdown_link = MARKDOWN_LINK.search(line)
        if markdown_link:
            link_text, url = markdown_link.groups()
            candidate = item_title
            generic_link_text = link_text.casefold().lstrip("🔗 ").strip()
            if not candidate and generic_link_text not in {"link", "source", "read more"}:
                candidate = link_text
            _emit_legacy_item(
                output,
                section=section,
                pillar=pillar,
                title=candidate,
                summary_parts=summary_parts,
                url=url,
                fallback_title=group_title,
            )
            item_title, summary_parts = "", []
            continue

        emoji_or_bare_url = HTTP_URL.search(line)
        if emoji_or_bare_url and (line.startswith(("🔗", "http://", "https://")) or "Source:" in line):
            url = _clean_url(emoji_or_bare_url.group(0))
            _emit_legacy_item(
                output,
                section=section,
                pillar=pillar,
                title=item_title,
                summary_parts=summary_parts,
                url=url,
                fallback_title=group_title,
            )
            item_title, summary_parts = "", []
            continue

        if item_title and not line.startswith("**"):
            summary_parts.append(line)

    if not output:
        warnings.append("no article URLs parsed from recognized content sections")
    return ParsedReport(
        path=path,
        report_date=report_date,
        title=title,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        cadence="legacy-daily",
        report_format="legacy-markdown",
        sites_checked=None,
        sites_succeeded=None,
        sites_failed=None,
        articles=tuple(output),
        warnings=tuple(warnings),
    )


def parse_historical_report(path: Path) -> ParsedReport:
    match = REPORT_NAME.fullmatch(path.name)
    if not match:
        raise ValueError(f"unsupported report filename: {path.name}")
    text = path.read_text(encoding="utf-8")
    if "Pillar A" in text and "Pillar B" in text and "Weekly Climate" in text:
        return _parse_weekly(path)
    return _parse_legacy(path, text, match.group(1))


def parse_report_directory(source_dir: Path) -> tuple[ParsedReport, ...]:
    paths: Iterable[Path] = sorted(source_dir.glob("climate-monitor-*.md"))
    return tuple(parse_historical_report(path) for path in paths)
