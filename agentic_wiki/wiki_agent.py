from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - openai is optional for offline demo mode.
    OpenAI = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WIKI_DIR = REPO_ROOT / "wiki"
DEFAULT_SOURCE_DIR = REPO_ROOT / "sources"
MAX_HISTORY_CHARS = 6000
MAX_EVIDENCE_CHARS_BRIEF = 9000
MAX_EVIDENCE_CHARS_DETAILED = 18000
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+:/-]*|[\u4e00-\u9fff]+", re.IGNORECASE)
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
URL_RE = re.compile(r"https?://[^\s)>\]]+")
SOURCE_ITEM_RE = re.compile(r"^(?:→\s*)?\*\*(.+?)\*\*\s*$")
DAY_RANGE_RE = re.compile(r"(?:past|last|recent)\s+(\d{1,2})\s+(?:day|days)", re.IGNORECASE)
ZH_DAY_RANGE_RE = re.compile(r"(?:过去|最近|近)\s*(\d{1,2})\s*(?:天|日)")
ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "what",
    "with",
}

QUERY_ALIASES = {
    "latest": "latest 2026-04-20 climate monitor update current summary",
    "today": "2026-04-20 latest daily report climate monitor",
    "secondary perils": "secondary perils severe convective storms wildfire flood nat cat losses",
    "nat cat": "natural catastrophe reinsurance catastrophe losses protection gap",
    "natural catastrophe": "natural catastrophe reinsurance catastrophe losses protection gap",
    "protection gap": "nat cat protection gap uninsured losses sovereign balance sheets",
    "parametric": "parametric insurance index triggered products protection gap",
    "actuarial": "actuaries actuarial climate risk modelling reserving pricing",
    "climate risk": "climate risk physical risk transition risk liability risk",
    "disclosure": "IFRS S2 ISSB TCFD TNFD disclosure reporting standards",
    "supervision": "IAIS FSB EIOPA climate risk supervision framework",
    "solvency": "solvency insurance regulation capital risk management",
    "reinsurance": "Swiss Re sigma reinsurance natural catastrophe losses",
    "talent": "talent gap climate analytics skills shortage insurance",
    "colombia": "WRI Colombia water energy food nexus energy communities",
}

CONCEPT_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Actuaries Climate Index", "topic", (r"\bactuaries climate index\b", r"\baci\b")),
    ("Adaptation Finance", "topic", (r"\badaptation finance\b", r"\badaptation fund\b")),
    ("Aon", "organization", (r"\baon\b",)),
    ("ARC", "organization", (r"\barc\b", r"\bafrican risk capacity\b")),
    ("CAS / SOA", "organization", (r"\bcas\b", r"\bsoa\b")),
    ("Climate Risk", "topic", (r"\bclimate risk\b", r"\bphysical risk\b", r"\btransition risk\b")),
    ("Colombia", "geography", (r"\bcolombia\b",)),
    ("CSRD / ESRS", "framework", (r"\bcsrd\b", r"\besrs\b")),
    ("EIOPA", "organization", (r"\beiopa\b",)),
    ("EU", "geography", (r"\beu\b", r"\beuropean union\b", r"\beuropean\b")),
    ("FSB", "organization", (r"\bfsb\b",)),
    ("IAIS", "organization", (r"\biais\b",)),
    ("IFRS S2", "framework", (r"\bifrs s2\b", r"\bissb\b", r"\bifrs\b")),
    ("IPCC", "organization", (r"\bipcc\b", r"\bar7\b")),
    ("ISO 14091", "framework", (r"\biso 14091\b",)),
    ("Madagascar", "geography", (r"\bmadagascar\b",)),
    ("Nat-Cat Protection Gap", "topic", (r"\bprotection gap\b", r"\bunder-?insurance\b")),
    ("NFIP", "program", (r"\bnfip\b", r"\bnational flood insurance program\b")),
    ("Parametric Insurance", "topic", (r"\bparametric insurance\b", r"\bparametric products?\b")),
    ("Secondary Perils", "topic", (r"\bsecondary perils?\b", r"\bsevere convective storms?\b")),
    ("Swiss Re", "organization", (r"\bswiss re\b", r"\bsigma\b")),
    ("Talent Gap", "topic", (r"\btalent gap\b", r"\bskills shortage\b")),
    ("TCFD", "framework", (r"\btcfd\b",)),
    ("TNFD", "framework", (r"\btnfd\b",)),
    ("US", "geography", (r"\bu\.?s\.?\b", r"\bunited states\b")),
    ("Weather Derivatives", "topic", (r"\bweather derivatives?\b", r"\bcdd/hdd\b", r"\bcme\b")),
    ("WRI", "organization", (r"\bwri\b", r"\bworld resources institute\b")),
)

CONCEPT_HEADING_SKIP = {
    "Actuarial Relevant Research",
    "Actuarial-Relevant Research",
    "All Source Links",
    "Daily Climate Actuarial Monitor",
    "Executive Summary",
    "Generated",
    "Key Facts",
    "Overview",
    "Part 1",
    "Part 2",
    "Related",
    "Report Date",
    "Sites Monitored",
    "Source Links",
    "Sources",
    "Summary",
    "Summary Statistics",
    "Tags",
    "Website Updates",
}
CONCEPT_HEADING_SKIP_KEYS = {item.casefold() for item in CONCEPT_HEADING_SKIP}

CONCEPT_TOKEN_CASE = {
    "aci": "ACI",
    "aon": "Aon",
    "arc": "ARC",
    "cas": "CAS",
    "csrd": "CSRD",
    "eiopa": "EIOPA",
    "esrs": "ESRS",
    "eu": "EU",
    "fsb": "FSB",
    "iais": "IAIS",
    "ifrs": "IFRS",
    "ipcc": "IPCC",
    "iso": "ISO",
    "issb": "ISSB",
    "nat": "Nat",
    "nfip": "NFIP",
    "soa": "SOA",
    "tcfd": "TCFD",
    "tnfd": "TNFD",
    "us": "US",
    "wri": "WRI",
}

BOILERPLATE_HEADINGS = {
    "all source links",
    "source links",
    "summary statistics",
    "tags",
}

