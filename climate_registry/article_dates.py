"""Import source-backed article collection and information-date observations."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from climate_monitor.dedupe import canonical_url

from .contract import validate_registry_contract


OBSERVATIONS_SCHEMA = "article-date-observations.v1"
_RFC3339 = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)\Z")
_DATE = re.compile(r"\d{4}-\d\d-\d\d\Z")
_MONTH = re.compile(r"\d{4}-\d\d\Z")
_YEAR = re.compile(r"\d{4}\Z")
_EVIDENCE_REQUIRED = ("source_system", "database", "table", "record_id", "source_url", "match_basis")


class ArticleDateImportError(ValueError):
    """Invalid or conflicting article date evidence."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ArticleDateImportError(f"{field} must be a trimmed non-empty string")
    return value


def _collection_timestamp(value: Any) -> str:
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        raise ArticleDateImportError("collection observed_at must be RFC3339 with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArticleDateImportError("collection observed_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ArticleDateImportError("collection observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _information_date(value: Any) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise ArticleDateImportError("page_information observed_at must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ArticleDateImportError("page_information observed_at is invalid") from exc
    return parsed.isoformat()


def _typed_date_kind(evidence: Any) -> str | None:
    if not isinstance(evidence, Mapping):
        return None
    kind = re.sub(r"[\s_]+", "-", str(evidence.get("date_kind") or "").strip().casefold())
    if kind in {"published", "publication", "date-published", "published-date"}:
        return "published"
    if kind in {"updated", "update", "modified", "last-modified", "date-modified", "updated-date"}:
        return "updated"
    if kind in {"revised", "revision", "version", "version-revision", "revision-date", "revised-date", "version-date"}:
        return "revision"
    if kind in {"event", "activity", "event-date", "activity-date"}:
        return "event"
    return None


def _typed_information_date(value: Any, precision: Any) -> str:
    if precision is None:
        if isinstance(value, str) and _DATE.fullmatch(value):
            return _information_date(value)
        raise ArticleDateImportError("typed page_information date must state its precision")
    if precision == "day":
        return _information_date(value)
    if precision == "month" and isinstance(value, str) and _MONTH.fullmatch(value):
        try:
            date.fromisoformat(value + "-01")
        except ValueError as exc:
            raise ArticleDateImportError("page_information month is invalid") from exc
        return value
    if precision == "year" and isinstance(value, str) and _YEAR.fullmatch(value):
        year = int(value)
        if 1 <= year <= 9999:
            return value
    raise ArticleDateImportError("typed page_information date must match its stated precision")



def report_observation_date(value: Any, evidence: Any) -> str | None:
    """Recognize a sourced report day without inventing a collection instant."""
    if (not isinstance(evidence, Mapping) or evidence.get("date_basis") != "daily_or_weekly_report_date"
            or evidence.get("report_date") != value or not isinstance(value, str)
            or _DATE.fullmatch(value) is None):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def select_article_dates(observations, *, collection_times=(), acquisition_observations=()):
    """Select precise source dates before the explicitly recorded report-day fallback."""
    collected, information, reports, updates, revisions, page_publications = [], [], [], [], [], []
    date_proofs = {}
    for item in observations:
        if item["kind"] == "collection":
            report_day = report_observation_date(item["observed_at"], item["evidence"])
            if report_day:
                reports.append(report_day)
            else:
                collected.append(_collection_timestamp(item["observed_at"]))
        elif item["kind"] == "page_information":
            evidence = item.get("evidence")
            precision = evidence.get("date_precision") if isinstance(evidence, Mapping) else None
            kind = _typed_date_kind(evidence)
            information_value = (
                _typed_information_date(item["observed_at"], precision)
                if kind else _information_date(item["observed_at"])
            )
            if not kind:
                information.append(information_value)
            if kind in {"published", "updated", "revision"}:
                if precision == "month":
                    information_value += "-15"
                elif precision == "year":
                    continue  # Retain the observation, without inventing a sortable day.
                field = {"published": "publication_date", "updated": "updated_date", "revision": "revision_date"}[kind]
                date_proofs[(field, information_value)] = {**evidence,
                    "text": evidence.get("date_text") or evidence.get("text") or item["observed_at"],
                    "source_date_precision": precision or "day",
                    **({"date_normalization": "month_midpoint"} if precision == "month" else {})}
            if kind == "published":
                page_publications.append(information_value)
            elif kind == "updated":
                updates.append(information_value)
            elif kind == "revision":
                revisions.append(information_value)
        else:
            raise ArticleDateImportError("invalid article date observation kind")
    for value in collection_times:
        if value and value not in reports:
            collected.append(_collection_timestamp(value))
    collected_at = max(collected, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))) if collected else None
    first_collected_at = min(collected, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))) if collected else None
    information_date = max(information) if information else None
    qualified_publication = []
    for item in acquisition_observations:
        if (item.get("publication_date_evidence")
                and item.get("processing_status", "complete") == "complete"
                and item.get("selection_status", "selected") == "selected"
                and _typed_date_kind(item.get("publication_date_evidence")) not in {"updated", "revision", "event"}):
            try:
                value = _information_date(item.get("publication_date"))
                qualified_publication.append(value)
                if isinstance(item["publication_date_evidence"], Mapping):
                    date_proofs.setdefault(("publication_date", value), dict(item["publication_date_evidence"]))
            except ArticleDateImportError:
                pass  # Non-day source precision remains unconfirmed, as in range selection.
    qualified_publication.extend(page_publications)
    publication_date = max(qualified_publication) if qualified_publication else None
    updated_date = max(updates) if updates else None
    revision_date = max(revisions) if revisions else None
    report_date = max(reports) if reports else None
    basis = ("collection_time" if collected_at else "information_date" if information_date else
             "publication_date" if publication_date else "report_date" if report_date else "unknown")
    result = {"collected_at": collected_at, "first_collected_at": first_collected_at, "information_date": information_date,
            "publication_date": publication_date, "report_date": report_date, "date_basis": basis,
            "updated_date": updated_date, "revision_date": revision_date}
    for field in ("publication_date", "updated_date", "revision_date"):
        if (field, result[field]) in date_proofs:
            result[field + "_evidence"] = date_proofs[(field, result[field])]
    return result


