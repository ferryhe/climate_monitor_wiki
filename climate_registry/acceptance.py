"""Read-only G6 acceptance. Observations never define the Registry target.

This module deliberately does not import any approval, refresh or reload entrypoint.
Captured evidence proves only the named surface at its observation time.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .acquisition_review import digest
from .publication import ENTITY_TABLES, public_revision, resolve_database
from .read_api import RegistryReader

SURFACES = ("display_api", "wiki_export", "rag", "chat")
# Current facts are compared separately from immutable appearances/version history.
FACT_FIELDS = (
    "title", "name", "summary", "report_summary", "summary_provenance", "summary_basis",
    "metadata_provenance", "document_kind", "publication_eligible", "canonical_url", "source_urls",
    "checked_source_url", "verification_status", "metadata_only", "current_version_id",
    "content_version_id", "published_candidate_sha256", "content_update_candidate_sha256",
    "publication_date", "updated_date", "revision_date", "first_collected_at", "collected_at",
    "content_date", "content_date_basis", "content_date_label", "content_date_authority",
    "content_date_precision", "content_date_normalization", "content_date_evidence",
    "publication_date_evidence", "updated_date_evidence", "revision_date_evidence",
    "publication_date_verification", "updated_date_verification", "revision_date_verification",
    "manual_date_overrides", "content_update", "summary_label", "source", "kind", "event_type",
    "date_precision", "raw_date", "start_date", "end_date", "deadline_date", "timezone", "status",
    "activity_dates", "is_activity", "core_article_id", "canonical_event_id", "canonical_pdf_event_id",
    "canonical_occurrence_mappings", "identity_mapping", "article_id", "event_id", "occurrence_id",
    "owner_agent_verification", "manual_verification", "original_summary_label", "categories", "keywords",
    "source_document_sha256", "page", "source_provenance", "source_kind", "organizer", "event_timezone",
    "location", "deadline_type", "relevance_reason", "publisher", "article_event_relations",
)
RELATION_FIELDS = ("occurrences", "pdf_occurrences", "pdf_observations", "sources")
PROVENANCE_FIELDS = (
    "article_id", "event_id", "occurrence_id", "source_document_sha256", "page", "raw_url",
    "core_article_id", "canonical_event_id", "canonical_pdf_event_id", "content_version_id",
    "source_url", "source_id", "source_kind", "source_revision_sha256", "published_candidate_sha256",
    "summary", "display_summary", "summary_label", "summary_basis", "verification_status",
    "metadata_only", "publication_date", "publication_date_evidence", "updated_date", "revision_date",
    "start_date", "end_date", "deadline_date", "date_precision", "source_provenance",
    "canonical_occurrence_mappings", "owner_agent_verification", "manual_verification",
)
RETRIEVAL_DATE_FIELDS = ("content_date", "content_date_basis", "content_date_precision", "content_date_authority",
    "content_date_normalization", "content_date_label", "content_date_evidence", "content_version_id", "published_candidate_sha256")
EVENT_DATE_FIELDS = ("start_date", "end_date", "deadline_date", "date_precision", "deadline_type")


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_facts(payload):
    """Common current-version contract for native public DTOs, without history bodies."""
    result = {key: payload.get(key) for key in FACT_FIELDS}
    for key in ("content", "available_content"):
        value = payload.get(key) or {}
        result[key] = {field: value.get(field) for field in (
            "content_version_id", "content_sha256", "markdown", "supporting_excerpt", "display_policy")}
    for key in RELATION_FIELDS:
        result[key] = sorted(
            [{field: row.get(field) for field in PROVENANCE_FIELDS} for row in payload.get(key, [])],
            key=digest,
        )
    return result


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _query_requirements(facts):
    bundle = facts.get("content_update") or {}
    current = bundle.get("summary") or facts.get("summary") or facts.get("name") or facts.get("title")
    values = [("current_content", current), ("change_note", bundle.get("change_note"))]
    result = [{"kind": kind, "text": value, "sha256": _sha(value)} for kind, value in values if value]
    if facts.get("event_id"):
        if facts.get("name") and facts["name"] != current:
            result.append({"kind": "event_identity", "text": facts["name"], "sha256": _sha(facts["name"])})
        result.extend({"kind": "event_" + field, "field": field, "text": value, "sha256": _sha(value)}
            for field, value in _event_expected_dates(facts).items())
    return result


def _source_covers(excerpt, statement):
    return " ".join(statement.split()).casefold() in " ".join(excerpt.split()).casefold()


def _statement_tokens(text):
    # Conservative factual-clause paraphrases; values, order and negation remain significant.
    text = re.sub(r"n['’]t\b", " not", text.casefold())
    text = re.sub(r"\bwent up\b", "increased", text)
    equivalents = {word: "increase" for word in ("rose", "risen", "rise", "rises", "rising", "increased", "increases", "increasing", "climbed", "climb")}
    equivalents.update({word: "decrease" for word in ("fell", "fall", "fallen", "decreased", "decreases", "decreasing", "declined", "decline")})
    equivalents["losses"] = "loss"
    return [equivalents.get(word, word) for word in re.findall(r"\d+(?:\.\d+)?|[^\W\d_]+", text)
        if word not in {"a", "an", "the", "has", "have", "had", "is", "are", "was", "were", "did", "does", "do"}]


def _referenced_denial(answer, statement):
    for match in re.finditer(r"\b(?:this|that|these|those|it|both)(?:\s+(?:claim|statement|figure|claims|statements|figures))?\s+"
            r"(?:is|are|remains?|was|were)\s+(?:also\s+)?(incorrect|inaccurate|wrong|false|untrue|unsupported|unsubstantiated|unproven|unverified|uncertain)\b", answer, re.I):
        if match[1].casefold() not in _statement_tokens(statement):
            return True
    return False


def _answer_for_hit(answer, hit, document, position):
    """Recognize only this source's native brief item, retaining visible claim text."""
    if not re.search(r"(?m)^- \[\d+\] \((?:wiki|raw source)\) ", answer):
        return answer, None
    from agentic_wiki.wiki_agent import _strip_markdown, _evidence_excerpt
    source_index = hit.get("index") or position
    titles = {Path(document["page"]).stem, document["markdown"].split("\n", 1)[0].removeprefix("# ")}
    if hit.get("title") is not None:
        titles &= {hit["title"]}
    corpus = {"wiki": "wiki", "source": "raw source"}.get(hit.get("corpus"))
    labels = re.escape(corpus) if corpus else "wiki|raw source"
    for title in titles:
        match = re.search(r"(?m)^- \[" + re.escape(str(source_index)) + r"\] \((?:" + labels + r")\) "
            + re.escape(title) + r": ([^\n]*)$", answer)
        if match is None:
            continue
        excerpt = hit["excerpt"]
        text = _strip_markdown(excerpt)
        if match[1] != _evidence_excerpt(text, 240, citation_text=excerpt):
            continue
        # These are the same visible source characters used by the native 240-character renderer.
        visible = text[:240].rstrip()
        start, end = 0, len(visible)
        heading = re.match(r"^#{1,4}\s+([^\n]+)\n", excerpt)
        if heading and heading[1] in {"Article semantic summary", "Registry article-version summary", "Verified article content"}:
            start = len(_strip_markdown(heading[0]))
            if heading[1] in {"Article semantic summary", "Registry article-version summary"}:
                provenance = list(re.finditer(r"(?m)^Provenance: (?:[^\n]*; content version: [^\n]*; article citation:"
                    r"|Registry article version; Registry article version:)[^\n]*$", excerpt))
                if provenance:
                    offset = text.rfind(_strip_markdown(provenance[-1][0]))
                    if offset >= start:
                        end = min(end, offset)
        return visible[start:end].strip(), source_index
    return "", source_index


def _answer_covers(answer, statement):
    statements = [value for value in re.split(r"(?<=[.!?;])\s+|\n+", statement) if value.strip()]
    if len(statements) > 1:
        return all(_answer_covers(answer, value) for value in statements)
    required = _statement_tokens(statement)
    if not required:
        return False
    if _referenced_denial(answer, statement):
        return False
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", answer):
        sentence = re.sub(r"\[\d+\]", "", sentence).strip()
        sentence = re.sub(r"^(?:according to (?:the )?(?:current |approved |latest )?(?:report|source)[,:]?\s*|"
            r"(?:the )?(?:current |approved |latest )?(?:report|source) (?:states|says|reports|confirms) that\s+)", "", sentence, flags=re.I)
        actual = _statement_tokens(sentence)
        # Credit an asserted clause, not a quoted substring within a denial or qualification.
        if actual == required:
            return True
    return False


