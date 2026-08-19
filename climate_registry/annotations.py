from __future__ import annotations

import json
import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from climate_monitor.dedupe import canonical_url


ALLOWED_CATEGORIES = frozenset(
    {
        "Physical Risk",
        "Transition Risk",
        "Adaptation & Resilience",
        "Climate Risk",
        "Insurance Risk",
        "Capital & Solvency",
        "Supervision & Disclosure",
        "Actuarial Modelling",
    }
)
TOP_LEVEL_FIELDS = {
    "schema_version",
    "annotation_method",
    "source_scope",
    "generated_on",
    "articles",
}
ARTICLE_FIELDS = {
    "canonical_url",
    "source_url",
    "title",
    "source_basis",
    "summary",
    "categories",
    "keywords",
}
SOURCE_BASES = {
    "original_content",
    "official_replacement",
    "publisher_excerpt",
    "report_fallback",
}
ALTERNATE_SOURCE_BASES = {"official_replacement", "publisher_excerpt"}
DISALLOWED_KEYWORDS = {"article", "news", "report", "update"}
MAX_ANNOTATION_BYTES = 2 * 1024 * 1024
MAX_ANNOTATION_FILES = 64
MAX_ANNOTATION_TOTAL_BYTES = 16 * 1024 * 1024
MAX_ANNOTATION_ARTICLES = 10_000


@dataclass(frozen=True)
class ArticleAnnotation:
    canonical_url: str
    source_url: str
    title: str
    source_basis: str
    summary: str
    categories: tuple[str, ...]
    keywords: tuple[str, ...]
    generated_on: str

    @property
    def provenance(self) -> str:
        return f"{self.source_basis}_annotation"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON member")
        output[key] = value
    return output


def _string_list(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    allowed: frozenset[str] | None = None,
) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        return None
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip() or len(item) > 100:
            return None
        key = item.casefold()
        if key in seen or (allowed is not None and item not in allowed):
            return None
        output.append(item)
        seen.add(key)
    return tuple(output)


def _load_batch_bytes(raw_bytes: bytes) -> tuple[ArticleAnnotation, ...] | None:
    try:
        payload = json.loads(
            raw_bytes.decode("utf-8"), object_pairs_hook=_unique_object
        )
    except (UnicodeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != TOP_LEVEL_FIELDS:
        return None
    generated_on = payload["generated_on"]
    try:
        parsed_date = date.fromisoformat(generated_on)
    except (TypeError, ValueError):
        return None
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["annotation_method"] != "subagent-original-content-v1"
        or payload["source_scope"] != "linked-original-content-with-report-fallback"
        or parsed_date.isoformat() != generated_on
        or not isinstance(payload["articles"], list)
        or not payload["articles"]
    ):
        return None

    output: list[ArticleAnnotation] = []
    for raw in payload["articles"]:
        if not isinstance(raw, dict) or set(raw) != ARTICLE_FIELDS:
            return None
        source_url, declared_canonical = raw["source_url"], raw["canonical_url"]
        title, summary, source_basis = raw["title"], raw["summary"], raw["source_basis"]
        categories = _string_list(raw["categories"], minimum=1, maximum=3, allowed=ALLOWED_CATEGORIES)
        keywords = _string_list(raw["keywords"], minimum=3, maximum=8)
        if not isinstance(source_url, str) or not isinstance(declared_canonical, str):
            return None
        try:
            parsed_url = urlsplit(source_url)
            parsed_canonical = urlsplit(declared_canonical)
            normalized_source = canonical_url(source_url)
            normalized_canonical = canonical_url(declared_canonical)
        except (TypeError, UnicodeError, ValueError):
            return None
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_canonical.scheme not in {"http", "https"}
            or not parsed_canonical.hostname
            or parsed_canonical.username is not None
            or parsed_canonical.password is not None
            or declared_canonical != normalized_canonical
            or not isinstance(title, str)
            or not title
            or title != title.strip()
            or len(title) > 500
            or not isinstance(source_basis, str)
            or source_basis not in SOURCE_BASES
            or not isinstance(summary, str)
            or not summary
            or summary != summary.strip()
            or len(summary) > 2_000
            or categories is None
            or keywords is None
            or any(keyword.casefold() in DISALLOWED_KEYWORDS for keyword in keywords)
        ):
            return None
        if (
            source_basis not in ALTERNATE_SOURCE_BASES
            and normalized_source != normalized_canonical
        ):
            return None
        output.append(
            ArticleAnnotation(
                canonical_url=normalized_canonical,
                source_url=source_url,
                title=title,
                source_basis=source_basis,
                summary=summary,
                categories=categories,
                keywords=keywords,
                generated_on=generated_on,
            )
        )
    return tuple(output)


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int | None, int | None]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        getattr(metadata, "st_mtime_ns", None),
        getattr(metadata, "st_ctime_ns", None),
    )


