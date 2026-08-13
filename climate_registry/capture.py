from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urljoin, urlsplit

from pypdf import PdfReader

from .errors import RegistryBuildError, RegistryInputError, RegistryLockError
from .fetch import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_TIMEOUT,
    FetchFailure,
    PinnedTransport,
    Resolver,
    Transport,
    _default_resolver,
    fetch_document,
)
from .persistent import (
    LATEST_SCHEMA_VERSION,
    _backup_connection,
    _exclusive_database_lock,
    _file_sha256,
    _fsync_parent,
    _sqlite_sidecars,
    _validate_database,
)

EXTRACTOR_VERSION = "1"
GENERATOR_NAME = "climate-registry-rules"
GENERATOR_VERSION = "1"
MAX_BATCH = 100
MAX_PDF_PAGES = 250
MAX_EXTRACTED_TEXT_CHARS = 2_000_000
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _MarkdownHTMLParser(HTMLParser):
    _blocks = {"title", "h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}

    def __init__(self, *, base_url: str, max_chars: int) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.max_chars = max_chars
        self.accumulated_chars = 0
        self.lines: list[str] = []
        self.current: list[str] = []
        self.prefix = ""
        self.suppressed = 0
        self.link_stack: list[tuple[int, str]] = []
        self.seen_tag = False

    def _guard(self, added_chars: int) -> None:
        self.accumulated_chars += added_chars
        if self.accumulated_chars > self.max_chars:
            raise FetchFailure(
                "extracted_text_too_large",
                "HTML extracted text exceeds the character limit",
            )

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.current)).strip()
        if text:
            self._guard(len(self.prefix) + (2 if self.lines else 0))
            self.lines.append(f"{self.prefix}{text}")
        self.current = []
        self.prefix = ""
        self.link_stack.clear()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self.seen_tag = True
        if tag in {"script", "style", "noscript", "svg"}:
            self.suppressed += 1
            return
        if self.suppressed:
            return
        if tag in self._blocks:
            self._flush()
            if tag == "title" or tag == "h1":
                self.prefix = "# "
            elif tag.startswith("h"):
                self.prefix = f"{'#' * int(tag[1])} "
            elif tag == "li":
                self.prefix = "- "
        elif tag == "br":
            self._guard(1)
            self.current.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.link_stack.append((len(self.current), href))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.suppressed = max(0, self.suppressed - 1)
            return
        if self.suppressed:
            return
        if tag == "a" and self.link_stack:
            start, href = self.link_stack.pop()
            label = re.sub(r"\s+", " ", "".join(self.current[start:])).strip()
            scheme = urlsplit(href).scheme.lower()
            if label and href and scheme in {"", "http", "https"}:
                rendered = f"[{label}]({urljoin(self.base_url, href)})"
                self._guard(max(0, len(rendered) - len(label)))
                self.current[start:] = [rendered]
        if tag in self._blocks:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self._guard(len(data))
            self.current.append(data)

    def markdown(self) -> str:
        self._flush()
        return "\n\n".join(self.lines).strip()


