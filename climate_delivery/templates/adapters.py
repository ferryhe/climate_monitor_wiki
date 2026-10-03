"""Two input adapters. See CONTRACT.md for mapping and missing-data rules."""
import re
import json
import hashlib
from datetime import date, datetime
from typing import Any

from .model import Citation, Report, Update


def _identity(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("report input sha256 is invalid")
    return value


def _date(value: Any) -> str:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return value
    except (TypeError, ValueError) as exc:
        raise ValueError("report date is invalid") from exc


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("report title is missing")
    return value


def _citations(values: list[dict]) -> tuple[Citation, ...]:
    result = []
    for item in values:
        if item.get("kind") not in {"url", "pdf", "pdf_page"}:
            raise ValueError("citation kind is invalid")
        url = item.get("url")
        if url is not None and (not isinstance(url, str) or not re.fullmatch(r"https?://[^\s]+", url)):
            raise ValueError("citation URL is invalid")
        if item.get("kind") == "url":
            if not url:
                raise ValueError("citation URL is missing")
            result.append(Citation(url, url))
        else:
            filename, page = item.get("filename"), item.get("page")
            if not isinstance(filename, str) or not filename or isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise ValueError("PDF citation filename/page is invalid")
            result.append(Citation(f"{filename}, page {page}", url))
    if not result:
        raise ValueError("update citation is required")
    return tuple(result)


def _paragraphs(*values: str | None) -> tuple[str, ...]:
    return tuple(part for value in values if value for part in value.split("\n\n") if part.strip())


def _date_basis(value: Any) -> tuple[tuple[str, str], ...]:
    if not value:
        return ()
    return (("Date basis", value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)),)


def _caveats(item: dict) -> tuple[str, ...]:
    caveats = item.get("caveats", [])
    if item.get("caveat"):
        caveats = [item["caveat"], *caveats]
    return tuple(caveats)


def range_executive_summary(snapshot: dict[str, Any]) -> list[str]:
    """Existing range narrative: copied stored summaries, never regenerated."""
    articles = snapshot["articles"]
    pdf_updates = snapshot.get("pdf_source_updates", [])
    exclusions = snapshot.get("pdf_source_exclusion_counts", {})
    start, end = snapshot["date_range"]["start"], snapshot["date_range"]["end"]
    summary = [f"{len(articles)} evidenced Registry article(s) were published from {start} through {end}."]
    points = snapshot.get("executive_summary")
    if points is None:
        points = [dict(item, kind="registry_article", text=item.get("summary")) for item in articles if isinstance(item.get("summary"), str) and item["summary"].strip()]
        points += [dict(item, kind="pdf_source", text=item.get("summary")) for item in pdf_updates if isinstance(item.get("summary"), str) and item["summary"].strip()]
    article_points = [point for point in points if point["kind"] == "registry_article"]
    pdf_points = [point for point in points if point["kind"] == "pdf_source"]
    if not articles and not pdf_updates:
        summary.append("No selected Registry articles or PDF source updates matched this range.")
    if article_points:
        summary.extend(f"{point['title']}: {point['text']} (Article ID: {point['article_id']}; publication date {point['publication_date']})." for point in article_points)
    elif articles:
        summary.append("No stored Registry article summaries are available for the selected range.")
    if "pdf_source_updates" in snapshot:
        summary.append(f"{len(pdf_updates)} PDF source update(s) overlap the range; their article publication dates are unconfirmed.")
        if pdf_points:
            summary.extend(f"{point['text']} ({point['filename']}, page {point['page']}, {point['document_sha256']}; article publication date unconfirmed; coverage period {point['coverage_period']['start']} through {point['coverage_period']['end']})." for point in pdf_points)
        elif pdf_updates:
            summary.append("No stored PDF source summaries are available for the selected range.")
        summary.append(f"PDF source observations excluded: {exclusions.get('unknown_coverage', 0)} with unknown coverage; {exclusions.get('non_overlapping_coverage', 0)} with non-overlapping coverage.")
    if snapshot["unknown_publication_date_count"]:
        summary.append(f"{snapshot['unknown_publication_date_count']} Registry article(s) with unknown publication dates were excluded.")
    return summary


def meeting_metadata(meeting: dict[str, Any]) -> list[tuple[str, Any]]:
    source = meeting.get("source") or ("snapshot" if meeting.get("snapshot_id") else "unavailable")
    metadata = [("Meeting source", source)]
    if meeting.get("snapshot_id"):
        metadata.append(("Meeting snapshot ID", meeting["snapshot_id"]))
    elif meeting.get("query_id"):
        metadata.extend([("Meeting query ID", meeting["query_id"]), ("Meeting query SHA-256", meeting.get("query_sha256") or "unavailable")])
    metadata.extend([("Meeting query base date", meeting.get("base_date") or "unavailable"),
                     ("Meeting query timezone", meeting.get("timezone") or "unavailable"),
                     ("Meeting coverage", (meeting.get("coverage") or {}).get("status", "unavailable"))])
    return metadata


