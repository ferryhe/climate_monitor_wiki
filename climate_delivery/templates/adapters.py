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
    # Stored summaries are the reader-facing excerpt. The full captured body
    # remains frozen for provenance and is only displayed when no summary was
    # supplied; concatenating both commonly repeats the lead paragraph.
    value = next((value for value in values if value and value.strip()), None)
    if value is None:
        return ()
    parts = [_reader_text(part) for part in value.split("\n\n")]
    if value == values[0] and len(values) > 1 and values[1]:
        repeated_title = re.match(r"^(.{24,120}?)\s+\1(?=\s|$)", parts[0] if parts else "")
        if repeated_title:
            parts[0] = parts[0][:repeated_title.end(1)] + parts[0][repeated_title.end():]
    return tuple(part for part in parts if part)


def _reader_text(value: str) -> str:
    """Remove navigation/formatting noise while retaining source prose."""
    value = re.sub(r"!\[[^]]*\]\([^)]*\)", "", value)
    value = re.sub(r"\[([^]]+)\]\((https?://[^ )]+)\)", r"\1 (\2)", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value, flags=re.M)
    value = re.sub(r"<!--.*?-->|</?[A-Za-z][A-Za-z0-9:-]*(?:\s+[^<>]*?)?\s*/?>", " ", value, flags=re.S)
    value = re.sub(r"(?im)^\s*(?:skip to content|menu|navigation|accept all cookies|cookie settings)\s*$", "", value)
    value = re.sub(r"(?i)cookies?\s*[🍪]?\s*This site uses cookies that need consent\.\s*(?:Accept all\s*)?(?:Accept selected\s*)?Reject all\s*", "", value)
    value = re.sub(r"(?i)Our work\s+Get involved\s+Resources\s+About Ceres\s+Donate\s+Search\s+Search Issues we work on", "", value)
    value = re.sub(r"(?i)About\s+News\s+Events\s+Programmes\s+Help centre\s+Skip navigation", "", value)
    value = re.sub(r"(?i)\bSkip (?:to content|navigation)\b|\bOpen main menu\b", "", value)
    value = re.sub(r"(?i)PERSONAL\s*BUSINESS\s*ABOUT\s*AXA\s*BetterMe\s*Blog\s*CONTACT\s*US\s*LOGIN\s*OUR\s*COMPANY\s*NEWS\s*ROOM\s*JOIN\s*US\s*News\s*Room", "", value)
    value = re.sub(r"(?i)PERSONAL\s*BUSINESS\s*ABOUT(?:\s+US)?\s*CONTACT\s*US\s*LOGIN", "", value)
    value = re.sub(r"\*{1,3}|_{1,3}|`{1,3}", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


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


def _precision_date(value: str, precision: str | None) -> str:
    """Keep partial dates partial when showing or calculating their interval."""
    if precision == "month" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value[:7]
    if precision == "quarter" and re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value):
        year, month = map(int, value[:7].split("-"))
        return f"{year}-Q{(month - 1) // 3 + 1}"
    if precision == "year" and re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value):
        return value[:4]
    return value