CorpusType = Literal["wiki", "source"]
AnswerMode = Literal["brief", "detailed"]
VALID_ANSWER_MODES = {"brief", "detailed"}


def _title_from_file(path: Path) -> str:
    return path.stem


def _context_title_from_path(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).stem


def _detect_type(title: str) -> str:
    if title == "index":
        return "index"
    if re.fullmatch(r"climate-monitor-\d{4}-\d{2}-\d{2}", title):
        return "daily"
    return "topic"


def _extract_date(title: str, markdown: str) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
    if match:
        return match.group(1)
    match = re.search(r"Updated\D+(\d{4}-\d{2}-\d{2})", markdown, re.IGNORECASE)
    return match.group(1) if match else "-"


def _normalize_text(text: str) -> str:
    return (
        text.replace("\r\n", "\n")
        .replace("\u922b\ufffd", "->")
        .replace("\u9225\ufffd", "-")
        .replace("\u951f\ufffd", "")
        .replace("â†’", "->")
        .replace("â€”", "-")
        .replace("â€“", "-")
        .replace("â€œ", '"')
        .replace("â€\x9d", '"')
        .replace("â€˜", "'")
        .replace("â€™", "'")
        .replace("�", "")
    )


def _strip_markdown(text: str) -> str:
    cleaned = re.sub(r"```[\s\S]*?```", " ", text)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]", r"\2 \1", cleaned)
    cleaned = re.sub(r"[#>*_`|]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _tokens(text: str) -> list[str]:
    values: list[str] = []
    for match in TOKEN_RE.finditer(text.lower()):
        token = match.group(0).strip("-_.,;:()[]{}")
        if len(token) < 2 or token in STOPWORDS:
            continue
        values.append(token)
    return values


def _expand_query(query: str) -> str:
    expanded = [query]
    lower = query.lower()
    for key, value in QUERY_ALIASES.items():
        if key in query or key.lower() in lower:
            expanded.append(value)
    if "ifrs" in lower or "issb" in lower:
        expanded.append("IFRS S2 ISSB scope 3 financed emissions climate disclosure")
    if "iais" in lower:
        expanded.append("IAIS climate risk holistic framework climada protection gaps")
    if "fsb" in lower:
        expanded.append("FSB climate roadmap vulnerability analysis financial stability")
    if "swiss" in lower or "sigma" in lower:
        expanded.append("Swiss Re sigma nat cat insured losses forecast")
    if "aci" in lower:
        expanded.append("Actuaries Climate Index ACI weather derivatives extremes")
    if any(term in lower for term in ["detail", "detailed", "evidence", "raw", "source"]):
        expanded.append("primary source detailed evidence figures dates")
    return " ".join(expanded)


def _shorten(text: str, limit: int = 900) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit].rstrip()}..."


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _date_range_days(question: str) -> int | None:
    lower = question.lower()
    if any(term in lower for term in ["last week", "past week", "recent week"]):
        return 7
    if any(term in question for term in ["过去一周", "最近一周", "近一周", "过去7天", "最近7天", "近7天"]):
        return 7

    match = DAY_RANGE_RE.search(question)
    if match:
        return max(1, min(int(match.group(1)), 30))

    match = ZH_DAY_RANGE_RE.search(question)
    if match:
        return max(1, min(int(match.group(1)), 30))

    return None


def _window_dates(question: str, latest_date: str) -> list[str]:
    days = _date_range_days(question)
    latest = _parse_iso_date(latest_date)
    if not days or latest is None:
        return []
    start = latest - timedelta(days=days - 1)
    return [(start + timedelta(days=offset)).isoformat() for offset in range(days)]


def _requested_dates(question: str, latest_date: str = "") -> list[str]:
    explicit = list(dict.fromkeys(ISO_DATE_RE.findall(question)))
    window = _window_dates(question, latest_date)
    return explicit or window


def _heading_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _is_boilerplate_heading(value: str) -> bool:
    return _heading_key(value) in BOILERPLATE_HEADINGS


def _asks_daily_summary(question: str) -> bool:
    lower = question.lower()
    return any(
        term in lower
        for term in [
            "daily report",
            "daily reports",
            "day by day",
            "past week",
            "last week",
            "past 7 days",
            "last 7 days",
            "recent 7 days",
            "summarize",
            "summary",
        ]
    ) or any(term in question for term in ["日报", "最近7天", "过去7天", "最近一周", "过去一周", "总结", "汇总"])


def _display_title(title: str) -> str:
    parts = [part for part in title.replace("_", "-").split("-") if part]
    if not parts:
        return title
    return " ".join(CONCEPT_TOKEN_CASE.get(part.lower(), part.title()) for part in parts)


def _clean_concept_candidate(text: str) -> str:
    cleaned = re.sub(r"^[#>*+\-\s]+", "", text).strip()
    cleaned = re.sub(r"[^\w\s/&:+.-]", " ", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:/")
    return cleaned


def _guess_concept_type(label: str) -> str:
    upper = label.upper()
    lower = label.lower()
    if label in {"EU", "US", "Colombia", "Madagascar"}:
        return "geography"
    if any(token in upper for token in ["IFRS", "TCFD", "TNFD", "ISO", "CSRD", "ESRS"]):
        return "framework"
    if any(token in lower for token in ["insurance", "risk", "perils", "gap", "finance", "derivatives"]):
        return "topic"
    if re.fullmatch(r"[A-Z0-9 /.&+-]{2,24}", label):
        return "organization"
    return "topic"


def _canonicalize_candidate(candidate: str) -> tuple[str, str] | None:
    cleaned = _clean_concept_candidate(candidate)
    if not cleaned:
        return None

    skip_key = cleaned.replace("-", " ").strip()
    if skip_key.casefold() in CONCEPT_HEADING_SKIP_KEYS or cleaned.casefold() in CONCEPT_HEADING_SKIP_KEYS:
        return None

    normalized = cleaned.casefold()
    for label, concept_type, patterns in CONCEPT_RULES:
        if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in patterns):
            return (label, concept_type)

    words = cleaned.split()
    if len(words) > 5 or re.fullmatch(r"[\d\s./-]+", cleaned):
        return None
    if re.search(r"\b20\d{2}\b", cleaned) and len(words) > 3:
        return None

    if ":" in cleaned:
        prefix = cleaned.split(":", 1)[0].strip()
        if prefix and prefix != cleaned:
            return _canonicalize_candidate(prefix)
    if " — " in cleaned:
        prefix = cleaned.split(" — ", 1)[0].strip()
        if prefix and prefix != cleaned:
            return _canonicalize_candidate(prefix)
    if " - " in cleaned:
        prefix = cleaned.split(" - ", 1)[0].strip()
        if prefix and prefix != cleaned:
            return _canonicalize_candidate(prefix)

    if " and " in cleaned and re.fullmatch(r"[A-Za-z0-9 /.+-]+", cleaned):
        parts = [part.strip() for part in cleaned.split(" and ") if part.strip()]
        if len(parts) == 2 and all(len(part.split()) <= 2 for part in parts):
            return None

    canonical = " ".join(CONCEPT_TOKEN_CASE.get(word.lower(), word.title()) for word in words)
    return (canonical, _guess_concept_type(canonical))