def _key_date(item: dict[str, Any], run_date: str | None = None) -> tuple[str, str, str, str, str]:
    start, end = item.get("start_date"), item.get("end_date")
    precision = item.get("date_precision") or item.get("precision")
    dates = list(dict.fromkeys(value for value in (start, end) if value))
    parts = [" through ".join(dates)] if dates else []
    parts.extend(value for value in (item.get("raw_date"), item.get("raw_time_text")) if value and value not in parts)
    if precision:
        parts.append(f"Precision: {precision}")
    if run_date and dates:
        # Import at use: meetings imports the delivery package while starting up.
        from climate_monitor.meetings import _date_bounds
        try:
            lower = _date_bounds(start or end, precision or "day", end=False)
            upper = _date_bounds(end or start, precision or "day", end=True)
            base = date.fromisoformat(run_date)
            if lower <= upper:
                marker = "past" if upper < base else "future" if lower > base else "current"
                parts.append(f"({marker})")
        except ValueError:
            pass  # Unknown precision/raw dates have no invented calendar bounds.
    when = "\n".join(parts) or "date unavailable"
    citations = []
    if item.get("source_filename"):
        citations.append(f"{item['source_filename']}, page {item.get('page')}")
    if item.get("source_document_sha256"):
        citations.append("SHA-256: " + item["source_document_sha256"])
    citations.extend(url for url in (item.get("url"), item.get("source_url")) if url)
    citations.extend(source["source_url"] for source in item.get("sources", []) if source.get("source_url"))
    citation = "\n".join(dict.fromkeys(citations)) or "Source not provided"
    return (str(when), str(item.get("name") or "Key date"),
            str(item.get("publisher") or item.get("institution") or item.get("organizer") or item.get("source") or "Not provided"),
            str(item.get("relevance") or item.get("actuarial_relevance") or item.get("relevance_reason") or "Not provided"), str(citation))


def _key_date_rows(item: dict[str, Any], run_date: str | None = None) -> tuple[tuple[str, str, str, str, str], ...]:
    deadline = item.get("deadline_date")
    rows = []
    if not deadline or any(item.get(field) for field in ("start_date", "end_date", "raw_date", "raw_time_text")):
        rows.append(_key_date(item, run_date))
    if deadline:
        # The meeting contract verifies deadline_date independently of event precision.
        deadline = _date(deadline)
        rows.append(_key_date(dict(item, start_date=deadline, end_date=deadline,
            date_precision="day", raw_date=None, raw_time_text=None,
            name=f"{item.get('name') or 'Key date'} — Deadline: {item.get('deadline_type') or 'type not provided'}"), run_date))
    return tuple(rows)


def _optional_tables(value: dict) -> dict:
    return {
        "coverage": tuple((str(row["institution"]), str(row["status"]), str(row.get("detail") or "Not provided")) for row in value.get("coverage", [])),
        "route_corrections": tuple((str(row["source"]), str(row["detail"])) for row in value.get("route_corrections", [])),
        "glossary": tuple((str(row["term"]), str(row["definition"])) for row in value.get("glossary", [])),
        "cross_cutting_watch": tuple(value.get("cross_cutting_watch", [])),
    }


