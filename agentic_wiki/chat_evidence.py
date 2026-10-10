"""Read-only Chat evidence, bounded tools and anonymous response frames."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ZONE = ZoneInfo("America/New_York")
URLS = re.compile(r"https?://[^\s<>\]\)]+")
ISO_DATES = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
MEETINGS = re.compile(r"\b(meetings?|conferences?|deadlines?|consultations?|expert.review|opportunit\w*|upcoming|agenda)\b", re.I)
RECENT = re.compile(r"\b(new|added|materially updated|newly|recent(?:ly)?(?:\s+updated)?)\b.*\b(reports?|articles?|publications?|content)\b|\b(reports?|articles?|content)\b.*\b(added|materially updated|recently updated)\b|最近.{0,12}(?:新增|更新).{0,12}(?:内容|文章|报告)|(?:内容|文章|报告).{0,12}(?:新增|更新)", re.I)
KNOWLEDGE_CHANGE = re.compile(r"\b(added|ingested|updated)\b|\bmaterial(?:ly)?[-\s]+updates?\b", re.I)
REFERENCES = re.compile(r"\b(its?|that|this|second|first|third|above|previous|former|latter)\b", re.I)
REFRESH = re.compile(r"\b(verify|reverify|recheck|current status|latest agenda|check again|up.to.date|still|currently|now|today)\b|\b(is|are)\b.{0,30}\b(open|closed|available)\b", re.I)


def refresh_requested(question):
    # Verifying a retained source's identity is not a request for newer page facts.
    facts_question = re.sub(r"\b(?:re)?verify\s+(?:(?:the|its|their|this|that)\s+)?(?:retained\s+)?(?:source\s+)?identity\b",
        "", question, flags=re.I)
    return bool(REFRESH.search(facts_question))
WEB_SEARCH = re.compile(r"\b(search|find|look up)\b.{0,60}\b(web|online|internet)\b", re.I)
DEADLINES = re.compile(r"\b(deadlines?|registration|cfp|early[- ]bird|call for (papers|proposals))\b", re.I)
MAX_SECONDS, MAX_TOOLS, MAX_READS, MAX_TEXT = 120, 16, 4, 32000
FINAL_RESERVE_SECONDS = 20
MAX_MODELS = 14


def _load_url_policy():
    from web_listening.request.model import RequestValidationError
    from web_listening.request.scope import canonicalize_url
    return canonicalize_url, RequestValidationError


class ResponseFrames:
    # shortcut: single-process TTL cache; use shared runtime storage for multiple replicas.
    def __init__(self):
        self.frames = OrderedDict()
        self.lock = threading.Lock()

    def get(self, token):
        with self.lock:
            item = self.frames.get(token or "")
            if item and time.monotonic() - item[0] <= 3600:
                return copy.deepcopy(item[1])
        return {}

    def save(self, frame):
        token = secrets.token_urlsafe(24)
        with self.lock:
            now = time.monotonic()
            for key in list(self.frames):
                if now - self.frames[key][0] > 3600:
                    del self.frames[key]
            self.frames[token] = (now, copy.deepcopy(frame))
            while len(self.frames) > 128:
                self.frames.popitem(last=False)
        return token


def window(question, now, *, knowledge_time=True):
    """Knowledge observations end at as-of; explicit event dates can be future."""
    local = now.astimezone(ZONE)
    dates = ISO_DATES.findall(question)
    period = re.search(r"\b(last|past)\s+\d+\s+(months?|quarters?|years?)\b", question, re.I)
    if period and len(dates) != 2:
        raise ValueError(f"Please provide explicit start and end dates for '{period[0]}'; calendar-month periods need date endpoints.")
    days = re.search(r"(?:last|past)\s+(\d+)\s+(days?|weeks?)", question, re.I)
    count = min(366, max(1, int(days[1]) * (7 if days[2].lower().startswith("week") else 1))) if days else 14
    start = datetime.combine(local.date() - timedelta(days=count - 1), day_time.min, ZONE)
    end = local
    if len(dates) == 2:
        start = datetime.combine(datetime.fromisoformat(dates[0]).date(), day_time.min, ZONE)
        end = datetime.combine(datetime.fromisoformat(dates[1]).date() + timedelta(days=1), day_time.min, ZONE) - timedelta(microseconds=1)
        if knowledge_time:
            end = min(local, end)
        if start > end:
            raise ValueError("Please provide a valid date range ending no later than the as-of date.")
    return start, end


def timestamp(value):
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return at if at.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def identity(item):
    return item.get("canonical_event_id") or item.get("event_id") or item.get("occurrence_id")


def meeting_end(item):
    """Return a known event end instant from approved fields."""
    from datetime import date

    if not item.get("end_date") or item.get("date_precision", "day") != "day":
        return None
    zone_name = item.get("event_timezone")
    time_range = re.search(r"(?<!\d)(\d{1,2}):([0-5]\d)\s*(am|pm)?\s*(?:-|–|—|to)\s*(\d{1,2}):([0-5]\d)\s*(am|pm)?(?!\d)",
        str(item.get("raw_time_text") or ""), re.I)
    if not zone_name or not time_range:
        return None
    try:
        zone = ZoneInfo(zone_name)
        event_date = date.fromisoformat(item["end_date"])
        hour, minute = map(int, time_range.groups()[3:5])
        meridiem = (time_range[6] or time_range[3] or "").lower()
        if meridiem:
            if hour > 12:
                return None
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
        local = datetime.combine(event_date, day_time(hour, minute), zone)
        utc = local.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            return None
        if local.replace(tzinfo=None).replace(tzinfo=zone, fold=0).utcoffset() != local.replace(tzinfo=None).replace(tzinfo=zone, fold=1).utcoffset():
            return None
        return local
    except (KeyError, TypeError, ValueError):
        return None


def online_format(item):
    """Use approved format/location fields; a registration URL is not a format."""
    for observation in [item, *(item.get("pdf_observations") or [])]:
        if re.search(r"\b(?:online|virtual)\b", str(observation.get("location") or ""), re.I):
            return True
        if str(observation.get("event_type") or "").casefold() == "webinar":
            return True
    return False


def source_urls(item):
    urls = list(item.get("source_urls") or [])
    for row in item.get("sources") or []:
        if row.get("source_url"):
            urls.append(row["source_url"])
    for key in ("canonical_url", "source_url", "online_url"):
        if item.get(key):
            urls.append(item[key])
    return list(dict.fromkeys(url for url in urls if isinstance(url, str) and url.startswith(("https://", "http://"))))


def web_excerpt(body, question, limit=MAX_TEXT):
    if len(body) <= limit:
        return body
    words = {word.casefold() for word in re.findall(r"[a-z]{4,}", URLS.sub("", question), re.I)
        if word.casefold() not in {"this", "that", "read", "explain", "which", "about", "what", "https"}}
    if not words:
        return body[:limit]
    passages = re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", body)
    ranked = sorted(enumerate(passages), key=lambda pair: (-sum(word in pair[1].casefold() for word in words), pair[0]))
    selected, remaining = [], limit
    for index, passage in ranked:
        if remaining <= 0:
            break
        excerpt = passage[:remaining]
        selected.append((index, excerpt))
        remaining -= len(excerpt) + 2
    return "\n\n".join(passage for _, passage in sorted(selected))[:limit]


def visible_source_order(text, entries):
    by_index = {entry["source"]["index"]: entry for entry in entries}
    visible, has_rows = [], False
    for line in text.splitlines():
        if not re.match(r"^\s*(?:#{1,6}\s*)?(?:[-*+]\s+|\d+[.)]\s+|\|)", line):
            continue
        indices = list(dict.fromkeys(int(index) for group in re.findall(r"\[([\d,\s]+)\]", line)
            for index in re.findall(r"\d+", group) if int(index) in by_index))
        cited = [by_index[index] for index in indices]
        has_rows |= bool(cited)
        if len(cited) > 1:
            cited = [entry for entry in cited if entry["source"]["title"].casefold() in line.casefold()]
        if len(cited) == 1 and cited[0] not in visible:
            visible.append(cited[0])
    return visible if has_rows else None


def article_text(detail, facts):
    text = "\n".join(f"{key}: {detail.get(key) or 'unknown'}" for key in ("publisher", "author", "publication_date", "summary", "categories", "keywords"))
    text += f"\nFirst added: {facts.get('first_ingested_at') or 'unknown'}\nMaterial update: {facts.get('substantive_updated_at') or 'unknown'}"
    return text + "\nCommittee relevance: use the cited findings/categories; no committee commitment is recorded."


def reader_url(url):
    """Drop only existing known tracking keys, retaining the exact functional URL."""
    from climate_monitor.dedupe import TRACKING_KEYS, TRACKING_PREFIXES
    parts = urlsplit(url)
    query = "&".join(part for part in parts.query.split("&")
        if (key := unquote_plus(part.split("=", 1)[0]).lower()) not in TRACKING_KEYS
        and not key.startswith(TRACKING_PREFIXES))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def verification_guidance(question, sources, answer=""):
    cited = {int(index) for group in re.findall(r"\[([\d,\s]+)\]", answer) for index in re.findall(r"\d+", group)}
    relevant = [source for source in sources if not cited or source.get("index") in cited]
    linked = [source for source in relevant if str(source.get("url", "")).startswith(("https://", "http://"))]
    targets = list(dict.fromkeys(source["url"] if source.get("title") == source["url"] else f"{source['title']}: {source['url']}" for source in linked))[:3]
    if KNOWLEDGE_CHANGE.search(question):
        return "Manual verification: check the approved Registry's first-ingested/material-update history for the requested window; publisher publication/update dates cannot establish when an item entered this knowledge base."
    if sources and all(source.get("heading") in {"article", "pdf_article", "web"} for source in sources) and re.search(r"\bpublication\s+date\b|\bpublished\b", question, re.I):
        return ("Manual verification: open " + ("; ".join(targets) or "the official article page")
            + " and check its publication date. If the page does not state it, ask the publisher to confirm the publication date.")
    if relevant and not linked and all(source.get("heading") == "wiki" for source in relevant):
        passages = "; ".join(f"Wiki passage [{source['index']}]" for source in relevant[:3])
        return ("Manual verification: open " + passages + " and follow its relevant original source links to check the requested facts in the full source. "
            "If the original source does not state them, ask its publisher to confirm.")
    event = bool(relevant) and all(source.get("heading") == "meeting" for source in relevant)
    if answer and linked:
        targets = [f"[{source['index']}]" for source in linked[:3]]
    return ("Manual verification: open " + ("; ".join(targets) or "the relevant official source from a publisher or regulator")
        + " and check the requested facts in the full source. If details are absent, ask the "
        + ("event organizer to confirm participation dates and requirements." if event else "publisher to confirm the missing facts."))


class ChatEvidence:
    def __init__(self, responder, reader_factory=None, meeting_view=None):
        self.responder = responder
        self.reader_factory = reader_factory
        self.meeting_view = meeting_view
        self.frames = ResponseFrames()

    @property
    def provider(self):
        anthropic = getattr(self.responder, "anthropic_client", None)
        selected = os.getenv("CLIMATE_CHAT_PROVIDER", "").strip().lower()
        if selected:
            return selected if selected in {"openai", "anthropic"} else "unavailable"
        return "openai" if self.responder.client else "anthropic" if anthropic else "offline"

    @property
    def client(self):
        return getattr(self.responder, "anthropic_client", None) if self.provider == "anthropic" else self.responder.client if self.provider == "openai" else None

    @property
    def model(self):
        return self.responder.anthropic_model if self.provider == "anthropic" else self.responder.model

    def capabilities(self):
        from climate_monitor.article_content_adapter import check_dependencies
        return {"model": "configured" if self.client else "unavailable", "provider": self.provider,
            "url_reader": "configured" if check_dependencies() == "available" else "unavailable",
            "web_search": "not_verified" if self.client else "unavailable"}

    def answer(self, question, *, history=None, context=None, language="en", answer_mode="detailed", context_path=None):
        online = bool(self.client)
        turn = EvidenceTurn(self, question, context if online else None, (history or []) if online else [], answer_mode=answer_mode, context_path=context_path)
        with tempfile.TemporaryDirectory(prefix="climate-chat-") as directory:
            turn.runtime_dir = Path(directory)
            if not online or URLS.search(question):
                turn.initial(context_path)
            text = turn.model_answer() if online else None
            if online and not text and not any(row["tool"] != "research_state" for row in turn.trace):
                turn.source_only = True
                turn.previous, turn.web, turn.history = {}, {}, []
                turn.initial(context_path)
            text = text or turn.extractive(compact=online)
        if not turn.clarification and (any(source.get("truncated") for source in turn.sources) or re.search(
                r"\b(unknown|partial|incomplete|unavailable|missing)\b|no (?:usable|verified|relevant).*evidence|not recorded|not stated|does not (?:specify|state)|cannot infer|budget.*(reached|exhausted)", text, re.I)):
            guidance = verification_guidance(question, turn.sources, text)
            if "Manual verification:" not in text and not re.search(r"\b(?:check|open|ask|verify)\b.{0,100}\b(?:official|publisher|organizer|source URL|Registry)\b", text, re.I):
                text += "\n\n" + guidance
        if turn.model_used and not turn.clarification and sum(entry["kind"] == "meeting" for entry in turn.evidence) > 1:
            # Summary citations do not enumerate meetings. Bind the displayed list rows.
            visible = visible_source_order(text, [entry for entry in turn.evidence if entry["kind"] == "meeting"])
            turn.ordered = [entry["source"]["evidence_id"] for entry in visible or []]
        if turn.source_order is None:
            entries = [{"kind": entry["kind"], "source": entry["source"]} for entry in turn.evidence]
            visible = visible_source_order(text, entries) if turn.model_used and len(entries) > 1 else None
            turn.source_order = entries if visible is None else visible
        token = self.frames.save({"ordered": turn.ordered, "focus": turn.focus,
            "web": turn.web, "sources": turn.sources,
            "source_order": turn.source_order, "source_focus": turn.source_focus})
        return {"text": text, "sources": turn.sources, "context": token,
            **({"generation_failed": True} if turn.generation_failed else {}),
            "needs_clarification": turn.clarification, "answer_mode": answer_mode, "language": language,
            "model": self.model if turn.model_used else "offline-extractive",
            "agent_mode": self.provider if turn.model_used else "offline", "capabilities": self.capabilities(),
            "tool_execution": turn.trace, "as_of": turn.now.isoformat(),
            "plan": {"sub_queries": [question], "reflection": {}, "retrieval_log": []},
            "retrieval_summary": {"wiki_hits": sum(s["corpus"] == "wiki" for s in turn.sources),
                "source_hits": sum(s["corpus"] != "wiki" for s in turn.sources)}}


class EvidenceTurn:
    def __init__(self, owner, question, context, history, *, answer_mode="detailed", context_path=None):
        self.owner, self.question, self.history = owner, question, history
        normalized_path = (context_path or "").replace("\\", "/").lstrip("/")
        self.context_path = normalized_path if any(doc.path == normalized_path for doc in [*owner.responder.kb.documents, *owner.responder.kb.source_documents]) else None
        self.named_page = None
        self.answer_mode = answer_mode
        self.now = datetime.now(timezone.utc)
        self.deadline = time.monotonic() + MAX_SECONDS
        self.previous = owner.frames.get(context)
        self.ordered, self.focus = [], None
        self.source_order, self.source_focus = None, None
        self.web = copy.deepcopy(self.previous.get("web", {}))
        self.sources, self.evidence, self.notes, self.trace = [], [], [], []
        if context and not self.previous:
            self.notes.append("Conversation context expired or is unavailable; prior page evidence cannot be reused. Provide its URL to read it again.")
        self.calls, self.reads, self.searches, self.chars, self.models = 0, 0, 0, 0, 0
        self.model_chars = 0
        self.model_texts = {}
        self.candidates = {}
        self.discovery_urls = {}
        self.failed_targets, self.fresh_web_ids = set(), set()
        self.research_plan = self.research_finish = None
        self.research_validation, self.research_attempt = None, []
        self.model_loop = False
        self.generation_failed = False
        self.recent_scanned = False
        self.final_checked = False
        self.seen = set()
        self.clarification = self.model_used = False
        self.source_only = not bool(owner.client)
        self.runtime_dir = None
        self.budget = None

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def research_remaining(self):
        return max(0, self.remaining() - (0 if self.source_only else FINAL_RESERVE_SECONDS))

    def wiki_source_urls(self, item, chunk, document=None):
        urls = item.get("source_urls") or [item.get("url", "")]
        if not self.source_only and chunk.corpus == "wiki":
            document = document or next((doc for doc in self.owner.responder.kb.documents if doc.path == chunk.path), None)
            # Only the explicit canonical field in a standalone article identifies its original page.
            if document and Path(chunk.path).name.startswith(("article-", "pdf-article-")):
                canonical = re.search(r"^Canonical article:\s*\[[^\]]*\]\((https?://[^)\s]+)\)", document.markdown, re.M)
                if canonical:
                    return [canonical[1]]
        return urls

    def add(self, item, *, kind, text, urls=None, version=None, expand=False, candidate=False, refocus=False):
        stable = identity(item) if kind == "meeting" else item.get("article_id") or item.get("evidence_id")
        key = (kind, stable or text)
        for existing in self.evidence:
            if existing["key"] == key:
                if refocus and (text != existing["text"] or version != existing["source"].get("version")):
                    available = max(0, MAX_TEXT - self.chars)
                    if not self.source_only and kind != "web":
                        available = min(available, max(0, MAX_TEXT // 2 - self.chars))
                    body = text[:available]
                    if not body:
                        return {"status": "budget_exhausted", "reason": "No text allowance remains for a new focused passage."}
                    self.chars += len(body)
                    value = {**existing, "text": body, "source": {**existing["source"], "snippet": body[:600], "version": version,
                        "truncated": len(body) < len(text) or item.get("truncated", False)}}
                    if kind == "web":
                        value["source"].update(retrieved_at=item.get("retrieved_at"), requested_url=item.get("requested_url"),
                            reader_url=item.get("reader_url"), final_url=item.get("final_url"), url=(urls or [existing["source"]["url"]])[0],
                            source_urls=urls or existing["source"]["source_urls"])
                    self.evidence[self.evidence.index(existing)] = value
                    self.sources[self.sources.index(existing["source"])] = value["source"]
                    return value
                if expand and len(text) > len(existing["text"]):
                    body = text[:len(existing["text"]) + max(0, MAX_TEXT - self.chars)]
                    self.chars += len(body) - len(existing["text"])
                    existing["text"] = body
                    existing["source"].update(snippet=body[:600], truncated=len(body) < len(text))
                return existing
        available = MAX_TEXT - self.chars
        source_limit = 16
        if not self.source_only and kind != "web":
            available = min(available, MAX_TEXT // 2 - self.chars)
            source_limit = 16 - MAX_READS  # Reserve citation slots for the governed page reads.
        if not candidate and (available <= 0 or len(self.sources) >= source_limit):
            self.notes.append("Citation slots reached; more local sources cannot be registered." if len(self.sources) >= source_limit else
                "Approved-extract text allowance reached; URL and other tool allowances are reported separately.")
            return None
        candidate_text = "\n".join(str(item.get(field) or "") for field in ("name", "summary", "relevance_reason", "raw_text")) if kind == "meeting" else text
        body = web_excerpt(candidate_text, self.question, 300) if candidate else text[:available]
        if not candidate:
            self.chars += len(body)
        index = len(self.sources) + 1
        urls = urls if urls is not None else source_urls(item)
        if len(urls) > 8:
            self.notes.append("Additional source links exceed the citation budget; source coverage is incomplete.")
        urls = [url for url in urls if isinstance(url, str) and len(url) <= 2048][:8]
        source = {"index": index, "title": (item.get("name") or item.get("title") or "Read source")[:300],
            "path": item.get("path") or "", "heading": kind, "url": (item.get("url") or item.get("path") or "") if kind == "wiki" else urls[0] if urls else "",
            "snippet": body[:600], "source_urls": urls, "type": item.get("type") or kind, "date": item.get("publication_date") or item.get("start_date") or item.get("date") or "-",
            "corpus": item.get("corpus") or ("wiki" if kind == "wiki" else "source"), "matched_section": item.get("heading"), "evidence_id": stable,
            "version": version, "retrieved_at": item.get("retrieved_at"), "truncated": len(body) < len(text) or item.get("truncated", False)
                or (kind == "web" and len(body) < len(item["body"]))}
        source["date_kind"] = "wiki_document_date" if kind == "wiki" else "event_start" if kind == "meeting" and item.get("start_date") else "publication_date" if item.get("publication_date") else "unknown"
        source["verification_status"] = item.get("verification_status")
        document = next((doc for doc in [*self.owner.responder.kb.documents, *self.owner.responder.kb.source_documents]
            if doc.path == source["path"] or "wiki/" + doc.path == source["path"]), None)
        heading = re.search(r"^#\s+(.+)$", document.markdown, re.M) if document else None
        source["document_title"] = heading[1][:300] if heading else source["title"]
        if candidate:
            source.pop("index")
            source.pop("snippet")
            value = {"kind": kind, "source": source, "preview": body,
                "read_status": "approved summary candidate; no current webpage has been read"}
            value["content_kind"] = "approved_event_index" if kind == "meeting" else "immutable_report_archive" if source["corpus"] == "source" and kind == "wiki" else "approved_wiki" if kind == "wiki" else kind
            if kind == "meeting":
                value["dates"] = {"event_start": item.get("start_date"), "event_end": item.get("end_date"),
                    "precision": item.get("date_precision"), "deadlines": [{"type": row.get("deadline_type"), "date": row.get("deadline_date"), "urls": source_urls(row)}
                        for row in [item, *(item.get("pdf_observations") or [])] if row.get("deadline_date")], "knowledge_version": version}
            else:
                value["date_kind"] = "wiki_document_date" if kind == "wiki" else "publication_date"
            self.candidates[stable] = value
            return value
        if kind == "web":
            source.update(requested_url=item.get("requested_url"), reader_url=item.get("reader_url"), final_url=item.get("final_url"))
            if source["truncated"]:
                self.notes.append(f"Reading window limitation: page evidence was truncated to {len(body)} characters; omitted text may contain additional findings, dates or actions.")
        self.sources.append(source)
        source.update(citation_label=f"[{index}]", citation_token=f"[[cite:{stable}]]" if stable else None)
        value = {"key": key, "index": index, "kind": kind, "text": body, "source": source,
            "content_kind": "governed_page_body" if kind == "web" else "immutable_report_archive" if kind == "wiki" and source["corpus"] == "source" else "approved_wiki" if kind == "wiki" else "approved_record",
            "read_status": "successful governed body read" if kind == "web" else "retained excerpt; not current webpage verification"}
        self.evidence.append(value)
        source["read_status"] = value["read_status"]
        value.update(citation_label=source["citation_label"], citation_token=source["citation_token"])
        return value

    def citation_catalog(self):
        return [{key: source.get(key) for key in ("citation_label", "citation_token", "evidence_id", "document_title",
            "matched_section", "url", "read_status", "date_kind")} for source in self.sources]

    def available_targets(self):
        targets = {key: {"evidence_id": key, "title": row["source"].get("document_title"), "read": any(
            source.get("evidence_id") == key for source in self.sources)} for key, row in self.candidates.items()}
        for row in [*self.candidates.values(), *self.evidence]:
            for url in row["source"].get("source_urls", []):
                if url.startswith(("https://", "http://")):
                    targets.setdefault(url, {"url": url, "title": row["source"].get("document_title")})
        targets.update({url: {"url": url, "title": row.get("title")} for url, row in self.discovery_urls.items()})
        return {key: {**value, "failed": key in self.failed_targets} for key, value in targets.items()}

    def research_state(self, arguments):
        action = arguments.get("action")
        if set(arguments) - {"action", "time_basis", "required_outputs", "results"}:
            return {"status": "invalid", "reason": "Research state accepts task/evidence fields only."}
        if action == "plan":
            if self.research_plan is not None:
                return {"status": "invalid", "reason": "The current turn already has its plan; finish checks evidence without rewriting it."}
            outputs = arguments.get("required_outputs")
            basis = arguments.get("time_basis")
            if not isinstance(outputs, list) or not 0 < len(outputs) <= 8 or not isinstance(basis, str) or not 0 < len(basis) <= 300:
                return {"status": "invalid", "reason": "Declare 1–8 short required outputs and their time basis."}
            if any(not isinstance(row, dict) or set(row) - {"id", "task", "requires_current_body"} or not isinstance(row.get("id"), str) or not 0 < len(row["id"]) <= 80
                    or not isinstance(row.get("task"), str) or not 0 < len(row["task"]) <= 500
                    or not isinstance(row.get("requires_current_body"), bool) for row in outputs) or len({row["id"] for row in outputs}) != len(outputs):
                return {"status": "invalid", "reason": "Each task needs a unique short ID, task and requires_current_body flag."}
            self.research_plan = {"time_basis": basis, "required_outputs": copy.deepcopy(outputs)}
            self.research_finish = None
            return {"status": "planned", "plan": self.research_plan, "note": "Task facts only, not private reasoning. Original question and date constraints remain authoritative."}
        if action != "finish" or not self.research_plan or not isinstance(arguments.get("results"), list):
            return {"status": "invalid", "reason": "Declare the current turn's plan before finishing."}
        results = arguments["results"]
        required = {row["id"]: row for row in self.research_plan["required_outputs"]}
        if any(not isinstance(row, dict) for row in results) or len(results) != len(required) or {row.get("id") for row in results} != set(required):
            return {"status": "invalid", "reason": "Finish must account for every planned output exactly once."}
        read = {source.get("evidence_id"): source for source in self.sources}
        targets = self.available_targets()
        pending = []
        for row in results:
            if set(row) - {"id", "status", "evidence_ids", "pending_targets", "gap"}:
                return {"status": "invalid", "reason": "Finish accepts task/evidence fields only."}
            ids, related = row.get("evidence_ids", []), row.get("pending_targets", [])
            if row.get("status") not in {"supported", "gap"} or not isinstance(ids, list) or len(ids) > 16 or not all(isinstance(key, str) for key in ids) or not isinstance(related, list) or len(related) > 8 or not all(isinstance(key, str) for key in related):
                return {"status": "invalid", "reason": "Use supported/gap, bounded evidence IDs and explicit related target IDs/URLs."}
            if row.get("status") == "gap" and (not isinstance(row.get("gap"), str) or not 0 < len(row["gap"]) <= 500):
                return {"status": "invalid", "reason": "A gap needs a short, concrete explanation."}
            unread = [key for key in ids if key not in read]
            if any(key not in targets for key in unread):
                return {"status": "invalid", "reason": "Unknown evidence IDs cannot support a finding."}
            if unread and row["status"] == "gap":
                return {"status": "invalid", "reason": "Gap evidence IDs must also be registered read evidence; use an empty list for unread targets."}
            if row["status"] == "supported":
                if not ids:
                    return {"status": "invalid", "reason": "Supported outputs need registered read evidence IDs."}
                if unread:
                    pending.append({"output": row["id"], "reason": "Unread candidate IDs cannot support a finding, even when tools are exhausted; read them or declare the concrete gap.", "evidence_ids": unread})
                related = [*related, *unread]
                if required[row["id"]]["requires_current_body"] and not any(key in self.fresh_web_ids for key in ids):
                    pending.append({"output": row["id"], "reason": "Current body requirement has no successful fresh governed read; revise to a truthful gap if it cannot be verified."})
            for target in dict.fromkeys(related):
                if target in targets and target not in self.failed_targets:
                    is_url = target.startswith(("https://", "http://"))
                    already_read = target in read if not is_url else any(source.get("url") == target and (source.get("evidence_id") in self.fresh_web_ids
                        or (not required[row["id"]]["requires_current_body"] and "governed body" in source.get("read_status", ""))) for source in self.sources)
                    if not already_read and self.calls < MAX_TOOLS and self.models < MAX_MODELS and self.research_remaining() > 0 and self.model_chars < MAX_TEXT and (not is_url or self.reads < MAX_READS):
                        pending.append({"output": row["id"], "target": target, "tool": "read_url" if is_url else "get_meeting_details" if self.candidates[target]["kind"] == "meeting" else "get_source_details",
                            **({"refresh": True} if is_url and required[row["id"]]["requires_current_body"] else {})})
        if pending:
            return {"status": "research_pending", "pending": pending, "note": "Only your explicitly associated targets were checked; unrelated candidates do not block completion."}
        self.research_finish = copy.deepcopy(results)
        return {"status": "finish_accepted", "results": self.research_finish, "canonical_citations": self.citation_catalog(), "note": "Compose the final normal answer using these fixed citation tokens; supported means the listed evidence was read, not an independent truth judgment."}

    def reader(self):
        if self.owner.reader_factory:
            return self.owner.reader_factory()
        import os
        from climate_registry.read_api import RegistryReader, RegistryUnavailableError
        path = os.getenv("CLIMATE_REGISTRY_DB", "").strip()
        if not path or os.getenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT") == "1":
            return RegistryReader.from_public_export(self.owner.responder.kb.wiki_dir.parent)
        return RegistryReader(path, repository_root=Path(__file__).resolve().parents[1])

    def meetings(self, base):
        items, page = [], 1
        reader = self.reader()
        with reader.public_snapshot():
            chronology = reader.knowledge_chronology()
            while True:
                payload = (self.owner.meeting_view(reader, page=page, base_date=base)
                    if self.owner.meeting_view else reader.meetings(page=page, page_size=100,
                        base_date=base, timezone_name="America/New_York", include_unknown=True))
                items.extend(payload["items"])
                if page >= payload["pagination"]["pages"] or page >= 10 or not self.research_remaining():
                    if page < payload["pagination"]["pages"]:
                        self.notes.append("Meeting scan budget reached; coverage is incomplete.")
                    break
                page += 1
        self.notes.append("Meeting coverage: " + json.dumps(payload.get("coverage", {}), ensure_ascii=False))
        return items, chronology

    def meeting_text(self, item, chronology):
        from climate_monitor.meetings import _date_bounds
        fields = ("event_type", "organizer", "start_date", "end_date", "raw_date", "date_precision",
            "raw_time_text", "event_timezone", "location", "online_url", "status", "verification_status", "relevance_reason")
        text = "\n".join(f"{key}: {item.get(key) or 'unknown'}" +
            (" (stored approved status; not current verification)" if key == "status" else "") for key in fields)
        deadlines = []
        for observation in [item, *(item.get("pdf_observations") or [])]:
            if observation.get("deadline_date"):
                value = (observation.get("deadline_type") or "unknown type", observation["deadline_date"], tuple(source_urls(observation)))
                if value not in deadlines:
                    deadlines.append(value)
        as_of = self.now.astimezone(ZONE).date()
        deadline_lines, expired = [], []
        for kind, at, urls in deadlines:
            try:
                date = _date_bounds(at, "day", end=True)
                expired.append(date < as_of)
                timing = "expired; not a current opportunity for this action" if date < as_of else (
                    "due today; participation availability requires source confirmation" if date == as_of else
                    "future deadline; participation availability requires source confirmation")
            except ValueError:
                expired.append(False)
                timing = "timing unknown; cannot classify this deadline"
            deadline_lines.append(f"{kind}: {at} [{timing}] ({', '.join(urls) or 'source not recorded'})")
        text += "\nDeadlines: " + ("; ".join(deadline_lines) or "unknown")
        for source in item.get("sources") or []:
            for field in ("date_evidence", "deadline_evidence", "status_evidence"):
                if source.get(field):
                    text += f"\nSource {field} ({source.get('source_url') or 'unknown URL'}): {source[field]}"
        text += "\nEligibility / submission requirements / additional deadlines: not recorded; cannot infer."
        text += "\nParticipation action: " + (item.get("online_url") or "unknown; a source link alone does not establish a registration or submission action")
        text += "\nFirst added: " + (chronology.get("first_ingested_at") or "unknown")
        text += "\nMaterial update: " + (chronology.get("substantive_updated_at") or "unknown")
        knowledge_query = self.question if KNOWLEDGE_CHANGE.search(self.question) or re.search(r"\b(last|past)\s+\d+\s+(days?|weeks?)\b|\brecent\b|\bnew(?:ly)?\s+(opportunit\w*|meetings?|conferences?|events?)\b", self.question, re.I) else ""
        start, end = window(knowledge_query, self.now)
        times = [timestamp(chronology.get(key)) for key in ("first_ingested_at", "substantive_updated_at")]
        recent = any(at and start <= at <= end for at in times)
        status = str(item.get("status") or "unknown")
        end_at = meeting_end(item)
        ended_today = bool(end_at and end_at.astimezone(timezone.utc) <= self.now.astimezone(timezone.utc))
        try:
            ended = ended_today if end_at else _date_bounds(item.get("end_date") or "", item.get("date_precision") or "day", end=True) < as_of
        except ValueError:
            ended = False
        if item.get("end_date") == as_of.isoformat():
            text += "\nEvent timing: " + ("ended at " + end_at.isoformat() if ended_today else
                "ends at " + end_at.isoformat() if end_at else
                "same-day end time or event timezone is unknown; cannot establish whether it has ended")
        closed = status in {"cancelled", "closed", "retrospective"}
        text += "\nOpportunity timing: " + ("ended; not a current participation opportunity" if ended else
            "closed/cancelled; not a current participation opportunity" if closed else
            "recorded participation deadlines expired; additional opportunities unknown" if expired and all(expired) else
            "recent addition/material update; participation availability requires source confirmation" if recent else
            "older item; participation availability requires source confirmation" if any(times) else
            "added/update time unknown; cannot classify as newly added")
        return text

    def search_knowledge(self, query, context_path=None, target="auto", corpus=None):
        if target == "knowledge_changes":
            target = "recent_articles"
        result = []
        if target == "meetings" or (target == "auto" and MEETINGS.search(query)):
            try:
                explicit = len(ISO_DATES.findall(query)) == 2
                knowledge_range = bool(explicit or re.search(r"\b(last|past)\s+\d+\s+(days?|weeks?)\b", query, re.I)) and bool(
                    KNOWLEDGE_CHANGE.search(query))
                try:
                    start, end = window(query, self.now, knowledge_time=knowledge_range)
                except ValueError as exc:
                    if self.source_only:
                        self.clarification = True
                    self.notes.append(str(exc))
                    return result
                upcoming = bool(re.search(r"\bupcoming\b", query, re.I))
                today = self.now.astimezone(ZONE).date()
                lower = "1900-01-01" if knowledge_range else start.date().isoformat() if explicit else (
                    (today - timedelta(days=1)).isoformat() if upcoming else today.isoformat())
                scope = {"business_date_lower": lower}
                items, chronology = self.meetings(lower)
                all_items = items
                explicit_filters = {}
                for field, pattern in (("organizer", r"\borganized\s+by\s+(.+?)(?=\s+(?:in|at|whose|with|online|from|between)\b|\s+从|[?.!,;]|$)"),
                    ("location", r"\b(?:located\s+in|in|at)\s+(.+?)(?=\s+(?:organized\s+by|whose|with|online|event\s+timezone|from|between)\b|\s+从|[?.!,;]|$)")):
                    match = re.search(pattern, query, re.I)
                    if match:
                        value = match[1].strip()
                        if field == "location":
                            try:
                                ZoneInfo(value)
                            except (ZoneInfoNotFoundError, ValueError, TypeError):
                                pass
                            else:
                                continue
                        if field == "location" and re.match(r"(?:(?:the\s+)?(?:last|past|next)\b|\d{4}-\d{2}-\d{2}\b|\d+\s+(?:days?|weeks?|months?)\b)", value, re.I):
                            continue
                        explicit_filters[field] = value
                named = [item for item in all_items if item.get("name") and item["name"].casefold() in query.casefold()]
                if named:
                    items = named
                    scope["name"] = list(dict.fromkeys(item["name"] for item in named))
                wanted = [kind for kind, pattern in (("registration", r"\bregistration\b"),
                    ("consultation", r"\bconsultation\w*\b"), ("expert_review", r"\bexpert.review\w*\b"),
                    ("conference", r"\bconferences?\b")) if re.search(pattern, query, re.I)]
                if wanted:
                    scope["type"] = wanted
                    items = [item for item in items if any(kind in " ".join(str(observation.get(field) or "")
                        for observation in [item, *(item.get("pdf_observations") or [])]
                        for field in ("event_type", "deadline_type", "name")).casefold().replace("expert review", "expert_review") for kind in wanted)]
                for field in ("organizer", "location"):
                    matched = {explicit_filters[field]} if field in explicit_filters else {
                        item.get(field) for item in all_items if item.get(field) and str(item[field]).casefold() in query.casefold()
                        and not (field == "location" and re.search(r"\b(?:online|virtual)\b", query, re.I)
                            and re.search(r"\b(?:online|virtual)\b", str(item[field]), re.I))}
                    if matched:
                        scope[field] = sorted(str(value) for value in matched)
                        normalized = {str(value).casefold() for value in matched}
                        items = [item for item in items if any(value in str(item.get(field) or "").casefold() for value in normalized)]
                online_only = bool(re.search(r"\b(?:online|virtual)(?:[-\s]+only)?\s+(?:meetings?|conferences?|events?)\b", query, re.I))
                if online_only:
                    scope["online_only"] = True
                    unknown = sum(not item.get("location") and str(item.get("event_type") or "").casefold() != "webinar" for item in all_items)
                    if unknown:
                        self.notes.append(f"Online-only filter coverage: {unknown} meetings have no approved event format/location recorded; they are excluded.")
                    items = [item for item in items if online_format(item)]
                timezones = set(re.findall(r"(?<![\w])([A-Za-z_+-]+(?:/[A-Za-z0-9_+-]+)+)(?![\w])", query))
                if timezones:
                    scope["event_timezone"] = sorted(timezones)
                    unknown = sum(not item.get("event_timezone") for item in all_items)
                    if unknown:
                        self.notes.append(f"Event-timezone filter coverage: {unknown} meetings have no approved event timezone recorded; they are excluded.")
                    items = [item for item in items if str(item.get("event_timezone") or "").casefold() in {zone.casefold() for zone in timezones}]
                for field in ("organizer", "location"):
                    if field in scope:
                        unknown = sum(not item.get(field) for item in all_items)
                        if unknown:
                            self.notes.append(f"{field.capitalize()} filter coverage: {unknown} meetings have no approved {field} recorded; they are excluded.")
                if not self.source_only:
                    from .wiki_agent import _tokens
                    terms = set(_tokens(query)) - {"list", "summarize", "upcoming", "recent", "new", "newly", "added", "updated", "last", "past", "days", "weeks", "months", "dates", "date", "meetings", "meeting", "events", "event", "conference", "conferences", "consultation", "consultations", "submission", "deadlines", "deadline", "registration", "expert", "review", "reviews", "opportunities", "online", "virtual", "timezone", "time", "zone", "whose", "organized"}
                    terms -= set(_tokens(" ".join(str(value) for key in ("organizer", "location") for value in scope.get(key, []))))
                    terms -= set(_tokens(" ".join(timezones)))
                    terms = {term for term in terms if not term.isdigit()}
                    if terms:
                        scope["topic_terms"] = sorted(terms)
                        ranked = []
                        for item in items:
                            words = set(_tokens(" ".join(str(item.get(field) or "") for field in ("name", "summary", "relevance_reason", "raw_text"))))
                            score = len(terms & words)
                            if score:
                                ranked.append((score, item))
                        items = [item for score, item in sorted(ranked, key=lambda row: -row[0])]
                if upcoming:
                    ended = [item for item in items if (
                        (end_at := meeting_end(item)) and end_at.astimezone(timezone.utc) <= self.now.astimezone(timezone.utc)
                        or not end_at and item.get("end_date") and item["end_date"] < today.isoformat())]
                    if ended:
                        ended_ids = {identity(item) for item in ended}
                        items = [item for item in items if identity(item) not in ended_ids]
                        self.notes.append(f"Excluded {len(ended)} meeting(s) whose approved end time is before the query instant or whose older end date cannot establish an upcoming event.")
                    uncertain = [item for item in items if item.get("end_date") and item["end_date"] <= today.isoformat() and not meeting_end(item)]
                    if uncertain:
                        self.notes.append(f"Upcoming timing is uncertain for {len(uncertain)} meeting(s) because approved exact end time or event timezone is missing.")
                if knowledge_range:
                    scope["knowledge_window"] = {"start": start.isoformat(), "end": end.isoformat()}
                    selected, unknown = [], 0
                    for item in items:
                        facts = chronology.get("meeting:" + str(identity(item)), {})
                        times = [timestamp(facts.get(key)) for key in ("first_ingested_at", "substantive_updated_at")]
                        if any(at and start <= at <= end for at in times):
                            selected.append(item)
                        elif not any(times):
                            unknown += 1
                    self.notes.append(f"Added/material-update window: {start.isoformat()} through {end.isoformat()} (America/New_York); event dates and deadlines are separate.")
                    if unknown:
                        self.notes.append(f"{unknown} matching meetings have unknown added/material-update time; cannot confirm a knowledge-window match. Rechecks and business dates are not substitutes.")
                    items = selected
                elif explicit:
                    scope["event_or_deadline_window"] = {"start": start.date().isoformat(), "end": end.date().isoformat()}
                    from climate_monitor.meetings import _date_bounds
                    def overlaps(item):
                        observations = [item, *(item.get("pdf_observations") or [])]
                        if not (item.get("start_date") or any(observation.get("deadline_date") for observation in observations)):
                            return True  # Preserve unknown dates as unknown coverage.
                        spans = []
                        if item.get("start_date"):
                            precision = item.get("date_precision") or "day"
                            spans.append((item["start_date"], item.get("end_date") or item["start_date"], precision))
                        for observation in observations:
                            if observation.get("deadline_date"):
                                spans.append((observation["deadline_date"], observation["deadline_date"], "day"))
                        for first, last, precision in spans:
                            try:
                                if _date_bounds(first, precision, end=False) <= end.date() and _date_bounds(last, precision, end=True) >= start.date():
                                    return True
                            except ValueError:
                                self.notes.append("An unsupported date precision prevents confirming a meeting-window match.")
                        return False
                    items = [item for item in items if overlaps(item)]
                self.notes.append("Meeting query scope: " + json.dumps(scope, ensure_ascii=False) +
                    ("; topic matches locate events, not current webpage facts." if not self.source_only else "; general topic words are not matched.") +
                    " Calendar results are auxiliary identity/summary evidence, not a substitute for topic findings.")
                for item in items[:12]:
                    key = identity(item)
                    facts = chronology.get("meeting:" + str(key), {})
                    evidence = self.add(item, kind="meeting", text=self.meeting_text(item, facts), version=facts.get("knowledge_id") or item.get("content_sha256") or item.get("source_document_sha256"), candidate=not self.source_only)
                    if evidence:
                        if self.source_only and key not in self.ordered:
                            self.ordered.append(key)
                        result.append(evidence)
                if not items:
                    self.notes.append("No matching meetings in the approved view; incomplete coverage does not prove none exist.")
                if len(items) > 12:
                    self.notes.append("Showing the first 12 meetings; additional records are not displayed.")
            except Exception as exc:
                self.notes.append(f"Meeting data unavailable ({type(exc).__name__}); coverage is incomplete.")
            return result
        if target == "recent_articles" or (target == "auto" and RECENT.search(query)):
            self.recent_scanned = True
            try:
                start, end = window(self.question, self.now)
            except ValueError as exc:
                if self.source_only:
                    self.clarification = True
                self.notes.append(str(exc))
                return result
            self.notes.append(f"Added/material-update window: {start.isoformat()} through {end.isoformat()} (America/New_York).")
            try:
                reader = self.reader()
                with reader.public_snapshot():
                    chronology = reader.knowledge_chronology()
                    selected, unknown, page = [], [], 1
                    def select(item, kind):
                        facts = chronology.get("article:" + item["article_id"], {})
                        times = [timestamp(facts.get(key)) for key in ("first_ingested_at", "substantive_updated_at")]
                        if any(at and start <= at <= end for at in times):
                            selected.append((item, facts, kind))
                        elif not any(times):
                            unknown.append((item, facts, kind))
                    while page <= 10 and self.research_remaining():
                        payload = reader.articles(page=page, page_size=100)
                        for item in payload["items"]:
                            select(item, "article")
                        if page >= payload["pagination"]["pages"]:
                            break
                        page += 1
                    if page > 10:
                        self.notes.append("Article scan budget reached; coverage is incomplete.")
                    try:
                        pdf_items = reader.pdf_articles_all()
                        for item in pdf_items[:1000]:
                            if not self.research_remaining():
                                self.notes.append("PDF article scan budget reached; coverage is incomplete.")
                                break
                            select(item, "pdf_article")
                        if len(pdf_items) > 1000:
                            self.notes.append("PDF article scan budget reached; coverage is incomplete.")
                    except Exception as exc:
                        self.notes.append(f"PDF article data unavailable ({type(exc).__name__}); coverage is incomplete.")
                    if unknown:
                        self.notes.append(f"{len(unknown)} scanned article/PDF record rows have unknown added/material-update time; records can share article IDs, so this is not a unique-article count. last_seen, collection and publication dates are not substitutes.")
                    if not selected:
                        self.notes.append("No confirmed matches in this knowledge-time window. Below are approved items with unknown chronology, not confirmed recent additions.")
                    selected.sort(key=lambda pair: max((timestamp(pair[1].get(key)) or datetime.min.replace(tzinfo=timezone.utc))
                        for key in ("first_ingested_at", "substantive_updated_at")), reverse=True)
                    if len(selected) > 8:
                        self.notes.append(f"Showing 8 of {len(selected)} confirmed recent items; answer coverage is incomplete.")
                    from .wiki_agent import _tokens
                    words = set(_tokens(query)) - {"summarize", "explain", "reports", "report", "articles", "article", "content", "contents", "added", "materially", "updated", "recent", "recently", "new", "newly", "was", "last", "days", "first", "ingested", "first-ingested", "time", "publication", "date", "committee", "findings", "relevance", "sources", "include", "separately", "this", "that", "climate"}
                    words = {word for word in words if not any(character.isdigit() for character in word) and word != "were"}
                    if not re.search(r"[a-z]{2,}", query, re.I):
                        words = set()
                    def relevance(pair):
                        item = pair[0]
                        title = set(_tokens(item.get("title") or ""))
                        summary = set(_tokens(item.get("summary") or ""))
                        return len(words & title) * 3 + len(words & summary)
                    candidates = sorted(selected or unknown, key=relevance, reverse=True)
                    if words:
                        candidates = [pair for pair in candidates if relevance(pair)]
                    for item, facts, kind in candidates[:8]:
                        detail = (reader.pdf_article if kind == "pdf_article" else reader.article)(item["article_id"])
                        text = article_text(detail, facts)
                        if not self.source_only:
                            text = web_excerpt(text, query, 1200)
                        evidence = self.add(detail, kind=kind, text=text, version=detail.get("published_candidate_sha256") or facts.get("knowledge_id"), candidate=not self.source_only)
                        if evidence:
                            result.append(evidence)
            except Exception as exc:
                self.notes.append(f"Article data unavailable ({type(exc).__name__}); coverage is incomplete.")
            return result
        if self.source_only and self.named_page and re.search(r"\bpublication\s+date\b|\bpublished\b", query, re.I):
            article_id = re.fullmatch(r"article-(article-[A-Za-z0-9_-]+)\.md", Path(self.named_page).name)
            if article_id:
                try:
                    reader = self.reader()
                    with reader.public_snapshot():
                        detail = reader.article(article_id[1])
                    if not detail.get("publication_date"):
                        text = ("Publication date: unknown\n"
                            f"Collection time: {detail.get('collected_at') or 'unknown'}\n"
                            "Collection time is not a publication date; report and update dates are separate observations.")
                        if re.search(r"\b(?:findings?|summary|summarize|conclusions?)\b", query, re.I):
                            text += f"\nKey findings: {detail.get('summary') or 'unknown'}"
                        evidence = self.add(detail, kind="article", text=text, version=detail.get("published_candidate_sha256"))
                        return [evidence] if evidence else []
                except Exception as exc:
                    self.notes.append(f"Publication date: unknown in available approved metadata ({type(exc).__name__}); collection, report and update dates are not substitutes.")
        from .wiki_agent import SearchHit, REGISTRY_RUNTIME_PATH_RE
        hits = ([SearchHit(chunk, 0, "Explicit current page identity") for chunk in self.owner.responder.kb.chunks
            if chunk.path == self.named_page][:6] if self.named_page else
            self.owner.responder.kb.search(query, top_k=6 if self.source_only else 24, context_path=context_path,
                **({"corpus": corpus or "wiki"} if corpus or (not self.source_only and not (context_path or "").lstrip("/").startswith("sources/")) else {})))
        if not self.source_only:
            distinct, articles, passages = [], set(), set()
            for hit in hits:
                chunk = hit.chunk
                single_article = chunk.corpus == "wiki" and REGISTRY_RUNTIME_PATH_RE.fullmatch(Path(chunk.path).name)
                passage = (chunk.heading, chunk.text) if chunk.corpus == "wiki" else (chunk.id,)
                if (single_article and chunk.path in articles) or passage in passages:
                    continue
                if single_article:
                    articles.add(chunk.path)
                passages.add(passage)
                distinct.append(hit)
                if len(distinct) == 6:
                    break
            hits = distinct
        for hit in hits:
            item = hit.to_source(len(self.sources) + 1, self.owner.responder.base_source_url)
            item["evidence_id"] = hit.chunk.id
            text = hit.chunk.text if self.source_only else web_excerpt(hit.chunk.markdown or hit.chunk.text, query, 1400)
            evidence = self.add(item, kind="wiki", text=text, urls=self.wiki_source_urls(item, hit.chunk), candidate=not self.source_only)
            if evidence:
                result.append(evidence)
        if not result:
            self.notes.append("No matching evidence in the available Wiki corpus; this does not prove no evidence exists.")
        return result

    def get_meeting_details(self, event_id):
        items, chronology = self.meetings("1900-01-01")
        item = next((item for item in items if identity(item) == event_id), None)
        if not item:
            return {"status": "unavailable", "reason": "Meeting is no longer in the effective public view; do not reuse cached fields."}
        facts = chronology.get("meeting:" + str(event_id), {})
        if event_id not in self.candidates:
            self.focus = event_id
        self.ordered = list(self.previous.get("ordered", [])) or [event_id]
        if self.source_order is None and any(entry["kind"] == "meeting" and entry["source"].get("evidence_id") == event_id for entry in self.previous.get("source_order", [])):
            self.source_order = copy.deepcopy(self.previous["source_order"])
        return self.add(item, kind="meeting", text=self.meeting_text(item, facts), version=facts.get("knowledge_id") or item.get("content_sha256"))

    def get_source_details(self, evidence_id, focus=None):
        entries = [*self.previous.get("source_order", []), *(self.source_order or []),
            *self.candidates.values(),
            *[{"kind": entry["kind"], "source": entry["source"]} for entry in self.evidence]]
        entry = next((entry for entry in entries if entry["source"].get("evidence_id") == evidence_id), None)
        if not entry:
            return {"status": "unavailable", "reason": "The original stable source identity is unavailable."}
        kind, source = entry["kind"], entry["source"]
        if evidence_id not in self.candidates:
            self.source_focus = copy.deepcopy(entry)
        if any(previous["source"].get("evidence_id") == evidence_id for previous in self.previous.get("source_order", [])):
            self.source_order = copy.deepcopy(self.previous["source_order"])
        if kind == "wiki":
            from .wiki_agent import SearchHit
            chunk = next((chunk for chunk in self.owner.responder.kb.chunks if chunk.id == evidence_id), None)
            if not chunk:
                return {"status": "unavailable", "reason": "Source is no longer in the current corpus; do not reuse cached snippets."}
            item = SearchHit(chunk, 0, "Original result identity").to_source(1, self.owner.responder.base_source_url)
            document = next((doc for doc in [*self.owner.responder.kb.documents, *self.owner.responder.kb.source_documents]
                if doc.path == chunk.path), None)
            body = document.markdown if document else chunk.markdown or chunk.text
            original_body = body
            version = re.search(r"^Registry content version:\s*(\S+)", body, re.M)
            full_text = self.source_only or re.search(r"\b(full|entire|verbatim)\b", self.question, re.I)
            if not full_text:
                single_article = Path(chunk.path).name.startswith(("article-", "pdf-article-")) or re.search(r"^Canonical article:", original_body, re.M)
                if chunk.id.endswith(":wiki:1") and Path(chunk.path).name != "registry-meetings.md" and document and document.type != "daily":
                    single_article = True
                if chunk.corpus == "wiki" and not single_article and chunk.heading != "wiki":
                    body = chunk.markdown or chunk.text
                    preamble = re.split(r"^##\s", original_body, maxsplit=1, flags=re.M)[0]
                    trust = [line for line in preamble.splitlines() if line.startswith("Provenance:") and re.search(r"\b(unchecked|partial|conflict|verified)\b", line, re.I)]
                    body = "\n".join(trust) + "\n" + body
                body = re.sub(r"^(?:Article citation:|Registry content version:|Report citation:)[^\n]*\n?", "", body, flags=re.M)
                body = re.sub(r"^Provenance:[^\n]*(?:SHA-256|content[-_ ]hash|retained approved summary)[^\n]*\n?", "", body, flags=re.M | re.I)
            limit = MAX_TEXT - self.chars if full_text else min(3000, MAX_TEXT - self.chars)
            text = web_excerpt(body, focus or self.question, limit)
            item["truncated"] = len(text) < len(original_body)
            return self.add(item, kind="wiki", text=text, urls=self.wiki_source_urls(item, chunk, document),
                version=version[1] if version else None, expand=True, refocus=bool(focus))
        if kind in {"article", "pdf_article"}:
            reader = self.reader()
            with reader.public_snapshot():
                detail = (reader.pdf_article if kind == "pdf_article" else reader.article)(evidence_id)
                facts = reader.knowledge_chronology().get("article:" + evidence_id, {})
                body = article_text(detail, facts)
                limit = MAX_TEXT - self.chars if self.source_only or re.search(r"\b(full|entire|verbatim)\b", self.question, re.I) else min(3000, MAX_TEXT - self.chars)
                text = body if self.source_only else web_excerpt(body, focus or self.question, limit)
                detail["truncated"] = len(text) < len(body)
                return self.add(detail, kind=kind, text=text, version=detail.get("published_candidate_sha256") or facts.get("knowledge_id"), expand=True, refocus=bool(focus))
        if kind == "web":
            return self.read_url(source.get("reader_url") or source.get("url"), refresh_requested(self.question), focus=focus)
        return {"status": "unavailable", "reason": "This source kind has no supported reader."}

    def initial(self, context_path):
        urls = [url.rstrip(".,;") for url in URLS.findall(self.question)]
        link_request = bool(re.search(r"\b(source|link)\s*[\[#]?\d+|"
            r"\b(above|previous|earlier|last|first|second|third)\b.{0,40}\b(source|link|url)\b|"
            r"\b(source|link|url)\b.{0,40}\b(above|previous|earlier|first|second|third)\b", self.question, re.I))
        reference_question = re.sub(r"\bfirst[-\s]+(?:ingested|added|seen|published)\b", "", self.question, flags=re.I)
        if RECENT.search(self.question):
            # The committee is the new article query's audience, not a prior source identity.
            reference_question = re.sub(r"\b(?:this|that)\s+(?:climate\s+)?committee\b", "", reference_question, flags=re.I)
        ordinal = re.search(r"\b(first|second|third)\b|\b(?:meeting|conference|event|item)\s+(?:number\s+|#\s*)?(\d+)\b|\b(\d+)(?:st|nd|rd|th)?\s+(?:meeting|conference|event|item|one)\b|(?<!\w)#(\d+)\b", reference_question, re.I)
        previous_web = [source for source in self.previous.get("sources", []) if source.get("heading") == "web"]
        page_reference = bool(re.search(r"\b(the|this|that)\s+(page|webpage|website)\b", self.question, re.I))
        page_followup = False
        if previous_web and self.web and re.match(r"^\s*(what|which|when|where|who|how)\b", self.question, re.I):
            terms = {word.rstrip("s") for word in re.findall(r"[a-z]{4,}", self.question.casefold())
                if word not in {"what", "when", "where", "which", "does", "about", "from", "have", "with"}}
            body = "\n".join(item["body"] for item in self.web.values()).casefold()
            names = {word.casefold() for word in re.findall(r"\b[A-Z][A-Za-z0-9-]+\b", self.question)
                if word.casefold() not in {"what", "which", "when", "where", "who", "how"}}
            # A topical question can paraphrase the body; an explicitly different named subject starts a new lookup.
            page_followup = bool(terms) and any(re.search(r"\b" + re.escape(term) + r"s?\b", body) for term in terms) and all(
                re.search(r"\b" + re.escape(name) + r"\b", body) for name in names)
        refer = bool(REFERENCES.search(reference_question)) or link_request or bool(ordinal) or page_followup or page_reference
        if self.source_only and not urls and (link_request or page_reference or re.search(r"\b(it|its)\b", reference_question, re.I)
                or (ordinal and re.search(r"\b(above|previous|earlier|the|number|item)\b", reference_question, re.I))):
            if not (link_request or ordinal):
                pages = []
                for doc in self.owner.responder.kb.documents:
                    heading = re.search(r"^#\s+(.+?)\s*$", doc.markdown, re.M)
                    names = [doc.title, heading[1] if heading else ""]
                    if any(name and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", self.question, re.I) for name in names):
                        pages.append(doc.path)
                if not pages and page_reference and self.context_path:
                    pages = [self.context_path]
                if len(pages) == 1:
                    self.named_page = pages[0]
                    self.execute("search_knowledge", {"query": self.question, "target": "wiki"})
                    return
                if len(pages) > 1:
                    self.clarification = True
                    self.notes.append("Please identify the current article/page by its exact path or source URL; its name matches multiple approved pages.")
                    return
            if re.search(r"\bfor\s+\w|\b(meetings?|conferences?|events?)\b", self.question, re.I) and not (link_request or page_reference or ordinal):
                try:
                    items, _ = self.meetings("1900-01-01")
                    named = [item for item in items if item.get("name") and identity(item) and re.search(
                        r"(?<!\w)" + re.escape(item["name"]) + r"(?!\w)", self.question, re.I)]
                    if len(named) == 1:
                        self.execute("get_meeting_details", {"event_id": identity(named[0])})
                        return
                except Exception as exc:
                    self.notes.append(f"Meeting data unavailable ({type(exc).__name__}); the current name cannot establish a unique approved object.")
            self.clarification = True
            self.notes.append("Source-only mode does not resolve a previous turn's object. Provide the current meeting/article name or exact source URL.")
            self.notes.append("Manual verification: open the official source, check its current facts, and ask again with its exact URL or object name.")
            return
        if self.source_only:
            refer, ordinal = False, None
        if refer and not self.previous and self.history and not urls and not ordinal and not link_request:
            last_answer = next((item.get("content", "") for item in reversed(self.history) if item.get("role") == "assistant"), "")
            last_question = next((item.get("content", "") for item in reversed(self.history) if item.get("role") == "user"), "")
            if MEETINGS.search(last_question):
                # Legacy text supplies identity hints only; resolve them against the approved meeting view.
                try:
                    items, _ = self.meetings("1900-01-01")
                    matches = [item for item in items if item.get("name") and re.search(
                        r"(?<!\w)" + re.escape(item["name"]) + r"(?!\w)", last_answer, re.I)]
                except Exception as exc:
                    matches = []
                    self.notes.append(f"Meeting data unavailable ({type(exc).__name__}); prior text cannot establish current facts.")
                if len(matches) == 1:
                    self.execute("get_meeting_details", {"event_id": identity(matches[0])})
                else:
                    self.clarification = True
                    self.notes.append("Please identify the meeting by its name or source URL; the previous text does not identify one unique current meeting.")
                return
        if refer and not urls and not link_request and not re.search(r"\b(meetings?|conferences?|events?)\b", self.question, re.I):
            order = self.previous.get("source_order", [])
            article_position = bool(ordinal and re.search(r"\b(articles?|reports?|notes?)\b", self.question, re.I))
            if article_position:
                order = [entry for entry in order if entry["kind"] in {"wiki", "article", "pdf_article"}]
            position = next((group.lower() for group in ordinal.groups() if group), "") if ordinal else ""
            index = {"first": 0, "second": 1, "third": 2}.get(position, int(position) - 1 if position.isdigit() else -1)
            selected = order[index] if 0 <= index < len(order) else None if ordinal else self.previous.get("source_focus")
            article_order = bool(order) and all(entry["kind"] in {"wiki", "article", "pdf_article"} for entry in order)
            if not selected and not ordinal and article_order:
                if len(order) == 1:
                    selected = order[0]
                else:
                    self.clarification = True
                    self.notes.append("Please identify the source by its number, name or source URL.")
                    return
            if selected:
                self.source_order = copy.deepcopy(self.previous["source_order"])
                if selected["kind"] == "meeting":
                    result = self.execute("get_meeting_details", {"event_id": selected["source"]["evidence_id"]})
                else:
                    self.source_focus = copy.deepcopy(selected)
                    result = self.execute("get_source_details", {"evidence_id": selected["source"]["evidence_id"]})
                self.clarification = result.get("status") == "unavailable"
                return
            if article_position:
                self.clarification = True
                self.notes.append("The requested article position is unavailable. Please identify it by name or source URL.")
                return
        explicit_search = bool(WEB_SEARCH.search(self.question)) and not urls and not link_request and not (refer and self.previous.get("ordered")) and not self.source_only
        if explicit_search:
            # A requested web search must not spend its reader/text budget on guessed URLs or broad Wiki hits.
            result = self.execute("search_web", {"query": self.question[:500]})
            confirmed = sum(item.get("status") in {"read", "cached"} and bool(item.get("body")) for item in result.get("read_results", []))
            self.notes.append(f"Web search: {result.get('status')}; {confirmed} candidate page bodies read. Unread candidates cannot establish page facts; no results is not proof of absence.")
        if refer and previous_web and self.web and not self.previous.get("ordered") and not urls and not link_request and not explicit_search:
            # A page follow-up keeps its known body even when its fact uses meeting vocabulary.
            targets = {source.get("reader_url") or source.get("url") for source in previous_web}
            if len(targets) > 1 and (page_reference or page_followup or re.search(r"\b(it|its|this|that)\b", self.question, re.I)):
                self.clarification = True
                self.sources = copy.deepcopy(self.previous["sources"])
                self.notes.append("Please identify the page by its source number or URL; the previous request read multiple pages.")
                return
            for url in list(self.web)[:MAX_READS]:
                self.execute("read_url", {"url": url, "refresh": refresh_requested(self.question)})
            return
        if link_request and not urls:
            previous_sources = self.previous.get("sources", [])
            match = re.search(r"(?:source|link)\s*(?:\[|#)?(\d+)", self.question, re.I)
            ordinal = re.search(r"\b(first|second|third)\b", self.question, re.I)
            index = int(match[1]) - 1 if match else {"first": 0, "second": 1, "third": 2}.get(ordinal[1].lower(), 0) if ordinal else 0
            if 0 <= index < len(previous_sources):
                urls.extend(previous_sources[index].get("source_urls", [])[:1] or [previous_sources[index].get("url")])
                self.focus = (previous_sources[index].get("evidence_id") if previous_sources[index].get("type") == "meeting"
                    else self.previous.get("focus"))
                self.ordered = list(self.previous.get("ordered", []))
            else:
                self.clarification = True
                self.notes.append("The original source link is unavailable. Please provide its URL.")
        if refer and self.previous.get("ordered") and not link_request and not urls:
            order = self.previous["ordered"]
            position = next((group.lower() for group in ordinal.groups() if group), "") if ordinal else ""
            index = {"first": 0, "second": 1, "third": 2}.get(position, int(position) - 1 if position.isdigit() else -1)
            focus = order[index] if 0 <= index < len(order) else None if ordinal else self.previous.get("focus")
            if not focus and len(order) == 1 and not ordinal:
                focus = order[0]
            if focus:
                result = self.execute("get_meeting_details", {"event_id": focus})
            else:
                self.clarification = True
                self.notes.append("Please identify the meeting by its number, name or source URL.")
        elif refer and not urls and ordinal:
            self.clarification = True
            self.notes.append("The original result list is unavailable. Please provide its name or source URL.")
        elif not urls and not self.clarification and not explicit_search:
            self.execute("search_knowledge", {"query": self.question, "context_path": context_path})
        if urls:
            targets = set(url for url in urls if isinstance(url, str) and url)
            for url in list(targets):
                try:
                    targets.add(reader_url(url))
                except ValueError:
                    pass  # The governed read below reports invalid URLs; do not reuse an old target.
            self.web = {key: item for key, item in self.web.items() if key in targets
                or item.get("requested_url") in targets or item.get("final_url") in targets}
        for url in dict.fromkeys(url for url in urls if url):
            self.execute("read_url", {"url": url, "refresh": refresh_requested(self.question)})

    def read_url(self, url, refresh=False, focus=None):
        if not self.research_remaining():
            return {"status": "budget_exhausted", "reason": "Research time ended; final synthesis time is reserved."}
        requested_url = url
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or len(url) > 2048:
            return {"status": "invalid", "reason": "A public HTTP(S) URL is required."}
        url = reader_url(url)
        try:
            canonicalize_url, RequestValidationError = _load_url_policy()
        except ImportError:
            from climate_monitor.article_content_adapter import UNAVAILABLE_REASON
            return {"status": "unavailable", "reason": UNAVAILABLE_REASON,
                "requested_url": requested_url, "reader_url": url}
        try:
            url = canonicalize_url(url)
        except RequestValidationError as exc:
            return {"status": "invalid", "reason": f"Governed URL policy: {exc.code}", "requested_url": requested_url}
        if not refresh and url in self.web:
            item = self.web[url]
            limit = MAX_TEXT - self.chars if self.source_only or URLS.fullmatch(self.question.strip()) else min(12000, MAX_TEXT - self.chars)
            evidence = self.add(item, kind="web", text=web_excerpt(item["body"], focus or self.question, limit), urls=[item["final_url"]], version=item["content_hash"], refocus=bool(focus))
            if evidence and evidence.get("status"):
                return evidence
            return {"status": "cached", **item, "body": item["body"] if self.source_only else evidence["text"] if evidence else "",
                "requested_url": requested_url, "source": evidence["source"] if evidence else None}
        if self.reads >= MAX_READS:
            return {"status": "budget_exhausted", "reason": "URL read limit reached."}
        if self.chars >= MAX_TEXT or len(self.sources) >= 16:
            return {"status": "budget_exhausted", "reason": "No room remains for page evidence; no URL request was sent."}
        self.reads += 1
        from climate_monitor.article_content_adapter import fetch_article_content
        from climate_monitor.request_budget import RequestBudget
        if self.budget is None:
            self.budget = RequestBudget(self.runtime_dir / "budget.json", {"attempt": 1,
                "run_id": secrets.token_hex(12), "budgets": {"runtime_seconds": self.research_remaining(),
                    "fetch_attempts": MAX_READS * 4, "search_attempts": 1, "search_results": 5, "retries_per_item": 0}})
        key = "chat-" + hashlib.sha256(url.encode()).hexdigest()[:24]
        record = fetch_article_content(key, url, data_root=self.runtime_dir / "reader", budget=self.budget)
        body = record.get("content")
        if record.get("status") != "ok" or not isinstance(body, str) or not body.strip():
            return {"status": (record.get("status") if record.get("status") != "ok" else "unavailable") or "unavailable", "reason": record.get("failure_reason") or "No full-page body was returned.", "requested_url": requested_url, "reader_url": url}
        final = record.get("final_url") or url
        item = {"evidence_id": key, "title": final, "requested_url": requested_url, "reader_url": url, "final_url": final,
            "body": web_excerpt(body, focus or self.question, MAX_TEXT), "content_hash": record.get("content_hash"),
            "retrieved_at": datetime.now(timezone.utc).isoformat(), "truncated": len(body) > MAX_TEXT}
        self.web[url] = item
        while sum(len(value["body"]) for value in self.web.values()) > MAX_TEXT:
            del self.web[next(iter(self.web))]
        limit = MAX_TEXT - self.chars if self.source_only or URLS.fullmatch(self.question.strip()) else min(12000, MAX_TEXT - self.chars)
        evidence = self.add(item, kind="web", text=web_excerpt(item["body"], focus or self.question, limit), urls=[final], version=item["content_hash"], refocus=bool(focus) or refresh)
        if evidence and evidence.get("status"):
            return evidence
        return {"status": "read", **item, "body": item["body"] if self.source_only else evidence["text"] if evidence else "",
            "source": evidence["source"] if evidence else None}

    def search_web(self, query, *, official_only=False, event_hint="", prior_urls=()):
        if self.searches >= 2 or self.models > MAX_MODELS - 3 or not self.owner.client or not self.research_remaining():
            return {"status": "unavailable", "reason": "Provider search is unavailable, both searches were used, or it would consume the reserved URL-selection and final-synthesis requests."}
        self.searches += 1
        self.models += 1
        client = self.owner.client.with_options(timeout=min(20, self.research_remaining()), max_retries=0)
        search_prompt = "Find primary sources for this question. Return candidate source URLs. Search summaries are not full-page evidence.\nAs-of: " + json.dumps(self.date_context()) + "\n" + query
        if official_only:
            search_prompt += "\nIdentify and cite only the primary official event or organizer pages for these deadlines. Exclude news/aggregator pages. If no official page is found, say unknown."
        if self.owner.provider == "anthropic":
            result = client.messages.create(model=self.owner.model, max_tokens=3000,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
                messages=[{"role": "user", "content": search_prompt}])
        else:
            result = client.responses.create(model=self.owner.model,
                tools=[{"type": "web_search"}], include=["web_search_call.action.sources"],
                max_tool_calls=1, max_output_tokens=1000, input=search_prompt)
        payload = result.model_dump()
        urls = []
        metadata = {}
        def retain_metadata(row):
            info = metadata.setdefault(row["url"], {})
            for key, value in (("title", row.get("title")),
                    ("preview", row.get("description") or row.get("snippet") or row.get("cited_text"))):
                if isinstance(value, str) and value:
                    info[key] = value
        cited_urls = []
        search_errors = []
        action_urls = []
        executed = False
        if self.owner.provider == "anthropic":
            for block in payload.get("content", []):
                if block.get("type") == "web_search_tool_result" and isinstance(block.get("content"), list):
                    executed = True
                    urls.extend(row["url"] for row in block["content"] if row.get("type") == "web_search_result" and row.get("url"))
                    for row in block["content"]:
                        if row.get("type") == "web_search_result" and row.get("url"):
                            retain_metadata(row)
                elif block.get("type") == "web_search_tool_result" and isinstance(block.get("content"), dict):
                    search_errors.append(block["content"].get("error_code") or "unavailable")
                if block.get("type") == "text":
                    cited_urls.extend(citation["url"] for citation in (block.get("citations") or [])
                        if citation.get("type") == "web_search_result_location" and citation.get("url"))
                    for citation in block.get("citations") or []:
                        if citation.get("type") == "web_search_result_location" and citation.get("url"):
                            retain_metadata(citation)
        for output in payload.get("output", []):
            if output.get("type") == "web_search_call" and output.get("status") == "completed":
                executed = True
                action_urls.extend(row["url"] for row in (output.get("action") or {}).get("sources", [])
                    if isinstance(row, dict) and row.get("url"))
                for row in (output.get("action") or {}).get("sources", []):
                    if isinstance(row, dict) and row.get("url"):
                        retain_metadata(row)
            for content in output.get("content", []):
                for annotation in content.get("annotations", []):
                    if annotation.get("type") == "url_citation" and annotation.get("url"):
                        urls.append(annotation["url"])
                        cited_urls.append(annotation["url"])
                        retain_metadata(annotation)
        if not executed:
            return {"status": "unavailable", "reason": "The provider returned no completed search call."}
        urls.extend(action_urls)
        if official_only:
            # Missing model citations do not erase structured results. Names only rank candidates, never prove facts.
            if cited_urls:
                urls = cited_urls
            else:
                words = set(re.findall(r"[a-z]{3,}", event_hint.casefold()))
                def priority(url):
                    try:
                        parts = urlsplit(url)
                        matches = sum(word in (parts.hostname or "").casefold() for word in words)
                        return (url in prior_urls, -matches, len(parts.path))
                    except ValueError:
                        return (True, 0, len(url))
                urls = sorted(dict.fromkeys(urls), key=priority)
        urls = list(dict.fromkeys(urls))[:5]
        if search_errors:
            self.notes.append("Provider search limit/error: " + ", ".join(search_errors) + "; only returned candidates were considered.")
        return {"status": "searched" if urls else "empty", "query": query, "provider": self.owner.provider,
            "candidate_urls": urls, "read_results": [], "provider_search_errors": search_errors,
            "candidates": [{"url": url, "title": str(metadata.get(url, {}).get("title") or "")[:200],
                "preview": str(metadata.get(url, {}).get("preview") or "")[:300], "read_status": "unread search hint"} for url in urls],
            "note": "Unread candidates and search snippets cannot support page facts. No results does not prove none exist."}

    def execute(self, name, arguments):
        start = time.monotonic()
        note_start = len(self.notes)
        key = (name, json.dumps(arguments, sort_keys=True))
        known_candidates = set(self.candidates) | set(self.discovery_urls)
        if self.calls >= MAX_TOOLS or not self.research_remaining():
            result = {"status": "budget_exhausted", "reason": "Shared turn budget exhausted."}
        elif key in self.seen and name != "research_state":
            result = {"status": "stopped", "reason": "Repeated tool target; no retry."}
        else:
            self.calls += 1
            self.seen.add(key)
            if name in {"search_knowledge", "get_source_details", "get_meeting_details", "read_url", "search_web"}:
                self.research_finish = None
            try:
                focus = arguments.get("focus")
                if focus is not None and (not isinstance(focus, str) or not 0 < len(focus) <= 500):
                    raise ValueError("Invalid focus; use a fact-finding phrase of at most 500 characters.")
                if self.model_loop and not self.research_plan and name != "research_state":
                    result = {"status": "invalid", "reason": "Declare research_state plan before choosing evidence tools."}
                elif name == "research_state" and not self.source_only:
                    result = self.research_state(arguments)
                elif name == "search_knowledge" and isinstance(arguments.get("query"), str) and 0 < len(arguments["query"]) <= 2000 and arguments.get("target", "auto") in {"auto", "wiki", "meetings", "recent_articles", "knowledge_changes"} and ("corpus" not in arguments or (arguments.get("target") == "wiki" and arguments["corpus"] in {"wiki", "source"})):
                    if not self.source_only and arguments.get("target") in {"recent_articles", "knowledge_changes"} and self.recent_scanned:
                        result = {"status": "stopped", "reason": "This request's knowledge-time scope was already checked; changing query words cannot supply missing approved chronology. Use topical source discovery/read_url for findings, without calling them confirmed new records."}
                    else:
                        result = {"status": "read" if self.source_only else "candidates", "evidence": self.search_knowledge(arguments["query"], self.named_page or self.context_path, arguments.get("target", "auto"), corpus=arguments.get("corpus"))}
                elif name == "get_meeting_details" and isinstance(arguments.get("event_id"), str) and len(arguments["event_id"]) <= 128:
                    result = self.get_meeting_details(arguments["event_id"]) or {"status": "unavailable"}
                elif name == "get_source_details" and isinstance(arguments.get("evidence_id"), str) and 0 < len(arguments["evidence_id"]) <= 128:
                    result = self.get_source_details(arguments["evidence_id"], focus=focus) or {"status": "unavailable"}
                elif name == "read_url" and isinstance(arguments.get("url"), str):
                    result = self.read_url(arguments["url"], bool(arguments.get("refresh")) or refresh_requested(self.question), focus=focus)
                elif name == "search_web" and isinstance(arguments.get("query"), str) and 0 < len(arguments["query"]) <= 500:
                    result = self.search_web(arguments["query"], official_only=bool(arguments.get("official_only")),
                        event_hint=arguments.get("event_hint", ""), prior_urls=arguments.get("prior_urls", ()))
                elif name == "ask_clarification" and isinstance(arguments.get("question"), str) and 0 < len(arguments["question"]) <= 1000:
                    self.clarification = True
                    self.notes.append(arguments["question"])
                    if not self.evidence:
                        self.source_order = copy.deepcopy(self.previous.get("source_order", []))
                        self.ordered = list(self.previous.get("ordered", []))
                        self.focus = self.previous.get("focus")
                    result = {"status": "clarification", "question": arguments["question"]}
                else:
                    result = {"status": "invalid", "reason": "Unsupported tool or arguments."}
                if self.source_only and name in {"search_knowledge", "get_meeting_details"} and refresh_requested(self.question):
                    entries = result.get("evidence", [result])
                    meetings = [entry for entry in entries if entry.get("kind") == "meeting"]
                    if meetings:
                        targets = list(dict.fromkeys(url for entry in meetings for url in entry["source"].get("source_urls", [])[:1]))
                        reads = [self.execute("read_url", {"url": url, "refresh": True}) for url in targets[:MAX_READS]]
                        result = {**result, "read_results": reads}
                        if not any(read.get("status") == "read" for read in reads):
                            self.notes.append("Fresh current status could not be verified; stored Registry status is not current verification.")
                        if len(targets) > MAX_READS or any(not entry["source"].get("source_urls") for entry in meetings):
                            self.notes.append("Fresh current status could not be verified for all matched meetings; source URLs or reading coverage are incomplete.")
            except Exception as exc:
                result = {"status": "unavailable", "reason": type(exc).__name__}
        if name == "research_state":
            self.research_validation = copy.deepcopy(result)
            if arguments.get("action") == "finish":
                self.research_attempt = [copy.deepcopy(row) for row in arguments.get("results", [])
                    if isinstance(row, dict) and isinstance(row.get("id"), str)] if isinstance(arguments.get("results"), list) else []
        if name == "search_web":
            self.discovery_urls.update({row["url"]: row for row in result.get("candidates", [])})
        if name in {"search_knowledge", "search_web"} and result.get("status") in {"candidates", "searched"}:
            result["new_candidate_count"] = len((set(self.candidates) | set(self.discovery_urls)) - known_candidates)
            result["available_targets"] = self.available_targets()
            if not result["new_candidate_count"]:
                result.update(status="no_new_candidates", note="No new candidate identities. Existing unread targets are shown; choose details, a relevant link or a different discovery method instead of near-synonymous searches.")
        target = arguments.get("url") or arguments.get("evidence_id") or arguments.get("event_id")
        if target and result.get("status") in {"unavailable", "failed", "invalid", "no_content", "budget_exhausted", "stopped", "unsupported_attachment", "interaction_required"}:
            self.failed_targets.add(target)
        source = result.get("source")
        if source and name in {"read_url", "get_source_details"} and result.get("status") in {"read", "cached"}:
            if result["status"] == "read" and result.get("body"):
                self.fresh_web_ids.add(source["evidence_id"])
            source["read_status"] = "fresh governed body read" if source["evidence_id"] in self.fresh_web_ids else "retained governed body; not refreshed"
        status = result.get("status", "read")
        self.trace.append({"tool": name, "target": arguments.get("url") or arguments.get("query") or arguments.get("event_id") or arguments.get("evidence_id"),
            "status": status, "seconds": round(time.monotonic() - start, 3)})
        if status in {"unavailable", "failed", "budget_exhausted", "stopped", "invalid", "no_content"}:
            self.notes.append(f"{name}: {status}; {result.get('reason', 'no usable evidence')}.")
            self.notes.append("Manual verification: open the official source URL, check the current requested facts, and ask again with its exact URL or a quoted passage.")
        return {**result, "notes": list(dict.fromkeys(self.notes[note_start:]))}

    def model_data(self, value):
        """Share one evidence-text allowance across initial and all tool messages."""
        truncated = False
        def bounded(item):
            nonlocal truncated
            if isinstance(item, list):
                return [bounded(child) for child in item]
            if not isinstance(item, dict):
                return item
            result = {}
            for key, child in item.items():
                if key == "snippet" and isinstance(child, str):
                    continue  # Citation metadata must not duplicate the supplied evidence text.
                if key in {"body", "text", "preview"} and isinstance(child, str):
                    if key != "preview" and child in self.model_texts:
                        result[key + "_reference"] = self.model_texts[child]
                        continue
                    if key != "preview":
                        self.model_texts[child] = (item.get("source") or {}).get("evidence_id") or item.get("evidence_id") or "previous supplied text"
                    remaining = max(0, MAX_TEXT - self.model_chars)
                    result[key] = child[:remaining]
                    self.model_chars += len(result[key])
                    if len(result[key]) < len(child):
                        truncated = True
                        result["truncated"] = True
                else:
                    result[key] = bounded(child)
            return result
        result = bounded(value)
        if truncated:
            note = "Model evidence text budget reached; page/text results are partial and coverage is incomplete."
            self.notes.append(note)
            if isinstance(result, dict):
                result["coverage_note"] = note
        return result

    def research_handoff(self):
        read = {source.get("evidence_id"): source for source in self.sources}
        results = {row["id"]: row for row in self.research_attempt}
        tasks = []
        for task in (self.research_plan or {}).get("required_outputs", []):
            row = results.get(task["id"], {})
            ids = [key for key in row.get("evidence_ids", []) if isinstance(key, str) and key in read][:16] if isinstance(row.get("evidence_ids", []), list) else []
            optional_urls = list(dict.fromkeys(url for key in ids for url in read[key].get("source_urls", []) if url.startswith(("https://", "http://"))))
            urls = []
            related = row.get("pending_targets", [])
            if isinstance(related, list):
                urls += [target for target in related if isinstance(target, str) and target in self.available_targets() and target.startswith(("https://", "http://"))]
                optional_urls += [url for target in related if isinstance(target, str) and target in self.candidates
                    for url in self.candidates[target]["source"].get("source_urls", []) if url.startswith(("https://", "http://"))]
            urls = list(dict.fromkeys(urls))
            unattempted = [url for url in urls if url not in self.failed_targets and not any(
                key in self.fresh_web_ids and read[key].get("url") == url for key in read)]
            tasks.append({**task, "reported_status": row.get("status", "unchecked"), "registered_support": ids,
                "current_support": [key for key in ids if key in self.fresh_web_ids],
                "retained_only": [key for key in ids if key not in self.fresh_web_ids],
                "optional_source_urls": list(dict.fromkeys(optional_urls)),
                "unattempted_current_targets": unattempted if task["requires_current_body"] else [],
                "gap": str(row.get("gap") or "")[:500]})
        return {"last_validation": self.research_validation, "tasks": tasks, "failed_targets": sorted(self.failed_targets),
            "date_context": self.date_context(), "verification_guidance": verification_guidance(self.question, self.sources),
            "optional_links_note": "Source links are optional discovery clues; only explicitly selected related pending targets require an attempt.",
            "coverage_notes": [note[:300] for note in self.notes[-6:]]}

    def date_context(self):
        local = self.now.astimezone(ZONE)
        context = {"utc": self.now.astimezone(timezone.utc).isoformat(), "new_york": local.isoformat(),
            "new_york_date": local.date().isoformat(), "time_zone": "America/New_York"}
        if KNOWLEDGE_CHANGE.search(self.question):
            try:
                start, end = window(self.question, self.now)
                context["knowledge_window"] = {"start": start.isoformat(), "end": end.isoformat(), "zone": "America/New_York"}
            except ValueError as exc:
                context["knowledge_window"] = {"clarification_required": str(exc)}
        else:
            context["knowledge_window"] = "No ingestion window requested; do not invent a two-week cutoff."
        return context

    def model_answer(self):
        self.model_loop = True
        from .wiki_agent import answer_mode_instruction
        schemas = []
        for name, fields, description in (("search_knowledge", {"query": {"type": "string", "description": "Core subject only: for X for audience Y, search X and explain implications for Y in the answer."}, "target": {"type": "string", "enum": ["wiki", "meetings", "knowledge_changes"]}, "corpus": {"type": "string", "enum": ["wiki", "source"], "description": "Only for target=wiki: default wiki. Choose source for an explicit original archive request or retained archive identity."}}, "Discover unregistered approved candidates using a short subject query, separate from the audience. wiki locates topic passages; meetings locates requested events/participation in the effective approved auxiliary index (name, summary/relevance, organizer/location, event/deadline types and dates); knowledge_changes checks requested first-ingested/material-update chronology once using the original question's canonical New York window. Ordinary recent/latest facts use publisher dates, not ingestion. Results disclose content/date type and matching scope. Choose a few different relevant details to register evidence; for a known URL requiring current facts use read_url directly. no_new_candidates means use existing relevant targets or another discovery method."),
            ("get_meeting_details", {"event_id": {"type": "string"}}, "Resolve a stable meeting ID in the current effective approved view, preserving separate event/deadline evidence."),
            ("get_source_details", {"evidence_id": {"type": "string"}, "focus": {"type": "string", "maxLength": 500}}, "Read retained findings for a stable Wiki/article/PDF/Web ID, rechecking visibility and version. Optional focus selects the needed facts. One independent article detail includes its relevant document body; reading more chunks of that same document is usually unnecessary. For a known URL needing current facts use read_url directly."),
            ("read_url", {"url": {"type": "string"}, "refresh": {"type": "boolean"}, "focus": {"type": "string", "maxLength": 500}}, "Read a public URL through the governed reader. Returns actual body/hash/URL or failure. Use refresh for current facts; ordinary follow-ups can reuse retained body."),
            ("search_web", {"query": {"type": "string", "description": "Core subject only: for X for audience Y, search X and explain implications for Y in the answer."}}, "Up to two bounded provider searches returning unverified candidate URLs. Reformulate once if relevant bodies fail or evidence is incomplete. Select relevant primary candidates and call read_url. Only successfully read bodies support page facts, never snippets."),
            ("research_state", {"action": {"type": "string", "enum": ["plan", "finish"]}, "time_basis": {"type": "string", "maxLength": 300},
                "required_outputs": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
                    "id": {"type": "string", "maxLength": 80}, "task": {"type": "string", "maxLength": 500}, "requires_current_body": {"type": "boolean"}},
                    "required": ["id", "task", "requires_current_body"], "additionalProperties": False}},
                "results": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
                    "id": {"type": "string", "maxLength": 80}, "status": {"type": "string", "enum": ["supported", "gap"]},
                    "evidence_ids": {"type": "array", "maxItems": 16, "items": {"type": "string"}},
                    "pending_targets": {"type": "array", "maxItems": 8, "items": {"type": "string"}}, "gap": {"type": "string", "maxLength": 500}},
                    "required": ["id", "status", "evidence_ids"], "additionalProperties": False}}},
                "Keep short current-turn task/evidence facts, not reasoning. plan supplies required_outputs and time_basis. finish supplies each task's supported/gap results, registered evidence_ids, concrete gaps and explicitly relevant pending target IDs/URLs; other schema fields do not rewrite the saved plan. Unread IDs and unverified current bodies cannot support completion. Missing approved chronology may be a gap while useful findings remain a separate task. Reserve a finish call and final synthesis; both actions share the tool allowance."),
            ("ask_clarification", {"question": {"type": "string"}}, "Ask a concise question only when multiple plausible object identities remain ambiguous. Missing evidence alone should yield gaps and manual verification guidance.")):
            schemas.append({"type": "function", "function": {"name": name,
                "description": description,
                "parameters": {"type": "object", "properties": fields, "required": ["query", "target"] if name == "search_knowledge" else [next(iter(fields))], "additionalProperties": False}}})
        messages = [{"role": "system", "content": Path(__file__).with_name("chat_instructions.md").read_text(encoding="utf-8") + "\nAs-of: " + json.dumps(self.date_context())},
            *[{"role": item["role"], "content": item["content"][:2000]} for item in self.history[-4:] if item.get("role") in {"user", "assistant"}],
            {"role": "user", "content": self.question},
            {"role": "user", "content": "Existing read evidence (with citation indices) and coverage:\n" + json.dumps(self.model_data({"evidence": self.evidence, "notes": self.notes}), ensure_ascii=False)}]
        messages[0]["content"] += f"\nAnswer mode: {self.answer_mode}\n" + answer_mode_instruction(self.answer_mode)
        candidates = {"context_path": self.context_path, "ordered": self.previous.get("ordered", []), "focus": self.previous.get("focus"),
            "source_focus": (self.previous.get("source_focus") or {}).get("source", {}).get("evidence_id"),
            "sources": [{"kind": entry["kind"], **{key: entry["source"].get(key) for key in ("evidence_id", "title", "type", "version", "url", "index",
                "citation_label", "citation_token", "document_title", "matched_section", "read_status", "date_kind")}}
                for entry in self.previous.get("source_order", [])[:16]]}
        messages.insert(-1, {"role": "user", "content": "Conversation identity candidates (not verified facts):\n" + json.dumps(candidates, ensure_ascii=False)})
        if URLS.fullmatch(self.question.strip()):
            messages[0]["content"] += " Give a concise overview of the supplied page with key findings, dates, actions and its cited source. State missing details as unknown and disclose any reading window limitation."
        base_system = messages[0]["content"]
        empty_synthesis_retry = False
        try:
            for _ in range(MAX_MODELS):
                if self.remaining() <= 0 or self.models >= MAX_MODELS:
                    break
                self.models += 1
                budget = {"model_requests": MAX_MODELS - self.models, "tools": MAX_TOOLS - self.calls,
                    "web_searches": 2 - self.searches,
                    "url_reads": MAX_READS - self.reads, "evidence_chars": MAX_TEXT - self.model_chars,
                    "seconds": round(self.remaining(), 1), "research_seconds": round(self.research_remaining(), 1), "final_reserve_seconds": FINAL_RESERVE_SECONDS}
                state = "Shared remaining budget: " + json.dumps(budget) + "\n" + json.dumps({"canonical_citations": self.citation_catalog(),
                    "date_context": self.date_context(), "research_plan": self.research_plan,
                    "research_finish": self.research_finish, "research_handoff": self.research_handoff(),
                    "available_targets": self.available_targets()}, ensure_ascii=False)
                final_window = not self.research_remaining()
                available_schemas = [schema for schema in schemas if not (
                    schema["function"]["name"] == "search_web" and (self.searches >= 2 or self.models > MAX_MODELS - 3))]
                if not self.research_plan:
                    available_schemas = [copy.deepcopy(schema) for schema in available_schemas if schema["function"]["name"] == "research_state"]
                for schema in available_schemas:
                    if schema["function"]["name"] == "research_state":
                        schema = copy.deepcopy(schema)
                        position = next(i for i, value in enumerate(available_schemas) if value["function"]["name"] == "research_state")
                        available_schemas[position] = schema
                        parameters = schema["function"]["parameters"]
                        parameters["properties"]["action"]["enum"] = ["finish"] if self.research_plan else ["plan"]
                        parameters["required"] = ["action", "results"] if self.research_plan else ["action", "time_basis", "required_outputs"]
                if self.calls >= MAX_TOOLS or self.models >= MAX_MODELS or empty_synthesis_retry or final_window:
                    available_schemas = []
                    state += " Compose the final cited answer now from the actual evidence; state any remaining missing facts. No further tools are available."
                    if self.research_finish is None:
                        state += " Research is partial: preserve the pending/invalid task checks and current_support arrays. Unchecked retained dates or open status are not current verification. Use the supplied verification guidance for the actual gaps."
                request_messages = list(messages)
                last = request_messages[-1]
                if last["role"] == "tool":
                    request_messages[-1] = {**last, "content": json.dumps({**json.loads(last["content"]), "runtime_state": state}, ensure_ascii=False)}
                elif last["role"] == "user" and isinstance(last["content"], list):
                    request_messages[-1] = {**last, "content": [*last["content"], {"type": "text", "text": state}]}
                else:
                    request_messages.insert(len(request_messages) - 1, {"role": "user", "content": state})
                client = self.owner.client.with_options(timeout=min(20, self.remaining() if not available_schemas else self.research_remaining()), max_retries=0)
                try:
                    if self.owner.provider == "anthropic":
                        response = client.messages.create(model=self.owner.model, max_tokens=3000 if self.answer_mode == "brief" else 6000,
                            system=base_system, messages=request_messages[1:],
                            **({"tools": [{"name": schema["function"]["name"], "description": schema["function"]["description"],
                                "input_schema": schema["function"]["parameters"]} for schema in available_schemas]} if available_schemas else {}))
                    else:
                        response = client.chat.completions.create(model=self.owner.responder.model, messages=request_messages,
                            **({"tools": available_schemas} if available_schemas else {}),
                            reasoning_effort="none", max_completion_tokens=3000 if self.answer_mode == "brief" else 6000)
                except Exception as exc:
                    if (isinstance(exc, TimeoutError) or type(exc).__name__ == "APITimeoutError") and not final_window and not self.research_remaining() and self.remaining() > 0 and self.models < MAX_MODELS:
                        self.notes.append("Research request reached its time allowance; remaining time is reserved for a partial final answer.")
                        continue
                    raise
                if self.owner.provider == "anthropic":
                    payload = response.model_dump()
                    if payload.get("stop_reason") == "max_tokens":
                        self.notes.append("Model output budget reached; this answer is partial.")
                    blocks = payload["content"]
                    calls = [block for block in blocks if block["type"] == "tool_use"]
                    if not calls:
                        text = "\n".join(block["text"] for block in blocks if block["type"] == "text")
                        if text:
                            if available_schemas and not self.final_checked and self.models < MAX_MODELS and self.research_remaining() > 0:
                                self.final_checked = bool(self.research_plan)
                                messages.append({"role": "assistant", "content": blocks})
                                messages.append({"role": "user", "content": self.final_feedback(text)})
                                continue
                            self.model_used = True
                            return self.final_text(text)
                        if not empty_synthesis_retry and self.models < MAX_MODELS and self.research_remaining() > 0:
                            empty_synthesis_retry = self.final_checked = True
                            if blocks:
                                messages.append({"role": "assistant", "content": blocks})
                            messages.append({"role": "user", "content": self.final_feedback("") + "\nThe previous response was empty. Produce the final supported answer now with tools disabled; if evidence is insufficient, give specific gaps and a verification step."})
                            continue
                        self.notes.append("Model returned an empty final response; no synthesized answer is available.")
                        break
                    if final_window:
                        self.notes.append("No synthesized final response was returned in the reserved time.")
                        break
                    messages.append({"role": "assistant", "content": blocks})
                    results = []
                    for call in calls:
                        result = self.execute(call["name"], call["input"] if isinstance(call["input"], dict) else {})
                        if result.get("status") == "clarification":
                            self.model_used = True
                            return result["question"]
                        results.append({"type": "tool_result", "tool_use_id": call["id"],
                            "content": json.dumps(self.model_data(result), ensure_ascii=False)})
                    messages.append({"role": "user", "content": results})
                    continue
                if getattr(response.choices[0], "finish_reason", None) == "length":
                    self.notes.append("Model output budget reached; this answer is partial.")
                message = response.choices[0].message
                calls = message.tool_calls or []
                if not calls:
                    if message.content:
                        if available_schemas and not self.final_checked and self.models < MAX_MODELS and self.research_remaining() > 0:
                            self.final_checked = bool(self.research_plan)
                            messages.append({"role": "assistant", "content": message.content})
                            messages.append({"role": "user", "content": self.final_feedback(message.content)})
                            continue
                        self.model_used = True
                        return self.final_text(message.content)
                    if not empty_synthesis_retry and self.models < MAX_MODELS and self.research_remaining() > 0:
                        empty_synthesis_retry = self.final_checked = True
                        messages.append({"role": "assistant", "content": message.content or ""})
                        messages.append({"role": "user", "content": self.final_feedback("") + "\nThe previous response was empty. Produce the final supported answer now with tools disabled; if evidence is insufficient, give specific gaps and a verification step."})
                        continue
                    self.notes.append("Model returned an empty final response; no synthesized answer is available.")
                    break
                if final_window:
                    self.notes.append("No synthesized final response was returned in the reserved time.")
                    break
                messages.append(message.model_dump(exclude_none=True))
                for call in calls:
                    try:
                        arguments = json.loads(call.function.arguments)
                        result = self.execute(call.function.name, arguments if isinstance(arguments, dict) else {})
                    except (ValueError, TypeError):
                        result = {"status": "invalid", "reason": "Invalid tool arguments."}
                    if result.get("status") == "clarification":
                        self.model_used = True
                        return result["question"]
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(self.model_data(result), ensure_ascii=False)})
            self.notes.append("Model/tool budget exhausted; returning the confirmed evidence extract.") if self.models >= MAX_MODELS or not self.remaining() else None
        except Exception as exc:
            self.notes.append(f"Model unavailable ({type(exc).__name__}); returning the confirmed evidence extract.")
        self.notes.append("Manual verification: open the official source URL, check the requested facts, and ask again with its exact URL, object name or a quoted passage.")
        self.generation_failed = True
        return None

    def final_feedback(self, draft):
        indices = {int(index) for group in re.findall(r"\[([\d,\s–-]+)\]", draft) for index in re.findall(r"\d+", group)}
        registered = sorted(source["index"] for source in self.sources)
        facts = {"original_question": self.question, "registered_citations": registered,
            "canonical_citations": self.citation_catalog(), "research_plan": self.research_plan,
            "research_handoff": self.research_handoff(),
            "unallocated_citation_tokens": [key for key in re.findall(r"\[\[cite:([^\]\r\n]+)\]\]", draft) if key not in {source.get("evidence_id") for source in self.sources}],
            "unallocated_citations": sorted(indices - set(registered)),
            "noncanonical_citations": re.findall(r"\[[^\]\n]*(?:index\s+\d+|\d[–-]\d)[^\]\n]*\]", draft),
            "read_page_citations": [entry["source"]["index"] for entry in self.evidence if entry["kind"] == "web"],
            "remaining_budget": {"model_requests": MAX_MODELS - self.models, "tools": MAX_TOOLS - self.calls,
                "web_searches": 2 - self.searches,
                "url_reads": MAX_READS - self.reads, "evidence_chars": MAX_TEXT - self.model_chars, "seconds": round(self.remaining(), 1), "research_seconds": round(self.research_remaining(), 1), "final_reserve_seconds": FINAL_RESERVE_SECONDS}}
        facts.update(self.date_context())
        return ("Evidence sufficiency check — treat the preceding answer as a draft. Check the original subject and all requested parts, freshness, conflicts and specific support for each citation. "
            "If relevant facts are missing, a cited page is unread, or a conflict is unresolved and tools remain, choose the needed tools now; otherwise give a concise self-contained final answer. "
            "When tools are exhausted, rewrite using the evidence already available; if the requested subject lacks support, state that gap instead of substituting unrelated topics. "
            "Discovery previews are not read page facts; coverage metadata and honest read failures may be explained without page citations. "
            "Approved excerpts registered from Wiki or Registry summaries are valid citations for those approved summaries and clearly labelled implications for insurance; read_page_citations identifies current webpage verification, not a citation whitelist. "
            "Keep supported analysis of the implications the user requested. Do not mention internal draft checks, corrections to a draft, earlier citation mistakes or a corrected answer. "
            "Complete the research_state checklist with finish, or plan it first if absent. Use the exact registered citation_token bound to each evidence ID; never renumber by narrative order or build a second numbered Sources list. The UI already provides the canonical sources. Coverage metadata needs no page citation. Only when the user asks for added/material-update history, preserve the exact supplied knowledge window: publication/update dates cannot substitute for approved ingestion/material-update history. "
            "For ordinary recent/latest topics, assess source publication/update facts without inventing an ingestion cutoff or adding an unrequested knowledge-history section. "
            "Only for requested event or participation facts, keep distinct deadlines and require read official bodies for current open status; otherwise explain that current status is unverified. Do not add an event section to unrelated topics. "
            "Prefer a few relevant findings over unrelated summaries; do not ask a new committee-scope question when the original question already identifies its subject. "
            "For absent requested facts state the gap and a concrete verification step.\n" + json.dumps(facts, ensure_ascii=False))

    def final_text(self, text):
        text = re.sub(r"cite([^\r\n]+)",
            lambda match: "".join(key if re.fullmatch(r"\[\[cite:[^\]\r\n]+\]\]", key) else "[[cite:" + key + "]]"
                for key in (value.strip() for value in match[1].split(""))), text)
        invalid_citations = False
        token_map = {source["citation_token"]: source["citation_label"] for source in self.sources if source.get("citation_token")}
        for token, label in token_map.items():
            text = text.replace(token, label)
        if re.search(r"\[\[cite:[^\r\n]*?\]\]", text):
            invalid_citations = True
            text = re.sub(r"\[\[cite:[^\r\n]*?\]\]", "", text)
        def citation(match):
            nonlocal invalid_citations
            key, number = match.group(1), int(match.group(2))
            source = next((source for source in self.sources if source["index"] == number), None)
            if source and source.get("evidence_id") == key:
                return f"[{number}]"
            invalid_citations = True
            return ""
        text = re.sub(r"\[([^\[\],]+),\s*index\s+(\d+)\]", citation, text)
        allocated = {source["index"] for source in self.sources}
        def allocated_citation(match):
            nonlocal invalid_citations
            indices = [int(index) for index in re.findall(r"\d+", match[1])]
            valid = [index for index in indices if index in allocated]
            invalid_citations |= len(valid) != len(indices)
            return "[" + ", ".join(map(str, valid)) + "]" if valid else ""
        text = re.sub(r"\[([\d,\s]+)\]", allocated_citation, text)
        if invalid_citations:
            text += "\n\nCitation limitation: some requested citations had no registered evidence; discovery previews do not verify page facts."
        if KNOWLEDGE_CHANGE.search(self.question) and any("No confirmed matches in this knowledge-time window" in note for note in self.notes) and "No confirmed matches" not in text:
            text += "\n\nNo confirmed matches in the requested added/material-update window; undated approved items cannot establish newness."
        if any("Model output budget reached" in note for note in self.notes):
            text += "\n\nModel output budget reached; this answer is partial."
        if any(source.get("truncated") for source in self.sources) and not re.search(r"\b(truncat\w*|reading window|partial|incomplete)\b", text, re.I):
            text += "\n\nReading window limitation: some supplied evidence is partial; omitted text may contain additional findings, dates or actions."
        current_meeting = refresh_requested(self.question) and any(entry["kind"] == "meeting" for entry in self.evidence)
        if not any(entry["kind"] == "web" for entry in self.evidence) and (current_meeting or any("Fresh current status could not be verified" in note for note in self.notes)) and not re.search(r"(?:could not|cannot|not)\s+(?:be\s+)?verif", text, re.I):
            text += "\n\nFresh current status could not be verified; stored status is not current verification."
        if self.model_loop and self.research_finish is None:
            text += "\n\nThis answer is partial; some requested facts remain unverified."
        return text

    def extractive(self, compact=False):
        if compact:
            lines = ["No synthesized final answer was returned; this response is incomplete."]
            lines.extend(f"[{entry['index']}] {entry['source']['title']}: {web_excerpt(entry['text'], self.question, 300)}" for entry in self.evidence[:3])
            notes = list(dict.fromkeys(self.notes))
            failures = [note for note in notes if note.startswith("Model unavailable (")]
            lines.extend([*list(dict.fromkeys([*failures, *notes[-2:]]))[:3], verification_guidance(self.question, self.sources)])
            return "\n\n".join(lines)
        lines = [f"As of {self.now.astimezone(ZONE).isoformat()} (America/New_York)."]
        if self.source_only:
            lines.append("Source-only mode: approved records and evidence extracts; no autonomous live search.")
        for entry in self.evidence:
            text = entry["text"]
            if entry["kind"] == "web" and URLS.fullmatch(self.question.strip()):
                # Reserve room for the reading-limit note and manual verification in a concise overview.
                text = web_excerpt(text, "key finding dates deadline registration action conclusion recommendation", 1200)
                lines.append("Source-only overview: selected passages from the retained page evidence.")
                if len(text) < len(entry["text"]):
                    if not entry["source"]["truncated"]:
                        self.notes.append("Reading window limitation: this overview shows selected passages; additional page evidence is retained for follow-up questions.")
                    entry["source"]["truncated"] = True
            lines.extend([f"\n{entry['index']}. **{entry['source']['title']}** [{entry['index']}]", text])
        lines.extend(["", *dict.fromkeys(self.notes)])
        if not self.evidence and not self.clarification:
            lines.append("No usable evidence was available for this request. Missing records are not proof of absence.")
            lines.append(verification_guidance(self.question, self.sources))
        return "\n".join(lines)