def extract_html(body: bytes, *, base_url: str, charset: str | None) -> str:
    if not body.strip() or b"<" not in body:
        raise FetchFailure("invalid_html", "HTML document is empty or malformed")
    try:
        text = body.decode(charset or "utf-8", errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise FetchFailure("invalid_charset", "HTML charset is invalid") from exc
    parser = _MarkdownHTMLParser(
        base_url=base_url, max_chars=MAX_EXTRACTED_TEXT_CHARS
    )
    try:
        parser.feed(text)
        parser.close()
    except FetchFailure:
        raise
    except Exception as exc:
        raise FetchFailure("invalid_html", "HTML extraction failed") from exc
    markdown = parser.markdown()
    if not parser.seen_tag or not markdown.strip():
        raise FetchFailure("empty_content", "HTML extraction produced no text")
    return markdown


def extract_pdf(body: bytes) -> str:
    if not body.startswith(b"%PDF-"):
        raise FetchFailure("invalid_pdf", "PDF signature is invalid")
    try:
        reader = PdfReader(BytesIO(body))
        if reader.is_encrypted:
            raise FetchFailure("invalid_pdf", "encrypted PDF is unsupported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise FetchFailure("pdf_too_many_pages", "PDF exceeds the page limit")
        pages = []
        extracted_chars = 0
        for index, page in enumerate(reader.pages, start=1):
            text = re.sub(r"[ \t]+", " ", page.extract_text() or "").strip()
            if text:
                extracted_chars += len(text)
                if extracted_chars > MAX_EXTRACTED_TEXT_CHARS:
                    raise FetchFailure(
                        "extracted_text_too_large",
                        "PDF extracted text exceeds the character limit",
                    )
                pages.append(f"## Page {index}\n\n{text}")
    except FetchFailure:
        raise
    except Exception as exc:
        raise FetchFailure("invalid_pdf", "PDF extraction failed") from exc
    markdown = "\n\n".join(pages).strip()
    if not markdown:
        raise FetchFailure("empty_content", "PDF extraction produced no text")
    return markdown


def _content_type_parts(content_type: str) -> tuple[str, str | None]:
    message = Message()
    message["content-type"] = content_type
    return message.get_content_type().lower(), message.get_content_charset()


def extract_markdown(
    body: bytes, content_type: str, *, base_url: str = "https://invalid.example/"
) -> tuple[str, str]:
    mime, charset = _content_type_parts(content_type)
    if mime in {"text/html", "application/xhtml+xml"}:
        markdown, method = (
            extract_html(body, base_url=base_url, charset=charset),
            "html-stdlib",
        )
    elif mime == "application/pdf":
        markdown, method = extract_pdf(body), "pypdf"
    else:
        raise FetchFailure(
            "unsupported_mime",
            "response content type is unsupported",
            content_type=mime,
        )
    if len(markdown) > MAX_EXTRACTED_TEXT_CHARS:
        raise FetchFailure(
            "extracted_text_too_large",
            "extracted Markdown exceeds the character limit",
        )
    return markdown, method


_STOPWORDS = {
    "about", "after", "also", "and", "are", "been", "being", "between", "but", "can",
    "could", "for", "from", "has", "have", "into", "its", "more", "not", "our", "that",
    "the", "their", "these", "this", "those", "through", "was", "were", "which", "will",
    "with", "would", "your", "page", "https", "www",
}
_CATEGORY_TERMS = {
    "climate-risk": ("climate risk", "physical risk", "transition risk", "warming", "emissions"),
    "insurance": ("insurance", "insurer", "reinsurance", "underwriting", "actuarial"),
    "catastrophe": ("catastrophe", "wildfire", "flood", "hurricane", "extreme weather"),
    "finance-investment": ("finance", "financial", "investment", "capital", "asset"),
    "policy-regulation": ("policy", "regulation", "regulatory", "standard", "supervisor"),
    "sustainability-disclosure": ("sustainability", "disclosure", "reporting", "taxonomy"),
}


def deterministic_enrichment(markdown: str) -> dict[str, object]:
    visible = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", markdown)
    visible = re.sub(r"(?mi)^#{1,6}\s+page\s+\d+\s*$", "", visible)
    visible = re.sub(r"(?m)^#{1,6}\s+", "", visible)
    visible = re.sub(r"(?m)^[-*]\s+", "", visible)
    compact = re.sub(r"\s+", " ", visible).strip()
    sentences = [item.strip() for item in re.split(r"(?<=[.!?。！？])\s*", compact) if item.strip()]
    summary = " ".join(sentences[:3])[:600].strip()
    if len(summary) < 40:
        raise FetchFailure("insufficient_content", "content is too short for deterministic enrichment")

    lowered = compact.lower()
    categories = [
        category
        for category, terms in _CATEGORY_TERMS.items()
        if any(term in lowered for term in terms)
    ]
    latin = re.findall(r"[a-z][a-z-]{2,}", lowered)
    han_runs = re.findall(r"[\u4e00-\u9fff]{2,}", compact)
    han_tokens: list[str] = []
    for run in han_runs:
        width = 4 if len(run) >= 4 else 2
        han_tokens.extend(run[index : index + width] for index in range(0, len(run) - width + 1, width))
    candidates = [item for item in latin if item not in _STOPWORDS] + han_tokens
    counts = Counter(candidates)
    first_position: dict[str, int] = {}
    for position, item in enumerate(candidates):
        first_position.setdefault(item, position)
    keywords = sorted(counts, key=lambda item: (-counts[item], first_position[item], item))[:12]
    if len(keywords) < 8:
        raise FetchFailure("insufficient_content", "content has too few distinct keywords")
    han_count = len(re.findall(r"[\u4e00-\u9fff]", compact))
    latin_count = len(re.findall(r"[A-Za-z]", compact))
    if han_count and latin_count:
        if min(han_count, latin_count) >= max(han_count, latin_count) * 0.15:
            language = "mixed"
        else:
            language = "zh" if han_count > latin_count else "en"
    elif han_count:
        language = "zh"
    elif latin_count:
        language = "en"
    else:
        language = "unknown"
    return {"summary": summary, "categories": categories, "keywords": keywords, "language": language}


def _safe_message(value: object) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = re.sub(r"https?://\S+", "[url]", text)
    return re.sub(r"\s+", " ", text).strip()[:240]


def _safe_header(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()[:1024] or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _select_articles(
    connection: sqlite3.Connection,
    article_ids: Sequence[str],
    *,
    refresh: bool,
    limit: int | None,
) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    if limit is not None and not 1 <= limit <= MAX_BATCH:
        raise RegistryInputError(f"limit must be between 1 and {MAX_BATCH}")
    unique_ids = list(dict.fromkeys(article_ids))
    if unique_ids:
        if len(unique_ids) > MAX_BATCH:
            raise RegistryInputError(f"at most {MAX_BATCH} article IDs may be selected")
        placeholders = ",".join("?" for _ in unique_ids)
        rows = connection.execute(
            "SELECT article_id, canonical_url, current_content_version_id, "
            f"publication_eligible FROM articles WHERE article_id IN ({placeholders}) "
            "ORDER BY article_id",
            unique_ids,
        ).fetchall()
        found = {row["article_id"] for row in rows}
        if found != set(unique_ids):
            raise RegistryInputError("one or more requested article IDs do not exist")
        if any(row["publication_eligible"] != 1 for row in rows):
            raise RegistryInputError("requested article is not publication-eligible")
        if limit is not None and len(rows) > limit:
            raise RegistryInputError("limit would truncate the explicit article selection")
        return rows
    where = "publication_eligible = 1"
    if not refresh:
        where += " AND current_content_version_id IS NULL"
    query = (
        "SELECT article_id, canonical_url, current_content_version_id, publication_eligible "
        f"FROM articles WHERE {where} "
        "ORDER BY CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM article_fetches f WHERE f.article_id = articles.article_id"
        ") THEN 0 ELSE 1 END, "
        "COALESCE((SELECT MAX(f.fetched_at) FROM article_fetches f "
        "WHERE f.article_id = articles.article_id), '') ASC, article_id"
    )
    query += " LIMIT ?"
    params: tuple[object, ...] = (limit or MAX_BATCH,)
    return connection.execute(query, params).fetchall()


def _conditional_headers(connection: sqlite3.Connection, article_id: str) -> tuple[dict[str, str], str | None]:
    row = connection.execute(
        """
        SELECT a.current_content_version_id,
               (SELECT f.etag FROM article_fetches f
                WHERE f.article_id = a.article_id
                  AND f.content_version_id = a.current_content_version_id
                  AND f.fetch_status IN ('success', 'not_modified')
                  AND f.etag IS NOT NULL AND length(trim(f.etag)) > 0
                ORDER BY f.fetched_at DESC, f.rowid DESC LIMIT 1),
               (SELECT f.last_modified FROM article_fetches f
                WHERE f.article_id = a.article_id
                  AND f.content_version_id = a.current_content_version_id
                  AND f.fetch_status IN ('success', 'not_modified')
                  AND f.last_modified IS NOT NULL AND length(trim(f.last_modified)) > 0
                ORDER BY f.fetched_at DESC, f.rowid DESC LIMIT 1)
        FROM articles a WHERE a.article_id = ?
        """,
        (article_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return {}, None
    headers: dict[str, str] = {}
    if row[1]:
        headers["If-None-Match"] = row[1]
    if row[2]:
        headers["If-Modified-Since"] = row[2]
    return headers, row[0]


def _insert_failed_fetch(
    connection: sqlite3.Connection,
    *,
    article_id: str,
    requested_url: str,
    fetched_at: str,
    failure: FetchFailure,
    id_factory: Callable[[str], str],
) -> None:
    connection.execute(
        """
        INSERT INTO article_fetches(
            fetch_id, article_id, requested_url, final_url, fetched_at, fetch_status,
            http_status, content_type, error_code, error_message
        ) VALUES (?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?)
        """,
        (
            id_factory("fetch"), article_id, requested_url, failure.final_url, fetched_at,
            failure.http_status, failure.content_type, failure.code, _safe_message(failure),
        ),
    )


def _enrich_content(connection: sqlite3.Connection, content_version_id: str, markdown: str, generated_at: str) -> str:
    existing = connection.execute(
        """
        SELECT status FROM article_enrichments
        WHERE content_version_id = ? AND generator_kind = 'deterministic'
          AND generator_name = ? AND generator_version = ?
        ORDER BY generated_at, enrichment_id LIMIT 1
        """,
        (content_version_id, GENERATOR_NAME, GENERATOR_VERSION),
    ).fetchone()
    if existing is not None:
        return "reused" if existing[0] == "complete" else "failed"
    enrichment_id = _stable_id("enrich", content_version_id, GENERATOR_NAME, GENERATOR_VERSION)
    try:
        result = deterministic_enrichment(markdown)
    except FetchFailure as exc:
        connection.execute(
            """
            INSERT INTO article_enrichments(
                enrichment_id, content_version_id, status, generator_kind, generator_name,
                generator_version, generated_at, error_code, error_message
            ) VALUES (?, ?, 'failed', 'deterministic', ?, ?, ?, ?, ?)
            """,
            (
                enrichment_id,
                content_version_id,
                GENERATOR_NAME,
                GENERATOR_VERSION,
                generated_at,
                exc.code,
                _safe_message(exc),
            ),
        )
        return "failed"
    connection.execute(
        """
        INSERT INTO article_enrichments(
            enrichment_id, content_version_id, status, summary, categories_json,
            keywords_json, language, generator_kind, generator_name, generator_version, generated_at
        ) VALUES (?, ?, 'complete', ?, ?, ?, ?, 'deterministic', ?, ?, ?)
        """,
        (
            enrichment_id, content_version_id, result["summary"],
            json.dumps(result["categories"], ensure_ascii=False, separators=(",", ":")),
            json.dumps(result["keywords"], ensure_ascii=False, separators=(",", ":")),
            result["language"], GENERATOR_NAME, GENERATOR_VERSION, generated_at,
        ),
    )
    return "complete"


def _capture_one(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    resolver: Resolver,
    transport: Transport,
    clock: Callable[[], str],
    id_factory: Callable[[str], str],
    timeout: float,
    max_bytes: int,
    max_redirects: int,
) -> dict[str, str]:
    article_id = row["article_id"]
    requested_url = row["canonical_url"]
    fetched_at = clock()
    headers, previous_version = _conditional_headers(connection, article_id)
    try:
        response = fetch_document(
            requested_url, headers=headers, resolver=resolver, transport=transport,
            timeout=timeout, max_bytes=max_bytes, max_redirects=max_redirects,
        )
        raw_content_type = response.headers.get("content-type", "")
        content_type = (
            _content_type_parts(raw_content_type)[0] if raw_content_type else None
        )
        if response.status == 304:
            if not previous_version:
                raise FetchFailure(
                    "invalid_not_modified",
                    "304 response has no prior content version",
                    http_status=304,
                    final_url=response.url,
                )
            connection.execute(
                """
                INSERT INTO article_fetches(
                    fetch_id, article_id, requested_url, final_url, fetched_at, fetch_status,
                    http_status, content_type, etag, last_modified, content_version_id
                ) VALUES (?, ?, ?, ?, ?, 'not_modified', 304, ?, ?, ?, ?)
                """,
                (id_factory("fetch"), article_id, requested_url, response.url, fetched_at, content_type,
                 _safe_header(response.headers.get("etag")),
                 _safe_header(response.headers.get("last-modified")), previous_version),
            )
            markdown = connection.execute(
                "SELECT markdown_content FROM article_content_versions "
                "WHERE content_version_id = ?",
                (previous_version,),
            ).fetchone()[0]
            enrichment = _enrich_content(
                connection, previous_version, markdown, fetched_at
            )
            status = (
                "not_modified" if enrichment != "failed" else "enrichment_failed"
            )
            return {
                "article_id": article_id,
                "status": status,
                "enrichment": enrichment,
            }
        if not 200 <= response.status <= 299:
            raise FetchFailure(
                "http_error",
                "server returned a non-success status",
                http_status=response.status,
                final_url=response.url,
                content_type=content_type,
            )
        if len(response.body) > max_bytes:
            raise FetchFailure(
                "body_too_large",
                "response exceeded the configured byte limit",
                http_status=response.status,
                final_url=response.url,
                content_type=content_type,
            )
        try:
            markdown, method = extract_markdown(
                response.body, raw_content_type, base_url=response.url
            )
        except FetchFailure as exc:
            raise FetchFailure(
                exc.code,
                str(exc),
                http_status=response.status,
                final_url=response.url,
                content_type=content_type,
            ) from exc
        lowered_markdown = markdown.lower()
        if len(markdown) < 4000 and any(
            marker in lowered_markdown
            for marker in ("captcha", "access denied", "verify you are human", "cloudflare ray id")
        ):
            raise FetchFailure(
                "blocked_response",
                "response appears to be an access challenge",
                http_status=response.status,
                final_url=response.url,
                content_type=content_type,
            )
        content_hash = hashlib.sha256(response.body).hexdigest()
        markdown_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        existing = connection.execute(
            "SELECT content_version_id, markdown_content FROM article_content_versions "
            "WHERE article_id = ? AND content_sha256 = ?",
            (article_id, content_hash),
        ).fetchone()
        if existing:
            content_version_id, stored_markdown = existing
            markdown = stored_markdown
            version_status = "reused"
        else:
            content_version_id = _stable_id("content", article_id, content_hash)
            connection.execute(
                """
                INSERT INTO article_content_versions(
                    content_version_id, article_id, content_sha256, markdown_content, markdown_sha256,
                    content_type, source_bytes, extraction_method, extraction_version, first_fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (content_version_id, article_id, content_hash, markdown, markdown_hash, content_type,
                 len(response.body), method, EXTRACTOR_VERSION, fetched_at),
            )
            version_status = "new"
        connection.execute(
            """
            INSERT INTO article_fetches(
                fetch_id, article_id, requested_url, final_url, fetched_at, fetch_status,
                http_status, content_type, etag, last_modified, content_version_id
            ) VALUES (?, ?, ?, ?, ?, 'success', ?, ?, ?, ?, ?)
            """,
            (id_factory("fetch"), article_id, requested_url, response.url, fetched_at, response.status,
             content_type, _safe_header(response.headers.get("etag")),
             _safe_header(response.headers.get("last-modified")), content_version_id),
        )
        connection.execute(
            "UPDATE articles SET current_content_version_id = ? WHERE article_id = ?",
            (content_version_id, article_id),
        )
        enrichment = _enrich_content(connection, content_version_id, markdown, fetched_at)
        status = "captured" if enrichment != "failed" else "enrichment_failed"
        return {
            "article_id": article_id,
            "status": status,
            "content_version": version_status,
            "enrichment": enrichment,
        }
    except FetchFailure as failure:
        _insert_failed_fetch(
            connection, article_id=article_id, requested_url=requested_url,
            fetched_at=fetched_at, failure=failure, id_factory=id_factory,
        )
        return {"article_id": article_id, "status": "failed", "error_code": failure.code}


def capture_enrich_registry(
    database: Path,
    backup_dir: Path,
    *,
    article_ids: Sequence[str] = (),
    limit: int | None = None,
    refresh: bool = False,
    resolver: Resolver = _default_resolver,
    transport: Transport | None = None,
    clock: Callable[[], str] = _now,
    id_factory: Callable[[str], str] = _new_id,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> dict[str, object]:
    """Capture eligible registry articles into an atomically installed candidate DB."""

    database = database.resolve()
    backup_dir = backup_dir.resolve()
    if database == REPOSITORY_ROOT or REPOSITORY_ROOT in database.parents:
        raise RegistryInputError("runtime registry database must be outside the repository")
    if backup_dir == REPOSITORY_ROOT or REPOSITORY_ROOT in backup_dir.parents:
        raise RegistryInputError("registry backups must be outside the repository")
    if not database.is_file():
        raise RegistryInputError(f"registry database does not exist: {database}")
    if database == backup_dir or backup_dir in database.parents:
        raise RegistryInputError("backup directory must not contain the live registry database")
    if backup_dir.exists() and not backup_dir.is_dir():
        raise RegistryInputError("backup path is not a directory")
    if timeout <= 0 or max_bytes < 1 or not 0 <= max_redirects <= 10:
        raise RegistryInputError("network limits are invalid")
    transport = transport or PinnedTransport()

    with _exclusive_database_lock(database):
        sidecars = _sqlite_sidecars(database)
        if sidecars:
            raise RegistryInputError("registry has active SQLite sidecar files; reconcile before capture")
        live_fingerprint = _file_sha256(database)
        source_connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        try:
            version = _validate_database(source_connection)
            if version != LATEST_SCHEMA_VERSION or version < 3:
                raise RegistryInputError("capture requires the current schema version 3")
            selected = _select_articles(source_connection, article_ids, refresh=refresh, limit=limit)
        finally:
            source_connection.close()
        if not selected:
            return {"status": "no-op", "selected": 0, "counts": {}, "articles": [], "backup": None}
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RegistryBuildError("could not create backup directory") from exc
        backup_path = backup_dir / f"{database.name}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.bak"
        if backup_path.exists():
            raise RegistryBuildError("backup destination already exists")
        try:
            descriptor, candidate_name = tempfile.mkstemp(
                prefix=f".{database.name}.", suffix=".candidate", dir=database.parent
            )
        except OSError as exc:
            raise RegistryBuildError("could not create capture candidate database") from exc
        os.close(descriptor)
        candidate = Path(candidate_name)
        try:
            candidate.unlink()
        except OSError as exc:
            raise RegistryBuildError("could not prepare capture candidate database") from exc
        backup_created = False
        try:
            source_connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
            try:
                _backup_connection(source_connection, backup_path)
                backup_created = True
                _backup_connection(source_connection, candidate)
            finally:
                source_connection.close()
            if os.name == "posix":
                backup_path.chmod(0o600)
                shutil.copymode(database, candidate)
            _fsync_parent(backup_path)
            backup_connection = sqlite3.connect(f"{backup_path.as_uri()}?mode=ro", uri=True)
            try:
                _validate_database(backup_connection)
            finally:
                backup_connection.close()

            connection = sqlite3.connect(candidate)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            results: list[dict[str, str]] = []
            try:
                for row in selected:
                    with connection:
                        results.append(
                            _capture_one(
                                connection, row, resolver=resolver, transport=transport, clock=clock,
                                id_factory=id_factory, timeout=timeout, max_bytes=max_bytes,
                                max_redirects=max_redirects,
                            )
                        )
                connection.execute("PRAGMA optimize")
                connection.row_factory = None
                _validate_database(connection)
            finally:
                connection.close()
            candidate_sidecars = _sqlite_sidecars(candidate)
            if candidate_sidecars:
                raise RegistryBuildError("capture candidate retained SQLite sidecar files")
            if _sqlite_sidecars(database) or _file_sha256(database) != live_fingerprint:
                raise RegistryLockError("live registry changed while capture candidate was prepared")
            os.replace(candidate, database)
            _fsync_parent(database)
            counts = dict(Counter(result["status"] for result in results))
            partial = bool(counts.get("failed") or counts.get("enrichment_failed"))
            return {
                "status": "partial" if partial else "updated",
                "selected": len(results),
                "counts": counts,
                "articles": results,
                "backup": str(backup_path),
            }
        except (RegistryInputError, RegistryBuildError, RegistryLockError):
            raise
        except Exception as exc:
            raise RegistryBuildError(f"capture candidate failed: {_safe_message(exc)}") from exc
        finally:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
            for sidecar in _sqlite_sidecars(candidate):
                try:
                    sidecar.unlink()
                except FileNotFoundError:
                    pass
            if not backup_created:
                try:
                    backup_path.unlink()
                except FileNotFoundError:
                    pass