def _quantitative_paraphrase(answer, facts, requirement):
    """Compare one approved numeric transition without dropping its remaining clauses."""
    from decimal import Decimal
    bundle = facts.get("content_update") or {}
    if _referenced_denial(answer, (bundle.get("summary") or "") + " " + (bundle.get("change_note") or "")):
        return False
    change = bundle.get("change_evidence") or {}
    pattern = r"(\d+(?:\.\d+)?)\s*(billion|million|percent)\b"
    before = re.findall(pattern, change.get("previous_excerpt", ""), re.I)
    after = re.findall(pattern, change.get("current_excerpt", ""), re.I)
    if (len(before) != 1 or len(after) != 1 or before[0][1].casefold() != after[0][1].casefold()
            or len(re.split(r"(?<=[.!?])\s+", requirement["text"].strip())) != 1):
        return False
    old, unit = before[0]
    new = after[0][0]
    unit = unit.casefold()
    quantities = [(number, name.casefold()) for number, name in re.findall(pattern, requirement["text"], re.I)]
    if (Decimal(new) == Decimal(old) or not quantities or len(quantities) > (2 if requirement["kind"] == "change_note" else 1)
            or not set(quantities) <= {(old, unit), (new, unit)}):
        return False

    def words(text):
        for phrase, token in ((r"up to|at most", "upperbound"), (r"at least|no less than", "lowerbound"),
                (r"subject to", "subjectto"), (r"approximately|about", "approximate")):
            text = re.sub(r"\b(?:" + phrase + r")\b", token, text, flags=re.I)
        return _statement_tokens(text)

    protected = {"not", "no", "never", "false", "cannot", "unconfirmed", "unknown", "uncertain", "whether", "if",
        "only", "preliminary", "provisional", "approximate", "upperbound", "lowerbound", "subjectto"}
    framing = {"increase", "decrease", "reached", "reach", "estimate", "estimated", "from", "to", "at",
        "now", "remain", "remains", "still", "but", "and", old, new, unit}
    reporting = r"\b(?:reached|reach|rose|increased|decreased|fell|estimated|remains?|is|are|totalled|totaled|reported)\b"
    previous_subject = words(re.split(reporting, change["previous_excerpt"], maxsplit=1, flags=re.I)[0])
    current_subject = words(re.split(reporting, change["current_excerpt"], maxsplit=1, flags=re.I)[0])
    topic = [word for word in previous_subject if word in current_subject and word not in protected | framing]
    if not topic:
        return False
    source_core = (set(topic) | framing) - protected
    residual = [word for word in words(requirement["text"]) if word not in source_core]
    allowed_residual = {word for text in (bundle.get("summary", ""), bundle.get("change_note", ""))
        for word in words(text) if word not in source_core}
    ratio = (r"(?:the\s+)?(?:revised|current|latest)\s+estimate\s+(?:is\s+|of\s+|stands\s+at\s+)?\$?"
        + re.escape(new) + r"\s*" + re.escape(unit) + r"\s*,?\s*(?:double|twice)\s+(?:the\s+)?(?:earlier|previous|prior)\s+\$?"
        + re.escape(old) + r"\s*" + re.escape(unit) + r"\b(?P<tail>.*)")
    transition = (r"(?P<subject>.+?)\s+(?:are\s+|is\s+)?now\s+estimated\s+at\s+\$?" + re.escape(new)
        + r"\s*" + re.escape(unit) + r"\s*,?\s*up\s+from\s+\$?" + re.escape(old) + r"\s*" + re.escape(unit) + r"\b(?P<tail>.*)")
    answer_core = (source_core | {"revised", "current", "latest", "stands", "of", "up", "double", "twice", "earlier", "previous", "prior", "figure"}) - protected
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", answer):
        sentence = re.sub(r"\[\d+\]", "", sentence).strip()
        match = re.fullmatch(ratio, sentence, flags=re.I) if Decimal(new) == 2 * Decimal(old) and "estimate" in words(bundle.get("change_note", "")) else None
        if match is None and Decimal(new) > Decimal(old):
            match = re.fullmatch(transition, sentence, flags=re.I)
            if match is not None:
                subject = [word for word in words(match["subject"]) if word not in allowed_residual]
                if subject not in (topic, topic + ["estimate"]):
                    match = None
        if match is None:
            continue
        if not set(words(match["tail"])) <= allowed_residual | {"but", "and", "estimate", "remains", "remain", "still"}:
            continue
        actual_residual = [word for word in words(sentence) if word not in answer_core]
        if not set(actual_residual) <= allowed_residual:
            continue
        # Residual words retain all qualifications beyond the numeric proposition.
        pending = iter(actual_residual)
        if all(any(actual == required for actual in pending) for required in residual):
            return True
    return False


def _event_expected_dates(facts):
    return {field: facts[field] for field in EVENT_DATE_FIELDS if facts.get(field) not in {None, "", "unknown"}}


def _event_date_literal(value):
    from datetime import date
    from climate_monitor.pdf_intake import _MONTH, _parse_calendar_date
    value = re.sub(r"\[\d+\]", "", value).strip().rstrip(".;")
    iso = r"20\d{2}-\d{2}(?:-\d{2})?"
    interval = re.fullmatch(rf"({iso})\s*(?:to|through|until|[–—])\s*({iso})", value, re.I)
    if interval:
        start, end = (_event_date_literal(part) for part in interval.groups())
        return {**start, "end_date": end.get("start_date")} if start and end else {}
    if re.fullmatch(iso, value):
        date.fromisoformat(value + ("-01" if len(value) == 7 else ""))
        return {"start_date": value, "end_date": None, "date_precision": "month" if len(value) == 7 else "day"}
    value = re.sub(r"\s+(?:to|through|until)\s+", "–", value, flags=re.I)
    cross = re.fullmatch(rf"({_MONTH})\s+(\d{{1,2}})\s*[–—-]\s*({_MONTH})\s+(\d{{1,2}}),?\s+(20\d{{2}})", value, re.I)
    if cross:
        value = f"{cross[2]} {cross[1]}–{cross[4]} {cross[3]} {cross[5]}"
    single = re.fullmatch(rf"({_MONTH})\s+(\d{{1,2}})(?:\s*[–—-]\s*(\d{{1,2}}))?,?\s+(20\d{{2}})", value, re.I)
    if single:
        value = single[2] + ("–" + single[3] if single[3] else "") + f" {single[1]} {single[4]}"
    result = _parse_calendar_date(value)
    return result if result.get("start_date") else {}


def _event_date_evidence(facts, evidence):
    expected = _event_expected_dates(facts)
    values = {field: set() for field in EVENT_DATE_FIELDS}
    def add(field, value):
        if value not in {None, ""}:
            values[field].add(value)
    metadata = {**{field: evidence[field] for field in EVENT_DATE_FIELDS if field in evidence}, **(evidence.get("date_metadata") or {})}
    for field in EVENT_DATE_FIELDS:
        add(field, metadata.get(field))
    text = evidence.get("excerpt") or ""
    sections = re.split(r"(?m)^##\s+", text)
    if len(sections) > 1:
        text = "\n".join(section for section in sections[1:] if section.splitlines()[0].strip() == facts.get("name"))
    for line in re.split(r"\n|(?<=[.!?])\s+", text):
        line = re.sub(r"^PDF observation:\s*", "", line.strip(), flags=re.I)
        name = facts.get("name") or ""
        if name and line.casefold().startswith(name.casefold()):
            line = line[len(name):].lstrip(" .:")
        label = re.match(r"^(Date\(s\)|Start date|End date|Deadline|Date precision|Deadline type):\s*(.+)$", line, re.I)
        role = "interval"
        if label:
            role = {"date(s)": "interval", "start date": "start_date", "end date": "end_date", "deadline": "deadline_date",
                "date precision": "date_precision", "deadline type": "deadline_type"}[label[1].casefold()]
            line = label[2]
        else:
            deadline = re.match(r"^(registration|submission|application)\s+(?:closes?|ends?|deadline(?:\s+is)?)\s+(?:on\s+)?(.+)$", line, re.I)
            if deadline:
                add("deadline_type", deadline[1].casefold())
                role, line = "deadline_date", deadline[2]
            else:
                line = re.sub(r"^(?:runs|takes place|meets|is held|is scheduled|will be held)\s+(?:from|on|in|for)\s+", "", line, flags=re.I)
        if role in {"date_precision", "deadline_type"}:
            add(role, line.strip().rstrip(".;").casefold())
            continue
        try:
            parsed = _event_date_literal(line)
        except ValueError:
            parsed = {}
        if role in {"end_date", "deadline_date"}:
            add(role, parsed.get("start_date"))
        else:
            for field in ("start_date", "end_date", "date_precision"):
                add(field, parsed.get(field))
    missing = [field for field in expected if not values[field]]
    mismatched = [field for field, actual in values.items() if actual and actual != {facts.get(field)}]
    if not any(expected.get(field) for field in ("start_date", "end_date", "deadline_date")):
        missing.append("approved_event_date_facts")
    if any(source.get(field) not in {None, "event_date", "activity_date"}
            for source in (evidence, metadata) for field in ("content_date_basis", "date_kind")):
        mismatched.append("date_kind")
    return sorted(missing), sorted(mismatched)