def _heading_candidates(markdown: str) -> list[str]:
    candidates: list[str] = []

    for match in re.finditer(r"^(#{2,4})\s+(.+?)\s*$", markdown, re.MULTILINE):
        candidates.append(match.group(2).strip())

    for line in markdown.splitlines():
        bold_only = SOURCE_ITEM_RE.match(line.strip())
        if bold_only:
            candidates.append(bold_only.group(1).strip())
            continue

        bullet_bold = re.match(r"^\s*[-*+]\s+\*\*([^*\n]+)\*\*", line)
        if bullet_bold:
            candidates.append(bullet_bold.group(1).strip())

    return candidates


def _extract_concepts(title: str, markdown: str, *, include_title: bool = False) -> set[tuple[str, str]]:
    matches: set[tuple[str, str]] = set()
    text = _strip_markdown(f"{title}\n{markdown}")

    for label, concept_type, patterns in CONCEPT_RULES:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            matches.add((label, concept_type))

    for candidate in _heading_candidates(markdown):
        canonical = _canonicalize_candidate(candidate)
        if canonical is not None:
            label, concept_type = canonical
            if label.casefold() not in CONCEPT_HEADING_SKIP_KEYS:
                matches.add((label, concept_type))

    if include_title and _detect_type(title) == "topic" and title not in {"index", "log"}:
        matches.add((_display_title(title), "topic"))

    return matches


def _safe_json_loads(value: str) -> Any:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if match:
        text = match.group(1)
    return json.loads(text)


@dataclass
class WikiDocument:
    title: str
    path: str
    file: str
    type: str
    date: str
    markdown: str
    text: str
    links: list[str]
    words: int
    corpus: CorpusType


@dataclass
class WikiChunk:
    id: str
    title: str
    path: str
    heading: str
    type: str
    date: str
    text: str
    markdown: str
    links: list[str]
    corpus: CorpusType
    tokens: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


@dataclass
class SearchHit:
    chunk: WikiChunk
    score: float
    reason: str

    def to_source(self, index: int, base_url: str = "") -> dict[str, Any]:
        url = f"{base_url}/{self.chunk.path}" if base_url else self.chunk.path
        return {
            "index": index,
            "title": self.chunk.title,
            "path": self.chunk.path,
            "heading": self.chunk.heading,
            "url": url,
            "score": round(self.score, 3),
            "snippet": _shorten(self.chunk.text, 420),
            "source_urls": self.chunk.urls[:4],
            "type": self.chunk.type,
            "date": self.chunk.date,
            "corpus": self.chunk.corpus,
        }