CONTENT_DATE_FIELDS = {"content_date", "content_date_basis", "content_date_label", "content_date_evidence",
    "content_date_authority", "content_date_precision", "content_date_normalization", "content_update",
    "manual_date_overrides", "first_collected_at", "updated_date_evidence", "updated_date_verification",
    "revision_date_evidence", "revision_date_verification", "publication_date_verification", "content_update_candidate_sha256", "content_version_id"}


def labelled_source_dates(value):
    """Reuse the existing labelled date grammar; return literal spans, not inferred date facts."""
    from climate_monitor.pdf_intake import _MONTH
    from .owner_checks import _dates
    date_value = rf"(?:\d{{4}}-\d{{2}}(?:-\d{{2}})?|\d{{1,2}}[ \t]+{_MONTH}[ \t]+\d{{4}}|{_MONTH}[ \t]+(?:\d{{1,2}},?[ \t]+)?\d{{4}}|\d{{4}})"
    pattern = rf"(?im)\b(?P<label>last[ \t]+updated|updated|published|posted|revised|revision[ \t]+date)[ \t]*:[ \t]*(?P<date>{date_value})(?:(?![\w-])|(?<=\d{{4}}-\d{{2}}-\d{{2}})(?=T\d{{2}}:))"
    selected = []
    for match in re.finditer(pattern, value):
        try:
            parsed = _dates(match["date"])
        except (ValueError, TypeError):
            parsed = {}
        label = re.sub(r"[ \t]+", " ", match["label"].casefold())
        kind = "updated" if label in {"updated", "last updated"} else "revision" if label in {"revised", "revision date"} else "published"
        end = match.end()
        if parsed.get("start_date") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", match["date"]):
            # Only the adjacent ISO clock is metadata; the proof keeps its literal day.
            clock = re.match(r"T(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d+)?)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?(?![\w:])", value[end:], re.I)
            if clock:
                end += clock.end()
        selected.append({"start":match.start(),"end":end,"label":match["label"],"text":match["date"],
            "date_kind":kind,"valid":bool(parsed.get("start_date")),"source_date_precision":parsed.get("date_precision", "unknown")})
    return selected