def adapt_range_report(snapshot: dict[str, Any]) -> Report:
    identity = _identity(snapshot["snapshot_sha256"])
    if snapshot["snapshot_id"] != "range-report-" + identity[:24]:
        raise ValueError("range report identity is invalid")
    frozen = {key: value for key, value in snapshot.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    if hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() != identity:
        raise ValueError("range report input sha256 does not match frozen content")
    start, end = _date(snapshot["date_range"]["start"]), _date(snapshot["date_range"]["end"])
    if start > end:
        raise ValueError("report date window is invalid")
    run_date = datetime.fromisoformat(snapshot["created_at"].replace("Z", "+00:00")).date().isoformat()
    updates = []
    for item in snapshot["articles"]:
        updates.append(Update(_title(item["title"]), item.get("publisher") or "Publisher not recorded",
            ", ".join(item.get("categories", [])) or "Topic not recorded", _title(item["article_id"]),
            item.get("content_version_id"), _date(item["publication_date"]), None,
            _paragraphs(item.get("summary"), item.get("content")) + _caveats(item),
            _date_basis(item.get("provenance", {}).get("publication_date")) + tuple((label, ", ".join(item[field])) for label, field in (("Categories", "categories"), ("Keywords", "keywords")) if item.get(field)),
            _citations(item["citations"] or ([{"kind": "url", "url": item["canonical_url"]}] if item.get("canonical_url") else [])),
            content_sha256=item.get("provenance", {}).get("content_version", {}).get("content_sha256")))
    for item in snapshot.get("pdf_source_updates", []):
        coverage = (_date(item["coverage_period"]["start"]), _date(item["coverage_period"]["end"]))
        if coverage[0] > coverage[1]:
            raise ValueError("PDF coverage dates are invalid")
        citations = list(item["citations"])
        if item.get("url") and not any(c.get("url") == item["url"] for c in citations):
            citations.append({"kind": "url", "url": item["url"]})
        updates.append(Update(_title(item["title"]), item.get("publisher") or "PDF Source Updates", "PDF Source Updates",
            item.get("core_article_id") or item["pdf_article_id"], None, None, coverage,
            _paragraphs(item.get("summary")) + _caveats(item), (("File", f"{item['filename']}, page {item['page']}"), ("SHA-256", item["document_sha256"])), _citations(citations),
            content_sha256=item.get("content_sha256")))
    meeting = snapshot["meeting"]
    notes = []
    records = []
    if meeting["status"] == "not_requested":
        notes.append("Key dates were not captured in this snapshot.")
    else:
        label = "Meeting query" if meeting.get("source") == "query" else "Meeting snapshot"
        notes.append(f"{label} status: {meeting['status']}.")
        notes.extend(f"{key}: {value}" for key, value in meeting_metadata(meeting))
        if meeting["status"] == "empty":
            notes.append("No future meetings.")
        records.extend(meeting["records"])
    calendar = snapshot.get("pdf_calendar")
    if calendar is not None:
        notes.append(f"PDF calendar status: {calendar['status']}. Coverage: {calendar['coverage'].get('status', 'unavailable')}.")
        records.extend(calendar["records"])
    return Report("range", snapshot["snapshot_id"], identity, f"Climate Registry report {start} to {end}",
        "Range report", f"{start} through {end} (inclusive, {snapshot['timezone']})", run_date,
        tuple(range_executive_summary(snapshot)), tuple(c for update in updates for c in update.citations),
        tuple(updates), key_dates=tuple(row for item in records for row in _key_date_rows(item, run_date)), date_notes=tuple(notes),
        **_optional_tables(snapshot))


def adapt_weekly_report(summary: dict[str, Any]) -> Report:
    report = summary["report"]
    report_date, identity = _date(report["date"]), _identity(report["sha256"])
    run_date = _date(report["run_date"]) if report.get("run_date") else None
    updates = []
    for item in summary["highlights"]:
        provenance = summary.get("article_provenance", {}).get(item["url"], {})
        semantics = summary.get("article_semantics", {}).get(item["url"], {})
        coverage = provenance.get("coverage_period")
        updates.append(Update(_title(item["title"]), provenance.get("source") or "Publisher not recorded",
            ", ".join(semantics.get("categories", [])) or "Topic not recorded",
            provenance.get("article_id"), provenance.get("content_version_id"),
            _date(provenance["publication_date"]) if provenance.get("publication_date") else None,
            (_date(coverage["start"]), _date(coverage["end"])) if coverage else None,
            _paragraphs(item["summary"]) + _caveats(provenance), (("Pillar", item["pillar"]),) +
            ((("Report article version", provenance["report_version_id"]),) if provenance.get("report_version_id") else ()) +
            _date_basis(provenance.get("publication_date_evidence") or provenance.get("date_basis")) +
            tuple((label, ", ".join(semantics[field])) for label, field in (("Categories", "categories"), ("Keywords", "keywords")) if semantics.get(field)),
            _citations(provenance.get("citations") or [{"kind": "url", "url": item["url"]}]),
            content_sha256=provenance.get("content_hash")))
    sites = report.get("sites", {})
    statistics = tuple((label, str(sites[key]) if sites.get(key) is not None else "unavailable") for label, key in (("Sites checked", "checked"), ("Succeeded", "succeeded"), ("Failed", "failed")))
    statistics += tuple((f"Pillar {pillar} updates", str(sum(item["pillar"] == pillar for item in summary["highlights"]))) for pillar in ("A", "B"))
    return Report("weekly", f"climate-monitor-{report_date}:{identity}", identity, _title(report["title"]),
        report.get("edition") or "Weekly report", report.get("window") or f"Report week of {report_date}; coverage window not provided",
        run_date,
        tuple(summary["executive_summary"]), tuple(c for update in updates for c in update.citations),
        tuple(updates), key_dates=tuple(row for item in summary.get("key_dates", []) for row in _key_date_rows(item, run_date)),
        date_notes=() if summary.get("key_dates") else ("Key dates were not provided in the frozen summary.",),
        statistics=statistics, coverage_notes=tuple(summary.get("monitoring_notes", [])), **_optional_tables(summary))
