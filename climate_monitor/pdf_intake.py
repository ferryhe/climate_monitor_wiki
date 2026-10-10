"""Read report PDFs into a provenance-preserving, TypeSafe-assisted bundle."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from io import BytesIO
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pypdf import PdfReader

from climate_monitor.dedupe import canonical_url
from climate_registry.audit import _stable_id


MAX_PDF_BYTES = 50_000_000
MAX_PDF_PAGES = 500
TYPE_SAFE_BATCH_SIZE = 20
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_MONTHS = {name.casefold(): number for number, name in enumerate((
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
), 1)}
_MONTHS.update({name[:3].casefold(): number for name, number in _MONTHS.items() if len(name) > 3})
_CHOICES = {
    "article": None,
    "event": None,
    "landing_page": None,
    "other": None,
}


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _month_number(value: str) -> int | None:
    return _MONTHS.get(value.casefold())


def _iso_day(day: str, month: str, year: str) -> str | None:
    month_number = _month_number(month)
    if month_number is None:
        return None
    try:
        return date(int(year), month_number, int(day)).isoformat()
    except ValueError:
        return None


def _parse_calendar_date(raw: str) -> dict[str, Any]:
    value = _compact(raw).replace("—", "–").rstrip(" .·")
    quarter = re.fullmatch(r"Q([1-4])\s+(20\d{2})", value, re.I)
    if quarter:
        return {"start_date": f"{quarter[2]}-Q{quarter[1]}", "end_date": None, "date_precision": "quarter"}
    vague_year = re.fullmatch(r"(?:(?:Early|Mid|Late)\s+)?(20\d{2})", value, re.I)
    if vague_year:
        return {"start_date": vague_year[1], "end_date": None, "date_precision": "year"}
    month_year_range = re.fullmatch(rf"({_MONTH})\s+(20\d{{2}})\s*[–-]\s*({_MONTH})\s+(20\d{{2}})", value, re.I)
    if month_year_range:
        start_month = _month_number(month_year_range[1])
        end_month = _month_number(month_year_range[3])
        if start_month and end_month:
            return {
                "start_date": f"{month_year_range[2]}-{start_month:02d}",
                "end_date": f"{month_year_range[4]}-{end_month:02d}",
                "date_precision": "month",
            }
    month_range = re.fullmatch(rf"({_MONTH})\s*[–-]\s*({_MONTH})\s+(20\d{{2}})", value, re.I)
    if month_range:
        start_month = _month_number(month_range[1])
        end_month = _month_number(month_range[2])
        if start_month and end_month:
            return {
                "start_date": f"{month_range[3]}-{start_month:02d}",
                "end_date": f"{month_range[3]}-{end_month:02d}",
                "date_precision": "month",
            }
    one_month = re.fullmatch(rf"({_MONTH})\s+(20\d{{2}})", value, re.I)
    if one_month:
        month_number = _month_number(one_month[1])
        return {
            "start_date": f"{one_month[2]}-{month_number:02d}" if month_number else None,
            "end_date": None,
            "date_precision": "month" if month_number else "unknown",
        }

    day_month_range = re.fullmatch(rf"(\d{{1,2}})(?:\s*[–-]\s*(\d{{1,2}}))?\s+({_MONTH})\s+(20\d{{2}})", value, re.I)
    if day_month_range:
        start = _iso_day(day_month_range[1], day_month_range[3], day_month_range[4])
        end = _iso_day(day_month_range[2], day_month_range[3], day_month_range[4]) if day_month_range[2] else None
        if start:
            return {"start_date": start, "end_date": end, "date_precision": "day"}
    cross_month_range = re.fullmatch(rf"(\d{{1,2}})\s+({_MONTH})\s*[–-]\s*(\d{{1,2}})\s+({_MONTH})\s+(20\d{{2}})", value, re.I)
    if cross_month_range:
        start = _iso_day(cross_month_range[1], cross_month_range[2], cross_month_range[5])
        end = _iso_day(cross_month_range[3], cross_month_range[4], cross_month_range[5])
        if start and end:
            return {"start_date": start, "end_date": end, "date_precision": "day"}
    single_day = re.fullmatch(rf"(\d{{1,2}})\s+({_MONTH})\s+(20\d{{2}})", value, re.I)
    if single_day:
        start = _iso_day(single_day[1], single_day[2], single_day[3])
        if start:
            return {"start_date": start, "end_date": None, "date_precision": "day"}
    return {"start_date": None, "end_date": None, "date_precision": "unknown"}


def _parse_report_period(text: str) -> tuple[str | None, str | None]:
    value = _compact(text).replace("—", "–")
    match = re.search(rf"(\d{{1,2}})\s*[–-]\s*(\d{{1,2}})\s+({_MONTH})\s+(20\d{{2}})", value, re.I)
    if match:
        return _iso_day(match[1], match[3], match[4]), _iso_day(match[2], match[3], match[4])
    match = re.search(rf"(\d{{1,2}})\s+({_MONTH})\s*[–-]\s*(\d{{1,2}})\s+({_MONTH})\s+(20\d{{2}})", value, re.I)
    if match:
        return _iso_day(match[1], match[2], match[5]), _iso_day(match[3], match[4], match[5])
    return None, None


def _reported_publication_date(text: str, anchor: str = "") -> tuple[str | None, str | None]:
    value = _compact(text)
    match = re.search(rf"\bIN WINDOW\s+(\d{{1,2}}\s+{_MONTH}\s+20\d{{2}})", value, re.I)
    if match:
        parsed = _parse_calendar_date(match.group(1))
        return match.group(1), parsed["start_date"]
    match = re.search(rf"\bIN WINDOW\s+({_MONTH}\s+20\d{{2}})\b", value, re.I)
    if match:
        parsed = _parse_calendar_date(match.group(1))
        return match.group(1), parsed["start_date"]
    date_pattern = rf"(?:\d{{1,2}}\s+{_MONTH}\s+20\d{{2}}|{_MONTH}\s+20\d{{2}})"
    label_pattern = r"(?:published|publication(?:\s+date)?|released|release\s+date|documents\s*&\s*reports)"
    match = re.search(
        rf"\b{label_pattern}\b\s*(?:on\s+|date\s*[:–—-]?\s*)?[:–—-]?\s*({date_pattern})",
        value,
        re.I,
    )
    if match:
        parsed = _parse_calendar_date(match.group(1))
        return match.group(1), parsed["start_date"]
    match = re.search(
        rf"\b\d{{1,2}}\s*[–-]\s*\d{{1,2}}\s+{_MONTH}\s+"
        rf"(\d{{1,2}}\s+{_MONTH}\s+20\d{{2}})\s+"
        r"(?:WORKSHOP REPORT|PRESS RELEASE|DATA ANALYSIS|POLICY BRIEF|MARKET DATA|"
        r"ANALYSIS|COMMENTARY|HANDBOOK|INITIATIVE|OP-ED|REPORT)\b",
        value,
        re.I,
    )
    if match:
        parsed = _parse_calendar_date(match.group(1))
        return match.group(1), parsed["start_date"]
    if not re.search(rf"\b{label_pattern}\b", anchor, re.I):
        return None, None
    anchor = _compact(anchor)
    match = re.search(rf"\b({date_pattern})\b", anchor, re.I)
    if not match:
        match = re.search(rf"\b({_MONTH}\s+20\d{{2}})\b", anchor, re.I)
    if match:
        parsed = _parse_calendar_date(match.group(1))
        return match.group(1), parsed["start_date"]
    return None, None


def _report_metadata(first_page: str) -> dict[str, Any]:
    compact = _compact(first_page)
    period = re.search(r"REPORTING PERIOD\s+(.+?)\s+DATE OF RUN", compact, re.I)
    run_date = re.search(rf"DATE OF RUN\s+(\d{{1,2}}\s+{_MONTH}\s+20\d{{2}})", compact, re.I)
    edition = re.search(r"\bEdition\s+(\d+)\b", compact, re.I)
    raw_period = period.group(1).strip(" .·") if period else None
    period_start, period_end = _parse_report_period(raw_period or "")
    parsed_run_date = _parse_calendar_date(run_date.group(1)) if run_date else {}
    return {
        "title": "Climate Risk Intelligence Report" if "climate risk intelligence report" in compact.casefold() else None,
        "edition": int(edition.group(1)) if edition else None,
        "reporting_period": raw_period,
        "period_start": period_start,
        "period_end": period_end,
        "date_of_run": parsed_run_date.get("start_date"),
    }


def _pdf_timestamp(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _extract_text_fragments(page: Any) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []

    def collect(text: str, cm: list[float], tm: list[float], _font: Any, font_size: Any, *_: Any) -> None:
        text = _compact(text)
        if not text:
            return
        x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
        y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
        try:
            size = max(float(font_size or 0), 0)
        except (TypeError, ValueError):
            size = 0
        # ponytail: estimate common report-font widths; use font metrics if this causes misassociation.
        width = size * sum(1 if ord(char) > 0x2E80 else 0.55 for char in text)
        font_name = str(_font.get("/BaseFont", "")) if _font else ""
        fragments.append({
            "x": x, "y": y, "right": x + width, "text": text,
            "font_size": size, "font_name": font_name,
        })

    try:
        page.extract_text(visitor_text=collect)
    except Exception:
        return []
    return fragments


def _extract_links(
    page: Any, page_number: int, fragments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    fragments = _extract_text_fragments(page) if fragments is None else fragments
    links = []
    for ref in page.get("/Annots") or []:
        try:
            annotation = ref.get_object()
            action = annotation.get("/A")
            if action is not None:
                action = action.get_object()
            url = str(action.get("/URI") or "").strip() if action else ""
            if not url:
                continue
            rect = [round(float(value), 2) for value in annotation.get("/Rect", [])]
            anchors = []
            if len(rect) == 4:
                x0, y0, x1, y1 = rect
                anchors = [
                    fragment["text"] for fragment in fragments
                    if x0 - 2 <= fragment["x"] <= x1 + 2 and y0 - 3 <= fragment["y"] <= y1 + 3
                ]
            links.append({
                "url": url,
                "anchor_text": _compact(" ".join(dict.fromkeys(anchors))),
                "page": page_number,
                "rect": rect,
            })
        except (TypeError, ValueError, KeyError):
            continue
    return links


def _group_page_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = []
    for url in dict.fromkeys(link["url"] for link in links):
        same_url = [link for link in links if link["url"] == url]
        same_url.sort(key=lambda link: link["rect"][1] if len(link["rect"]) == 4 else 0, reverse=True)
        clusters: list[list[dict[str, Any]]] = []
        for link in same_url:
            rect = link["rect"]
            center = (rect[1] + rect[3]) / 2 if len(rect) == 4 else None
            prior_rect = clusters[-1][-1]["rect"] if clusters else []
            prior_center = (prior_rect[1] + prior_rect[3]) / 2 if len(prior_rect) == 4 else None
            if center is not None and prior_center is not None and abs(center - prior_center) <= 16:
                clusters[-1].append(link)
            else:
                clusters.append([link])
        for cluster in clusters:
            rects = [link["rect"] for link in cluster if len(link["rect"]) == 4]
            grouped.append({
                "url": url,
                "anchor_text": _compact(" ".join(link["anchor_text"] for link in cluster if link["anchor_text"])),
                "page": cluster[0]["page"],
                "rect": [
                    round(min(rect[0] for rect in rects), 2),
                    round(min(rect[1] for rect in rects), 2),
                    round(max(rect[2] for rect in rects), 2),
                    round(max(rect[3] for rect in rects), 2),
                ] if rects else [],
                "rects": rects,
            })
    return grouped


def _find_anchor_line(lines: list[str], anchor: str, occurrence: int = 0) -> int | None:
    target = [word.casefold() for word in re.findall(r"\w+", anchor, re.UNICODE)]
    if not target:
        return None
    words = [
        (word.casefold(), line_index)
        for line_index, line in enumerate(lines)
        for word in re.findall(r"\w+", line, re.UNICODE)
    ]
    matches = []
    best = (0, None)
    for start, (word, line_index) in enumerate(words):
        if word != target[0]:
            continue
        matched = 0
        while (matched < len(target) and start + matched < len(words)
               and words[start + matched][0] == target[matched]):
            matched += 1
        if matched == len(target):
            matches.append(line_index)
        elif matched > best[0]:
            best = (matched, line_index)
    if matches:
        return matches[min(occurrence, len(matches) - 1)]
    return best[1] if best[0] >= min(3, len(target)) else None


_RECORD_MARKERS = {"updates", "news", "publications", "upcoming events", "watch item", "key dates"}


def _large_bold_lines(page: dict[str, Any]) -> list[int]:
    lines = page["text"].splitlines()
    result = set()
    for fragment in page.get("_text_fragments", []):
        if fragment.get("font_size", 0) < 14 or "bold" not in fragment.get("font_name", "").casefold():
            continue
        text = _compact(fragment.get("text", "")).casefold()
        if text:
            result.update(
                index for index, line in enumerate(lines)
                if text in _compact(line).casefold()
            )
    return sorted(result)


def _record_context(page: dict[str, Any], anchor: str, occurrence: int = 0) -> tuple[str, str]:
    lines = page["text"].splitlines()
    anchor_line = _find_anchor_line(lines, anchor, occurrence)
    if anchor_line is None:
        return "\n".join(lines).strip(), "verbatim_pdf_page_context"

    numbered = [
        index for index, line in enumerate(lines)
        if line.strip().isdigit() and len(line.strip()) <= 3 and int(line.strip()) < 1000
    ]
    markers = [
        index for index, line in enumerate(lines)
        if (value := _compact(line).casefold()) in _RECORD_MARKERS
        or any(value.startswith(marker + " ") for marker in _RECORD_MARKERS)
    ]
    bold = _large_bold_lines(page)
    starts = [index + 1 for index in numbered if index < anchor_line]
    starts.extend(index + 1 for index in markers if index < anchor_line)
    starts.extend(index for index in bold if index <= anchor_line)
    start = min(anchor_line, max(starts, default=0))

    ends = [index for index in numbered if index > anchor_line]
    ends.extend(index for index in markers if index > anchor_line)
    ends.extend(index for index in bold if index > anchor_line)
    ends.extend(
        index for index, line in enumerate(lines)
        if index > anchor_line and re.search(r"\bPage\s+\d+\s+of\s+\d+\b", line, re.I)
    )
    end = min(ends, default=len(lines))
    basis = "verbatim_pdf_record" if ends else "verbatim_pdf_page_context"
    return "\n".join(lines[start:end]).strip(), basis


def _calendar_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for page in pages:
        lines = page["text"].splitlines()
        if "table of contents" in page["text"].casefold():
            continue
        if any(re.fullmatch(r"\s*DATE\(S\)\s+EVENT\s+HOST\s+RELEVANCE\s*", line, re.I) for line in lines):
            found.append(page)
        elif re.search(r"(?mi)^\s*Key Dates\s*$", page["text"]):
            found.append(page)
    return found


def _executive_summary(pages: list[dict[str, Any]]) -> str | None:
    section: list[str] = []
    active = False
    for page in pages:
        lines = page["text"].splitlines()
        if not active:
            if not lines or _compact(lines[0]).casefold() != "executive summary":
                continue
            active = True
            lines = lines[1:]
        if re.search(r"(?mi)^\s*(?:Key Dates|DATE\(S\)\s+EVENT\s+HOST\s+RELEVANCE)\s*$", "\n".join(lines)):
            break
        section.extend(line for line in lines if not re.search(r"AI-assisted|AI-generated.*see disclaimer|Page \d+ of \d+", line, re.I))
    text = "\n".join(section).strip()
    return text or None


def _is_calendar_date_line(value: str) -> bool:
    value = _compact(value).rstrip(" .·")
    if _parse_calendar_date(value)["date_precision"] != "unknown":
        return True
    return bool(re.fullmatch(
        r"(?:TBA|TBD|TBC|ongoing|various dates|date\s+(?:TBA|TBD|TBC|to be (?:announced|confirmed))|"
        r"to be (?:announced|confirmed)|H[1-2]\s+20\d{2})",
        value,
        re.I,
    ))


def _is_calendar_kind_line(value: str) -> bool:
    return "".join(_compact(value).split()).casefold() in {
        "event", "deadline", "launch", "publication", "watch", "webinar",
    }


def _calendar_row_starts(lines: list[str]) -> list[int]:
    starts = []
    for index, line in enumerate(lines):
        if not _is_calendar_date_line(line):
            continue
        next_line = next((candidate for candidate in lines[index + 1:] if candidate.strip()), "")
        if _is_calendar_kind_line(next_line):
            starts.append(index)
    return starts


def _calendar_section_start(page: dict[str, Any], starts: list[int]) -> int | None:
    if not starts:
        return None
    lines = page["text"].splitlines()
    candidates = [
        index for index, line in enumerate(lines)
        if index > starts[0] and re.search(r"\bPage\s+\d+\s+of\s+\d+\b", line, re.I)
    ]
    for fragment in page.get("_text_fragments", []):
        if fragment.get("font_size", 0) < 14 or "bold" not in fragment.get("font_name", "").casefold():
            continue
        text = _compact(fragment.get("text", "")).casefold()
        if not text:
            continue
        candidates.extend(
            index for index, line in enumerate(lines)
            if index > starts[0] and text in _compact(line).casefold()
        )
    return min(candidates) if candidates else None


def _link_overlaps_title(link: dict[str, Any], title: str, fragments: list[dict[str, Any]]) -> bool:
    rect = link.get("rect", [])
    normalized_title = _compact(title).casefold()
    if len(rect) != 4 or not normalized_title:
        return False
    x0, y0, x1, y1 = rect
    for fragment in fragments:
        text = _compact(fragment["text"]).casefold()
        if not text or not (
            text == normalized_title or text in normalized_title or normalized_title in text
        ):
            continue
        if (x0 - 2 <= fragment["right"] and fragment["x"] <= x1 + 2
                and y0 - 3 <= fragment["y"] <= y1 + 3):
            return True
    return False


def _extract_calendar_items(document: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for page in _calendar_pages(document["pages"]):
        lines = page["text"].splitlines()
        starts = _calendar_row_starts(lines)
        section_start = _calendar_section_start(page, starts)
        if section_start is not None:
            starts = [start for start in starts if start < section_start]
        rows = []
        for ordinal, start in enumerate(starts, 1):
            end = starts[ordinal] if ordinal < len(starts) else section_start or len(lines)
            row = [line.strip() for line in lines[start:end] if line.strip()]
            if len(row) >= 3 and _is_calendar_kind_line(row[1]):
                rows.append({"ordinal": ordinal, "row": row, "links": []})

        fragments = page.get("_text_fragments", [])
        for link in page["links"]:
            anchor = _compact(link["anchor_text"]).casefold()
            matches = [
                entry for entry in rows
                if anchor and anchor in _compact("\n".join(entry["row"])).casefold()
            ]
            if matches:
                for entry in matches:
                    entry["links"].append(link)
                continue
            matches = [entry for entry in rows if _link_overlaps_title(link, entry["row"][2], fragments)]
            if len(matches) == 1:
                matches[0]["links"].append(link)

        for entry in rows:
            ordinal, row = entry["ordinal"], entry["row"]
            raw_date = row[0]
            raw_kind = row[1]
            kind = "".join(_compact(raw_kind).split()).casefold()
            row_text = "\n".join(row)
            page_links = entry["links"]
            linked_title = max((link["anchor_text"] for link in page_links), key=len, default=None)
            title = linked_title or row[2]
            date_fields = _parse_calendar_date(raw_date)
            source_urls = list(dict.fromkeys(link["url"] for link in page_links))
            explicit_location = _explicit_calendar_location(row_text)
            explicit_online_url = _explicit_calendar_online_url(row_text)
            identity_urls = sorted({canonical_url(url) or url for url in source_urls})
            item_hash = _sha256(f"{document['source']['sha256']}\n{page['page']}\n{ordinal}\n{row_text}")
            summary = "\n".join(row[2:]).strip()
            identity_title = _compact(linked_title or " ".join(row[2:5])).casefold()
            if identity_urls:
                # ponytail: title disambiguates reused links; use source event IDs if PDFs expose them.
                event_identity = "\n".join((kind, identity_title, *identity_urls))
            else:
                event_identity = "\n".join((
                    kind, date_fields["start_date"] or raw_date.casefold(),
                    date_fields["end_date"] or "", identity_title,
                ))
            items.append({
                "event_id": f"event-{_sha256(event_identity)[:24]}",
                "occurrence_id": f"pdf-event-occurrence-{item_hash[:24]}",
                "name": title,
                "kind": kind,
                "raw_kind": raw_kind,
                "raw_date": raw_date,
                **date_fields,
                "date_evidence": raw_date,
                "summary": summary,
                "summary_basis": "verbatim_pdf_row",
                "summary_sha256": _sha256(summary),
                "raw_text": row_text,
                "page": page["page"],
                "source_urls": source_urls,
                "location": explicit_location,
                "online_url": explicit_online_url,
                "source_links": [{"url": link["url"], "rect": link.get("rect", [])} for link in page_links],
                "source_document_sha256": document["source"]["sha256"],
                "content_sha256": _sha256(row_text),
            })
    return items


@lru_cache(maxsize=2)
def _calendar_table_rows(raw_pdf: bytes, pages: tuple[int, ...]) -> tuple[dict[str, Any], ...]:
    """Read column geometry; line-order extraction mixes the four calendar cells."""
    import pdfplumber

    rows = []
    with pdfplumber.open(BytesIO(raw_pdf)) as pdf:
        for number in pages:
            page = pdf.pages[number - 1]
            for table in page.find_tables():
                columns = None
                for row, values in zip(table.rows, table.extract()):
                    if [_compact(v) for v in values if v] == ["DATE(S)", "EVENT", "HOST", "RELEVANCE"]:
                        columns = [cell for cell, value in zip(row.cells, values) if value]
                        continue
                    if columns is None:
                        continue
                    top, bottom = row.bbox[1], row.bbox[3]
                    raw_cells = [page.crop((cell[0] + 1, top + .3, cell[2] - .3, bottom - .3))
                        .extract_text(x_tolerance=2, y_tolerance=2) or "" for cell in columns]
                    cells = [_compact(value) for value in raw_cells]
                    match = re.fullmatch(r"(.+?)\s+(EVENT|DEADLINE|LAUNCH|PUBLICATION|WATCH|WEBINAR)", cells[0], re.I)
                    if match is None:
                        continue
                    urls = list(dict.fromkeys(link["uri"] for link in page.hyperlinks
                        if link.get("uri", "").startswith(("https://", "http://"))
                        and link["top"] < bottom and link["bottom"] > top
                        and link["x0"] < columns[1][2] and link["x1"] > columns[1][0]))
                    rows.append({"page": number, "raw_date": match[1], "kind": match[2].lower(),
                        "name": _calendar_event_title(raw_cells[1], cells[1]),
                        "publisher": cells[2], "relevance": cells[3],
                        "location": _explicit_calendar_location(raw_cells[1], multiline=True),
                        "online_url": _explicit_calendar_online_url(raw_cells[1], multiline=True),
                        "source_urls": urls})
    return tuple(rows)


def recover_calendar_fields(raw_pdf: bytes, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover cells for existing and new imports, preserving stored observations."""
    pages = tuple(sorted({item["page"] for item in items if item.get("raw_text")}))
    if not pages:
        return items
    digest = _sha256(raw_pdf)
    if any(item["source_document_sha256"] != digest for item in items):
        raise ValueError("calendar PDF bytes do not match the source document")
    normalize = lambda value: re.sub(r"[^\w]", "", value.casefold())
    rows = _calendar_table_rows(raw_pdf, pages)
    result = []
    for item in items:
        text = normalize(item.get("raw_text") or "")
        matches = [row for row in rows if row["page"] == item["page"]
            and row["kind"] == item["kind"] and normalize(row["raw_date"]) == normalize(item["raw_date"])
            and all(normalize(row[key]) in text for key in ("name", "publisher", "relevance"))]
        if len(matches) > 1:
            # Legacy raw text preserves the event cell, including labeled fields.
            # Use that bounded cell evidence to disambiguate otherwise identical rows;
            # if it cannot identify exactly one row, leave the item untouched.
            matches = [row for row in matches if any(
                normalize(value) in text
                for value in (row["location"], row["online_url"])
                if value
            ) and all(
                normalize(value) in text
                for value in (row["location"], row["online_url"])
                if value
            )]
        if len(matches) == 1:
            row = matches[0]
            item = dict(item, name=row["name"], publisher=row["publisher"], relevance=row["relevance"],
                source_urls=row["source_urls"] or item["source_urls"],
                calendar_field_basis="pdf_table_cells")
            item["location"] = row["location"]
            item["online_url"] = row["online_url"]
        result.append(item)
    return result