def _range_date_label(item: dict[str, Any]) -> str:
    if item.get("material_versions"):
        value = max(item["material_versions"], key=lambda v: v["original_period_time"])
        return value["selection_reason"].replace("_", " ") + " " + value["original_period_time"].split("T", 1)[0]
    basis = item.get("date_basis")
    if basis == "collection_time":
        collected = (item.get("collected_at") or "").split("T", 1)[0]
        return "newly collected" + (f" {collected}" if collected else "; date not recorded")
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
    """Show the frozen synthesis or explain its statistical fallback."""
    articles = snapshot["articles"]
    pdf_updates = snapshot.get("pdf_source_updates", [])
    exclusions = snapshot.get("pdf_source_exclusion_counts", {})
    start, end = snapshot["date_range"]["start"], snapshot["date_range"]["end"]
    narrative = snapshot.get("executive_narrative")
    if isinstance(narrative, dict) and narrative.get("status") == "available":
        return [f"{row['heading']}: {text}" for row in narrative["sections"] for text in row["items"]]
    basis_counts = {basis: sum(item.get("date_basis") == basis for item in articles)
                    for basis in ("collection_time", "information_date", "publication_date", "report_date")}
    summary = [f"Reporting window: {start} through {end} (inclusive).",
        f"Selected updates: {len(articles)} Registry articles and {len(pdf_updates)} imported PDF updates.",
        "Selection dates: " + ("; ".join(f"{count} {basis.replace('_', ' ')}" for basis, count in basis_counts.items() if count)
            or "no dated Registry records") + "."]
    if not articles and not pdf_updates:
        summary.append("No selected Registry articles or imported PDF updates matched this range.")
    dated = sum(bool(item.get("publication_date")) for item in pdf_updates)
    if pdf_updates:
        summary.append(f"PDF publication dates: {dated} recorded; {len(pdf_updates) - dated} unconfirmed.")
        summary.append(f"PDF observations excluded: {exclusions.get('unknown_coverage', 0)} unknown coverage; {exclusions.get('non_overlapping_coverage', 0)} outside the window.")
    if snapshot.get("date_unknown_count", 0):
        summary.append(f"Registry articles excluded for missing reliable dates: {snapshot['date_unknown_count']}.")
    elif "date_unknown_count" not in snapshot and snapshot["unknown_publication_date_count"]:
        summary.append(f"Registry articles excluded for unknown publication dates: {snapshot['unknown_publication_date_count']}.")
    narrative_status = (narrative or {}).get("status", "offline")
    summary.append(f"LLM narrative status: {narrative_status}; this summary contains snapshot statistics only.")
    return summary


def _key_date(item: dict[str, Any], run_date: str | None = None) -> tuple[str, str, str, str, str]:
    start, end = item.get("start_date"), item.get("end_date")
    precision = item.get("date_precision") or item.get("precision")
    dates = list(dict.fromkeys(_precision_date(value, precision) for value in (start, end) if value))
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
            lower = _date_bounds(_precision_date(start or end, precision or "day"), precision or "day", end=False)
            upper = _date_bounds(_precision_date(end or start, precision or "day"), precision or "day", end=True)
            base = date.fromisoformat(run_date)
            if lower <= upper:
                marker = "past" if upper < base else "future" if lower > base else "current"
                parts.append(f"({marker})")
        except ValueError:
            pass  # Unknown precision/raw dates have no invented calendar bounds.
    when = "\n".join(parts) or "date unavailable"
    citations = []
    source_locators = [(item.get("source_filename"), item.get("page"))]
    source_locators.extend((observation.get("source_filename"), observation.get("page"))
        for observation in item.get("pdf_observations", []))
    citations.extend(f"{filename}, page {page}" for filename, page in dict.fromkeys(source_locators) if filename and page)
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
        # Retain the source wording while sorting/highlighting from recorded bounds.
        bounded_date = " through ".join(dates) if dates else None
        when = "\n".join(value for value in (bounded_date,
            item["raw_date"] if item["raw_date"] != bounded_date else None,
            f"Precision: {precision}" if precision else None,
            "End date unconfirmed" if unresolved_end else None) if value)
    for detail in (item.get("raw_time_text"), item.get("event_timezone") or item.get("timezone")):
        if detail and detail not in when:
            when += "\n" + detail
    event = item.get("name") or "Key date"
    details = calendar_details(item)
    if details:
        event += "\n" + "\n".join(details)
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
    if online_url and online_url not in source_urls:
        details.append("Registration / event link: " + str(online_url))
    if item.get("deadline_type"):
        details.append("Deadline type: " + str(item["deadline_type"]))
    source_locators = [(item.get("source_filename"), item.get("page"))]
    source_locators.extend((observation.get("source_filename"), observation.get("page"))
        for observation in item.get("pdf_observations", []))
    for filename, page in dict.fromkeys(source_locators):
        if filename and page:
            details.append(f"Source: {filename}, page {page}")
    source_urls = list(dict.fromkeys(source_urls))[1:]
    if source_urls:
        details.append("Additional source: " + ", ".join(source_urls))
    return tuple(details)


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


