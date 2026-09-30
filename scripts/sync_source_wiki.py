from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = REPO_ROOT / "sources"
DEFAULT_WIKI_DIR = REPO_ROOT / "wiki"
DAILY_FILE_RE = re.compile(r"^climate-monitor-(\d{4}-\d{2}-\d{2})\.md$")
LAST_UPDATED_RE = re.compile(r"^_Last updated: .+_$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")


def _default_cadence() -> str:
    cadence = os.environ.get("CLIMATE_WIKI_CADENCE", "daily").strip().lower()
    return cadence if cadence in {"daily", "weekly"} else "daily"


DEFAULT_CADENCE = _default_cadence()


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
    pruned_pages: list[str] = field(default_factory=list)


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


def _iter_dates(start: str, end: str, *, step_days: int = 1) -> list[str]:
    current = date.fromisoformat(start)
    final = date.fromisoformat(end)
    days: list[str] = []
    while current <= final:
        days.append(current.isoformat())
        current += timedelta(days=step_days)
    return days


def _iter_report_dates(known: set[str], cadence: str) -> list[str]:
    """Enumerate the report dates the wiki should contain.

    daily  -> every calendar day between the first and last known report, so a
              skipped weekday shows up as an explicit "No report" gap row.
    weekly -> exactly the dates that exist. Filling a 7-day grid is wrong here:
              the corpus mixes a historical daily run (April 2026) with the
              current weekly cadence, and any synthetic grid manufactures dozens
              of phantom "No report" pages that never corresponded to a run.
    """
    if cadence == "daily":
        return _iter_dates(min(known), max(known))
    return sorted(known)


def _strip_markdown(text: str) -> str:
    cleaned = text
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = WIKILINK_RE.sub(lambda match: match.group(2) or match.group(1), cleaned)
    cleaned = re.sub(r"<br\s*/?>", " ", cleaned, flags=re.IGNORECASE)
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