def _calendar_event_title(value: str, fallback: str) -> str:
    """Keep event-title lines while excluding explicitly labeled cell fields."""
    lines = value.splitlines()
    field_line = re.compile(
        r"\s*(?:location|venue|address|online|meeting|registration)\s*(?:link|url)?\s*:", re.I
    )
    first_field = next((index for index, line in enumerate(lines) if field_line.match(line)), None)
    if first_field is None:
        return fallback
    title = _compact(" ".join(lines[:first_field]))
    return title or fallback


def _explicit_calendar_location(value: str, *, multiline: bool = False) -> str | None:
    lines = value.splitlines()
    for index, line in enumerate(lines):
        match = re.fullmatch(r"\s*(?:location|venue|address)\s*:\s*(.*?)\s*", line, re.I)
        if match:
            first = match.group(1).strip()
            if not first and not multiline:
                return None
            continuation = []
            if multiline:
                for next_line in lines[index + 1:]:
                    if not next_line.strip():
                        if continuation:
                            break
                        continue
                    if re.match(
                        r"\s*(?:location|venue|address|online|meeting|registration)\s*(?:link|url)?\s*:",
                        next_line, re.I,
                    ):
                        break
                    continuation.append(next_line.strip())
            location = "\n".join([part for part in (first, *continuation) if part])
            return location or None
    for line in lines:
        match = re.fullmatch(r"\s*(online|virtual|hybrid)(?:\s+(?:meeting|event))?\s*", line, re.I)
        if match:
            return match.group(1).strip()
    return None


