"""Two input adapters. See CONTRACT.md for mapping and missing-data rules."""
import re
import json
import hashlib
from datetime import date, datetime
from typing import Any

from climate_monitor.publisher_mapping import publisher_name

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


def _material_summary_paragraphs(item):
    return tuple("Source verification summary: "+value["summary"] for value in item.get("selected_material_summaries",[]))


def _date_basis(value: Any) -> tuple[tuple[str, str], ...]:
    if not value:
        return ()
    return (("Date basis", value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)),)


def _caveats(item: dict) -> tuple[str, ...]:
    caveats = item.get("caveats", [])
    if item.get("caveat"):
        caveats = [item["caveat"], *caveats]
    return tuple(caveats)


def _range_date_label(item: dict[str, Any]) -> str:
    if item.get("material_versions"):
        value = max(item["material_versions"], key=lambda v: v["original_period_time"])
        return value["selection_reason"].replace("_", " ") + " " + value["original_period_time"]
    basis = item.get("date_basis")
    if basis == "collection_time":
        return f"collection time {item.get('collected_at') or 'not recorded'}"
    if basis == "information_date":
        return f"information date {item.get('information_date') or item.get('range_date') or 'not recorded'}"
    if basis == "publication_date":
        return f"publication date {item.get('publication_date') or item.get('range_date') or 'not recorded'}"
    if basis == "report_date":
        return f"report date {item.get('range_date') or 'not recorded'} (article publication date unconfirmed)"
    return f"publication date {item.get('publication_date') or 'not recorded'}"


def _range_selection_summary(articles: list[dict[str, Any]], start: str, end: str) -> str:
    if not articles:
        return f"No evidenced Registry articles were selected for {start} through {end}."
    if not any(item.get("date_basis") for item in articles):
        return f"{len(articles)} evidenced Registry article(s) were published from {start} through {end}."
    counts = {basis: sum(item.get("date_basis") == basis for item in articles)
              for basis in ("collection_time", "information_date", "publication_date", "report_date")}
    details = []
    for basis, label in (("collection_time", "collection time"),
                         ("information_date", "page information date"),
                         ("publication_date", "publication date"), ("report_date", "report date")):
        if counts[basis]:
            details.append(f"{counts[basis]} by {label}")
    return f"{len(articles)} evidenced Registry article(s) were selected for {start} through {end} ({'; '.join(details)})."


def range_executive_summary(snapshot: dict[str, Any]) -> list[str]:
    """Existing range narrative: copied stored summaries, never regenerated."""
    articles = snapshot["articles"]
    pdf_updates = snapshot.get("pdf_source_updates", [])
    exclusions = snapshot.get("pdf_source_exclusion_counts", {})
    start, end = snapshot["date_range"]["start"], snapshot["date_range"]["end"]
    summary = [_range_selection_summary(articles, start, end)]
    points = snapshot.get("executive_summary")
    if points is None:
        points = [dict(item, kind="registry_article", text=item.get("summary")) for item in articles if isinstance(item.get("summary"), str) and item["summary"].strip()]
        points += [dict(item, kind="pdf_source", text=item.get("summary")) for item in pdf_updates if isinstance(item.get("summary"), str) and item["summary"].strip()]
    article_points = [point for point in points if point["kind"] == "registry_article"]
    pdf_points = [point for point in points if point["kind"] == "pdf_source"]
    if not articles and not pdf_updates:
        summary.append("No selected Registry articles or PDF source updates matched this range.")
    if article_points:
        summary.extend(
            f"{point['title']}: {point['text']} ({_range_date_label(point)})."
            for point in article_points
        )
    elif articles:
        summary.append("No stored Registry article summaries are available for the selected range.")
    if "pdf_source_updates" in snapshot:
        dated = sum(bool(item.get("publication_date")) for item in pdf_updates)
        if dated:
            summary.append(f"{dated} update(s) carry publication dates stated in the imported PDF inside the requested range.")
        unknown = len(pdf_updates) - dated
        if unknown or not pdf_updates:
            summary.append(f"{unknown} PDF source update(s) overlap the range; their article publication dates are unconfirmed.")
        if pdf_points:
            for item in pdf_updates:
                if item.get("summary"):
                    basis = (f"PDF-stated publication date {item['publication_date']}" if item.get("publication_date")
                        else f"article publication date unconfirmed; coverage period {item['coverage_period']['start']} through {item['coverage_period']['end']}")
                    summary.append(f"{item['summary']} ({basis}).")
        elif pdf_updates:
            summary.append("No stored PDF source summaries are available for the selected range.")
        summary.append(f"PDF source observations excluded: {exclusions.get('unknown_coverage', 0)} with unknown coverage; {exclusions.get('non_overlapping_coverage', 0)} with non-overlapping coverage.")
    if snapshot.get("date_unknown_count", 0):
        summary.append(f"{snapshot['date_unknown_count']} Registry article(s) with no reliable collection or information date were excluded.")
    elif "date_unknown_count" not in snapshot and snapshot["unknown_publication_date_count"]:
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
    unresolved_end = bool(start and not end and item.get("needs_confirmation"))
    if unresolved_end:
        parts.append("End date unconfirmed")
    if run_date and dates and not unresolved_end:
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
    citations.extend(item.get("source_urls") or [])
    citations.extend(source["source_url"] for source in item.get("sources", []) if source.get("source_url"))
    if item.get("online_url"):
        citations.append(item["online_url"])
    citation = "\n".join(dict.fromkeys(citations)) or "Source not provided"
    imported = item.get("source_kind") == "pdf" or bool(item.get("source_document_sha256"))
    if imported and item.get("raw_date"):
        when = item["raw_date"]
    for detail in (item.get("raw_time_text"), item.get("event_timezone") or item.get("timezone")):
        if detail and detail not in when:
            when += "\n" + detail
    event = item.get("name") or "Key date"
    if imported:
        citation = "\n".join(dict.fromkeys(value for value in citations if value.startswith(("https://", "http://")))) + "\nPDF import"
    host = str(item.get("publisher") or item.get("institution") or item.get("organizer") or item.get("source") or "Not provided")
    return (str(when), str(event),
            host,
            str(item.get("relevance") or item.get("actuarial_relevance") or item.get("relevance_reason") or "Not provided"), str(citation))