def render_daily_page(
    day: str, *, summary: str, has_source: bool, cadence: str = "daily"
) -> str:
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
            f"#climate-monitor #{cadence}-report #{day}",
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
    cadence: str = "daily",
) -> str:
    label = "Daily" if cadence == "daily" else "Weekly"
    latest_date = daily_days[-1] if daily_days else ""
    rows = []
    for day in daily_days:
        status = "✅" if day in source_days else "⚠️ No report"
        rows.append(f"| {day} | [[climate-monitor-{day}]] | {status} |")

    blocks = [
        "# Wiki Index",
        "",
        f"_Last updated: {latest_date} - {len(topic_pages)} pages + {len(daily_days)} {label.lower()} report pages_",
        "",
        f"## {label} Reports",
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


def _citation(occurrence: dict) -> str:
    files = sorted({item.get("filename", "") for item in occurrence.get("source_observations", []) if item.get("filename")})
    filename = ", ".join(files) or "unknown PDF"
    sha256 = occurrence.get("source_document_sha256", "unknown")
    page = occurrence.get("page", "unknown")
    url = occurrence.get("raw_url", "")
    link = f"; [original link]({url})" if url else ""
    return f"PDF: {filename}; SHA-256: {sha256}; page {page}{link}"


def _pdf_heading(occurrence: dict) -> str:
    return f"PDF report observation: {occurrence.get('occurrence_id') or 'unknown'}"


def _render_registry_article(article: dict) -> str | None:
    content = article.get("available_content") or article.get("content", {})
    report_summary = article.get("report_summary")
    appearances = article.get("appearances", [])
    pdf_occurrences = article.get("pdf_occurrences", [])
    acquisition_observations = article.get("acquisition_observations", [])
    current_version_has_appearance = any(
        appearance.get("version_id") == article.get("current_version_id")
        and appearance.get("summary") == report_summary
        for appearance in appearances
    )
    if not (content.get("markdown") or content.get("supporting_excerpt") or article.get("summary") or report_summary or acquisition_observations or any(item.get("summary") for item in pdf_occurrences) or any(item.get("summary") for item in appearances)):
        return None

    title = article.get("title") or next(
        (item.get("title") for item in acquisition_observations if item.get("title")), article["article_id"]
    )
    blocks = [f"# {title}", "", f"Canonical article: [{article['canonical_url']}]({article['canonical_url']})"]
    if article.get("categories"):
        blocks.extend(["", f"Categories: {', '.join(article['categories'])}"])
    if article.get("keywords"):
        blocks.extend(["", f"Keywords: {', '.join(article['keywords'])}"])
    if content.get("markdown") or content.get("supporting_excerpt"):
        blocks.extend(["", "## Verified article content", "", content.get("markdown") or content["supporting_excerpt"], "", f"Article citation: [{article['canonical_url']}]({article['canonical_url']})"])
        if content.get("content_version_id"):
            blocks.append(f"Registry content version: {content['content_version_id']}")
        if content.get("content_sha256"):
            blocks.append(f"Content SHA-256: {content['content_sha256']}")
        if content.get("acquisition_item_id"):
            blocks.append(f"Acquisition observation: {content['acquisition_item_id']}; fetch: {content.get('fetch_id') or 'unknown'}")
    if article.get("summary"):
        semantic_content_version = article.get("enrichment", {}).get("content_version_id")
        blocks.extend(["", "## Article semantic summary", "", article["summary"], "", f"Provenance: {article.get('summary_provenance') or 'unknown'}; content version: {semantic_content_version or 'unknown'}; article citation: [{article['canonical_url']}]({article['canonical_url']})"])
    if report_summary and not current_version_has_appearance:
        blocks.extend(["", "## Registry article-version summary", "", report_summary, "", f"Provenance: Registry article version; Registry article version: {article.get('current_version_id') or 'unknown'}; Article citation: [{article['canonical_url']}]({article['canonical_url']})"])
    for appearance in appearances:
        if appearance.get("summary"):
            blocks.extend(["", f"## Report observation: {appearance['source_filename']}", "", appearance["summary"], "", f"Report citation: {appearance['source_filename']}; SHA-256: {appearance['source_sha256']}; [original link]({appearance['original_url']})"])
    for occurrence in pdf_occurrences:
        if occurrence.get("summary"):
            blocks.extend(["", f"## {_pdf_heading(occurrence)}", "", occurrence["summary"], "", _citation(occurrence)])
    for observation in acquisition_observations:
        blocks.extend([
            "", f"## Acquisition observation: {observation['acquisition_item_id']}", "",
            observation.get("summary") or "No summary was stored for this observation.", "",
            f"Provenance: {observation['discovery_kind']} observation; source: {observation['source_name']}; "
            f"discovered: {observation['discovered_at']}; reference: {observation['discovery_ref']}; "
            f"[original link]({observation['raw_url']})",
            f"Batch: {observation['batch_id']}; fetch: {observation['fetch_id']}; "
            f"content version: {observation.get('content_version_id') or observation.get('resolved_fetch', {}).get('content_version_id') or 'none'}; "
            f"status: {observation['processing_status']}/{observation['material_status']}",
        ])
        for origin in observation.get("origins", []):
            search = origin.get("search") or {}
            search_detail = (
                f"; search: {search['search_id']} ({search['engine']}, {search['status']}, {search['attempted_at']})"
                if search else ""
            )
            blocks.append(
                f"- Origin ({origin.get('discovery_kind', 'unknown')}): {origin.get('source', 'unknown')}; "
                f"{origin.get('discovered_at', 'unknown')}; {origin.get('discovery_ref', 'unknown')}; "
                f"[link]({origin.get('url', observation['raw_url'])}){search_detail}"
            )
    blocks.append("")
    return "\n".join(blocks)


def _render_registry_source_observations(items: list[dict]) -> str | None:
    blocks = ["# Registry report source observations", "", "These are report-provided summaries; article details are unconfirmed."]
    kept = 0
    for item in items:
        for occurrence in item.get("occurrences", []):
            if not occurrence.get("summary"):
                continue
            kept += 1
            blocks.extend(["", f"## {_pdf_heading(occurrence)}", "", occurrence["summary"], "", "Report-provided summary; article details are unconfirmed.", _citation(occurrence)])
    return "\n".join([*blocks, ""]) if kept else None


def _registry_pages(database: Path, wiki_dir: Path) -> dict[str, str]:
    from climate_registry.read_api import RegistryReader

    reader = RegistryReader(database, repository_root=REPO_ROOT)
    pages: dict[str, str] = {}
    page = 1
    while True:
        payload = reader.articles(page=page, page_size=100)
        for item in payload["items"]:
            if item["document_kind"] != "article" or not item["publication_eligible"]:
                continue
            article = reader.article(item["article_id"])
            rendered = _render_registry_article(article)
            if rendered:
                pages[f"article-{article['article_id']}.md"] = rendered
        if page >= payload["pagination"]["pages"]:
            break
        page += 1
    page = 1
    pdf_items: list[dict] = []
    while True:
        payload = reader.pdf_articles(page=page, page_size=100)
        pdf_items.extend(reader.pdf_article(item["article_id"]) for item in payload["items"])
        if page >= payload["pagination"]["pages"]:
            break
        page += 1
    rendered = _render_registry_source_observations(pdf_items)
    if rendered:
        pages["registry-source-observations.md"] = rendered
    states = {name: _write_if_changed(wiki_dir / name, content) for name, content in pages.items()}
    source_only = wiki_dir / "registry-source-observations.md"
    if not rendered and source_only.exists():
        source_only.unlink()
        states[source_only.name] = "deleted"
    return states


def sync_source_wiki(
    *,
    source_dir: Path = DEFAULT_SOURCE_DIR,
    wiki_dir: Path = DEFAULT_WIKI_DIR,
    cadence: str = DEFAULT_CADENCE,
    prune_sourceless: bool = True,
    registry_database: Path | None = None,
) -> SyncResult:
    if cadence not in {"daily", "weekly"}:
        raise ValueError(f"unsupported cadence: {cadence!r} (expected daily or weekly)")
    wiki_dir.mkdir(parents=True, exist_ok=True)
    source_dates = _discover_daily_dates(source_dir)
    existing_daily_dates = _discover_daily_dates(wiki_dir)
    known_dates = source_dates | existing_daily_dates
    registry_states = _registry_pages(registry_database, wiki_dir) if registry_database else {}
    if not known_dates and not registry_states:
        raise RuntimeError(
            "No climate-monitor report files were found in sources/ or wiki/."
        )

    pruned_pages: list[str] = []
    if cadence == "weekly" and prune_sourceless and known_dates:
        # The April daily run left placeholder pages for dates that never had a
        # report ("No report - source file missing"). Under weekly cadence those
        # are pure noise in the index and in retrieval, so drop any report page
        # with no matching sources/ file.
        for orphan in sorted(existing_daily_dates - source_dates):
            orphan_path = wiki_dir / f"climate-monitor-{orphan}.md"
            if orphan_path.exists():
                orphan_path.unlink()
                pruned_pages.append(orphan_path.name)
        known_dates = set(source_dates)
        if not known_dates:
            raise RuntimeError("No climate-monitor source files were found in sources/.")

    daily_days = _iter_report_dates(known_dates, cadence) if known_dates else []
    topic_pages = _read_topic_pages(wiki_dir)
    index_tail = _preserved_index_tail(wiki_dir / "index.md")

    created_pages: list[str] = []
    updated_pages: list[str] = []
    unchanged_pages: list[str] = []
    warnings: list[str] = []

    for name, state in registry_states.items():
        if state == "created":
            created_pages.append(name)
        elif state == "updated":
            updated_pages.append(name)
        elif state == "deleted":
            pruned_pages.append(name)
        else:
            unchanged_pages.append(name)

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

        page_content = render_daily_page(
            day, summary=summary, has_source=has_source, cadence=cadence
        )
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
        cadence=cadence,
    )
    index_state = _write_if_changed(wiki_dir / "index.md", index_content)
    if index_state == "created":
        created_pages.append("index.md")
    elif index_state == "updated":
        updated_pages.append("index.md")
    else:
        unchanged_pages.append("index.md")

    return SyncResult(
        latest_date=daily_days[-1] if daily_days else "",
        topic_pages=len(topic_pages),
        daily_pages=len(daily_days),
        source_days=len(source_dates),
        missing_days=[day for day in daily_days if day not in source_dates],
        created_pages=created_pages,
        updated_pages=updated_pages,
        unchanged_pages=unchanged_pages,
        warnings=warnings,
        pruned_pages=pruned_pages,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate dated wiki report pages from sources/ and rebuild "
            "wiki/index.md. Supports daily and weekly cadences."
        )
    )
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--wiki-dir", type=Path, default=DEFAULT_WIKI_DIR)
    parser.add_argument("--registry-database", type=Path)
    parser.add_argument(
        "--cadence",
        choices=("daily", "weekly"),
        default=DEFAULT_CADENCE,
        help="Report cadence: controls date-grid expansion and page/index labels.",
    )
    parser.add_argument(
        "--keep-sourceless",
        action="store_true",
        help="Weekly cadence only: keep legacy report pages that have no sources/ file.",
    )
    args = parser.parse_args()

    result = sync_source_wiki(
        source_dir=args.source_dir,
        wiki_dir=args.wiki_dir,
        cadence=args.cadence,
        prune_sourceless=not args.keep_sourceless,
        registry_database=args.registry_database,
    )
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