def _explicit_calendar_online_url(value: str, *, multiline: bool = False) -> str | None:
    lines = value.splitlines()
    label = re.compile(r"\s*(?:online|meeting|registration)\s*(?:link|url)\s*:\s*(.*?)\s*$", re.I)
    next_field = re.compile(
        r"\s*(?:location|venue|address|online|meeting|registration)\s*(?:link|url)?\s*:", re.I
    )
    for index, line in enumerate(lines):
        match = label.fullmatch(line)
        if not match:
            continue
        parts = [match.group(1).strip()] if match.group(1).strip() else []
        if multiline:
            for next_line in lines[index + 1:]:
                if not next_line.strip():
                    if parts:
                        break
                    continue
                if next_field.match(next_line):
                    break
                parts.append(next_line.strip())
        candidate = "".join("".join(parts).split())
        url = re.match(r"https?://\S+", candidate)
        if url:
            return url.group(0).rstrip(".,)")
    return None


def _read_pdf(path: Path) -> dict[str, Any]:
    if path.suffix.casefold() != ".pdf" or not path.is_file():
        raise ValueError(f"input is not a PDF file: {path}")
    raw = path.read_bytes()
    if len(raw) > MAX_PDF_BYTES:
        raise ValueError(f"PDF exceeds the {MAX_PDF_BYTES}-byte limit: {path.name}")
    if not raw.startswith(b"%PDF-"):
        raise ValueError(f"PDF signature is invalid: {path.name}")
    reader = PdfReader(BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError(f"encrypted PDF is unsupported: {path.name}")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError(f"PDF exceeds the {MAX_PDF_PAGES}-page limit: {path.name}")
    pages = []
    for page_number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if len(text) > 1_000_000:
            raise ValueError(f"extracted page exceeds the text limit: {path.name} page {page_number}")
        fragments = _extract_text_fragments(page)
        pages.append({
            "page": page_number,
            "text": text,
            "content_sha256": _sha256(text),
            "links": _group_page_links(_extract_links(page, page_number, fragments)),
            "_text_fragments": fragments,
        })
    if not any(page["text"].strip() for page in pages):
        raise ValueError(f"PDF contains no extractable text: {path.name}")
    metadata = _report_metadata(pages[0]["text"])
    pdf_metadata = reader.metadata
    source_path = str(path.resolve())
    full_text = "\n\f\n".join(page["text"] for page in pages)
    executive_summary = _executive_summary(pages)
    document = {
        "source": {
            "path": source_path,
            "filename": path.name,
            "source_observations": [{"path": source_path, "filename": path.name}],
            "media_type": "application/pdf",
            "size_bytes": len(raw),
            "sha256": _sha256(raw),
            "pdf_metadata": {str(key): str(value) for key, value in (pdf_metadata or {}).items()},
            "pdf_created_at": _pdf_timestamp(getattr(pdf_metadata, "creation_date", None)),
            "pdf_modified_at": _pdf_timestamp(getattr(pdf_metadata, "modification_date", None)),
            "original_pdf_base64": base64.b64encode(raw).decode("ascii"),
        },
        **metadata,
        "page_count": len(pages),
        "extracted_text_sha256": _sha256(full_text),
        "executive_summary": executive_summary,
        "executive_summary_sha256": _sha256(executive_summary) if executive_summary else None,
        "pages": pages,
    }
    document["calendar_items"] = recover_calendar_fields(raw, _extract_calendar_items(document))
    return document


def report_update_fields(occurrence: dict[str, Any], document: dict[str, Any]) -> dict[str, Any] | None:
    """Read an explicitly dated report item, excluding calendar/website links."""
    summary, anchor = occurrence.get("summary") or "", occurrence.get("anchor_text") or ""
    marker = re.search(r"\n(IN WINDOW[^\n]+)\n", summary, re.I)
    if marker is None or re.fullmatch(r"(?:www\.)?[a-z0-9.-]+\.[a-z]+(?:/[^ ]*)?", anchor, re.I):
        return None
    normalize = lambda value: re.sub(r"[^\w]", "", value.casefold())
    head = summary[:marker.start()]
    page = next((page for page in document.get("pages", []) if page["page"] == occurrence["page"]), None)
    if page is None:
        return None
    titles = [link["anchor_text"] for link in page["links"]
        if canonical_url(link["url"]) == canonical_url(occurrence["raw_url"])
        and len(link["anchor_text"]) > 20 and normalize(link["anchor_text"]) in normalize(head)]
    title = _compact(max(titles, key=len) if titles else head)
    publisher = None
    for prior in document.get("pages", []):
        if prior["page"] > page["page"]:
            break
        lines = prior["text"].splitlines()
        if prior["page"] == page["page"]:
            stop = _find_anchor_line(lines, title)
            if stop is not None:
                lines = lines[:stop]
        for index, line in enumerate(lines):
            if index and line.startswith("Website:"):
                publisher = _compact(lines[index - 1])
    tail = summary[marker.end():]
    source = re.search(r"(?:📎\s*)?Source:\s*(.+)", tail)
    source_label = _compact(source[1]) if source else ""
    if source:
        tail = tail[:source.start()]
    badges = _compact(marker[1])[len("IN WINDOW "):]
    date_label = _compact(occurrence.get("publication_date_evidence") or "")
    if date_label and badges.casefold().startswith(date_label.casefold()):
        badges = badges[len(date_label):].strip()
    return {"title": title, "publisher": publisher, "topic": badges or "Update",
        "summary": tail.strip(), "source_label": source_label}


def _article_records(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for document in documents:
        occurrence_ordinals: dict[tuple[str, int], int] = {}
        calendar_links = {
            (event["page"], canonical_url(link["url"]), tuple(link.get("rect", [])))
            for event in document["calendar_items"] for link in event.get("source_links", [])
        }
        anchor_ordinals: dict[tuple[int, str], int] = {}
        for page in document["pages"]:
            for link in page["links"]:
                parsed = urlsplit(link["url"])
                if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
                    continue
                canonical = canonical_url(link["url"])
                if not canonical:
                    continue
                # Calendar hyperlinks belong to the calendar observation. The same
                # URL in a non-calendar paragraph remains an article occurrence.
                if (page["page"], canonical, tuple(link.get("rect", []))) in calendar_links:
                    continue
                record = grouped.setdefault(canonical, {
                    "article_id": _stable_id("article", canonical),
                    "canonical_url": canonical,
                    "title": None,
                    "type_safe_classification": None,
                    "occurrences": [],
                })
                anchor_key = (page["page"], _compact(link["anchor_text"]).casefold())
                anchor_ordinals[anchor_key] = anchor_ordinals.get(anchor_key, 0) + 1
                context, summary_basis = _record_context(
                    page, link["anchor_text"] or "", anchor_ordinals[anchor_key] - 1
                )
                publication_date_evidence, publication_date = _reported_publication_date(
                    context, link["anchor_text"]
                )
                occurrence_key = (canonical, page["page"])
                occurrence_ordinals[occurrence_key] = occurrence_ordinals.get(occurrence_key, 0) + 1
                occurrence_id = "pdf-article-occurrence-" + _sha256(
                    f"{document['source']['sha256']}\n{canonical}\n{page['page']}\n"
                    f"{occurrence_ordinals[occurrence_key]}\n{_sha256(context)}"
                )[:24]
                record["occurrences"].append({
                    "occurrence_id": occurrence_id,
                    "raw_url": link["url"],
                    "anchor_text": link["anchor_text"],
                    "page": page["page"],
                    "report_date": document.get("date_of_run"),
                    "reporting_period": document.get("reporting_period"),
                    "publication_date_evidence": publication_date_evidence,
                    "publication_date": publication_date,
                    "source_document_sha256": document["source"]["sha256"],
                    "source_document": document["source"]["filename"],
                    "summary": context,
                    "summary_basis": summary_basis,
                    "content_sha256": _sha256(context),
                    "page_sha256": page["content_sha256"],
                })
                title = link["anchor_text"]
                if title and (not record["title"] or len(title) > len(record["title"])):
                    record["title"] = title
    return [grouped[key] for key in sorted(grouped)]


def _typesafe_classify(records: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        return {"status": "not_configured", "classified": 0, "failed_batches": 0}
    if not records:
        return {"status": "complete", "classified": 0, "failed_batches": 0}
    try:
        from typesafe_sdk import Choice, TypeSafeClient
    except ImportError:
        return {"status": "unavailable", "classified": 0, "failed_batches": 1, "error": "sdk_missing"}

    classified = failed = 0
    try:
        with TypeSafeClient(api_key=api_key, timeout=20.0) as client:
            for start in range(0, len(records), TYPE_SAFE_BATCH_SIZE):
                batch = records[start:start + TYPE_SAFE_BATCH_SIZE]
                state = {"items": [
                    {
                        "index": index,
                        "url": item["canonical_url"],
                        "anchor_text": item["title"] or "",
                        "context": item["occurrences"][0]["summary"][:1200],
                    }
                    for index, item in enumerate(batch)
                ]}
                questions = {
                    f"item_{index}": Choice(
                        instructions=(
                            f"Classify items[{index}] using only its link title and nearby report text. "
                            "Use article for a specific publication, news story, paper, dataset, or research report; "
                            "event for a dated event, meeting, webinar, launch, deadline, or open call; "
                            "landing_page for a publisher homepage, archive, organization site, or portal; "
                            "other for any other link. This is a routing suggestion, not a factual claim."
                        ),
                        criteria=_CHOICES,
                    )
                    for index in range(len(batch))
                }
                try:
                    response = client.system_one(state, questions)
                    for index, item in enumerate(batch):
                        label = response.choices[f"item_{index}"].choice
                        item["type_safe_classification"] = {
                            "provider": "typesafe",
                            "label": label if label in _CHOICES else "other",
                        }
                        classified += 1
                except Exception as exc:
                    failed += 1
                    logging.getLogger(__name__).warning(
                        "TypeSafe PDF classification failed (%s); preserving unclassified records",
                        type(exc).__name__,
                    )
    except Exception as exc:
        failed += 1
        logging.getLogger(__name__).warning(
            "TypeSafe PDF classification unavailable (%s); preserving unclassified records",
            type(exc).__name__,
        )
    return {
        "status": "complete" if not failed else "partial",
        "classified": classified,
        "failed_batches": failed,
    }


def _input_pdfs(inputs: Iterable[str | Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for value in inputs:
        path = Path(value).expanduser()
        paths = sorted(
            (item for item in path.iterdir() if item.suffix.casefold() == ".pdf"),
            key=lambda item: str(item).casefold(),
        ) if path.is_dir() else [path]
        for item in paths:
            resolved = item.resolve()
            if not resolved.is_file() or resolved.suffix.casefold() != ".pdf":
                raise ValueError(f"input is not a PDF file or directory: {item}")
            found.setdefault(str(resolved).casefold(), resolved)
    if not found:
        raise ValueError("no PDF files found")
    return list(found.values())


def import_pdf_reports(inputs: Iterable[str | Path]) -> dict[str, Any]:
    """Build a normalized bundle while preserving each PDF's text and provenance."""
    documents = [_read_pdf(path) for path in _input_pdfs(inputs)]
    documents_by_sha: dict[str, dict[str, Any]] = {}
    for document in documents:
        source = document["source"]
        existing = documents_by_sha.setdefault(source["sha256"], document)
        if existing is not document:
            known_paths = {item["path"] for item in existing["source"]["source_observations"]}
            existing["source"]["source_observations"].extend(
                item for item in source["source_observations"] if item["path"] not in known_paths
            )
    documents = list(documents_by_sha.values())
    articles = _article_records(documents)
    for document in documents:
        for page in document["pages"]:
            page.pop("_text_fragments", None)
    typesafe = _typesafe_classify(articles)
    calendar_items = [item for document in documents for item in document["calendar_items"]]
    calendar_inputs = [{"canonical_url": next(iter(item["source_urls"]), ""),
                        "title": item.get("name"), "occurrences": [{"summary": item["raw_text"]}]}
                       for item in calendar_items]
    calendar_typesafe = _typesafe_classify(calendar_inputs)
    for item, classified in zip(calendar_items, calendar_inputs):
        item["type_safe_classification"] = classified.get("type_safe_classification")
    return {
        "schema_version": "climate-pdf-intake.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "typesafe": typesafe,
        "calendar_typesafe": calendar_typesafe,
        "documents": documents,
        "articles": articles,
        "calendar_items": calendar_items,
    }


def _paths_alias(left: Path, right: Path) -> bool:
    left, right = left.expanduser(), right.expanduser()
    try:
        if left.exists() and right.exists() and left.samefile(right):
            return True
    except OSError:
        pass
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _validate_output_path(output: Path | None, inputs: list[Path], registry: Path | None) -> None:
    if output is None:
        return
    protected = [*inputs, *([registry] if registry is not None else [])]
    if any(_paths_alias(output, path) for path in protected):
        raise ValueError("--output must not overwrite an input PDF or the Registry database")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Extract searchable report PDFs into a provenance-preserving JSON bundle.")
    parser.add_argument("--input", action="append", required=True, help="PDF file or directory of PDFs; may be repeated")
    parser.add_argument("--output", type=Path, help="Optional JSON output path (requires --apply)")
    parser.add_argument("--registry-db", type=Path, help="Existing Registry SQLite database")
    parser.add_argument("--backup-dir", type=Path, help="Directory for the pre-import Registry backup")
    parser.add_argument("--apply", action="store_true", help="Persist the bundle to the Registry after creating a backup")
    args = parser.parse_args(argv)
    if args.apply and not (args.registry_db and args.backup_dir):
        parser.error("--apply requires both --registry-db and --backup-dir")
    if not args.apply and (args.output or args.registry_db or args.backup_dir):
        parser.error("--output, --registry-db, and --backup-dir require --apply")
    try:
        input_paths = _input_pdfs(args.input)
        _validate_output_path(args.output, input_paths, args.registry_db)
        bundle = import_pdf_reports(input_paths)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        registry_result = None
        if args.apply:
            from climate_registry.pdf_intake import persist_pdf_intake

            registry_result = persist_pdf_intake(args.registry_db, args.backup_dir, bundle)
    except Exception as exc:
        parser.exit(2, f"pdf intake failed ({type(exc).__name__}): {str(exc)[:300]}\n")
    print(json.dumps({
        "status": "complete" if args.apply else "dry_run",
        "output": str(args.output) if args.output else None,
        "documents": len(bundle["documents"]),
        "articles": len(bundle["articles"]),
        "calendar_items": len(bundle["calendar_items"]),
        "typesafe": bundle["typesafe"],
        "registry": registry_result,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