def calendar_details(item: dict[str, Any]) -> tuple[str, ...]:
    """Return supplied venue/context outside the linked event-name cell."""
    details = []
    if item.get("location"):
        details.append("Location: " + str(item["location"]))
    source_urls = [value for value in (item.get("url"), item.get("source_url")) if value]
    source_urls.extend(item.get("source_urls") or [])
    source_urls.extend(source["source_url"] for source in item.get("sources", []) if source.get("source_url"))
    online_url = item.get("online_url")
    if online_url and source_urls and online_url not in source_urls:
        details.append("Online event: " + str(online_url))
    if item.get("deadline_type"):
        details.append("Deadline type: " + str(item["deadline_type"]))
    source_urls = list(dict.fromkeys(source_urls))[1:]
    if source_urls:
        details.append("Additional sources: " + ", ".join(source_urls))
    if item.get("summary") and item.get("calendar_field_basis") != "pdf_table_cells":
        details.append("Verbatim context: " + str(item["summary"]))
    return tuple(details)


def calendar_detail_notes(items: list[dict[str, Any]]) -> tuple[str, ...]:
    notes = []
    for item in items:
        details = calendar_details(item)
        if details:
            notes.append("Calendar details: " + str(item.get("name") or "Key date") + " — " + "; ".join(details))
    return tuple(notes)


def _key_date_rows(item: dict[str, Any], run_date: str | None = None) -> tuple[tuple[str, str, str, str, str], ...]:
    deadline = item.get("deadline_date")
    rows = []
    pure_deadline = item.get("event_type") == "deadline" and not item.get("start_date") and not item.get("end_date")
    if not deadline or (not pure_deadline and any(item.get(field) for field in ("start_date", "end_date", "raw_date", "raw_time_text"))):
        rows.append(_key_date(item, run_date))
    if deadline:
        # The meeting contract verifies deadline_date independently of event precision.
        deadline = _date(deadline)
        deadline_type = item.get("deadline_type")
        name = item.get("name") or "Key date"
        if deadline_type:
            name = f"Deadline: {deadline_type} — {name}"
        rows.append(_key_date(dict(item, start_date=deadline, end_date=deadline,
            date_precision="day", raw_date=item.get("raw_date") if pure_deadline else None, raw_time_text=None,
            name=name), run_date))
    return tuple(rows)


def calendar_date_bounds(when: str) -> tuple[str | None, str | None, str]:
    from climate_monitor.pdf_intake import _parse_calendar_date
    from climate_monitor.meeting_fields import TIME_TEXT, TIMEZONE_TEXT
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:\n| through |$)", when):
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", when.split("\n")[0])
        precision = re.search(r"Precision: (\w+)", when)
        return dates[0], dates[-1], precision[1] if precision else "day"
    parsed = _parse_calendar_date(when)
    if parsed["date_precision"] == "unknown":
        suffixes = [match.start() for pattern in (TIME_TEXT, TIMEZONE_TEXT) if (match := pattern.search(when))]
        if suffixes:
            parsed = _parse_calendar_date(when[:min(suffixes)].rstrip(" ,;("))
    return parsed["start_date"], parsed["end_date"] or parsed["start_date"], parsed["date_precision"]


