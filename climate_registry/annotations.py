from __future__ import annotations

import json
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
SOURCE_BASES = {"original_content", "report_fallback"}
DISALLOWED_KEYWORDS = {"article", "news", "report", "update"}


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


def _load_batch(path: Path) -> tuple[ArticleAnnotation, ...] | None:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeError, ValueError):
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
            normalized = canonical_url(source_url)
        except (TypeError, UnicodeError, ValueError):
            return None
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or declared_canonical != normalized
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
        output.append(
            ArticleAnnotation(
                canonical_url=normalized,
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


def load_article_annotations(metadata_dir: str | Path | None) -> dict[str, ArticleAnnotation]:
    if metadata_dir is None:
        return {}
    directory = Path(metadata_dir)
    try:
        paths = sorted(directory.glob("articles-*.json"))
    except OSError:
        return {}
    if not paths:
        return {}
    output: dict[str, ArticleAnnotation] = {}
    for path in paths:
        batch = _load_batch(path)
        if batch is None:
            return {}
        for annotation in batch:
            if annotation.canonical_url in output:
                return {}
            output[annotation.canonical_url] = annotation
    return output