def _retrieval_dates(facts, hit):
    if facts.get("event_id"):
        return _event_date_evidence(facts, hit)
    if facts.get("content_date_basis") is None:
        return [], []
    metadata = hit.get("date_metadata")
    if not isinstance(metadata, dict):
        metadata = {field: hit[field] for field in RETRIEVAL_DATE_FIELDS if field in hit}
    expected = {field: facts.get(field) for field in RETRIEVAL_DATE_FIELDS if facts.get(field) is not None}
    expected.update(date=facts.get("content_date") or "", date_kind=facts["content_date_basis"])
    actual = {**metadata, **{field: hit[field] for field in ("date", "date_kind") if field in hit}}
    if facts.get("content_date") is None and actual.get("date") == "-":
        actual["date"] = ""  # SearchHit.to_source's explicit unknown-date display sentinel.
    missing = sorted(field for field in expected if field not in actual or actual[field] is None)
    mismatched = sorted(field for field in expected if field not in missing and actual[field] != expected[field])
    if metadata.get("content_date_normalization") != facts.get("content_date_normalization"):
        mismatched.append("content_date_normalization")
    return missing, mismatched


def _hit_target(entities, identity, requested):
    matches = [row for row in entities if row["present"] and identity in {
        (row["entity_kind"], row["entity_id"]), tuple(row["public_entity"]), tuple(row["serving_entity"])}]
    return next((row for row in matches if (row["entity_kind"], row["entity_id"]) == requested), None) or next(
        (row for row in matches if (row["entity_kind"], row["entity_id"]) == identity), None) or (matches[0] if len(matches) == 1 else None)


def _summary_facts(facts):
    # Reports retain exact comparison hashes, not bodies, summaries, URLs or private receipts.
    return {"sha256": digest(facts), "fields": {key: digest(value) for key, value in facts.items()},
        "dates": {key: facts.get(key) for key in ("publication_date", "updated_date", "revision_date",
            "content_date", "content_date_basis", "content_date_authority", "content_date_precision",
            "content_date_normalization", "start_date", "end_date", "deadline_date", "date_precision")},
        "content_version_id": facts.get("content_version_id"),
        "published_candidate_sha256": facts.get("published_candidate_sha256")}


def _references(value):
    """Keep typed IDs/hashes for private evidence lookup, without raw text or paths."""
    references = {}
    def visit(item, prefix):
        if isinstance(item, dict):
            for key, child in item.items():
                path = prefix + "." + key if prefix else key
                if (key.endswith(("_sha256", "_id")) or key in {"sha256", "page", "discovery_kind", "scope"}) and isinstance(child, (str, int)):
                    references[path] = child
                elif key in {"packet_json", "evidence_json"} and isinstance(child, str):
                    visit(json.loads(child), path)
                else:
                    visit(child, path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{prefix}[{index}]")
    visit(value, "")
    return references


def _approval_references(connection, candidate):
    return [{"review_id": row["review_id"], "status": row["status"], "reviewed_at": row["reviewed_at"],
        "receipt_sha256": digest(json.loads(row["receipt_json"]))} for row in connection.execute(
        "SELECT * FROM registry_reviews WHERE candidate_sha256=? AND status IN ('pass','accepted_legacy') ORDER BY review_id", (candidate,))] if candidate else []


def _published_changes(connection, snapshot, projection):
    baseline_sha = (projection.get("approval_inheritance_baseline") or {}).get("candidate_sha256")
    row = connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (baseline_sha,)).fetchone()
    if row is None:
        return {"status": "unknown", "reason": "approval_baseline_not_retained_or_not_frozen"}
    previous = json.loads(row[0])
    if digest(previous) != baseline_sha:
        raise ValueError("approval baseline snapshot hash differs")
    def fields(value):
        result = {"tables." + key: digest(rows) for key, rows in value.get("tables", {}).items()}
        result.update({"display." + key: digest(field) for key, field in value.get("derived_display", {}).items()})
        return result
    before, after = fields(previous), fields(snapshot)
    changed = {key: {"before_sha256": before.get(key), "after_sha256": after.get(key)}
        for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)}
    return {"status": "compared", "baseline_candidate_sha256": baseline_sha, "changed": changed,
        "summary_unchanged": previous.get("derived_display", {}).get("summary") == snapshot.get("derived_display", {}).get("summary"),
        "content_package_unchanged": previous.get("derived_display", {}).get("content_update") == snapshot.get("derived_display", {}).get("content_update")}


def _records(reader):
    from .publication import _public_article_dto, _pdf_dto, _calendar_dto, _merge_pdf_article_payloads
    values = {}
    for kind, identity in reader._public_candidates("article", "pdf_article"):
        if kind == "article":
            values[(kind, identity)] = _public_article_dto(reader.article(identity))
        else:
            values[(kind, identity)] = _pdf_dto(_merge_pdf_article_payloads(reader.pdf_article(identity), None))
    # An approved core snapshot can serve a PDF parent suppressed from candidate iteration.
    for item in reader.pdf_articles_all(include_linked=True):
        key = ("pdf_article", item["article_id"])
        if key not in values:
            values[key] = _pdf_dto(_merge_pdf_article_payloads(reader.pdf_article(key[1]), None))
    for item in reader.meetings_all(base_date="1900-01-01", include_unknown=True):
        identity = item.get("canonical_event_id") or item.get("canonical_pdf_event_id") or item.get("event_id")
        if identity:
            values[("meeting", identity)] = _calendar_dto(item)
    values.update(_calendar_records([_calendar_dto(item) for item in reader.pdf_calendar_items_all()]))
    return values


def _calendar_records(items):
    groups = {}
    for item in items:
        groups.setdefault(item["event_id"], []).append(item)
    return {("pdf_meeting", identity): {**sorted(rows, key=lambda row: row["occurrence_id"])[0],
        **({"occurrences": sorted(rows, key=lambda row: row["occurrence_id"])} if len(rows) > 1 else {})}
        for identity, rows in groups.items()}


def _pages(reader, records):
    from .wiki import render_registry_page
    names = {"registry-meetings.md"} if any(key[0] in {"meeting", "pdf_meeting"} for key in records) else set()
    for (kind, identity), record in records.items():
        if kind == "article":
            names.add(f"article-{identity}.md")
        elif kind == "pdf_article":
            names.update(_entity_pages(kind, identity, record))
    return {name: content for name in sorted(names)
        if (content := render_registry_page(reader.database, name, reader=reader)) is not None}


def _entity_pages(kind, identity, payload):
    if kind == "article":
        return [f"article-{identity}.md"]
    if kind == "pdf_article":
        if payload.get("core_article_id"):
            return [f"article-{payload['core_article_id']}.md"]
        return [f"registry-source-observation-{row['occurrence_id']}.md" for row in payload.get("occurrences", [])]
    return ["registry-meetings.md"]


def _pdf_identities(target):
    articles, calendar = set(), set()
    for entity in target["entities"]:
        facts = entity["facts"] or {}
        identities = calendar if entity["entity_kind"] in {"meeting", "pdf_meeting"} else articles
        for row in [facts, *[row for name in RELATION_FIELDS for row in facts.get(name, [])]]:
            if row.get("source_document_sha256") and row.get("occurrence_id"):
                identities.add(row["occurrence_id"])
    return articles, calendar


def _known_selection(connection, key, states, permitted, aliases):
    if key in states or key in permitted or aliases.get(key) in permitted:
        return True
    table, column = next(iter(ENTITY_TABLES[key[0]].items()))
    if connection.execute(f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (key[1],)).fetchone():
        return True
    for row in connection.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates WHERE entity_kind=? AND entity_id=?", key):
        snapshot = json.loads(row["snapshot_json"])
        if digest(snapshot) == row["candidate_sha256"] and (snapshot.get("entity_kind"), snapshot.get("entity_id")) == key:
            return True
    return False