def _calendar_source_alias_groups(records: list[dict[str, Any]]) -> tuple[tuple[str, ...], ...]:
    """Mirror the meeting merge identity so each source citation follows its row."""
    groups: dict[str, list[str]] = {}
    for source_index, item in enumerate(records, 1):
        imported = bool(item.get("source_document_sha256"))
        identity = ((item.get("canonical_event_id") or item.get("canonical_pdf_event_id") or item.get("occurrence_id"))
            if imported else item.get("event_id"))
        identity = identity or str(len(groups))
        groups.setdefault(identity, []).append(f"C{source_index}")
    return tuple(tuple(aliases) for aliases in groups.values())


def calendar_date_bounds(when: str) -> tuple[str | None, str | None, str]:
    from climate_monitor.pdf_intake import _parse_calendar_date
    from climate_monitor.meeting_fields import TIME_TEXT, TIMEZONE_TEXT
    precision = re.search(r"Precision: (\w+)", when)
    recorded_precision = precision[1].casefold() if precision else None
    if recorded_precision in {"time", "hour", "minute", "datetime"}:
        recorded_precision = "day"
    date_text = when.split("Precision:", 1)[0].strip()
    if recorded_precision in {"quarter", "year"}:
        partial = re.search(r"\b\d{4}-Q[1-4]\b|\b\d{4}\b", date_text)
        if partial:
            from climate_monitor.meetings import _date_bounds
            try:
                return (_date_bounds(partial[0], recorded_precision, end=False).isoformat(),
                        _date_bounds(partial[0], recorded_precision, end=True).isoformat(),
                        recorded_precision)
            except ValueError:
                pass
    iso_dates = re.findall(r"\b\d{4}-\d{2}(?:-\d{2})?\b", date_text)
    if recorded_precision and iso_dates:
        from climate_monitor.meetings import _date_bounds
        try:
            lower = _date_bounds(iso_dates[0], recorded_precision, end=False).isoformat()
            upper = _date_bounds(iso_dates[-1], recorded_precision, end=True).isoformat()
            return lower, upper, recorded_precision
        except ValueError:
            pass
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:\n| through |$)", date_text):
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", date_text.split("\n")[0])
        return dates[0], dates[-1], recorded_precision or "day"
    if recorded_precision:
        from climate_monitor.meetings import _date_bounds
        parsed_text = date_text.splitlines()[-1] if len(date_text.splitlines()) > 1 else date_text
        parsed_raw = _parse_calendar_date(parsed_text)
        candidate = parsed_raw.get("start_date")
        if candidate:
            try:
                lower = _date_bounds(candidate, recorded_precision, end=False).isoformat()
                upper = _date_bounds(candidate, recorded_precision, end=True).isoformat()
                return lower, upper, recorded_precision
            except ValueError:
                pass
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
    excluded = {"snapshot_id", "snapshot_sha256", "executive_narrative"} if prefix == "biweekly-" else {"snapshot_id", "snapshot_sha256", "created_at", "executive_narrative"}
    frozen = {key: value for key, value in snapshot.items() if key not in excluded}
    if hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() != identity:
        raise ValueError("range report input sha256 does not match frozen content")
    start, end = _date(snapshot["date_range"]["start"]), _date(snapshot["date_range"]["end"])
    if start > end:
        raise ValueError("report date window is invalid")
    run_date = datetime.fromisoformat(snapshot["created_at"].replace("Z", "+00:00")).date().isoformat()
    updates = []
    article_update_refs = {}
    pdf_update_refs = {}
    for item_index, item in enumerate(snapshot["articles"], 1):
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
        update = Update(_title(item["title"]), institution,
            ", ".join(item.get("categories", [])) or "Topic not recorded", _title(item["article_id"]),
            item.get("content_version_id"), _date(item["publication_date"]) if item.get("publication_date") else None, None,
            _paragraphs(item.get("summary"), item.get("content")) + _material_summary_paragraphs(item) + _caveats(item),
            date_metadata,
            citations,
            content_sha256=item.get("provenance", {}).get("content_version", {}).get("content_sha256"),
            imported_from_pdf=any(c.get("kind") in {"pdf", "pdf_page"} for c in item["citations"]),
            date_basis=basis, information_date=item.get("information_date"), collected_at=item.get("collected_at"),
            report_date=item.get("range_date") if basis == "report_date" else None)
        updates.append(update)
        article_update_refs[item_index] = update
    for item_index, item in enumerate(snapshot.get("pdf_source_updates", []), 1):
        coverage = (_date(item["coverage_period"]["start"]), _date(item["coverage_period"]["end"]))
        if coverage[0] > coverage[1]:
            raise ValueError("PDF coverage dates are invalid")
        citations = list(item["citations"])
        if item.get("url") and not any(c.get("url") == item["url"] for c in citations):
            citations.append({"kind": "url", "url": item["url"]})
        source_urls = [item.get("url"), *(citation.get("url") for citation in item.get("citations", []))]
        institution = publisher_name(item.get("publisher"), source_urls)
        topic = (", ".join(item["categories"]) or "PDF Source Updates") if "categories" in item else item.get("topic") or "PDF Source Updates"
        category_metadata = (("Categories", ", ".join(item["categories"])),) if item.get("categories") else ()
        source_topic = (("Source topic", item["topic"]),) if "categories" in item and item.get("topic") else ()
        update = Update(_title(item["title"]), institution, topic,
            item.get("core_article_id") or item["pdf_article_id"], None,
            _date(item["publication_date"]) if item.get("publication_date") else None, coverage,
            _paragraphs(item.get("summary")) + _material_summary_paragraphs(item) + _caveats(item), (("File", f"{item['filename']}, page {item['page']}"), ("SHA-256", item["document_sha256"])) +
            ((("PDF article ID", item["pdf_article_id"]),) if item.get("core_article_id") else ()) + category_metadata + source_topic, _citations(citations),
            content_sha256=item.get("content_sha256"),
            article_id_label="Core article ID" if item.get("core_article_id") else "PDF article ID",
            imported_from_pdf=True)
        updates.append(update)
        pdf_update_refs[item_index] = update
    meeting = snapshot["meeting"]
    notes = []
    records = []
    if meeting["status"] == "not_requested":
        notes.append("Key dates were not captured in this snapshot.")
    else:
        coverage = (meeting.get("coverage") or {}).get("status")
        if meeting["status"] in {"partial", "failed", "unavailable"} or coverage in {"partial", "failed", "unavailable"}:
            notes.append(f"Meeting coverage is {coverage or meeting['status']}; listed entries may be incomplete.")
        elif meeting["status"] == "empty" and not (snapshot.get("pdf_calendar") or {}).get("records"):
            notes.append("No future meetings were recorded in the frozen snapshot.")
        records.extend(meeting["records"])
    calendar = snapshot.get("pdf_calendar")
    if calendar is not None:
        if calendar["status"] == "unavailable" or calendar.get("coverage", {}).get("status") in {"partial", "unavailable", "failed"}:
            status = calendar.get("coverage", {}).get("status") or calendar["status"]
            notes.append(f"Imported PDF calendar coverage is {status}; listed entries may be incomplete.")
        records.extend(calendar["records"])
    from climate_monitor.meeting_fields import merge_meeting_observations
    calendar_alias_groups = _calendar_source_alias_groups(records)
    records = merge_meeting_observations(records)
    calendar_targets = []
    calendar_row_targets = []
    key_dates = []
    for group_index, item in enumerate(records, 1):
        row_start = len(key_dates) + 1
        target = f"calendar-record-{group_index}"
        calendar_row_targets.append((row_start, target))
        key_dates.extend(_key_date_rows(item, run_date))
        if group_index <= len(calendar_alias_groups):
            calendar_targets.extend((alias, target) for alias in calendar_alias_groups[group_index - 1])
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
    if snapshot.get("schema_version") != "climate-biweekly-report.v1":
        executive = range_executive_summary(snapshot)
    narrative = snapshot.get("executive_narrative") or {}
    # The PDF groups by institution while preserving source order inside each
    # institution. Keep evidence labels aligned with those printed numbers.
    ordered_updates = sorted(updates, key=lambda update: (update.institution.casefold(), update.topic.casefold()))
    update_number_by_object = {}
    reader_update_targets = []
    for number, update in enumerate(ordered_updates, 1):
        update_number_by_object[id(update)] = number
    for source_index, update in article_update_refs.items():
        reader_update_targets.append((f"article-{source_index}", update_number_by_object[id(update)]))
    for source_index, update in pdf_update_refs.items():
        reader_update_targets.append((f"pdf-update-{source_index}", update_number_by_object[id(update)]))
    source_rows = {row.get("id"): row for row in narrative.get("evidence", []) if isinstance(row, dict)}
    citation_rows = []
    for evidence_id, row in source_rows.items():
        title = str(row.get("title") or row.get("name") or evidence_id)
        source_url = next((url for url in [row.get("url"), row.get("citation"),
            *(row.get("citations") or []), *(row.get("sources") or [])]
            if isinstance(url, str) and url.startswith(("http://", "https://"))), None)
        if evidence_id.startswith(("A", "P")):
            linked_update = (article_update_refs.get(row.get("article_index")) if evidence_id.startswith("A")
                else pdf_update_refs.get(row.get("pdf_index")))
            if linked_update is None:
                matches = [update for update in ordered_updates if update.article_id == row.get("article_id")]
                linked_update = matches[0] if len(matches) == 1 else None
            update_number = update_number_by_object.get(id(linked_update)) if linked_update else None
            if update_number is not None:
                label = f"Update {update_number}: {title}"
                pdf_target = f"update-{update_number}"
            else:
                label, pdf_target = title, None
            html_target = (f"article-{row['article_index']}" if evidence_id.startswith("A") and row.get("article_index")
                else f"pdf-update-{row['pdf_index']}" if evidence_id.startswith("P") and row.get("pdf_index") else None)
        else:
            label = "Calendar: " + title
            if row.get("date"):
                label += " — " + str(row["date"])
            pdf_target = html_target = dict(calendar_targets).get(evidence_id, "calendar")
        source_citation = str(row.get("citation")) if row.get("citation") and not source_url else None
        citation_rows.append((evidence_id, label, source_url, pdf_target, html_target, source_citation))
    executive_citations = tuple(citation_rows)
    executive_sections = tuple((row["heading"], tuple(row["items"]))
                               for row in narrative.get("sections", [])) if narrative.get("status") == "available" else ()
    if executive_sections:
        executive = []
    return Report("range", snapshot["snapshot_id"], identity, f"Climate Registry report {start} to {end}",
        "Range report", f"{start} through {end} (inclusive, {snapshot['timezone']})", run_date,
        tuple(executive), (),
        tuple(updates), key_dates=tuple(key_dates), date_notes=tuple(notes),
        **_optional_tables(snapshot), executive_citations=executive_citations,
        executive_sections=executive_sections, reader_update_targets=tuple(reader_update_targets),
        calendar_targets=tuple(calendar_targets), calendar_row_targets=tuple(calendar_row_targets))


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
        date_notes=() if summary.get("key_dates") else ("Key dates were not provided in the frozen summary.",),
        statistics=statistics, coverage_notes=tuple(summary.get("monitoring_notes", [])), **_optional_tables(summary))