def _body_text(value):
    # Ignore presentation, while keeping numeric signs and comparisons in the source facts.
    from html import unescape
    value = re.sub(r"<((?:https?://|mailto:)[^<>\s]+)>", r"\1", value)
    # Inline wrappers do not create whitespace between a source sign/decimal and its number.
    from .capture import _MarkdownHTMLParser
    block_tags = _MarkdownHTMLParser._blocks | {"br"}
    value = unescape(re.sub(r"</?([A-Za-z][A-Za-z0-9:-]*)(?:\s[^<>]*?)?\s*/?>",
        lambda match: " " if match[1].casefold() in block_tags else "", value))
    # Resolve one inner layer before adjoining units; code contents stay literal.
    paired = re.compile(r"([`*~]+)(.+?)\1|(?<!\w)(_+)(.+?)\3(?!\w)", re.S)
    def visible_inline(match):
        content = match[2] or match[4]
        if "`" in (match[1] or match[3]):
            return content
        return paired.sub(lambda inner: inner[2] or inner[4], content)
    value = paired.sub(visible_inline, value)
    value = re.sub(r"(?m)^[ \t]*(?:>[ \t]*)+(?=[^\d\s+\-−±.=<>≤≥]|$)", "", value)
    # Remove only the already recognized label/date span, keeping same-line source corrections.
    for match in reversed(labelled_source_dates(value)):
        if not match["valid"]:
            continue
        value = value[:match["start"]] + value[match["end"]:]
    value = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", value)
    from unicodedata import category
    currency = re.escape("".join(sorted({"$"} | {char for char in value if category(char) == "Sc"})))
    units = currency + "%‰‱٪％"
    value = value.casefold()
    # Prefix words remain literal; their spelling does not decide whether the scalar has a sign.
    scalar = rf"(?:[^\W\d_]+\s*)?(?:[{currency}]\s*)?(?:\d*\.\d+|\d+)"
    horizontal = r"[^\S\r\n\v\f\x85\u2028\u2029]"
    # A numeric list item and a paragraph negative can flatten to the same spaced prefix.
    tokens = re.finditer(rf"(?P<leading_minus>(?m:^{horizontal}*-{horizontal}+))(?={scalar})|[{units}]|[<>≤≥]=?|[+\-−±](?=\s*{scalar})|\d*\.\d+|\w+", value)
    return " ".join("[leading-spaced-minus]" if match.lastgroup else match[0] for match in tokens)


def validate_content_date_evidence(field, proof, *, article, current, version):
    """Validate a typed date against its exact retained article body."""
    expected_kind = {"publication_date": "published", "updated_date": "updated", "revision_date": "revision"}.get(field)
    if (not expected_kind or not isinstance(proof, dict) or proof.get("content_version_id") != version
            or _typed_date_kind(proof) != expected_kind
            or proof.get("source_url") != article.get("canonical_url")
            or not proof.get("final_url") or not proof.get("captured_at")
            or not isinstance(proof.get("text"), str) or not proof["text"].strip()
            or proof["text"] not in current["markdown_content"]):
        raise ValueError("source date must bind the current body, type, URL and literal source evidence")
    _collection_timestamp(proof["captured_at"])
    from .owner_checks import _dates
    parsed = _dates(proof["text"])
    precision = proof.get("source_date_precision", proof.get("date_precision"))
    expected = parsed["start_date"]
    if precision == "month" and proof.get("date_normalization") == "month_midpoint":
        expected = expected + "-15" if parsed["date_precision"] == "month" else None
    elif precision != "day" or parsed["date_precision"] != "day":
        raise ValueError("content update date must retain day or explicit month-midpoint precision")
    if proof.get("value") != expected or parsed["end_date"] is not None:
        raise ValueError("content update date differs from literal source date precision")