def freeze_target(database, selection=None):
    """Freeze publications, receipts, public facts and page content in ONE read transaction.

    Selection entries name approved exact candidates or held entries and their previous
    approved candidate (null for a held new entity). Omitting selection inventories all.
    """
    from .enrichment_contract import review_projection
    database = resolve_database(database)
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1], public=False)
    gaps, entities = [], []
    with reader.connect() as connection:
        revision = public_revision(database, connection=connection)
        if revision is None:
            raise ValueError("Registry has no publication contract")
        states = {(row["entity_kind"], row["entity_id"]): dict(row)
            for row in connection.execute("SELECT * FROM registry_publication")}
        entries = selection if selection is not None else [
            {"entity_kind": kind, "entity_id": identity, "disposition": "inventory"}
            for kind, identity in sorted(states)]
        keys = [(entry["entity_kind"], entry["entity_id"]) for entry in entries]
        if (selection is not None and not keys) or len(set(keys)) != len(keys) or any(key[0] not in ENTITY_TABLES for key in keys):
            raise ValueError("acceptance selection must contain unique Registry identities")
        # The business read bypasses a configured Git export; subsequent reads use
        # permitted approved projections from this exact canonical connection.
        reader.public = True
        with reader.public_snapshot(direct=True, source_connection=connection):
            records = _records(reader)
            permitted = reader._public_candidates(*ENTITY_TABLES)
            from .identity import approved_aliases
            aliases = approved_aliases(permitted)
            scope_keys = {aliases.get(key, key) for key in keys}
            pages = _pages(reader, {key: value for key, value in records.items() if key in scope_keys})
            absent_pages = set()
            for entry, key in zip(entries, keys):
                original_state = states.get(key, {})
                public_key = aliases.get(key, key)
                record = records.get(public_key) or {}
                serving_key = ("article", record["core_article_id"]) if key[0] == "pdf_article" and record.get("core_article_id") else public_key
                candidate_basis = entry.get("candidate_basis", "publication")
                if candidate_basis not in {"publication", "canonical_serving"} or candidate_basis == "canonical_serving" and not (
                        key[0] == "pdf_article" and serving_key[0] == "article"):
                    raise ValueError("selection candidate basis has no canonical PDF binding")
                publication_key = serving_key if candidate_basis == "canonical_serving" else key
                state = states.get(publication_key, {})
                published = state.get("published_candidate_sha256")
                visible = bool(original_state.get("is_visible")) if original_state else bool(states.get(serving_key, {}).get("is_visible"))
                disposition = entry.get("disposition", "approved")
                if disposition not in ({"inventory"} if selection is None else {"approved", "held"}):
                    raise ValueError("invalid selection disposition")
                required = entry.get("baseline_candidate_sha256") if disposition == "held" else entry.get("candidate_sha256")
                if disposition != "inventory" and ("baseline_candidate_sha256" if disposition == "held" else "candidate_sha256") not in entry:
                    raise ValueError("selection requires an exact candidate or held baseline")
                if disposition != "inventory" and published != required:
                    gaps.append({"code": "publication_candidate_differs", "entity": list(key),
                        "expected": required, "actual": published})
                if disposition == "held" and not _known_selection(connection, key, states, permitted, aliases):
                    gaps.append({"code": "selected_identity_unresolved", "entity": list(key)})
                if disposition == "approved" and (not required or not visible or public_key not in records):
                    gaps.append({"code": "approved_entity_not_public", "entity": list(key)})
                snapshot = None
                reviews = []
                if published:
                    row = connection.execute("SELECT * FROM registry_candidates WHERE candidate_sha256=?", (published,)).fetchone()
                    if row is None:
                        raise ValueError("published candidate is missing")
                    snapshot = json.loads(row["snapshot_json"])
                    if digest(snapshot) != published or (row["entity_kind"], row["entity_id"]) != publication_key:
                        raise ValueError("published candidate hash or identity differs")
                    reviews = _approval_references(connection, published)
                    if not reviews:
                        gaps.append({"code": "approval_missing", "entity": list(key)})
                projection = review_projection(snapshot) if snapshot else {}
                facts = content_facts(records[public_key]) if public_key in records else None
                retrieval_facts = facts
                ownership = {}
                if facts and key[0] == "pdf_article" and facts.get("core_article_id"):
                    serving_key = ("article", facts["core_article_id"])
                    owner = records.get(serving_key)
                    binding = snapshot if snapshot is not None else permitted.get(serving_key)
                    expected = {(row.get("occurrence_id"), row.get("source_document_sha256"), row.get("page"))
                        for row in (binding or {}).get("tables", {}).get("pdf_intake_article_occurrences", []) if row.get("article_id") == key[1]}
                    owned = {(row.get("occurrence_id"), row.get("source_document_sha256"), row.get("page"))
                        for row in (owner or {}).get("pdf_occurrences", [])}
                    mappings = facts.get("canonical_occurrence_mappings") or {}
                    if (serving_key not in permitted or not owner or not expected or not expected <= owned
                            or any(value != serving_key[1] for value in mappings.values())):
                        gaps.append({"code": "linked_pdf_occurrence_owner_differs", "entity": list(key)})
                    else:
                        ownership = {row[0]: serving_key[1] for row in expected}
                        retrieval_facts = content_facts(owner)
                serving_sha = (retrieval_facts or {}).get("published_candidate_sha256") or published
                serving_snapshot = snapshot
                if serving_sha and serving_sha != published:
                    serving_row = connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (serving_sha,)).fetchone()
                    serving_snapshot = json.loads(serving_row[0]) if serving_row else None
                    if serving_snapshot is None or digest(serving_snapshot) != serving_sha:
                        raise ValueError("canonical serving snapshot hash differs")
                serving_projection = projection if serving_sha == published else review_projection(serving_snapshot)
                if disposition == "held" and published is None and facts is None:
                    if key[0] == "article":
                        absent_pages.add(f"article-{key[1]}.md")
                    elif key[0] == "pdf_article" and state.get("latest_candidate_sha256"):
                        latest = connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",
                            (state["latest_candidate_sha256"],)).fetchone()
                        pending = json.loads(latest[0]) if latest else {}
                        absent_pages.update(f"registry-source-observation-{row['occurrence_id']}.md"
                            for row in pending.get("tables", {}).get("pdf_intake_article_occurrences", []))
                    elif key[0] in {"meeting", "pdf_meeting"}:
                        from .wiki import render_registry_page
                        content = render_registry_page(database, "registry-meetings.md", reader=reader)
                        if content is None:
                            absent_pages.add("registry-meetings.md")
                        else:
                            pages["registry-meetings.md"] = content
                # Visibility, eligibility and canonical absorption are independent facts.
                entities.append({"entity_kind": key[0], "entity_id": key[1], "disposition": disposition,
                    "selected_for_acceptance": selection is not None,
                    "publication_state": "published" if published else "unpublished",
                    "candidate_basis": candidate_basis, "publication_entity": list(publication_key),
                    "original_publication_candidate_sha256": original_state.get("published_candidate_sha256"),
                    "pending_candidate": original_state.get("latest_candidate_sha256") != original_state.get("published_candidate_sha256"),
                    "required_candidate_sha256": required, "published_candidate_sha256": published,
                    "latest_candidate_sha256": state.get("latest_candidate_sha256"),
                    "original_latest_candidate_sha256": original_state.get("latest_candidate_sha256"),
                    "is_visible": visible, "publicly_permitted": public_key in permitted or bool(ownership),
                    "public_entity": list(public_key),
                    "serving_entity": list(serving_key), "occurrence_ownership": ownership,
                    "serving_candidate_sha256": serving_sha,
                    "serving_evidence_references": _references(serving_snapshot),
                    "serving_comparison_baseline": serving_projection.get("content_comparison_baseline"),
                    "serving_approval_baseline": serving_projection.get("approval_inheritance_baseline"),
                    "retrieval_facts": retrieval_facts,
                    "query_requirements": _query_requirements(retrieval_facts) if retrieval_facts else [],
                    "present": facts is not None, "facts": facts,
                    "pages": _entity_pages(*public_key, records[public_key]) if public_key in records else [],
                    "scope": projection.get("scope"), "contract_sha256": digest(projection),
                    "source_paths": sorted({row.get("discovery_kind") for row in projection.get("source_entries", {}).get("acquisition", [])
                        if row.get("discovery_kind") in {"site", "search"}} | ({"pdf"} if any(
                            projection.get("source_entries", {}).get(key) for key in ("pdf_links", "pdf_occurrences"))
                            or snapshot and snapshot.get("tables", {}).get("pdf_intake_calendar_items") else set())),
                    "source_entries": projection.get("source_entries", {}),
                    "source_bindings": projection.get("source_bindings", []),
                    "comparison_baseline": projection.get("content_comparison_baseline"),
                    "approval_baseline": projection.get("approval_inheritance_baseline"),
                    "date_authority": projection.get("authority", {}),
                    "evidence_references": _references({"snapshot": snapshot, "review_projection": projection}),
                    "published_changes": _published_changes(connection, snapshot, projection) if snapshot else {"status": "not_published"},
                    "source_fact_acceptance": "not_published" if snapshot is None else "inherited_not_repeated" if projection.get("scope") in {
                        "human_article_dates", "article_publication_date_only", "article_date_fields_only"} else "approved_scope_only",
                    "receipt_references": reviews,
                    "original_receipt_references": _approval_references(connection, original_state.get("published_candidate_sha256")),
                    "serving_receipt_references": _approval_references(connection, (retrieval_facts or {}).get("published_candidate_sha256")),
                    "snapshot_sha256": digest(snapshot) if snapshot else None})
            display = observe_local_display(connection)
    from .read_api import sort_articles_by_publication_date
    selected = {tuple(entity["serving_entity"]) if tuple(entity["serving_entity"]) in records else tuple(entity["public_entity"]) for entity in entities
        if entity["present"] and entity["serving_entity"][0] in {"article", "pdf_article"}}
    public_articles = {key: value for key, value in records.items() if key[0] in {"article", "pdf_article"}
        and not (key[0] == "pdf_article" and ("article", value.get("core_article_id") or value.get("canonical_display_article_id")) in records)}
    ordered = sort_articles_by_publication_date([{**public_articles[key], "_acceptance_key": list(key)} for key in sorted(public_articles)])
    public_order = [row["_acceptance_key"] for row in ordered]
    unselected = [{"entity_kind": kind, "entity_id": identity,
        "publication_state": "published" if state.get("published_candidate_sha256") else "unpublished",
        "published_candidate_sha256": state.get("published_candidate_sha256"),
        "latest_candidate_sha256": state.get("latest_candidate_sha256")}
        for (kind, identity), state in sorted(states.items()) if selection is None or (kind, identity) not in set(keys)]
    publication = {"status": "fail" if gaps else "unverified" if selection is None else "pass", "gaps": gaps,
        "mode": "inventory" if selection is None else "exact_selection",
        "coverage": {"selected_count": len(keys) if selection is not None else 0,
            "published_count": sum(bool(state.get("published_candidate_sha256")) for state in states.values()),
            "unpublished_count": sum(not state.get("published_candidate_sha256") for state in states.values()),
            "pending_candidate_count": sum(state.get("latest_candidate_sha256") != state.get("published_candidate_sha256") for state in states.values()),
            "unselected": unselected}}
    if selection is None:
        publication["reason"] = "exact_approved_or_held_selection_not_supplied"
    return {"schema_version": "registry-acceptance-target.v1", "registry_revision": revision, "_database": str(database),
        "frozen_at": _stamp(), "entities": entities, "pages": pages,
        "absent_pages": sorted(absent_pages - pages.keys()),
        "article_order": [key for key in public_order if tuple(key) in selected],
        "public_article_order": public_order,
        "publication": publication, "local_display": display}