class WikiKnowledgeBase:
    def __init__(
        self,
        wiki_dir: Path = DEFAULT_WIKI_DIR,
        source_dir: Path = DEFAULT_SOURCE_DIR,
    ) -> None:
        self.wiki_dir = wiki_dir
        self.source_dir = source_dir
        self.documents: list[WikiDocument] = []
        self.source_documents: list[WikiDocument] = []
        self.source_documents_by_title: dict[str, WikiDocument] = {}
        self.chunks: list[WikiChunk] = []
        self.document_concepts: dict[str, list[dict[str, str]]] = {}
        self.concepts: list[dict[str, Any]] = []
        self.latest_date = ""
        self._load()

    def _load(self) -> None:
        if not self.wiki_dir.exists():
            raise FileNotFoundError(f"Wiki directory not found: {self.wiki_dir}")

        wiki_docs, wiki_chunks = self._load_directory(self.wiki_dir, "wiki")
        source_docs, source_chunks = self._load_directory(self.source_dir, "source")

        self.documents = wiki_docs
        self.source_documents = source_docs
        self.source_documents_by_title = {doc.title: doc for doc in source_docs}
        self.chunks = wiki_chunks + source_chunks
        self.document_concepts, self.concepts = self._build_concept_index()
        self.latest_date = max(
            (
                doc.date
                for doc in [*wiki_docs, *source_docs]
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc.date)
            ),
            default="",
        )

    def _load_directory(
        self,
        directory: Path,
        corpus: CorpusType,
    ) -> tuple[list[WikiDocument], list[WikiChunk]]:
        if not directory.exists():
            return [], []

        docs: list[WikiDocument] = []
        chunks: list[WikiChunk] = []
        for path in sorted(directory.glob("*.md")):
            markdown = _normalize_text(path.read_text(encoding="utf-8"))
            title = _title_from_file(path)
            links = []
            if corpus == "wiki":
                links = [
                    item.replace("wiki/", "").replace(".md", "").strip()
                    for item in LINK_RE.findall(markdown)
                ]
            text = _strip_markdown(markdown)
            doc = WikiDocument(
                title=title,
                path=f"{directory.name}/{path.name}",
                file=path.name,
                type=_detect_type(title),
                date=_extract_date(title, markdown),
                markdown=markdown,
                text=text,
                links=links,
                words=len(_tokens(text)),
                corpus=corpus,
            )
            docs.append(doc)
            chunks.extend(self._chunk_document(doc))
        return docs, chunks

    def _split_markdown_sections(self, markdown: str, title: str) -> list[tuple[str, list[str]]]:
        sections: list[tuple[str, list[str]]] = []
        current_heading = title
        current_lines: list[str] = []

        for line in markdown.splitlines():
            heading = re.match(r"^(#{1,4})\s+(.+?)\s*$", line)
            if heading:
                if current_lines:
                    sections.append((current_heading, current_lines))
                current_heading = heading.group(2).strip()
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_lines:
            sections.append((current_heading, current_lines))

        if not sections:
            sections = [(title, [markdown])]
        return sections

    def _split_source_sections(self, doc: WikiDocument) -> list[tuple[str, list[str]]]:
        blocks: list[tuple[str, list[str]]] = []

        for section_heading, lines in self._split_markdown_sections(doc.markdown, doc.title):
            current_heading = section_heading
            current_lines: list[str] = []

            for line in lines:
                item_heading = SOURCE_ITEM_RE.match(line.strip())
                if item_heading:
                    if current_lines:
                        blocks.append((current_heading, current_lines))
                    current_heading = item_heading.group(1).strip()
                    current_lines = [line]
                    continue
                current_lines.append(line)

            if current_lines:
                blocks.append((current_heading, current_lines))

        return blocks

    def _build_chunk(
        self,
        doc: WikiDocument,
        heading: str,
        lines: list[str],
        index: int,
    ) -> WikiChunk | None:
        markdown = "\n".join(lines).strip()
        text = _strip_markdown(markdown)
        if not text:
            return None

        return WikiChunk(
            id=f"{doc.file}:{doc.corpus}:{index}",
            title=doc.title,
            path=doc.path,
            heading=heading,
            type=doc.type,
            date=doc.date,
            text=text,
            markdown=markdown,
            links=doc.links,
            corpus=doc.corpus,
            tokens=_tokens(f"{doc.title} {heading} {text}"),
            urls=URL_RE.findall(markdown),
        )

    def _chunk_document(self, doc: WikiDocument) -> list[WikiChunk]:
        sections = (
            self._split_source_sections(doc)
            if doc.corpus == "source"
            else self._split_markdown_sections(doc.markdown, doc.title)
        )

        chunks: list[WikiChunk] = []
        for index, (heading, lines) in enumerate(sections, start=1):
            chunk = self._build_chunk(doc, heading, lines, index)
            if chunk is not None:
                chunks.append(chunk)
        return chunks

    def reload(self) -> None:
        self._load()

    def _build_concept_index(self) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, Any]]]:
        source_by_title = {doc.title: doc for doc in self.source_documents}
        concept_index: dict[str, dict[str, Any]] = {}
        document_concepts: dict[str, list[dict[str, str]]] = {}

        for doc in self.documents:
            doc_matches = _extract_concepts(doc.title, doc.markdown, include_title=True)
            source_matches: set[tuple[str, str]] = set()
            source_matches_by_title: dict[str, set[tuple[str, str]]] = {}

            source_titles: set[str] = set()
            if doc.type == "daily" and doc.title in source_by_title:
                source_titles.add(doc.title)
            if doc.type == "topic":
                source_titles.update(link for link in doc.links if link in source_by_title)

            for source_title in source_titles:
                source_doc = source_by_title[source_title]
                matched = _extract_concepts(source_doc.title, source_doc.markdown)
                source_matches_by_title[source_title] = matched
                source_matches.update(matched)

            combined = sorted(doc_matches | source_matches, key=lambda item: (item[1], item[0].casefold()))
            document_concepts[doc.path] = [
                {
                    "label": label,
                    "type": concept_type,
                }
                for label, concept_type in combined
            ]

            for label, concept_type in doc_matches | source_matches:
                bucket = concept_index.setdefault(
                    label,
                    {
                        "id": re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-"),
                        "label": label,
                        "type": concept_type,
                        "documents": set(),
                        "source_documents": set(),
                    },
                )
                bucket["documents"].add(doc.path)
                for source_title, matched in source_matches_by_title.items():
                    source_doc = source_by_title[source_title]
                    if (label, concept_type) in matched:
                        bucket["source_documents"].add(source_doc.path)

        concepts = sorted(
            (
                {
                    "id": item["id"],
                    "label": item["label"],
                    "type": item["type"],
                    "documents": sorted(item["documents"]),
                    "source_documents": sorted(item["source_documents"]),
                    "document_count": len(item["documents"]),
                    "source_document_count": len(item["source_documents"]),
                }
                for item in concept_index.values()
                if item["documents"]
            ),
            key=lambda item: (-item["document_count"], item["label"].casefold()),
        )
        return document_concepts, concepts

    def stats(self) -> dict[str, Any]:
        return {
            "documents": len(self.documents),
            "source_documents": len(self.source_documents),
            "retrieval_documents": len(self.documents) + len(self.source_documents),
            "chunks": len(self.chunks),
            "concepts": len(self.concepts),
            "wiki_words": sum(doc.words for doc in self.documents),
            "source_words": sum(doc.words for doc in self.source_documents),
            "words": sum(doc.words for doc in self.documents + self.source_documents),
        }

    def document_catalog(self, github_blob_base_url: str = "") -> list[dict[str, Any]]:
        return [
            {
                "title": doc.title,
                "path": doc.path,
                "file": doc.file,
                "type": doc.type,
                "date": doc.date,
                "words": doc.words,
                "links": doc.links,
                "concepts": self.document_concepts.get(doc.path, []),
                "source_path": self.source_documents_by_title[doc.title].path
                if doc.type == "daily" and doc.title in self.source_documents_by_title
                else None,
                "source_url": (
                    f"{github_blob_base_url}/{self.source_documents_by_title[doc.title].path}"
                    if github_blob_base_url and doc.type == "daily" and doc.title in self.source_documents_by_title
                    else None
                ),
            }
            for doc in self.documents
        ]

    def concept_catalog(self) -> list[dict[str, Any]]:
        return self.concepts

    def search(
        self,
        query: str,
        *,
        top_k: int = 6,
        context_path: str | None = None,
    ) -> list[SearchHit]:
        expanded = _expand_query(query)
        query_tokens = _tokens(expanded)
        if not query_tokens:
            query_tokens = _tokens(query)
        query_set = set(query_tokens)
        query_lower = expanded.lower()
        requested_dates = _requested_dates(expanded, self.latest_date)
        asks_latest = any(term in query_lower for term in ["latest", "current", "recent"])
        asks_evidence = any(
            term in query_lower
            for term in ["detail", "detailed", "evidence", "quote", "quotes", "raw", "source"]
        )
        asks_daily = _asks_daily_summary(query)
        requested_date_set = set(requested_dates)
        context_path = (context_path or "").lstrip("/")
        context_title = _context_title_from_path(context_path)

        scored: list[SearchHit] = []
        for chunk in self.chunks:
            chunk_set = set(chunk.tokens)
            overlap = query_set & chunk_set
            context_match = bool(context_path and chunk.path == context_path)
            raw_context_match = bool(
                context_title and chunk.corpus == "source" and chunk.title == context_title
            )
            date_match = bool(
                requested_date_set
                and (chunk.date in requested_date_set or any(day in chunk.title for day in requested_date_set))
            )
            if (
                not overlap
                and query_lower not in chunk.text.lower()
                and not context_match
                and not raw_context_match
                and not date_match
            ):
                continue

            score = 0.0
            reason_parts: list[str] = []
            for token in overlap:
                token_weight = 1.0 + math.log(1 + chunk.tokens.count(token))
                if token in chunk.title.lower() or token in chunk.heading.lower():
                    token_weight += 1.8
                score += token_weight

            if overlap:
                reason_parts.append(f"matched {', '.join(sorted(list(overlap))[:5])}")

            title_lower = chunk.title.lower().replace("-", " ")
            heading_lower = chunk.heading.lower()
            for phrase in {query.strip().lower(), query_lower.strip()}:
                if phrase and len(phrase) > 4:
                    if phrase in chunk.text.lower():
                        score += 5.0
                        reason_parts.append("exact phrase")
                    if phrase in title_lower or phrase in heading_lower:
                        score += 4.0
                        reason_parts.append("title phrase")

            if context_match:
                score += 10.0
                reason_parts.append("active Obsidian note")
            elif raw_context_match:
                score += 7.0
                reason_parts.append("raw source for active note")

            for requested_date in requested_dates:
                if requested_date in chunk.title:
                    score += 6.0
                    reason_parts.append("requested date in title")
                elif requested_date == chunk.date:
                    score += 3.0
                    reason_parts.append("requested date")

            if requested_date_set and chunk.date and chunk.date not in requested_date_set:
                if chunk.type == "daily" or chunk.corpus == "source":
                    score -= 4.5

            if asks_daily and chunk.type == "daily":
                score += 2.5
                reason_parts.append("daily report intent")

            if asks_daily and _heading_key(chunk.heading) in {"summary", "executive summary"}:
                score += 3.5
                reason_parts.append("summary section")

            if asks_latest and self.latest_date:
                if self.latest_date in chunk.title:
                    score += 6.0
                    reason_parts.append("latest dated report")
                elif chunk.date == self.latest_date:
                    score += 3.0
                    reason_parts.append("latest dated page")

            if asks_evidence and chunk.corpus == "source":
                score += 2.0
                reason_parts.append("raw evidence corpus")

            if chunk.corpus == "source":
                score += 0.8
            elif chunk.type == "topic":
                score += 0.4
            elif chunk.type == "daily":
                score += 0.2

            if _is_boilerplate_heading(chunk.heading):
                score -= 8.0

            if chunk.title == "log" and not any(term in query_lower for term in ["log", "history"]):
                score -= 2.0

            if "no climate monitor report" in chunk.text.lower():
                if not any(requested_date in chunk.title for requested_date in requested_dates):
                    score -= 6.0

            if score > 0:
                scored.append(
                    SearchHit(
                        chunk=chunk,
                        score=score,
                        reason="; ".join(reason_parts) or "lexical match",
                    )
                )

        scored.sort(key=lambda item: item.score, reverse=True)

        deduped: list[SearchHit] = []
        seen: set[tuple[str, str]] = set()
        for hit in scored:
            key = (hit.chunk.path, hit.chunk.heading)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(hit)
            if len(deduped) >= top_k:
                break
        return deduped