def validate_content_update(snapshot):
    """Consume a complete, exact-candidate bundle; generation and semantic review stay upstream."""
    bundle = snapshot.get("derived_display", {}).get("content_update")
    if bundle is None:
        return None
    if not isinstance(bundle, dict) or bundle.get("schema_version") not in {"article-content-update.v1", "article-content-update.source-explanation.v2"}:
        raise ValueError("unsupported content update bundle")
    tables = snapshot.get("tables", {})
    articles = tables.get("articles", [])
    article = next((row for row in articles if row.get("article_id") == snapshot["entity_id"]),
        articles[0] if snapshot.get("entity_kind") == "article" and len(articles) == 1 else {})
    version = article.get("current_content_version_id")
    rows = {row["content_version_id"]: row for row in tables.get("article_content_versions", [])
        if row.get("article_id") == article.get("article_id")}
    current = rows.get(version)
    previous = rows.get(bundle.get("previous_content_version_id"))
    source_only = bundle["schema_version"] == "article-content-update.source-explanation.v2"
    if source_only:
        baseline = bundle.get("previous_publication", {})
        if (previous or bundle.get("previous_content_version_id") is not None or bundle.get("previous_body_sha256") is not None
                or baseline.get("body_status") != "unavailable" or baseline.get("differences_status") != "unconfirmed"
                or not isinstance(baseline.get("summary"), str) or not baseline["summary"].strip()
                or baseline.get("summary_sha256") != hashlib.sha256(baseline["summary"].encode()).hexdigest()
                or not re.fullmatch(r"[0-9a-f]{64}", str(baseline.get("candidate_sha256", "")))
                or not isinstance(baseline.get("limitation"), str) or not baseline["limitation"].strip()
                or bundle.get("change_evidence", {}).get("basis") != "source_explanation"):
            raise ValueError("source explanation requires an exact published summary and explicit unavailable prior body/unconfirmed differences")
    if (not current or (not source_only and (not previous or current == previous)) or bundle.get("content_version_id") != version
            or bundle.get("body_sha256") != hashlib.sha256(current["markdown_content"].encode()).hexdigest()
            or not source_only and (bundle.get("previous_body_sha256") != hashlib.sha256(previous["markdown_content"].encode()).hexdigest()
                or _body_text(current["markdown_content"]) == _body_text(previous["markdown_content"]))):
        raise ValueError("content update must bind distinct retained substantive bodies for this article")
    summary = bundle.get("summary")
    if (not isinstance(summary, str) or not summary.strip() or bundle.get("summary_content_version_id") != version
            or bundle.get("summary_sha256") != hashlib.sha256(summary.encode()).hexdigest()
            or bundle.get("summary_support") != "complete"):
        raise ValueError("content update needs a complete covering summary bound to the exact body")
    display_summary = snapshot.get("derived_display", {}).get("summary")
    if display_summary != summary:
        raise ValueError("content update summary and current approved display summary differ")
    evidence = bundle.get("change_evidence", {})
    if not isinstance(bundle.get("change_note"), str) or not bundle["change_note"].strip():
        raise ValueError("content update needs an evidence-based change note")
    if evidence.get("basis") == "body_comparison":
        old, new = evidence.get("previous_excerpt"), evidence.get("current_excerpt")
        if (not isinstance(old, str) or not old.strip() or old not in previous["markdown_content"]
                or not isinstance(new, str) or not new.strip() or new not in current["markdown_content"]
                or _body_text(old) == _body_text(new)):
            raise ValueError("change note must retain different literal old and new body evidence")
    elif evidence.get("basis") == "source_explanation":
        explanation = evidence.get("text")
        if (not isinstance(explanation, str) or not explanation.strip()
                or explanation not in current["markdown_content"]):
            raise ValueError("source update explanation must be retained in the current captured body")
    else:
        raise ValueError("change note needs a retained body comparison or source explanation")
    if "items" in evidence:
        items = evidence["items"]
        if not isinstance(items, list) or not items:
            raise ValueError("itemized changes need literal evidence for every item")
        for item in items:
            if not isinstance(item, dict) or not item.get("note") or item["note"] not in bundle["change_note"]:
                raise ValueError("each change item must appear in the separate update note")
            if item.get("basis") == "body_comparison" and not source_only:
                old, new = item.get("previous_excerpt"), item.get("current_excerpt")
                if not old or old not in previous["markdown_content"] or not new or new not in current["markdown_content"] or _body_text(old) == _body_text(new):
                    raise ValueError("each change item needs different literal previous/current evidence")
            elif item.get("basis") == "source_explanation":
                if not item.get("text") or item["text"] not in current["markdown_content"]:
                    raise ValueError("each change item needs a literal current source explanation")
            else:
                raise ValueError("unsupported itemized change basis")
    fetches = [row for row in tables.get("article_fetches", [])
        if row.get("article_id") == article.get("article_id") and row.get("content_version_id") == version
        and row.get("fetch_status") == "success" and 200 <= (row.get("http_status") or 0) <= 299]
    observed = min((_collection_timestamp(row["fetched_at"]) for row in fetches), default=None)
    if not observed or bundle.get("first_observed_at") != observed or _collection_timestamp(current["first_fetched_at"]) != observed:
        raise ValueError("content version discovery must equal its first trusted successful observation")
    date_evidence = bundle.get("date_evidence")
    if not isinstance(date_evidence, dict) or set(date_evidence) - {"updated_date", "revision_date"}:
        raise ValueError("content update date evidence must be typed Updated or Revision")
    for field, proof in date_evidence.items():
        validate_content_date_evidence(field, proof, article=article, current=current, version=version)
    return bundle