def observe_local_display(connection):
    """Read stored DTOs directly; display_snapshot opens rw and is unsuitable here."""
    from .display import DisplayReader
    try:
        display = DisplayReader(connection)
        records = []
        for row in connection.execute("SELECT kind,identity,dto FROM display_details ORDER BY kind,identity"):
            records.append({"entity_kind": row[0], "entity_id": row[1], "payload": json.loads(row[2])})
        # These tables retain separate Dates identities and already published ordering.
        pdf_calendar = []
        for table, kind in (("display_pdf_calendar", "pdf_meeting"), ("display_meetings", "meeting")):
            where = " WHERE start_date=(SELECT min(start_date) FROM display_meetings) AND include_unknown=1" if kind == "meeting" else ""
            for row in connection.execute(f"SELECT dto FROM {table}" + where):
                payload = json.loads(row[0])
                if kind == "pdf_meeting":
                    pdf_calendar.append(payload)
                    continue
                identity = payload.get("event_id") or payload.get("occurrence_id")
                if identity:
                    records.append({"entity_kind": kind, "entity_id": identity, "payload": payload})
        records.extend({"entity_kind": key[0], "entity_id": key[1], "payload": payload}
            for key, payload in _calendar_records(pdf_calendar).items())
        return {"status": "observed", "generation_id": display.version, "records": records}
    except (sqlite3.Error, ValueError, RuntimeError):
        return {"status": "unverified", "reason": "stored_display_unavailable"}


def _compare_records(target, records):
    gaps, actual, bodies = [], {}, {}
    for item in records:
        if item.get("role", "current") != "current":
            continue
        key = (item["entity_kind"], item["entity_id"])
        if key in actual:
            gaps.append({"code": "duplicate_current_identity", "entity": list(key)})
        actual[key] = content_facts(item["payload"])
        payload = item["payload"]
        content = payload.get("available_content") or payload.get("content") or {}
        identity = (payload.get("identity_mapping") or {}).get("canonical") or {}
        body_key = (payload.get("canonical_url"), content.get("content_sha256") or _sha(content.get("markdown") or ""))
        if content.get("markdown") and body_key[0]:
            owner = (identity.get("entity_kind", key[0]), identity.get("entity_id", key[1]))
            if body_key in bodies and bodies[body_key] != owner:
                gaps.append({"code": "duplicate_current_body", "entity": list(key)})
            bodies[body_key] = owner
    comparisons = []
    for entity in target["entities"]:
        key = (entity["entity_kind"], entity["entity_id"])
        expected, observed = entity["facts"], actual.get(tuple(entity["public_entity"]))
        fields = sorted(field for field in set(expected or {}) | set(observed or {})
            if (expected or {}).get(field) != (observed or {}).get(field))
        if (expected is None) != (observed is None):
            gaps.append({"code": "presence_differs", "entity": list(key)})
        elif fields:
            gaps.append({"code": "current_content_differs", "entity": list(key), "fields": fields})
        comparisons.append({"entity": list(key), "actual": _summary_facts(observed) if observed else None})
    return gaps, comparisons