class AgenticWikiResponder:
    def __init__(
        self,
        wiki_dir: Path = DEFAULT_WIKI_DIR,
        source_dir: Path = DEFAULT_SOURCE_DIR,
    ) -> None:
        self.kb = WikiKnowledgeBase(wiki_dir, source_dir)
        self.model = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
        self.base_source_url = self._github_blob_base_url()
        self.source_document_base_url = self._github_blob_base_url(
            branch=self._github_default_branch()
        ) or self.base_source_url
        self.client = None
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if api_key and OpenAI is not None:
            self.client = OpenAI(api_key=api_key)

    @staticmethod
    def _github_default_branch() -> str:
        try:
            remote_head = subprocess.run(
                ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except Exception:
            return "main"

        if remote_head.startswith("origin/"):
            return remote_head.split("/", 1)[1] or "main"
        return remote_head or "main"

    @staticmethod
    def _github_blob_base_url(branch: str | None = None) -> str:
        try:
            remote = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except Exception:
            return ""

        if branch is None:
            try:
                branch = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=REPO_ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            except Exception:
                branch = ""

        if remote.startswith("git@github.com:"):
            remote = remote.replace("git@github.com:", "https://github.com/")
        if remote.endswith(".git"):
            remote = remote[:-4]
        if remote.startswith("https://github.com/"):
            return f"{remote}/blob/{branch or 'main'}"
        return ""

    def config(self) -> dict[str, Any]:
        return {
            "agent_mode": "openai" if self.client else "offline",
            "model": self.model if self.client else "offline-extractive",
            "wiki": self.kb.stats(),
            "documents": self.kb.document_catalog(self.source_document_base_url),
            "concepts": self.kb.concept_catalog(),
            "github_blob_base_url": self.base_source_url,
            "answer_modes": ["brief", "detailed"],
            "default_answer_mode": "detailed",
            "retrieval_corpora": {
                "wiki_documents": len(self.kb.documents),
                "source_documents": len(self.kb.source_documents),
            },
            "obsidian_plugin": {
                "id": "climate-agent-chat",
                "default_server_url": "http://localhost:8501",
            },
        }

    def answer(
        self,
        question: str,
        *,
        history: list[dict[str, str]] | None = None,
        context_path: str | None = None,
        language: str = "en",
        answer_mode: AnswerMode = "detailed",
    ) -> dict[str, Any]:
        question = question.strip()
        if not question:
            raise ValueError("Question is empty.")
        if answer_mode not in VALID_ANSWER_MODES:
            raise ValueError(f"Unsupported answer mode: {answer_mode}")

        planned_queries = self._plan_queries(question, history or [], language, answer_mode)
        requested_dates = _requested_dates(question, self.kb.latest_date)
        hits: list[SearchHit] = []
        retrieval_log: list[dict[str, Any]] = []
        seen_chunks: set[str] = set()
        initial_top_k = 6 if answer_mode == "detailed" else 5
        if requested_dates:
            initial_top_k = min(20, max(initial_top_k, len(requested_dates) * 2))

        for query in planned_queries:
            round_hits = self.kb.search(query, top_k=initial_top_k, context_path=context_path)
            new_hits = []
            for hit in round_hits:
                if hit.chunk.id in seen_chunks:
                    continue
                seen_chunks.add(hit.chunk.id)
                hits.append(hit)
                new_hits.append(hit)
            retrieval_log.append(
                {
                    "query": query,
                    "hits": [
                        {
                            "path": hit.chunk.path,
                            "heading": hit.chunk.heading,
                            "score": round(hit.score, 3),
                            "corpus": hit.chunk.corpus,
                        }
                        for hit in new_hits
                    ],
                }
            )

        reflection = self._reflect(question, hits, answer_mode)
        if reflection["additional_queries"]:
            for query in reflection["additional_queries"]:
                follow_up_top_k = 5
                if requested_dates:
                    follow_up_top_k = min(18, max(follow_up_top_k, len(requested_dates) * 2 - 1))
                round_hits = self.kb.search(query, top_k=follow_up_top_k, context_path=context_path)
                new_hits = []
                for hit in round_hits:
                    if hit.chunk.id in seen_chunks:
                        continue
                    seen_chunks.add(hit.chunk.id)
                    hits.append(hit)
                    new_hits.append(hit)
                retrieval_log.append(
                    {
                        "query": query,
                        "hits": [
                            {
                                "path": hit.chunk.path,
                                "heading": hit.chunk.heading,
                                "score": round(hit.score, 3),
                                "corpus": hit.chunk.corpus,
                            }
                            for hit in new_hits
                        ],
                    }
                )

        ranked_hits = self._rank_for_answer(
            question,
            hits,
            context_path=context_path,
            answer_mode=answer_mode,
        )
        max_sources = 10 if answer_mode == "detailed" else 8
        if requested_dates:
            max_sources = max(max_sources, min(14, len(requested_dates) + 4))
        sources = [
            hit.to_source(index, self.base_source_url)
            for index, hit in enumerate(ranked_hits[:max_sources], start=1)
        ]
        text = self._synthesize(question, ranked_hits[:max_sources], history or [], language, answer_mode)

        return {
            "text": text,
            "sources": sources,
            "plan": {
                "sub_queries": planned_queries,
                "reflection": reflection,
                "retrieval_log": retrieval_log,
            },
            "model": self.model if self.client else "offline-extractive",
            "agent_mode": "openai" if self.client else "offline",
            "language": language,
            "answer_mode": answer_mode,
            "retrieval_summary": {
                "wiki_hits": sum(1 for hit in ranked_hits[:max_sources] if hit.chunk.corpus == "wiki"),
                "source_hits": sum(1 for hit in ranked_hits[:max_sources] if hit.chunk.corpus == "source"),
            },
        }

    def _chat(self, messages: list[dict[str, str]], *, temperature: float = 0.0) -> str:
        if self.client is None:
            raise RuntimeError("OpenAI client is not configured.")
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
        )
        return response.choices[0].message.content or ""

    def _plan_queries(
        self,
        question: str,
        history: list[dict[str, str]],
        language: str,
        answer_mode: AnswerMode,
    ) -> list[str]:
        local = self._local_plan(question, answer_mode)
        if self.client is None:
            return local

        history_text = self._render_history(history)
        messages = [
            {
                "role": "system",
                "content": (
                    "You plan retrieval for a grounded Markdown RAG system that has both curated wiki notes "
                    "and raw daily source reports. Return JSON only: {\"sub_queries\": [\"...\"]}. "
                    "Create 2-4 search queries that cover the user's intent and include canonical English domain terms. "
                    "If the user wants detail, include a raw-source-oriented query."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Language: {language}\n"
                    f"Answer mode: {answer_mode}\n"
                    f"Recent conversation:\n{history_text or '(none)'}\n\n"
                    f"Question: {question}\n"
                    "The corpus covers climate risk, natural catastrophe insurance, "
                    "actuarial research, ISSB/IFRS S2, IAIS, FSB, Swiss Re, "
                    "parametric insurance, protection gaps, and raw daily monitoring reports."
                ),
            },
        ]
        try:
            payload = _safe_json_loads(self._chat(messages, temperature=0.0))
            values = payload.get("sub_queries", []) if isinstance(payload, dict) else []
            planned = self._unique_queries([str(item) for item in values] + local, 4)
            return planned or local
        except Exception:
            return local

    def _local_plan(self, question: str, answer_mode: AnswerMode) -> list[str]:
        expanded = _expand_query(question)
        queries = [question, expanded]
        lower = question.lower()
        window_dates = _window_dates(question, self.kb.latest_date)
        if window_dates:
            date_span = " ".join(window_dates)
            queries.append(f"Climate Monitor daily report summaries {date_span}")
            if answer_mode == "detailed":
                queries.append(f"Climate Monitor raw daily reports highlights figures dates {date_span}")
        if any(term in lower for term in ["latest", "current", "recent"]):
            queries.append(f"{self.kb.latest_date} latest Climate Monitor summary")
        if _asks_daily_summary(question):
            queries.append("Climate Monitor daily report summary highlights")
        if "compare" in lower or "difference" in lower or "distinguish" in lower:
            queries.append("climate risk frameworks comparison IAIS FSB ISSB TCFD TNFD")
        if answer_mode == "detailed":
            queries.append(f"{question} raw source detailed evidence figures dates")
        return self._unique_queries(queries, 4)

    @staticmethod
    def _unique_queries(values: list[str], limit: int) -> list[str]:
        seen: set[str] = set()
        output: list[str] = []
        for value in values:
            query = re.sub(r"\s+", " ", value).strip()
            if not query:
                continue
            key = query.lower()
            if key in seen:
                continue
            seen.add(key)
            output.append(query)
            if len(output) >= limit:
                break
        return output

    def _reflect(
        self,
        question: str,
        hits: list[SearchHit],
        answer_mode: AnswerMode,
    ) -> dict[str, Any]:
        requested_dates = _requested_dates(question, self.kb.latest_date)
        if not hits:
            return {
                "decision": "continue",
                "reason": "No evidence found on the first pass; broadened to the wiki index and raw daily reports.",
                "additional_queries": ["climate risk insurance actuarial wiki index raw source report"],
            }

        top_paths = {hit.chunk.path for hit in hits[:5]}
        source_hits = [hit for hit in hits[:6] if hit.chunk.corpus == "source"]
        if answer_mode == "detailed" and not source_hits:
            return {
                "decision": "continue",
                "reason": "Detailed mode wants raw evidence, but the first pass was mostly curated wiki notes.",
                "additional_queries": [f"{question} primary source report figures dates"],
            }

        if requested_dates:
            covered_dates = {
                hit.chunk.date
                for hit in hits
                if hit.chunk.date in set(requested_dates) and not _is_boilerplate_heading(hit.chunk.heading)
            }
            if len(covered_dates) < min(len(requested_dates), 4):
                return {
                    "decision": "continue",
                    "reason": "A date-range question needs broader daily coverage across the requested window.",
                    "additional_queries": [
                        f"Climate Monitor daily report summaries {' '.join(requested_dates)}"
                    ],
                }

        if len(top_paths) == 1 and "index.md" not in next(iter(top_paths), ""):
            return {
                "decision": "continue",
                "reason": "Evidence is concentrated in one page; adding index context for cross-links.",
                "additional_queries": [f"{question} wiki index related topics"],
            }

        return {
            "decision": "synthesize",
            "reason": "Retrieved evidence covers multiple relevant chunks.",
            "additional_queries": [],
        }

    def _rank_for_answer(
        self,
        question: str,
        hits: list[SearchHit],
        *,
        context_path: str | None = None,
        answer_mode: AnswerMode,
    ) -> list[SearchHit]:
        if not hits:
            return []

        expanded_tokens = set(_tokens(_expand_query(question)))
        normalized_context_path = (context_path or "").lstrip("/")
        context_title = _context_title_from_path(normalized_context_path)
        requested_dates = _requested_dates(question, self.kb.latest_date)
        asks_daily = _asks_daily_summary(question)

        def corpus_bonus(hit: SearchHit) -> float:
            if hit.chunk.corpus == "source":
                return 1.6 if answer_mode == "detailed" else 1.0
            if hit.chunk.type == "topic":
                return 0.9
            if hit.chunk.type == "daily":
                return 0.3
            return 0.1

        def answer_score(hit: SearchHit) -> float:
            overlap = len(expanded_tokens & set(hit.chunk.tokens))
            context_bonus = 0.0
            if normalized_context_path and hit.chunk.path == normalized_context_path:
                context_bonus += 6.0
            if context_title and hit.chunk.corpus == "source" and hit.chunk.title == context_title:
                context_bonus += 4.5
            date_bonus = 0.0
            if requested_dates and hit.chunk.date in set(requested_dates):
                date_bonus += 3.0
            if asks_daily and hit.chunk.type == "daily":
                date_bonus += 2.0
            if asks_daily and _heading_key(hit.chunk.heading) in {"summary", "executive summary"}:
                date_bonus += 2.5
            if _is_boilerplate_heading(hit.chunk.heading):
                date_bonus -= 10.0
            return hit.score + overlap + corpus_bonus(hit) + context_bonus + date_bonus

        ranked = sorted(hits, key=answer_score, reverse=True)

        if requested_dates and asks_daily:
            selected: list[SearchHit] = []
            selected_ids: set[str] = set()
            for requested_date in requested_dates:
                candidates = [
                    hit
                    for hit in ranked
                    if hit.chunk.date == requested_date and hit.chunk.id not in selected_ids
                ]
                if not candidates:
                    continue

                def coverage_key(hit: SearchHit) -> tuple[int, int, float]:
                    heading = _heading_key(hit.chunk.heading)
                    summary_rank = 0
                    if heading == "summary":
                        summary_rank = 3
                    elif heading == "executive summary":
                        summary_rank = 2
                    elif _is_boilerplate_heading(hit.chunk.heading):
                        summary_rank = -3
                    corpus_rank = 1 if hit.chunk.path.startswith("wiki/") else 0
                    return (summary_rank, corpus_rank, answer_score(hit))

                best = max(candidates, key=coverage_key)
                selected.append(best)
                selected_ids.add(best.chunk.id)

            remainder = [hit for hit in ranked if hit.chunk.id not in selected_ids]
            ranked = selected + remainder

        if not normalized_context_path:
            return ranked

        context_hits = [hit for hit in ranked if hit.chunk.path == normalized_context_path]
        raw_context_hits = [
            hit
            for hit in ranked
            if hit.chunk.corpus == "source" and hit.chunk.title == context_title
        ]
        pinned_ids = {hit.chunk.id for hit in context_hits + raw_context_hits}
        non_context_hits = [hit for hit in ranked if hit.chunk.id not in pinned_ids]
        return context_hits + raw_context_hits + non_context_hits

    def _synthesize(
        self,
        question: str,
        hits: list[SearchHit],
        history: list[dict[str, str]],
        language: str,
        answer_mode: AnswerMode,
    ) -> str:
        if not hits:
            return (
                "I could not find enough evidence in the current Climate Monitor corpus to answer this question. "
                "Try a more specific topic such as secondary perils, IFRS S2, IAIS, "
                "parametric insurance, the nat-cat protection gap, or a specific report date."
            )

        if self.client is None:
            return self._offline_answer(question, hits, language, answer_mode)

        evidence = self._format_evidence(hits, answer_mode)
        history_text = self._render_history(history)
        requested_dates = _requested_dates(question, self.kb.latest_date)
        system = (
            "You are an agentic assistant for the Climate Monitor Wiki. "
            "Answer only from the supplied evidence. "
            "If the evidence is incomplete, say what is missing. "
            "Cite every material claim using bracket citations like [1] or [2]. "
            "Do not invent sources, dates, figures, or URLs."
        )
        if answer_mode == "detailed":
            user_instruction = (
                "Write a genuinely detailed answer. Start with a short direct answer, then expand with "
                "supporting evidence, concrete figures, dates, and distinctions between the curated wiki view "
                "and the raw source reports when useful. Avoid being terse."
            )
        else:
            user_instruction = (
                "Write a concise answer with a short direct answer first, then key evidence bullets only if useful."
            )
        if requested_dates and _asks_daily_summary(question):
            user_instruction += (
                f" This is a date-window daily-report summary request. Cover the full window from "
                f"{requested_dates[0]} to {requested_dates[-1]} and mention exact dates explicitly."
            )

        user = (
            "Answer language: English\n"
            f"Answer mode: {answer_mode}\n"
            f"Recent conversation:\n{history_text or '(none)'}\n\n"
            f"Question: {question}\n\n"
            f"Evidence:\n{evidence}\n\n"
            f"{user_instruction}"
        )
        try:
            return self._chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=self.temperature,
            ).strip()
        except Exception as exc:
            fallback = self._offline_answer(question, hits, language, answer_mode)
            return f"{fallback}\n\n(OpenAI synthesis failed, so this answer used offline extraction: {exc})"

    def _format_evidence(self, hits: list[SearchHit], answer_mode: AnswerMode) -> str:
        blocks: list[str] = []
        used_chars = 0
        block_limit = 1500 if answer_mode == "detailed" else 1000
        char_limit = (
            MAX_EVIDENCE_CHARS_DETAILED if answer_mode == "detailed" else MAX_EVIDENCE_CHARS_BRIEF
        )
        for index, hit in enumerate(hits, start=1):
            source_urls = "\n".join(f"URL: {url}" for url in hit.chunk.urls[:4])
            block = (
                f"[{index}] {hit.chunk.corpus.upper()} | {hit.chunk.path} | {hit.chunk.heading} | {hit.chunk.date}\n"
                f"{_shorten(hit.chunk.text, block_limit)}\n"
                f"{source_urls}".strip()
            )
            used_chars += len(block)
            if used_chars > char_limit:
                break
            blocks.append(block)
        return "\n\n".join(blocks)

    def _offline_answer(
        self,
        question: str,
        hits: list[SearchHit],
        language: str,
        answer_mode: AnswerMode,
    ) -> str:
        del language  # Current offline mode is English-only in practice.

        if answer_mode == "brief":
            lines = [
                "I found relevant evidence, but no OpenAI API key is configured, so this is a concise extractive answer.",
                "",
                "Key evidence:",
            ]
            for index, hit in enumerate(hits[:5], start=1):
                corpus = "raw source" if hit.chunk.corpus == "source" else "wiki"
                lines.append(
                    f"- [{index}] ({corpus}) {hit.chunk.title}: {_shorten(hit.chunk.text, 240)}"
                )
            lines.append("")
            lines.append("Set OPENAI_API_KEY in .env to enable synthesized answers.")
            return "\n".join(lines)

        requested_dates = _requested_dates(question, self.kb.latest_date)
        if requested_dates and _asks_daily_summary(question):
            lines = [
                "I found relevant evidence, but no OpenAI API key is configured, so this is a detailed extractive answer.",
                "",
                f"Coverage window: {requested_dates[0]} to {requested_dates[-1]}",
                "",
                "Day-by-day summary:",
            ]
            used_dates: set[str] = set()
            for hit in hits:
                if hit.chunk.date not in requested_dates or hit.chunk.date in used_dates:
                    continue
                if _is_boilerplate_heading(hit.chunk.heading):
                    continue
                used_dates.add(hit.chunk.date)
                lines.append(f"- {hit.chunk.date}: {_shorten(hit.chunk.text, 360)}")

            source_hits = [hit for hit in hits if hit.chunk.corpus == "source" and hit.chunk.date in requested_dates]
            if source_hits:
                lines.extend(
                    [
                        "",
                        f"Raw source coverage: {len({hit.chunk.date for hit in source_hits})} day(s) in the window have raw-source evidence in the selected set.",
                    ]
                )
            lines.append("")
            lines.append("Set OPENAI_API_KEY in .env to enable synthesized detailed answers.")
            return "\n".join(lines)

        leading = hits[0]
        lines = [
            "I found relevant evidence, but no OpenAI API key is configured, so this is a detailed extractive answer.",
            "",
            "Direct answer:",
            _shorten(leading.chunk.text, 520),
            "",
            "Detailed evidence:",
        ]
        for index, hit in enumerate(hits[:8], start=1):
            corpus = "raw source" if hit.chunk.corpus == "source" else "wiki"
            lines.append(
                f"- [{index}] ({corpus}) {hit.chunk.path} | {hit.chunk.heading}: {_shorten(hit.chunk.text, 420)}"
            )
        source_hits = [hit for hit in hits[:8] if hit.chunk.corpus == "source"]
        if source_hits:
            lines.extend(
                [
                    "",
                    f"Raw source coverage: {len(source_hits)} of the top {min(len(hits), 8)} evidence blocks came from sources/.",
                ]
            )
        lines.append("")
        lines.append("Set OPENAI_API_KEY in .env to enable synthesized detailed answers.")
        return "\n".join(lines)

    @staticmethod
    def _render_history(history: list[dict[str, str]]) -> str:
        rendered: list[str] = []
        for item in history[-8:]:
            role = item.get("role", "")
            content = item.get("content", "")
            if role not in {"user", "assistant"} or not content:
                continue
            rendered.append(f"{role}: {content}")
        text = "\n".join(rendered)
        if len(text) > MAX_HISTORY_CHARS:
            text = text[-MAX_HISTORY_CHARS:]
        return text