def content_date_projection(record, snapshot=None):
    """One date contract for a reader's own complete approved version."""
    collected = [record.get("first_collected_at"), record.get("collected_at")]
    collected.extend(row.get("fetched_at") for row in
        (record.get("available_content") or {}).get("collection_observations", []) if row.get("status") == "success")
    if snapshot:
        collected.extend(row.get("fetched_at") for row in snapshot.get("tables", {}).get("article_fetches", [])
            if row.get("article_id") == (record.get("core_article_id") or record.get("article_id"))
            and row.get("fetch_status") == "success"
            and 200 <= (row.get("http_status") or 0) <= 299)
    collected = [_collection_timestamp(value) for value in collected if value]
    first_collected = min(collected, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")), default=None)
    collection_fields = {"first_collected_at": first_collected} if first_collected else {}
    if snapshot and snapshot.get("static_payload") and record.get("content_date_basis"):
        validate_exported_content_update(record)
        return {**{key: record[key] for key in CONTENT_DATE_FIELDS if key in record}, **collection_fields}
    bundle = validate_content_update(snapshot) if snapshot else record.get("content_update")
    candidates = []
    def add(value, basis, label, evidence=None, authority=None):
        if not value:
            return
        try:
            _information_date(value)
        except ArticleDateImportError:
            return
        evidence = evidence or {}
        candidates.append((value, basis, label, evidence, authority or evidence.get("authority") or "source"))
    from .owner_dates import applicable_manual_dates
    decisions = record.get("manual_date_overrides", {})
    active = applicable_manual_dates(snapshot or record, decisions)
    publication = active.get("publication_date")
    if publication:
        add(publication["new_value"], "publication_date", "Published", publication["evidence"])
    elif "publication_date" in decisions:
        source = decisions["publication_date"]["source_effective"]
        add(source.get("publication_date"), "publication_date", "Published", source.get("publication_date_evidence"))
    elif (record.get("publication_date_evidence") or {}).get("authority") != "human_override":
        add(record.get("publication_date"), "publication_date", "Published", record.get("publication_date_evidence"))
    if bundle:
        for field in ("updated_date", "revision_date"):
            proof = bundle["date_evidence"].get(field)
            override = active.get(field)
            verification = record.get(field + "_verification", {})
            correction = (verification.get("review_scope") == "article_date_fields_only"
                and verification.get("content_version_id") == bundle["content_version_id"])
            if override:
                add(override["new_value"], field, "Updated" if field == "updated_date" else "Revised", override["evidence"])
            elif correction:
                add(record.get(field), field, "Updated" if field == "updated_date" else "Revised", record.get(field + "_evidence"))
            elif proof:
                add(proof["value"], field, "Updated" if field == "updated_date" else "Revised", proof)
        if not bundle["date_evidence"] and not any(row[1] in {"updated_date", "revision_date"} for row in candidates):
            add(bundle["first_observed_at"][:10], "content_version_discovery", "Discovered update on",
                {"first_observed_at": bundle["first_observed_at"], "content_version_id": bundle["content_version_id"]}, "trusted_observation")
    if not candidates:
        if first_collected:
            add(first_collected[:10], "collection_time", "Collected on", {"collected_at": first_collected}, "collection_fallback")
    value, basis, label, evidence, authority = max(candidates, key=lambda row: (row[0], row[1] != "publication_date")) if candidates else (None, "unknown", "Date unknown", {}, "unknown")
    return {"content_date": value, "content_date_basis": basis, "content_date_label": label,
        **collection_fields,
        "content_date_evidence": evidence, "content_date_authority": authority,
        "content_date_precision": evidence.get("source_date_precision", evidence.get("date_precision", "day" if value else "unknown")),
        "content_date_normalization": evidence.get("date_normalization"),
        **({"content_update_candidate_sha256": record.get("published_candidate_sha256"),
            "content_version_id": bundle["content_version_id"]} if bundle else {})}


def validate_exported_content_update(record):
    """Static consumers retain the exact approved bundle; no date/summary splice is accepted."""
    bundle = record.get("content_update")
    if bundle is None:
        if record.get("content_date_basis") in {"updated_date", "revision_date", "content_version_discovery"}:
            raise ValueError("dynamic content date requires a complete approved content package")
        return None
    version = record.get("content_version_id") or (record.get("content") or {}).get("content_version_id")
    source_only = isinstance(bundle, dict) and bundle.get("schema_version") == "article-content-update.source-explanation.v2"
    if source_only:
        baseline = bundle.get("previous_publication", {})
        if (bundle.get("previous_content_version_id") is not None or bundle.get("previous_body_sha256") is not None
                or baseline.get("body_status") != "unavailable" or baseline.get("differences_status") != "unconfirmed"
                or not baseline.get("summary") or baseline.get("summary_sha256") != hashlib.sha256(baseline["summary"].encode()).hexdigest()
                or not re.fullmatch(r"[0-9a-f]{64}", str(baseline.get("candidate_sha256", ""))) or not baseline.get("limitation")
                or bundle.get("change_evidence", {}).get("basis") != "source_explanation"):
            raise ValueError("static source explanation lacks its exact unavailable-body baseline")
    if (not isinstance(bundle, dict) or bundle.get("schema_version") not in {"article-content-update.v1", "article-content-update.source-explanation.v2"}
            or not version or bundle.get("content_version_id") != version
            or bundle.get("summary_content_version_id") != version or bundle.get("summary_support") != "complete"
            or bundle.get("summary") != record.get("summary") or not bundle.get("summary")
            or bundle.get("summary_sha256") != hashlib.sha256(bundle["summary"].encode()).hexdigest()
            or not record.get("published_candidate_sha256")
            or record.get("content_update_candidate_sha256") != record["published_candidate_sha256"]
            or not bundle.get("change_note") or not isinstance(bundle.get("date_evidence"), dict)
            or bundle.get("change_evidence", {}).get("basis") not in {"body_comparison", "source_explanation"}
            or not source_only and (not bundle.get("previous_content_version_id") or bundle["previous_content_version_id"] == version)
            or any(not isinstance(bundle.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", bundle[key])
                for key in (("body_sha256",) if source_only else ("body_sha256", "previous_body_sha256")))):
        raise ValueError("static content update is incomplete, stale or not bound to its current approved version")
    _collection_timestamp(bundle.get("first_observed_at"))
    expected = content_date_projection(record)
    for key in ("content_date", "content_date_basis", "content_date_authority", "content_date_precision", "content_date_normalization"):
        if record.get(key) != expected[key]:
            raise ValueError("static content date does not match its complete approved package")
    return bundle


def _article_event_from_observation(item: Mapping[str, Any]) -> dict[str, Any] | None:
    evidence = item.get("evidence")
    if _typed_date_kind(evidence) != "event":
        return None
    if not isinstance(evidence, Mapping):
        raise ArticleDateImportError("event date evidence must be an object")
    precision = evidence.get("date_precision")
    if precision not in {"day", "month", "year"}:
        raise ArticleDateImportError("event date evidence must state day, month, or year precision")
    start = _typed_information_date(evidence.get("event_start_date", item.get("observed_at")), precision)
    observed = _typed_information_date(item.get("observed_at"), precision)
    end = _typed_information_date(evidence.get("event_end_date", start), precision)
    if observed != start:
        raise ArticleDateImportError("event observed_at must equal its start date")
    if start > end:
        raise ArticleDateImportError("event end date must not precede its start date")
    event_name = evidence.get("event_name")
    if not isinstance(event_name, str) or not event_name.strip():
        raise ArticleDateImportError("event evidence must include event_name")
    raw_date = evidence.get("date_text")
    if not isinstance(raw_date, str) or not raw_date.strip():
        raise ArticleDateImportError("event evidence must include its source date text")
    event_type = evidence.get("event_type")
    if event_type not in {"meeting", "conference", "summit", "webinar", "deadline", "retrospective"}:
        raise ArticleDateImportError("event evidence has an unsupported event_type")
    status = evidence.get("event_status", "scheduled")
    if status not in {"scheduled", "tentative", "postponed", "cancelled", "conflict", "retrospective"}:
        raise ArticleDateImportError("event evidence has an unsupported event_status")
    canonical = json.dumps([event_name.casefold().strip(), start, end, precision], ensure_ascii=False, separators=(",", ":"))
    event_id = "reviewed-article-event-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    source_url = evidence.get("source_url")
    manual = (evidence.get("authority") == "human_override" and evidence.get("authorization_id")
        and evidence.get("instruction_reference"))
    urls = list(dict.fromkeys(value for value in (source_url, evidence.get("event_url")) if isinstance(value, str) and value.strip()))
    result = {
        "event_id": event_id, "article_id": item.get("article_id"),
        "name": event_name.strip(), "event_type": event_type, "status": status,
        "date_precision": precision, "start_date": start, "end_date": end,
        "raw_date": raw_date.strip(), "raw_time_text": evidence.get("raw_time_text"),
        "event_timezone": evidence.get("event_timezone"), "organizer": evidence.get("event_organizer"),
        "location": evidence.get("event_location"), "online_url": evidence.get("event_url") or source_url,
        "relevance_reason": evidence.get("event_summary"), "source_urls": urls,
        "source_count": 1, "verification_status": "verified" if evidence.get("review_status") == "pass" else "unchecked",
        "verification_basis": "independent_article_date_review" if evidence.get("review_status") == "pass" else None,
        "origin": "reviewed_article_page", "collection_status": "collected", "access_status": "accessible",
    }
    if manual:
        from .owner_dates import MANUAL_AUTHORIZATION
        result.update(authority="human_override", instruction_reference=evidence["instruction_reference"],
            authorization_id=evidence["authorization_id"], verification_status="unchecked",
            verification_basis=MANUAL_AUTHORIZATION, origin="human_instruction", access_status="unchecked", collection_status="authorized",
            replaces_event_id=evidence.get("event_id"))
    return result


def select_article_events(observations: Any) -> list[dict[str, Any]]:
    """Project only typed, source-backed activity dates into the Dates view."""
    events = []
    for item in observations or []:
        if not isinstance(item, Mapping) or item.get("kind") != "page_information":
            continue
        event = _article_event_from_observation(item)
        if event is not None and (event["verification_status"] == "verified" or event.get("authority") == "human_override"):
            events.append(event)
    manual_keys = {event.get("replaces_event_id") for event in events if event.get("authority") == "human_override"}
    return [event for event in events if event.get("authority") == "human_override"
        or event["event_id"] not in manual_keys]


def validate_observations(payload: Any) -> list[dict[str, Any]]:
    if (not isinstance(payload, Mapping) or set(payload) != {"schema_version", "observations"}
            or payload.get("schema_version") != OBSERVATIONS_SCHEMA
            or not isinstance(payload.get("observations"), list)):
        raise ArticleDateImportError(f"input must use {OBSERVATIONS_SCHEMA}")
    checked = []
    seen = {}
    duplicate_identities = set()
    for raw in payload["observations"]:
        if not isinstance(raw, Mapping) or set(raw) != {
            "article_id", "canonical_url", "observation_kind", "observed_at", "evidence"
        }:
            raise ArticleDateImportError("article date observation fields are invalid")
        item = dict(raw)
        item["article_id"] = _text(item["article_id"], "article_id")
        item["canonical_url"] = _text(item["canonical_url"], "canonical_url")
        try:
            canonical = canonical_url(item["canonical_url"])
        except (TypeError, ValueError) as exc:
            raise ArticleDateImportError("canonical_url is invalid") from exc
        if canonical != item["canonical_url"]:
            raise ArticleDateImportError("canonical_url must already be canonical")
        kind = item["observation_kind"]
        if kind not in {"collection", "page_information"}:
            raise ArticleDateImportError("observation_kind must be collection or page_information")
        evidence = item["evidence"]
        if not isinstance(evidence, Mapping) or not set(_EVIDENCE_REQUIRED) <= set(evidence):
            raise ArticleDateImportError("evidence is missing source identity fields")
        evidence = dict(evidence)
        for field in _EVIDENCE_REQUIRED:
            if field == "record_id" and type(evidence[field]) is int:
                evidence[field] = str(evidence[field])
            _text(evidence[field], f"evidence.{field}")
        report_day = report_observation_date(item["observed_at"], evidence) if kind == "collection" else None
        if kind == "collection":
            item["observed_at"] = report_day or _collection_timestamp(item["observed_at"])
        elif _typed_date_kind(evidence):
            item["observed_at"] = _typed_information_date(item["observed_at"], evidence.get("date_precision"))
        else:
            item["observed_at"] = _information_date(item["observed_at"])
        if kind == "collection" and report_day is None:
            source_record = (evidence["source_system"], evidence["table"])
            if source_record not in {
                ("web_listening", "site_snapshots"),
                ("web_listening", "changes"),
                ("web_listening", "observations"),
                ("climate_registry", "article_fetches"),
            }:
                raise ArticleDateImportError("collection evidence must identify a verified acquisition record")
            if source_record == ("climate_registry", "article_fetches") and (
                evidence.get("match_basis") != "successful_article_fetch"
                or type(evidence.get("http_status")) is not int
                or not 200 <= evidence["http_status"] <= 299
            ):
                raise ArticleDateImportError("article_fetches collection evidence must be successful HTTP 2xx")
        item["evidence"] = evidence
        if _typed_date_kind(evidence) == "event":
            _article_event_from_observation(item)
        identity = (
            item["article_id"], kind, evidence["source_system"], evidence["database"],
            evidence["table"], evidence["record_id"],
        )
        normalized = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        prior = seen.setdefault(identity, normalized)
        if prior != normalized:
            raise ArticleDateImportError("the same source record has conflicting date observations")
        if identity in duplicate_identities:
            continue
        duplicate_identities.add(identity)
        item["_identity"] = identity
        item["_evidence_json"] = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        checked.append(item)
    return checked


def _database_path(value: str | Path) -> Path:
    path = Path(value)
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
    except OSError as exc:
        raise ArticleDateImportError(f"database does not exist: {path}") from exc
    if not path.is_absolute() or path != resolved or not path.is_file():
        raise ArticleDateImportError("database must be an absolute canonical regular file")
    return path


def _has_collection(connection: sqlite3.Connection, article_id: str) -> bool:
    for row in connection.execute(
        """SELECT observed_at,evidence_json FROM article_date_observations
           WHERE article_id=? AND observation_kind='collection'""", (article_id,),
    ):
        if report_observation_date(row[0], json.loads(row[1])) is None:
            _collection_timestamp(row[0])
            return True
    return bool(connection.execute(
        """SELECT 1 FROM article_fetches f
           JOIN article_content_versions c
             ON c.article_id=f.article_id AND c.content_version_id=f.content_version_id
           WHERE f.article_id=? AND f.fetch_status='success'
             AND f.http_status BETWEEN 200 AND 299 LIMIT 1""",
        (article_id,),
    ).fetchone())


def import_observations(
    database: str | Path,
    payload: Any,
    *,
    entity_kind: str = "article",
    write: bool = False,
    recorded_at: str | None = None,
) -> dict[str, int]:
    """Validate and optionally append evidence to a migrated Registry."""
    from .persistent import _exclusive_database_lock
    from .publication import require_publication_migration, resolve_database, stage_entities
    if entity_kind not in {"article", "pdf_article"}:
        raise ArticleDateImportError("entity_kind must be article or pdf_article")
    table = "article_date_observations" if entity_kind == "article" else "pdf_article_date_observations"
    entity_table = "articles" if entity_kind == "article" else "pdf_intake_articles"
    path = resolve_database(_database_path(database))
    items = validate_observations(payload)
    mode = "rw" if write else "ro"
    with _exclusive_database_lock(path) if write else nullcontext():
        connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            version = validate_registry_contract(connection)
            minimum_version = 18 if entity_kind == "article" else 25
            if version < minimum_version:
                raise ArticleDateImportError(
                    f"Registry schema {minimum_version} or later is required for {entity_kind}; run the normal migration first"
                )
            if write:
                require_publication_migration(connection)
                connection.execute("BEGIN IMMEDIATE")
            if entity_kind == "pdf_article" and any(item["observation_kind"] != "page_information"
                    or _typed_date_kind(item["evidence"]) is None for item in items):
                raise ArticleDateImportError("PDF article date evidence must be typed page_information")
            collection_articles = {
                item["article_id"] for item in items if item["observation_kind"] == "collection"
                and report_observation_date(item["observed_at"], item["evidence"]) is None
            }
            for item in items:
                article = connection.execute(
                    f"SELECT canonical_url FROM {entity_table} WHERE article_id=?", (item["article_id"],)
                ).fetchone()
                if article is None or article["canonical_url"] != item["canonical_url"]:
                    raise ArticleDateImportError("article_id and exact canonical_url do not match Registry")
                if (item["observation_kind"] == "page_information"
                        and _typed_date_kind(item["evidence"]) is None
                        and entity_kind == "article"
                        and (item["article_id"] in collection_articles
                             or _has_collection(connection, item["article_id"]))):
                    raise ArticleDateImportError("page_information is allowed only when no collection evidence exists")

            counts = {"inserted": 0, "already_present": 0}
            stamp = recorded_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            stamp = _collection_timestamp(stamp)
            for item in items:
                evidence = item["evidence"]
                identity = item["_identity"]
                source = (item["article_id"], item["observation_kind"], *identity[2:])
                old = connection.execute(
                    f"""SELECT canonical_url, observed_at, evidence_json FROM {table}
                       WHERE article_id=? AND observation_kind=? AND source_system=?
                         AND source_database=? AND source_table=? AND source_record_id=?""",
                    source,
                ).fetchone()
                expected = (item["canonical_url"], item["observed_at"], item["_evidence_json"])
                if old is not None:
                    if tuple(old) != expected:
                        raise ArticleDateImportError("stored source record conflicts with imported date evidence")
                    counts["already_present"] += 1
                    continue
                if not write:
                    counts["inserted"] += 1
                    continue
                stable_input = "\n".join(map(str, (item["article_id"], item["observation_kind"], *identity[2:])))
                observation_id = "article-date-" + hashlib.sha256(stable_input.encode()).hexdigest()[:32]
                connection.execute(
                    f"""INSERT INTO {table}(
                           observation_id, article_id, canonical_url, observation_kind, observed_at,
                           source_system, source_database, source_table, source_record_id,
                           evidence_json, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (observation_id, item["article_id"], item["canonical_url"], item["observation_kind"],
                     item["observed_at"], evidence["source_system"], evidence["database"],
                     evidence["table"], evidence["record_id"], item["_evidence_json"], stamp),
                )
                counts["inserted"] += 1
            if write:
                if counts["inserted"]:
                    entity_ids = {item["article_id"] for item in items}
                    if entity_kind == "article":
                        stage_entities(connection, article_ids=entity_ids)
                    else:
                        from .publication import canonical_entity
                        linked_core_ids = {
                            owner_id
                            for identity in entity_ids
                            for owner_kind, owner_id in [canonical_entity(connection, "pdf_article", identity)]
                            if owner_kind == "article"
                        }
                        stage_entities(connection, article_ids=linked_core_ids, pdf_article_ids=entity_ids)
                    from .display import refresh_display
                    refresh_display(connection)
                connection.commit()
            return counts
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