def _validate_capture(capture):
    """Read the captured bytes, never a status or a caller's comparison conclusion."""
    path = Path(capture["path"]).resolve(strict=True)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != capture.get("sha256"):
        raise ValueError("capture_body_hash_differs")
    stamp = datetime.fromisoformat(capture["observed_at"].replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
        raise ValueError("capture_time_invalid")
    if not capture.get("endpoint") or not capture.get("command"):
        raise ValueError("capture_location_or_command_missing")
    location = urlsplit(capture["endpoint"])
    hostname = location.hostname or ""
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if location.port is not None:
        netloc += f":{location.port}"
    endpoint = (urlunsplit((location.scheme, netloc, location.path, "", ""))
        if location.scheme in {"https", "http"} else "local-surface-sha256:" + _sha(capture["endpoint"]))
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("capture must be a surface object")
    return value, {"body_sha256": capture["sha256"], "observed_at": capture["observed_at"],
        "endpoint": endpoint, "command_sha256": _sha(capture["command"]),
        "artifact_name": path.name}


def _article_sort_gaps(target, observed):
    if not target["article_order"]:
        return []
    order = observed.get("article_order")
    if not isinstance(order, list) or not all(isinstance(row, list) and len(row) == 2 for row in order):
        return [{"code": "article_sort_order_not_observed"}]
    expected = [tuple(row) for row in target["public_article_order"]]
    actual = [tuple(row) for row in order]
    records = [row for row in observed.get("records", []) if row.get("role", "current") == "current"]
    totals = observed.get("record_totals") or {}
    keys = set()
    for row in records:
        if row["entity_kind"] not in {"article", "pdf_article"}:
            continue
        key = (row["entity_kind"], row["entity_id"])
        owner = row["payload"].get("core_article_id") or row["payload"].get("canonical_display_article_id")
        if key[0] == "pdf_article" and ("article", owner) in expected:
            key = ("article", owner)
        keys.add(key)
    if (len(actual) != len(expected) or len(set(actual)) != len(actual) or set(actual) != set(expected)
            or keys != set(expected) or any(type(totals.get(kind)) is not int
                or totals[kind] != sum(row["entity_kind"] == kind for row in records) for kind in ("article", "pdf_article"))):
        return [{"code": "article_sort_inventory_incomplete"}]
    # Check each selected position, without accepting/rejecting unrelated rows' mutual order.
    if any(set(expected[:expected.index(tuple(key))]) != set(actual[:actual.index(tuple(key))]) for key in target["article_order"]):
        return [{"code": "article_sort_order_differs"}]
    return []


def check_surface(target, surface, capture=None):
    """Check an externally captured surface, including actual query/citation content.

    Capture format is documented in docs/publication-acceptance.md. A fixture run
    can demonstrate the contract but can never receive a production surface PASS.
    """
    if capture is None:
        return {"status": "unverified", "gaps": [{"code": "surface_not_observed"}], "next_owner": "surface_operator"}
    try:
        observed, reference = _validate_capture(capture)
        retrieval = surface in {"rag", "chat"}
        gaps, comparisons = ([], []) if retrieval else _compare_records(target, observed.get("records", []))
        activation = observed.get("activation") or {}
        missing = []
        if not any(entity["present"] for entity in target["entities"]):
            missing.append("no_current_public_entities_in_scope")
        # Missing rows are not negative evidence from a partial list or a few hits.
        if not retrieval:
            records = [row for row in observed.get("records", []) if row.get("role", "current") == "current"]
            for entity in target["entities"]:
                if entity["present"]:
                    continue
                kind = entity["public_entity"][0]
                count = (observed.get("record_totals") or {}).get(kind)
                if type(count) is not int or count != sum(row["entity_kind"] == kind for row in records):
                    missing.append("absence_inventory_incomplete:" + kind)
        state = activation.get("state")
        if state not in {None, "unknown", "loaded_activation_unverified", "active"}:
            gaps.append({"code": "activation_state_differs"})
        elif state != "active":
            missing.append("active_generation_not_observed")
        if not activation.get("generation_id") or not activation.get("registry_revision"):
            missing.append("generation_mapping_missing")
        elif activation["registry_revision"] != target["registry_revision"]:
            gaps.append({"code": "generation_registry_revision_differs"})
        # Per-entity mappings bridge IDs across layers; identical generation names are not required.
        mappings = {(item["entity_kind"], item["entity_id"]): item for item in activation.get("mappings", [])}
        for entity in target["entities"]:
            if not entity["present"]:
                continue
            key = (entity["entity_kind"], entity["entity_id"])
            mapping = mappings.get(key, {})
            if mapping.get("candidate_sha256") is not None and mapping["candidate_sha256"] != entity["published_candidate_sha256"]:
                gaps.append({"code": "current_entity_generation_candidate_differs", "entity": list(key)})
            elif mapping.get("candidate_sha256") is None:
                missing.append("current_entity_generation_mapping_missing:" + ":".join(key))
            if not retrieval:
                if mapping.get("facts_sha256") is not None and mapping["facts_sha256"] != digest(entity["facts"]):
                    gaps.append({"code": "current_entity_generation_content_differs", "entity": list(key)})
                elif mapping.get("facts_sha256") is None:
                    missing.append("current_entity_generation_mapping_missing:" + ":".join(key))
        if surface == "wiki_export":
            manifest = observed.get("manifest") or {}
            pages = observed.get("pages") or {}
            if not manifest or manifest.get("registry_revision") != target["registry_revision"]:
                missing.append("wiki_manifest_revision_missing")
            for page, content in target["pages"].items():
                actual = pages.get(page)
                if actual != content or (manifest.get("pages") or {}).get(page) != _sha(actual or ""):
                    gaps.append({"code": "wiki_page_content_or_manifest_differs", "page": page})
            for page in target["absent_pages"]:
                if page in pages or page in manifest.get("pages", {}):
                    gaps.append({"code": "held_current_page_present", "page": page})
            pdf_ids, calendar_ids = _pdf_identities(target)
            if not pdf_ids <= set(manifest.get("pdf_occurrence_ids", [])) or not calendar_ids <= set(manifest.get("pdf_calendar_occurrence_ids", [])):
                gaps.append({"code": "wiki_manifest_pdf_occurrences_missing"})
        if surface in {"display_api", "wiki_export"}:
            gaps.extend(_article_sort_gaps(target, observed))
        if retrieval:
            queries = observed.get("queries") or []
            documents = observed.get("documents") or {}
            if type(observed.get("corpus_document_count")) is not int or observed["corpus_document_count"] != len(documents):
                missing.append("complete_corpus_inventory_not_observed")
            for document in documents.values():
                if document.get("page") in target["absent_pages"] and document.get("role", "current") == "current":
                    gaps.append({"code": "held_current_page_present", "page": document["page"]})
            # Shared pages can expose held facts even when every query hits another page.
            for page, content in target["pages"].items():
                current = [row for row in documents.values() if row.get("page") == page and row.get("role", "current") == "current"]
                if not current or any(not isinstance(row.get("markdown"), str) or row.get("complete") is False for row in current):
                    gaps.append({"code": "corpus_current_page_not_fully_observed", "page": page})
                if any(row.get("complete") is not False and isinstance(row.get("markdown"), str) and row["markdown"] != content for row in current):
                    gaps.append({"code": "corpus_current_page_differs", "page": page})
                if sum(row.get("role", "current") == "current" and row.get("complete") is not False
                        and row.get("markdown") == content for row in documents.values()) > 1:
                    gaps.append({"code": "duplicate_current_body", "page": page})
            covered = set()
            observed_targets = set()
            unknown_targets = set()
            expected_targets = {(row["entity_kind"], row["entity_id"]) for row in target["entities"] if row["present"]}
            content_coverage = {key: {"retrieved": set(), "answered": set()} for key in expected_targets}
            for entity in target["entities"]:
                # A corpus must prove held new identities absent as well as positive hits.
                if entity["present"]:
                    continue
                key = (entity["entity_kind"], entity["entity_id"])
                if key in mappings:
                    gaps.append({"code": "presence_differs", "entity": list(key)})
                if entity["entity_kind"] == "article" and any(row.get("page") == f"article-{entity['entity_id']}.md"
                        and row.get("role", "current") == "current" for row in documents.values()):
                    gaps.append({"code": "presence_differs", "entity": list(key)})
            for query in queries:
                if (not query.get("query") or query.get("status") in {"unavailable", "unqueryable", "failed"}
                        or not (isinstance(query.get("hits"), list) or isinstance(query.get("answer"), str))):
                    continue
                requested = query.get("target") or {}
                requested_key = (requested.get("entity_kind"), requested.get("entity_id"))
                if requested_key in expected_targets:
                    observed_targets.add(requested_key)
                elif len(expected_targets) == 1:
                    observed_targets.update(expected_targets)
                else:
                    observed_targets.update((hit.get("entity_kind"), hit.get("entity_id"))
                        for hit in query.get("hits", []) if (hit.get("entity_kind"), hit.get("entity_id")) in expected_targets)
                if not query.get("hits"):
                    continue
                if surface == "chat" and (not query.get("answer") or not query.get("citations")):
                    continue
                off_target_hits = {index for index, hit in enumerate(query["hits"])
                    if (page := documents.get(hit.get("document_id"), {}).get("page")) and page not in target["pages"]
                    and _hit_target(target["entities"], (hit.get("entity_kind"), hit.get("entity_id")), requested_key) is None}
                if len(off_target_hits) == len(query["hits"]):
                    unknown_targets.update({requested_key} if requested_key in expected_targets else expected_targets)
                for index, hit in enumerate(query["hits"]):
                    if index in off_target_hits:
                        continue  # Known unrelated pages neither prove nor invalidate selected current evidence.
                    if hit.get("role", "current") != "current":
                        continue  # Retained report/history hits do not prove current retrieval.
                    key = (hit.get("entity_kind"), hit.get("entity_id"))
                    entity = _hit_target(target["entities"], key, requested_key)
                    document = documents.get(hit.get("document_id"), {})
                    page = document.get("page")
                    expected_markdown = target["pages"].get(page)
                    if document.get("role", "current") != "current" or document.get("complete") is False:
                        missing.append("retrieved_current_document_missing")
                        unknown_targets.update({requested_key} if requested_key in expected_targets else expected_targets)
                        continue
                    if not all(key):
                        page_entities = [row for row in target["entities"] if row["present"] and page in row["pages"]]
                        exact = [row for row in page_entities if hit.get("candidate_sha256") and hit["candidate_sha256"] == row["serving_candidate_sha256"]]
                        requested_exact = next((row for row in exact if (row["entity_kind"], row["entity_id"]) == requested_key), None)
                        if len(exact) == 1 or requested_exact is not None:
                            entity = requested_exact or exact[0]
                        else:
                            wrong_page_content = expected_markdown is not None and document.get("markdown") is not None and document["markdown"] != expected_markdown
                            wrong_unique_version = len(page_entities) == 1 and hit.get("candidate_sha256") and not exact
                            if wrong_page_content or wrong_unique_version:
                                gaps.append({"code": "query_current_version_differs", "page": page})
                            else:
                                missing.append("retrieved_identity_unverified")
                                unknown_targets.update({requested_key} if requested_key in expected_targets else expected_targets)
                            continue
                    if entity is None:
                        continue  # An explicitly different indexed identity cannot prove this target.
                    key = (entity["entity_kind"], entity["entity_id"])
                    if not document or not page or document.get("markdown") is None:
                        missing.append("retrieved_current_document_missing")
                        unknown_targets.add(key)
                        continue
                    current_sha = entity["serving_candidate_sha256"]
                    if (hit.get("candidate_sha256") is not None and hit["candidate_sha256"] != current_sha
                            or page not in entity["pages"] or expected_markdown is None
                            or document.get("markdown") != expected_markdown):
                        gaps.append({"code": "query_current_version_differs", "entity": list(key)})
                        continue
                    if hit.get("candidate_sha256") is None:
                        missing.append("retrieved_candidate_mapping_missing")
                        unknown_targets.add(key)
                        continue
                    event_facts = entity["retrieval_facts"].get("event_id") is not None
                    answer_date_missing, answer_date_mismatched = [], []
                    answer = query.get("answer", "")
                    native_index = None
                    if surface == "chat":
                        answer, native_index = _answer_for_hit(answer, hit, document, index + 1)
                    answer_denied = surface == "chat" and _referenced_denial(query["answer"], " ".join(row["text"] for row in entity["query_requirements"]))
                    if answer_denied:
                        gaps.append({"code": "chat_approved_claim_denied", "entity": list(key)})
                    if surface == "chat" and event_facts:
                        answer_date_missing, answer_date_mismatched = _event_date_evidence(entity["retrieval_facts"], {"excerpt": answer})
                        if answer_date_mismatched:
                            gaps.append({"code": "chat_event_date_facts_differs", "entity": list(key), "fields": answer_date_mismatched})
                    date_missing, date_mismatched = _retrieval_dates(entity["retrieval_facts"], hit)
                    if date_mismatched:
                        gaps.append({"code": "retrieval_date_metadata_differs", "entity": list(key), "fields": date_mismatched})
                        continue
                    if date_missing:
                        gaps.append({"code": "retrieval_date_metadata_missing", "entity": list(key), "fields": date_missing})
                        unknown_targets.add(key)
                        continue
                    excerpt = hit.get("excerpt")
                    if not excerpt or excerpt not in expected_markdown or not hit.get("document_id"):
                        missing.append("retrieved_current_text_missing")
                        continue
                    if surface == "chat":
                        cited = [citation for citation in query["citations"] if citation.get("document_id") == hit["document_id"]
                            and citation.get("candidate_sha256") == hit["candidate_sha256"]
                            and citation.get("excerpt") == excerpt
                            and (native_index is None or citation.get("index") in {None, native_index})]
                        if not cited:
                            missing.append("chat_current_content_citation_missing")
                            continue
                        citation_dates = [_retrieval_dates(entity["retrieval_facts"], citation) for citation in cited]
                        mismatched = sorted({field for _, fields in citation_dates for field in fields})
                        if mismatched:
                            gaps.append({"code": "citation_date_metadata_differs", "entity": list(key), "fields": mismatched})
                            continue
                        if not any(not fields for fields, _ in citation_dates):
                            gaps.append({"code": "citation_date_metadata_missing", "entity": list(key)})
                            unknown_targets.add(key)
                            continue
                    for requirement in entity["query_requirements"]:
                        if requirement.get("field"):
                            content_coverage[key]["retrieved"].add(requirement["kind"])
                            if surface == "chat" and not answer_denied and requirement["field"] not in answer_date_missing + answer_date_mismatched:
                                content_coverage[key]["answered"].add(requirement["kind"])
                            continue
                        if _source_covers(excerpt, requirement["text"]):
                            content_coverage[key]["retrieved"].add(requirement["kind"])
                            if surface == "chat" and not answer_denied and (_answer_covers(answer, requirement["text"])
                                    or event_facts and requirement["text"] == entity["retrieval_facts"].get("name") and _source_covers(answer, requirement["text"])
                                    or _quantitative_paraphrase(answer, entity["retrieval_facts"], requirement)):
                                content_coverage[key]["answered"].add(requirement["kind"])
                    covered.add(key)
            for entity in target["entities"]:
                key = (entity["entity_kind"], entity["entity_id"])
                if surface == "chat" and entity["present"] and entity["retrieval_facts"].get("event_id"):
                    needed_dates, _ = _event_date_evidence(entity["retrieval_facts"], {})
                    absent_dates = [field for field in needed_dates if "event_" + field not in content_coverage[key]["answered"]]
                    if absent_dates:
                        gaps.append({"code": "chat_event_date_facts_missing", "entity": list(key), "fields": absent_dates})
                        unknown_targets.add(key)
                if entity["present"] and key not in covered:
                    code = "targeted_current_query_missing:" if key in observed_targets and key not in unknown_targets else "targeted_current_query_not_observed:"
                    missing.append(code + ":".join(key))
                if entity["present"]:
                    required = {row["kind"] for row in entity["query_requirements"]}
                    for kind in ("retrieved", "answered") if surface == "chat" else ("retrieved",):
                        absent = sorted(required - content_coverage[key][kind])
                        if absent:
                            code = "approved_content_coverage_unverified" if key in unknown_targets or key not in observed_targets else "approved_content_coverage_missing"
                            gaps.append({"code": code, "entity": list(key), "coverage": kind, "requirements": absent})
        gaps.extend({"code": code} for code in sorted(set(missing)))
        fixture = capture.get("evidence_kind") != "captured"
        status = "fail" if any(gap["code"] in {"presence_differs", "duplicate_current_identity", "duplicate_current_body", "current_content_differs",
            "generation_registry_revision_differs", "wiki_page_content_or_manifest_differs",
            "wiki_manifest_pdf_occurrences_missing", "query_current_version_differs", "article_sort_order_differs",
            "held_current_page_present", "chat_current_content_citation_missing", "retrieved_current_text_missing",
            "activation_state_differs", "current_entity_generation_candidate_differs", "current_entity_generation_content_differs",
            "retrieval_date_metadata_differs", "citation_date_metadata_differs", "approved_content_coverage_missing", "corpus_current_page_differs",
            "chat_event_date_facts_differs", "chat_approved_claim_denied"}
            or gap["code"].startswith("targeted_current_query_missing:") for gap in gaps) else "unverified" if gaps else "fixture_match" if fixture else "pass"
        return {"status": status, "evidence_scope": "fixture_contract_only" if fixture else "captured_observation",
            "observed": reference, "actual_registry_revision": activation.get("registry_revision"),
            "generation_id": activation.get("generation_id"), "gaps": gaps, "entities": comparisons,
            "content_coverage": [{"entity": list(key), **{kind: sorted(values) for kind, values in coverage.items()}}
                for key, coverage in sorted(content_coverage.items())] if retrieval else [],
            "query_evidence": [{"query_sha256": _sha(row.get("query", "")), "answer_sha256": _sha(row.get("answer", "")),
                "hits_sha256": digest(row.get("hits", [])), "citations_sha256": digest(row.get("citations", []))}
                for row in observed.get("queries", [])],
            "documents": {identity: {"page": row.get("page"), "markdown_sha256": _sha(row.get("markdown", ""))}
                for identity, row in observed.get("documents", {}).items()},
            "next_owner": "surface_operator" if status != "pass" else None}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Never echo transport errors, request URLs, authorization or arbitrary response text.
        reason = str(exc) if str(exc) in {"capture_body_hash_differs", "capture_time_invalid",
            "capture_location_or_command_missing"} else type(exc).__name__
        return {"status": "unverified", "gaps": [{"code": "capture_unqueryable_or_invalid", "reason": reason}],
            "last_observed_generation_id": capture.get("last_observed_generation_id"), "next_owner": "surface_operator"}


def observe_rag(knowledge_base, requests):
    """Query an ALREADY loaded corpus; never construct/reload it or borrow target text.

    Requests carry entity kind/id and query text. An operator must attach independent
    active generation evidence; loaded corpus content alone proves no activation.
    """
    documents = {doc.path: {"page": doc.file, "markdown": doc.markdown} for doc in knowledge_base.documents}
    queries = []
    for request in requests:
        hits = knowledge_base.search(request["query"])
        queries.append({"query": request["query"],
            "target": {key: request[key] for key in ("entity_kind", "entity_id")}, "hits": [{
            **_indexed_hit_identity(hit.chunk),
            "index": hit_index, "title": getattr(hit.chunk, "title", None), "corpus": getattr(hit.chunk, "corpus", None),
            "document_id": hit.chunk.path, "excerpt": hit.chunk.markdown,
            "candidate_sha256": hit.chunk.date_metadata.get("published_candidate_sha256"),
            "date": getattr(hit.chunk, "date", None), "date_metadata": dict(hit.chunk.date_metadata),
            "date_kind": hit.chunk.date_metadata.get("content_date_basis"),
            "role": "current" if hit.chunk.path in documents else "history"} for hit_index, hit in enumerate(hits, start=1)]})
    return {"documents": documents, "queries": queries,
        "corpus_document_count": len(knowledge_base.documents),
        "activation": {"state": "loaded_activation_unverified", "registry_revision": knowledge_base.public_registry_revision}}


def _indexed_hit_identity(chunk):
    import re
    metadata = chunk.date_metadata
    if metadata.get("entity_kind") in ENTITY_TABLES and metadata.get("entity_id"):
        return {key: metadata[key] for key in ("entity_kind", "entity_id")}
    article = re.fullmatch(r"(?:wiki/)?article-([A-Za-z0-9_-]+)\.md", chunk.path)
    if article:
        return {"entity_kind": "article", "entity_id": article[1]}
    occurrence = re.fullmatch(r"(?:wiki/)?registry-source-observation-([A-Za-z0-9_-]+)\.md", chunk.path)
    # A PDF occurrence or shared Dates page does not identify its Registry parent.
    return {"entity_kind": None, "entity_id": None,
        **({"occurrence_id": occurrence[1]} if occurrence else {})}


def check_local_runtime(target, runtime_dir):
    """Inspect the existing active pointer protocol, not a prepared candidate label."""
    from .pdf_pipeline import load_active_projection, load_projection_manifest, approved_projection_snapshot
    try:
        directory, metadata = load_active_projection(Path(runtime_dir))
        if directory is None:
            return {"status": "unverified", "reason": "active_pointer_missing"}
        manifest = load_projection_manifest(directory, metadata)
        snapshot = approved_projection_snapshot(Path(runtime_dir), metadata, database=target["_database"])
        if snapshot is None or manifest is None:
            return {"status": "unverified", "reason": "active_snapshot_or_manifest_missing"}
        gaps = []
        actual_revision = public_revision(snapshot)
        if actual_revision != target["registry_revision"]:
            gaps.append({"code": "generation_registry_revision_differs"})
        for page, expected in target["pages"].items():
            path = directory / page
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                gaps.append({"code": "wiki_page_content_differs", "page": page})
        gaps.extend({"code": "held_current_page_present", "page": page} for page in target["absent_pages"] if (directory/page).exists())
        expected_pdf, calendar_ids = _pdf_identities(target)
        if not expected_pdf <= set(manifest.get("pdf_occurrence_ids", [])) or not calendar_ids <= set(manifest.get("pdf_calendar_occurrence_ids", [])):
            gaps.append({"code": "wiki_manifest_pdf_occurrences_missing"})
        return {"status": "fail" if gaps else "pass", "evidence_scope": "local_runtime_wiki_only_does_not_prove_serving_or_chat",
            "generation_id": metadata["generation_id"], "actual_registry_revision": actual_revision,
            "manifest_sha256": metadata["manifest_sha256"], "snapshot_sha256": metadata.get("public_registry_sha256"), "gaps": gaps}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return {"status": "unverified", "reason": "active_runtime_unqueryable_or_invalid", "next_owner": "projection_operator"}


def check_local_export(target, path):
    from .verification import PUBLICATION_POLICY
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        sha = value.pop("artifact_sha256")
        if digest(value) != sha or value.get("publication_policy") != PUBLICATION_POLICY:
            raise ValueError("export hash or policy differs")
        records = [{"entity_kind": kind, "entity_id": item[identity], "payload": item}
            for collection, kind, identity in (("articles", "article", "article_id"), ("pdf_articles", "pdf_article", "article_id"),
                ("meeting_records", "meeting", "event_id"))
            for item in value.get(collection, []) if item.get(identity)]
        records.extend({"entity_kind": key[0], "entity_id": key[1], "payload": payload}
            for key, payload in _calendar_records(value.get("meetings", [])).items())
        gaps, comparisons = _compare_records(target, records)
        for page, expected in target["pages"].items():
            if value.get("wiki_pages", {}).get(page) != expected:
                gaps.append({"code": "wiki_export_page_differs", "page": page})
        gaps.extend({"code": "held_current_page_present", "page": page} for page in target["absent_pages"] if page in value.get("wiki_pages", {}))
        # v1 exports have an artifact hash, but no complete canonical revision/visibility binding.
        return {"status": "fail" if gaps else "unverified", "content_status": "fail" if gaps else "match",
            "artifact_sha256": sha, "entities": comparisons, "gaps": gaps,
            "reason": "export_v1_has_no_activation_or_complete_registry_revision_binding", "next_owner": "projection_operator"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "unverified", "reason": "public_export_unqueryable_or_invalid", "next_owner": "projection_operator"}


def accept_publication(database, *, selection=None, captures=None, command=None, runtime_dir=None, public_export=None):
    target = freeze_target(database, selection)
    surfaces = {name: check_surface(target, name, (captures or {}).get(name)) for name in SURFACES}
    local = target.pop("local_display")
    if local["status"] == "observed":
        gaps, comparisons = _compare_records(target, local.pop("records"))
        local.update(status="fail" if gaps else "pass", gaps=gaps, entities=comparisons,
            evidence_scope="local_stored_display_only_does_not_prove_api")
    artifacts = {}
    if runtime_dir is not None:
        artifacts["runtime_wiki"] = check_local_runtime(target, runtime_dir)
    if public_export is not None:
        artifacts["public_export"] = check_local_export(target, public_export)
    try:
        latest_revision = public_revision(database)
        latest_gap = "registry_revision_changed_after_freeze"
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        latest_revision = None
        latest_gap = "latest_registry_revision_unqueryable"
    current = latest_revision == target["registry_revision"]
    target_report = {**{key: value for key, value in target.items() if key != "_database"},
        "database_reference_sha256": _sha(target["_database"]),
        "pages": {name: _sha(content) for name, content in target["pages"].items()},
        "entities": [{**{key: value for key, value in row.items() if key not in {
            "facts", "retrieval_facts", "query_requirements", "source_entries", "source_bindings", "date_authority", "comparison_baseline", "approval_baseline",
            "serving_comparison_baseline", "serving_approval_baseline"}},
            "facts": _summary_facts(row["facts"]) if row["facts"] else None,
            "retrieval_facts": _summary_facts(row["retrieval_facts"]) if row["retrieval_facts"] else None,
            "query_requirements": [{"kind": value["kind"], "sha256": value["sha256"]} for value in row["query_requirements"]],
            "source_entries_sha256": digest(row["source_entries"]), "source_bindings_sha256": digest(row["source_bindings"]),
            "date_authority_sha256": digest(row["date_authority"]),
            "comparison_baseline": _baseline_reference(row["comparison_baseline"]),
            "approval_baseline": _baseline_reference(row["approval_baseline"]),
            "serving_comparison_baseline": _baseline_reference(row["serving_comparison_baseline"]),
            "serving_approval_baseline": _baseline_reference(row["serving_approval_baseline"])} for row in target["entities"]]}
    return {"schema_version": "registry-publication-acceptance.v1", "checked_at": _stamp(),
        "verification_scope": "frozen_target_and_recorded_observation_times", "online_completion": "not_asserted",
        "command": command or "accept_publication", "frozen_target_sha256": digest(target_report), "frozen_target": target_report,
        "registry_publication": target["publication"], "local_display": local, "local_artifacts": artifacts, "surfaces": surfaces,
        "observed_latest_registry_revision": latest_revision, "frozen_target_is_latest": current,
        "status": "pass" if current and target["publication"]["status"] == "pass" and all(
            row["status"] == "pass" for row in surfaces.values()) and not any(
                row["status"] == "fail" for row in [local, *artifacts.values()]) else "blocking",
        "business_writes": False,
        "gaps": [] if current else [{"code": latest_gap, "next_owner": "acceptance_operator"}]}


def _baseline_reference(value):
    if not isinstance(value, dict):
        return {"status": "unknown", "sha256": digest(value)}
    return {key: value[key] for key in ("candidate_sha256", "content_version_id", "body_sha256", "status", "reason") if key in value} | {
        "sha256": digest(value)}


def write_acceptance_report(database, report_path, *, selection_path=None, captures_path=None, runtime_dir=None, public_export=None):
    """Existing Registry CLI reporting operation; only its designated report is written."""
    from .errors import RegistryInputError
    if not Path(database).is_absolute():
        raise RegistryInputError("database must be an absolute external path")
    database = Path(database).expanduser().resolve()
    destination = Path(report_path).resolve()
    inputs = [database, *[Path(path).resolve() for path in (selection_path, captures_path, public_export) if path]]
    if destination in inputs or destination.suffix != ".json" or destination.exists():
        raise RegistryInputError("report must be a new designated JSON file, separate from inputs")
    if runtime_dir is not None and Path(runtime_dir).resolve() in destination.parents:
        raise RegistryInputError("report must be outside runtime projection storage")
    try:
        captures = json.loads(Path(captures_path).read_text(encoding="utf-8")) if captures_path else {}
        if not isinstance(captures, dict) or set(captures) - set(SURFACES):
            raise ValueError("invalid surface capture index")
        if destination in [Path(capture["path"]).resolve() for capture in captures.values()]:
            raise RegistryInputError("report must not overwrite capture evidence")
        selection = json.loads(Path(selection_path).read_text(encoding="utf-8")) if selection_path else None
        report = accept_publication(database, selection=selection, captures=captures,
            command="python -m climate_registry accept-publication", runtime_dir=runtime_dir, public_export=public_export)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
        report = {"schema_version": "registry-publication-acceptance.v1", "status": "blocking", "business_writes": False,
            "registry_publication": {"status": "unverified", "reason": type(exc).__name__},
            "surfaces": {name: {"status": "unverified", "reason": "target_unavailable"} for name in SURFACES}}
    try:
        with destination.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
    except OSError as exc:
        raise RegistryInputError("acceptance evidence report could not be written") from exc
    return {"status": report["status"], "business_writes": False,
        "registry_publication_status": report["registry_publication"]["status"],
        "report_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
