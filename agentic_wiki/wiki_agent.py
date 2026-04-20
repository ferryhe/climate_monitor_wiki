from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - openai is optional for offline demo mode.
    OpenAI = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WIKI_DIR = REPO_ROOT / "wiki"
MAX_HISTORY_CHARS = 6000
MAX_EVIDENCE_CHARS = 9000
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+:/-]*|[\u4e00-\u9fff]+", re.IGNORECASE)
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
URL_RE = re.compile(r"https?://[^\s)>\]]+")

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


def _title_from_file(path: Path) -> str:
    return path.stem


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
    return " ".join(expanded)


def _shorten(text: str, limit: int = 900) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit].rstrip()}..."


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
        }


class WikiKnowledgeBase:
    def __init__(self, wiki_dir: Path = DEFAULT_WIKI_DIR) -> None:
        self.wiki_dir = wiki_dir
        self.documents: list[WikiDocument] = []
        self.chunks: list[WikiChunk] = []
        self.latest_date = ""
        self._load()

    def _load(self) -> None:
        if not self.wiki_dir.exists():
            raise FileNotFoundError(f"Wiki directory not found: {self.wiki_dir}")

        docs: list[WikiDocument] = []
        chunks: list[WikiChunk] = []
        for path in sorted(self.wiki_dir.glob("*.md")):
            markdown = _normalize_text(path.read_text(encoding="utf-8"))
            title = _title_from_file(path)
            doc_type = _detect_type(title)
            links = [
                item.replace("wiki/", "").replace(".md", "").strip()
                for item in LINK_RE.findall(markdown)
            ]
            text = _strip_markdown(markdown)
            doc = WikiDocument(
                title=title,
                path=f"wiki/{path.name}",
                file=path.name,
                type=doc_type,
                date=_extract_date(title, markdown),
                markdown=markdown,
                text=text,
                links=links,
                words=len(_tokens(text)),
            )
            docs.append(doc)
            chunks.extend(self._chunk_document(doc))

        self.documents = docs
        self.chunks = chunks
        self.latest_date = max(
            (doc.date for doc in docs if re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc.date)),
            default="",
        )

    def _chunk_document(self, doc: WikiDocument) -> list[WikiChunk]:
        sections: list[tuple[str, list[str]]] = []
        current_heading = doc.title
        current_lines: list[str] = []

        for line in doc.markdown.splitlines():
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
            sections = [(doc.title, [doc.markdown])]

        chunks: list[WikiChunk] = []
        for index, (heading, lines) in enumerate(sections, start=1):
            markdown = "\n".join(lines).strip()
            text = _strip_markdown(markdown)
            if not text:
                continue
            chunk = WikiChunk(
                id=f"{doc.file}:{index}",
                title=doc.title,
                path=doc.path,
                heading=heading,
                type=doc.type,
                date=doc.date,
                text=text,
                markdown=markdown,
                links=doc.links,
                tokens=_tokens(f"{doc.title} {heading} {text}"),
                urls=URL_RE.findall(markdown),
            )
            chunks.append(chunk)
        return chunks

    def reload(self) -> None:
        self._load()

    def stats(self) -> dict[str, Any]:
        return {
            "documents": len(self.documents),
            "chunks": len(self.chunks),
            "words": sum(doc.words for doc in self.documents),
        }

    def document_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "title": doc.title,
                "path": doc.path,
                "file": doc.file,
                "type": doc.type,
                "date": doc.date,
                "words": doc.words,
                "links": doc.links,
            }
            for doc in self.documents
        ]

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
        requested_dates = re.findall(r"\d{4}-\d{2}-\d{2}", expanded)
        asks_latest = any(term in query_lower for term in ["latest", "current", "recent"])
        context_path = (context_path or "").lstrip("/")

        scored: list[SearchHit] = []
        for chunk in self.chunks:
            chunk_set = set(chunk.tokens)
            overlap = query_set & chunk_set
            context_match = bool(context_path and chunk.path == context_path)
            if not overlap and query_lower not in chunk.text.lower() and not context_match:
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

            for requested_date in requested_dates:
                if requested_date in chunk.title:
                    score += 6.0
                    reason_parts.append("requested date in title")
                elif requested_date == chunk.date:
                    score += 3.0
                    reason_parts.append("requested date")

            if asks_latest and self.latest_date:
                if self.latest_date in chunk.title:
                    score += 6.0
                    reason_parts.append("latest dated report")
                elif chunk.date == self.latest_date:
                    score += 3.0
                    reason_parts.append("latest dated page")

            if chunk.title == "log" and not any(term in query_lower for term in ["log", "history"]):
                score -= 2.0

            if "no climate monitor report" in chunk.text.lower():
                if not any(requested_date in chunk.title for requested_date in requested_dates):
                    score -= 6.0

            if chunk.type == "topic":
                score += 0.4
            elif chunk.type == "daily":
                score += 0.2

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
    def __init__(self, wiki_dir: Path = DEFAULT_WIKI_DIR) -> None:
        self.kb = WikiKnowledgeBase(wiki_dir)
        self.model = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
        self.base_source_url = self._github_blob_base_url()
        self.client = None
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if api_key and OpenAI is not None:
            self.client = OpenAI(api_key=api_key)

    @staticmethod
    def _github_blob_base_url() -> str:
        try:
            remote = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except Exception:
            return ""

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
            "documents": self.kb.document_catalog(),
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
    ) -> dict[str, Any]:
        question = question.strip()
        if not question:
            raise ValueError("Question is empty.")

        planned_queries = self._plan_queries(question, history or [], language)
        hits: list[SearchHit] = []
        retrieval_log: list[dict[str, Any]] = []
        seen_chunks: set[str] = set()

        for query in planned_queries:
            round_hits = self.kb.search(query, top_k=5, context_path=context_path)
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
                        }
                        for hit in new_hits
                    ],
                }
            )

        reflection = self._reflect(question, hits)
        if reflection["additional_queries"]:
            for query in reflection["additional_queries"]:
                round_hits = self.kb.search(query, top_k=4, context_path=context_path)
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
                            }
                            for hit in new_hits
                        ],
                    }
                )

        ranked_hits = self._rank_for_answer(question, hits, context_path=context_path)
        sources = [
            hit.to_source(index, self.base_source_url)
            for index, hit in enumerate(ranked_hits[:8], start=1)
        ]
        text = self._synthesize(question, ranked_hits[:8], history or [], language)

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
    ) -> list[str]:
        local = self._local_plan(question)
        if self.client is None:
            return local

        history_text = self._render_history(history)
        messages = [
            {
                "role": "system",
                "content": (
                    "You plan retrieval for a grounded Markdown wiki RAG system. "
                    "Return JSON only: {\"sub_queries\": [\"...\"]}. "
                    "Create 2-4 search queries that cover the user's intent. "
                    "Include canonical English domain terms for ambiguous queries."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Language: {language}\n"
                    f"Recent conversation:\n{history_text or '(none)'}\n\n"
                    f"Question: {question}\n"
                    "The wiki covers climate risk, natural catastrophe insurance, "
                    "actuarial research, ISSB/IFRS S2, IAIS, FSB, Swiss Re, "
                    "parametric insurance and protection gaps."
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

    def _local_plan(self, question: str) -> list[str]:
        expanded = _expand_query(question)
        queries = [question, expanded]
        lower = question.lower()
        if any(term in lower for term in ["latest", "current", "recent"]):
            queries.append("2026-04-20 latest Climate Monitor summary")
        if "compare" in lower or "difference" in lower or "distinguish" in lower:
            queries.append("climate risk frameworks comparison IAIS FSB ISSB TCFD TNFD")
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

    def _reflect(self, question: str, hits: list[SearchHit]) -> dict[str, Any]:
        if not hits:
            return {
                "decision": "continue",
                "reason": "No evidence found on the first pass; broadened to the wiki index and climate risk overview.",
                "additional_queries": ["climate risk insurance actuarial wiki index"],
            }

        top_paths = {hit.chunk.path for hit in hits[:5]}
        if len(top_paths) == 1 and "index.md" not in top_paths:
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
    ) -> list[SearchHit]:
        if not hits:
            return []
        expanded_tokens = set(_tokens(_expand_query(question)))
        normalized_context_path = (context_path or "").lstrip("/")

        def answer_score(hit: SearchHit) -> float:
            overlap = len(expanded_tokens & set(hit.chunk.tokens))
            source_bonus = 0.8 if hit.chunk.type == "topic" else 0.2
            return hit.score + overlap + source_bonus

        ranked = sorted(hits, key=answer_score, reverse=True)
        if not normalized_context_path:
            return ranked

        # Keep active-note evidence first to ensure Obsidian context is honored.
        context_hits = [
            hit for hit in ranked if hit.chunk.path == normalized_context_path
        ]
        if not context_hits:
            return ranked

        non_context_hits = [
            hit for hit in ranked if hit.chunk.path != normalized_context_path
        ]
        return context_hits + non_context_hits

    def _synthesize(
        self,
        question: str,
        hits: list[SearchHit],
        history: list[dict[str, str]],
        language: str,
    ) -> str:
        if not hits:
            return (
                "I could not find enough evidence in the current Climate Monitor Wiki to answer this question. "
                "Try a more specific topic such as secondary perils, IFRS S2, IAIS, "
                "parametric insurance, or the nat-cat protection gap."
            )

        if self.client is None:
            return self._offline_answer(question, hits, language)

        evidence = self._format_evidence(hits)
        history_text = self._render_history(history)
        system = (
            "You are an agentic assistant for the Climate Monitor Wiki. "
            "Answer only from the supplied evidence. "
            "If the evidence is incomplete, say what is missing. "
            "Cite every material claim using bracket citations like [1] or [2]. "
            "Prefer concise, decision-useful answers for actuaries and insurance risk professionals. "
            "Do not invent sources, dates, figures, or URLs."
        )
        user = (
            "Answer language: English\n"
            f"Recent conversation:\n{history_text or '(none)'}\n\n"
            f"Question: {question}\n\n"
            f"Evidence:\n{evidence}\n\n"
            "Write the final answer with a short direct answer first, then key evidence bullets if useful."
        )
        try:
            return self._chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=self.temperature,
            ).strip()
        except Exception as exc:
            fallback = self._offline_answer(question, hits, language)
            return f"{fallback}\n\n(OpenAI synthesis failed, so this answer used offline extraction: {exc})"

    def _format_evidence(self, hits: list[SearchHit]) -> str:
        blocks: list[str] = []
        used_chars = 0
        for index, hit in enumerate(hits, start=1):
            source_urls = "\n".join(f"URL: {url}" for url in hit.chunk.urls[:3])
            block = (
                f"[{index}] {hit.chunk.path} | {hit.chunk.heading} | {hit.chunk.date}\n"
                f"{_shorten(hit.chunk.text, 1000)}\n"
                f"{source_urls}".strip()
            )
            used_chars += len(block)
            if used_chars > MAX_EVIDENCE_CHARS:
                break
            blocks.append(block)
        return "\n\n".join(blocks)

    def _offline_answer(self, question: str, hits: list[SearchHit], language: str) -> str:
        lines = [
            "I found relevant wiki evidence, but no OpenAI API key is configured, so this is an extractive answer.",
            "",
            "Key evidence:",
        ]
        for index, hit in enumerate(hits[:5], start=1):
            lines.append(f"- [{index}] {hit.chunk.title}: {_shorten(hit.chunk.text, 240)}")
        lines.append("")
        lines.append("Set OPENAI_API_KEY in .env to enable synthesized agentic answers.")
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