def _safe_batch_bytes(path: Path) -> bytes | None:
    descriptor: int | None = None
    try:
        path_before = os.lstat(path)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(path_before.st_mode)
            or stat.S_ISLNK(path_before.st_mode)
            or bool(getattr(path_before, "st_file_attributes", 0) & reparse)
            or path_before.st_size > MAX_ANNOTATION_BYTES
        ):
            return None
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or bool(getattr(opened_before, "st_file_attributes", 0) & reparse)
            or _metadata_identity(opened_before) != _metadata_identity(path_before)
        ):
            return None
        chunks: list[bytes] = []
        remaining = MAX_ANNOTATION_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw_bytes = b"".join(chunks)
        opened_after = os.fstat(descriptor)
        path_after = os.lstat(path)
        if (
            len(raw_bytes) > MAX_ANNOTATION_BYTES
            or _metadata_identity(opened_after) != _metadata_identity(opened_before)
            or _metadata_identity(path_after) != _metadata_identity(opened_before)
            or stat.S_ISLNK(path_after.st_mode)
            or bool(getattr(path_after, "st_file_attributes", 0) & reparse)
        ):
            return None
        return raw_bytes
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_catalog_files(
    metadata_dir: str | Path | None,
) -> tuple[str, list[tuple[str, bytes]]]:
    if metadata_dir is None:
        return "absent", []
    directory = Path(metadata_dir)
    try:
        directory_metadata = os.lstat(directory)
    except FileNotFoundError:
        return "absent", []
    except OSError:
        return "invalid", []
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISDIR(directory_metadata.st_mode)
        or stat.S_ISLNK(directory_metadata.st_mode)
        or bool(getattr(directory_metadata, "st_file_attributes", 0) & reparse)
    ):
        return "invalid", []
    try:
        paths = sorted(directory.glob("articles-*.json"))
    except OSError:
        return "invalid", []
    if not paths:
        return "absent", []
    if len(paths) > MAX_ANNOTATION_FILES:
        return "invalid", []
    files: list[tuple[str, bytes]] = []
    total_bytes = 0
    for path in paths:
        raw_bytes = _safe_batch_bytes(path)
        if raw_bytes is None:
            return "invalid", []
        total_bytes += len(raw_bytes)
        if total_bytes > MAX_ANNOTATION_TOTAL_BYTES:
            return "invalid", []
        files.append((path.name, raw_bytes))
    try:
        directory_after = os.lstat(directory)
    except OSError:
        return "invalid", []
    if _metadata_identity(directory_after) != _metadata_identity(directory_metadata):
        return "invalid", []
    return "valid", files


def _catalog_fingerprint(status: str, files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    digest.update(status.encode("ascii") + b"\0")
    for name, raw_bytes in files:
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(raw_bytes).to_bytes(8, "big"))
        digest.update(hashlib.sha256(raw_bytes).digest())
    return digest.hexdigest()


def annotation_catalog_fingerprint(metadata_dir: str | Path | None) -> str:
    status, files = _read_catalog_files(metadata_dir)
    return _catalog_fingerprint(status, files)


def load_article_annotations_catalog(
    metadata_dir: str | Path | None,
) -> tuple[str, dict[str, ArticleAnnotation], str]:
    status, files = _read_catalog_files(metadata_dir)
    fingerprint = _catalog_fingerprint(status, files)
    if status != "valid":
        return status, {}, fingerprint
    output: dict[str, ArticleAnnotation] = {}
    for _name, raw_bytes in files:
        batch = _load_batch_bytes(raw_bytes)
        if batch is None or len(output) + len(batch) > MAX_ANNOTATION_ARTICLES:
            return "invalid", {}, fingerprint
        for annotation in batch:
            if annotation.canonical_url in output:
                return "invalid", {}, fingerprint
            output[annotation.canonical_url] = annotation
    return "valid", output, fingerprint


def load_article_annotations_strict(
    metadata_dir: str | Path | None,
) -> tuple[str, dict[str, ArticleAnnotation]]:
    """Return ``absent``, ``valid`` or ``invalid`` without hiding bad input."""
    status, annotations, _fingerprint = load_article_annotations_catalog(metadata_dir)
    return status, annotations


def load_article_annotations(metadata_dir: str | Path | None) -> dict[str, ArticleAnnotation]:
    status, annotations = load_article_annotations_strict(metadata_dir)
    return annotations if status == "valid" else {}
