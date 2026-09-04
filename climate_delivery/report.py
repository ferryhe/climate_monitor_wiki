import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .errors import InputError


REPORT_NAME = re.compile(r"^climate-monitor-(\d{4}-\d{2}-\d{2})\.md$")
REPORT_DATE = re.compile(r"^\*\*Report Date:\*\*\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
COUNTS = re.compile(
    r"Sites checked:\s*\*\*(\d+|unknown)\*\*\s*,\s*succeeded:\s*\*\*(\d+|unknown)\*\*\s*,\s*failed:\s*\*\*(\d+|unknown)\*\*",
    re.IGNORECASE,
)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
URL = re.compile(r"https?://[^\s)>]+")
LINK_MARKER = "🔗"
MARKDOWN_LINK_ENTRY = re.compile(r"^\s*[-*]\s+\[[^\]]+\]\((.+)\)\s*$")
BARE_LINK_ENTRY = re.compile(r"^\s*[-*]\s+<?([A-Za-z][A-Za-z0-9+.-]*:[^\s>]+)>?\s*$")
ITEM_METADATA = re.compile(
    r"^\s*[-*]\s+\*\*(Categories|Keywords):\*\*\s*(.*?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Highlight:
    pillar: str
    title: str
    summary: str
    url: str
    categories: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class WeeklyReport:
    path: Path
    filename: str
    report_date: str
    title: str
    sha256: str
    checked: int | None
    succeeded: int | None
    failed: int | None
    monitoring_notes: tuple[str, ...]
    highlights: tuple[Highlight, ...]
    original_links: tuple[str, ...]


def _section(text: str, required: str) -> str:
    headings = list(HEADING.finditer(text))
    matches = [item for item in headings if len(item.group(1)) == 2 and required.casefold() in item.group(2).casefold()]
    if not matches:
        raise InputError(f"weekly report is missing the {required} section")
    if len(matches) != 1:
        raise InputError(f"weekly report must contain exactly one {required} section")
    match = matches[0]
    end = len(text)
    for following in headings:
        if following.start() > match.start() and len(following.group(1)) <= 2:
            end = following.start()
            break
    return text[match.end() : end].strip()


def _clean_markdown(value: str) -> str:
    value = re.sub(r"\*\*(.*?)\*\*", r"\1", value)
    value = re.sub(r"`(.*?)`", r"\1", value)
    return re.sub(r"\s+", " ", value).strip(" -")


def _bullets(section: str) -> tuple[str, ...]:
    return tuple(_clean_markdown(match.group(1)) for match in re.finditer(r"^\s*-\s+(.+?)\s*$", section, re.MULTILINE))


def _metadata_values(value: str) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,;]", value):
        cleaned = _clean_markdown(raw)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            output.append(cleaned)
            seen.add(key)
    return tuple(output)


def _highlights(section: str, pillar: str) -> list[Highlight]:
    lines = section.splitlines()
    output: list[Highlight] = []
    index = 0
    while index < len(lines):
        title_match = re.match(r"^\s*-\s+\*\*(.+?)\*\*(?:\s*\([^)]*\))?\s*$", lines[index])
        if not title_match:
            index += 1
            continue
        title = _clean_markdown(title_match.group(1))
        summary = ""
        item_url = ""
        categories: tuple[str, ...] = ()
        keywords: tuple[str, ...] = ()
        cursor = index + 1
        while cursor < len(lines):
            if re.match(r"^\s*-\s+\*\*(.+?)\*\*", lines[cursor]):
                metadata = ITEM_METADATA.match(lines[cursor])
                if metadata:
                    values = _metadata_values(metadata.group(2))
                    if metadata.group(1).casefold() == "categories":
                        categories = values
                    else:
                        keywords = values
                    cursor += 1
                    continue
                break
            url_match = URL.search(lines[cursor])
            if url_match:
                item_url = url_match.group(0).rstrip(".,")
            elif not summary and re.match(r"^\s+-\s+", lines[cursor]):
                summary = _clean_markdown(re.sub(r"^\s+-\s+", "", lines[cursor]))
            cursor += 1
        if item_url:
            output.append(
                Highlight(
                    pillar=pillar,
                    title=title,
                    summary=summary,
                    url=item_url,
                    categories=categories,
                    keywords=keywords,
                )
            )
        index = max(cursor, index + 1)
    return output


def _validate_highlight_links(section: str) -> None:
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            stripped = stripped[2:].lstrip()
        if not stripped.startswith(LINK_MARKER):
            continue
        target = stripped[len(LINK_MARKER) :].strip()
        if not re.fullmatch(r"https?://\S+", target):
            raise InputError("highlight link lines must use an HTTP(S) URL")


def _validate_original_link_entries(section: str) -> None:
    for line in section.splitlines():
        markdown = MARKDOWN_LINK_ENTRY.fullmatch(line)
        bare = BARE_LINK_ENTRY.fullmatch(line)
        if markdown is None and bare is None:
            continue
        target = (markdown or bare).group(1).strip()
        if not re.fullmatch(r"https?://\S+", target):
            raise InputError("Original Links entries must use HTTP(S) URLs")


def parse_weekly_report(
    path: Path, *, raw: bytes | None = None, allow_offcycle: bool = False
) -> WeeklyReport:
    path = Path(path)
    if not path.is_file():
        raise InputError("report file does not exist")
    name_match = REPORT_NAME.fullmatch(path.name)
    if not name_match:
        raise InputError("report filename must be climate-monitor-YYYY-MM-DD.md")
    try:
        if raw is None:
            raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError("report must be a readable UTF-8 file") from exc

    date_matches = REPORT_DATE.findall(text)
    if len(date_matches) != 1:
        raise InputError("report must contain exactly one Report Date")
    filename_date, declared_date = name_match.group(1), date_matches[0]
    if filename_date != declared_date:
        raise InputError("Report Date must match the report filename")
    try:
        parsed_date = date.fromisoformat(declared_date)
    except ValueError as exc:
        raise InputError("Report Date is invalid") from exc
    if parsed_date.weekday() != 0 and not allow_offcycle:
        raise InputError("weekly Report Date must be a Monday")

    h1 = [match.group(2).strip() for match in HEADING.finditer(text) if len(match.group(1)) == 1]
    if len(h1) != 1 or not h1[0]:
        raise InputError("weekly report must contain exactly one H1")

    executive = _section(text, "Executive Summary")
    pillar_a = _section(text, "Pillar A")
    pillar_b = _section(text, "Pillar B")
    _validate_highlight_links(pillar_a)
    _validate_highlight_links(pillar_b)
    links_section = _section(text, "Original Links")
    _validate_original_link_entries(links_section)
    count_matches = COUNTS.findall(executive)
    if len(count_matches) != 1:
        raise InputError("Executive Summary must contain checked, succeeded, and failed counts")
    raw_counts = count_matches[0]
    unknown = [value.casefold() == "unknown" for value in raw_counts]
    if any(unknown):
        if not all(unknown):
            raise InputError("checked, succeeded, and failed counts must all be integers or all unknown")
        checked = succeeded = failed = None
    else:
        checked, succeeded, failed = (int(value) for value in raw_counts)
        if succeeded + failed != checked:
            raise InputError("succeeded and failed counts must sum to checked")

    links = tuple(dict.fromkeys(match.group(0).rstrip(".,") for match in URL.finditer(links_section)))
    if not links:
        raise InputError("Original Links must contain at least one HTTP(S) URL")
    highlights = tuple(_highlights(pillar_a, "A") + _highlights(pillar_b, "B"))
    if not highlights:
        raise InputError("Pillar A or Pillar B must contain at least one linked highlight")

    return WeeklyReport(
        path=path,
        filename=path.name,
        report_date=declared_date,
        title=_clean_markdown(h1[0]),
        sha256=hashlib.sha256(raw).hexdigest(),
        checked=checked,
        succeeded=succeeded,
        failed=failed,
        monitoring_notes=_bullets(executive),
        highlights=highlights,
        original_links=links,
    )