def _optional_tables(value: dict) -> dict:
    return {
        "coverage": tuple((str(row["institution"]), str(row["status"]), str(row.get("detail") or "Not provided")) for row in value.get("coverage", [])),
        "route_corrections": tuple((str(row["source"]), str(row["detail"])) for row in value.get("route_corrections", [])),
        "glossary": tuple((str(row["term"]), str(row["definition"])) for row in value.get("glossary", [])),
        "cross_cutting_watch": tuple(value.get("cross_cutting_watch", [])),
    }


def adapt_range_report(snapshot: dict[str, Any]) -> Report:
    identity = _identity(snapshot["snapshot_sha256"])
    prefix = "biweekly-" if snapshot.get("schema_version") == "climate-biweekly-report.v1" else "range-report-"
    if snapshot["snapshot_id"] != prefix + identity[:24]:
        raise ValueError("range report identity is invalid")
    excluded = {"snapshot_id", "snapshot_sha256"} if prefix == "biweekly-" else {"snapshot_id", "snapshot_sha256", "created_at"}
    frozen = {key: value for key, value in snapshot.items() if key not in excluded}
    if hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() != identity:
        raise ValueError("range report input sha256 does not match frozen content")
    start, end = _date(snapshot["date_range"]["start"]), _date(snapshot["date_range"]["end"])
    if start > end:
        raise ValueError("report date window is invalid")
    run_date = datetime.fromisoformat(snapshot["created_at"].replace("Z", "+00:00")).date().isoformat()
    updates = []
    for item in snapshot["articles"]:
        basis = item.get("date_basis")
        date_metadata = (("Date basis", _range_date_label(item)),)
        if basis == "report_date":
            date_metadata += (("Report date", item["range_date"]),
                              ("Article publication date", "Unconfirmed"))
        if item.get("publication_date") and basis != "publication_date":
            date_metadata += (("Publication date", item["publication_date"]),)
        if item.get("information_date") and basis != "information_date":
            date_metadata += (("Information date", item["information_date"]),)
        citations = _citations(item["citations"] or ([{"kind": "url", "url": item["canonical_url"]}] if item.get("canonical_url") else []))
        institution = publisher_name(item.get("publisher"), [citation.url for citation in citations if citation.url])
        updates.append(Update(_title(item["title"]), institution,
            ", ".join(item.get("categories", [])) or "Topic not recorded", _title(item["article_id"]),
            item.get("content_version_id"), _date(item["publication_date"]) if item.get("publication_date") else None, None,
            _paragraphs(item.get("summary"), item.get("content")) + _material_summary_paragraphs(item) + _caveats(item),
            date_metadata + tuple((label, ", ".join(item[field])) for label, field in (("Categories", "categories"), ("Keywords", "keywords")) if item.get(field)),
            citations,
            content_sha256=item.get("provenance", {}).get("content_version", {}).get("content_sha256"),
            imported_from_pdf=any(c.get("kind") in {"pdf", "pdf_page"} for c in item["citations"]),
            date_basis=basis, information_date=item.get("information_date"), collected_at=item.get("collected_at"),
            report_date=item.get("range_date") if basis == "report_date" else None))
    for item in snapshot.get("pdf_source_updates", []):
        coverage = (_date(item["coverage_period"]["start"]), _date(item["coverage_period"]["end"]))
        if coverage[0] > coverage[1]:
            raise ValueError("PDF coverage dates are invalid")
        citations = list(item["citations"])
        if item.get("url") and not any(c.get("url") == item["url"] for c in citations):
            citations.append({"kind": "url", "url": item["url"]})
        source_urls = [item.get("url"), *(citation.get("url") for citation in item.get("citations", []))]
        institution = publisher_name(item.get("publisher"), source_urls)
        updates.append(Update(_title(item["title"]), institution, item.get("topic") or "PDF Source Updates",
            item.get("core_article_id") or item["pdf_article_id"], None,
            _date(item["publication_date"]) if item.get("publication_date") else None, coverage,
            _paragraphs(item.get("summary")) + _material_summary_paragraphs(item) + _caveats(item), (("File", f"{item['filename']}, page {item['page']}"), ("SHA-256", item["document_sha256"])) +
            ((("PDF article ID", item["pdf_article_id"]),) if item.get("core_article_id") else ()), _citations(citations),
            content_sha256=item.get("content_sha256"),
            article_id_label="Core article ID" if item.get("core_article_id") else "PDF article ID",
            imported_from_pdf=True))
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
    from climate_monitor.meeting_fields import merge_meeting_observations
    records = merge_meeting_observations(records)
    notes.extend(calendar_detail_notes(records))
    # The full stored summaries appear once in the numbered updates. The
    # executive page states selection facts, without repeating raw PDF rows.
    executive = [_range_selection_summary(snapshot["articles"], start, end)]
    if "pdf_source_updates" in snapshot:
        dated = sum(bool(item.get("publication_date")) for item in snapshot["pdf_source_updates"])
        unknown = len(snapshot["pdf_source_updates"]) - dated
        if dated:
            executive.append(f"{dated} update(s) carry publication dates stated in the imported PDF inside the requested range.")
        if unknown:
            executive.append(f"{unknown} PDF update(s) overlap the range; their publication days are unconfirmed.")
        exclusions = snapshot.get("pdf_source_exclusion_counts", {})
        executive.append(f"PDF source observations excluded: {exclusions.get('unknown_coverage', 0)} with unknown coverage; {exclusions.get('non_overlapping_coverage', 0)} with non-overlapping coverage.")
    if snapshot.get("date_unknown_count", 0):
        executive.append(f"{snapshot['date_unknown_count']} Registry article(s) with no reliable collection or information date were excluded.")
    elif "date_unknown_count" not in snapshot and snapshot['unknown_publication_date_count']:
        executive.append(f"{snapshot['unknown_publication_date_count']} Registry article(s) with unknown publication dates were excluded.")
    if snapshot.get("schema_version") == "climate-biweekly-report.v1":
        executive = [f"{len(snapshot['articles'])} article(s) and {len(snapshot.get('pdf_source_updates', []))} PDF update(s) were selected by first ingestion or substantive information changes in the New York 14-day window.",
            "Original publication dates are preserved independently of the selection time."]
        late = sum(v["selection_reason"] == "late_review_carryforward" for v in snapshot["material_versions"])
        if late:
            executive.append(f"{late} previously unreported material version(s) were carried forward after delayed acquisition review.")
        delayed = sum(v["selection_reason"] == "delayed_activation_carryforward" for v in snapshot["material_versions"])
        if delayed:
            executive.append(f"{delayed} previously unreported material version(s) were carried forward after delayed activation.")
        if snapshot.get("coverage_gaps"):
            executive.append(f"{len(snapshot['coverage_gaps'])} material version(s) lack reliable legacy ingestion times and remain a coverage gap.")
    return Report("range", snapshot["snapshot_id"], identity, f"Climate Registry report {start} to {end}",
        "Range report", f"{start} through {end} (inclusive, {snapshot['timezone']})", run_date,
        tuple(executive), (),
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
        citations = _citations(provenance.get("citations") or [{"kind": "url", "url": item["url"]}])
        institution = publisher_name(provenance.get("source"), [citation.url for citation in citations if citation.url])
        updates.append(Update(_title(item["title"]), institution,
            ", ".join(semantics.get("categories", [])) or "Topic not recorded",
            provenance.get("article_id"), provenance.get("content_version_id"),
            _date(provenance["publication_date"]) if provenance.get("publication_date") else None,
            (_date(coverage["start"]), _date(coverage["end"])) if coverage else None,
            _paragraphs(item["summary"]) + _caveats(provenance), (("Pillar", item["pillar"]),) +
            ((("Report article version", provenance["report_version_id"]),) if provenance.get("report_version_id") else ()) +
            _date_basis(provenance.get("publication_date_evidence") or provenance.get("date_basis")) +
            tuple((label, ", ".join(semantics[field])) for label, field in (("Categories", "categories"), ("Keywords", "keywords")) if semantics.get(field)),
            citations,
            content_sha256=provenance.get("content_hash"),
            imported_from_pdf=any(c.get("kind") in {"pdf", "pdf_page"} for c in provenance.get("citations", []))))
    sites = report.get("sites", {})
    statistics = tuple((label, str(sites[key]) if sites.get(key) is not None else "unavailable") for label, key in (("Sites checked", "checked"), ("Succeeded", "succeeded"), ("Failed", "failed")))
    statistics += tuple((f"Pillar {pillar} updates", str(sum(item["pillar"] == pillar for item in summary["highlights"]))) for pillar in ("A", "B"))
    return Report("weekly", f"climate-monitor-{report_date}:{identity}", identity, _title(report["title"]),
        report.get("edition") or "Weekly report", report.get("window") or f"Report week of {report_date}; coverage window not provided",
        run_date,
        tuple(summary["executive_summary"]), tuple(c for update in updates for c in update.citations),
        tuple(updates), key_dates=tuple(row for item in summary.get("key_dates", []) for row in _key_date_rows(item, run_date)),
        date_notes=calendar_detail_notes(summary.get("key_dates", [])) if summary.get("key_dates") else ("Key dates were not provided in the frozen summary.",),
        statistics=statistics, coverage_notes=tuple(summary.get("monitoring_notes", [])), **_optional_tables(summary))
