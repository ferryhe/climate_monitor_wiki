from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


TAXONOMY_SCHEMA_VERSION = "article-category-taxonomy.v1"
DEFAULT_TAXONOMY_ID = "climate-actuarial-v1"
DEFAULT_TAXONOMY_SHA256 = "3deefa1cc0df7a2e1ce8ef538271c0ab4465bb928f20e6418aafc8a834794d94"
DEFAULT_TAXONOMY_PATH = (
    Path(__file__).resolve().parents[1]
    / "monitoring"
    / "taxonomies"
    / "article_categories_v1.yaml"
)
MAX_TAXONOMY_BYTES = 64 * 1024
_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_TOP_LEVEL_FIELDS = {"schema_version", "taxonomy_id", "constraints", "categories"}
_CONSTRAINT_FIELDS = {"summary", "categories", "keywords"}
_CATEGORY_FIELDS = {"id", "label", "description", "signals"}


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    output: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in output:
            raise ValueError("taxonomy contains a duplicate YAML key")
        output[key] = loader.construct_object(value_node, deep=deep)
    return output


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class SemanticConstraints:
    summary_min_chars: int
    summary_max_chars: int
    categories_min_items: int
    categories_max_items: int
    keywords_min_items: int
    keywords_max_items: int
    keyword_max_chars: int
    disallowed_keywords: frozenset[str]


@dataclass(frozen=True)
class ArticleCategory:
    id: str
    label: str
    description: str
    signals: tuple[str, ...]


@dataclass(frozen=True)
class ArticleTaxonomy:
    schema_version: str
    taxonomy_id: str
    sha256: str
    categories: tuple[ArticleCategory, ...]
    constraints: SemanticConstraints

    @property
    def allowed_labels(self) -> frozenset[str]:
        return frozenset(item.label for item in self.categories)

    @property
    def labels_by_signal(self) -> dict[str, str]:
        return {
            signal: item.label
            for item in self.categories
            for signal in item.signals
        }


def _mapping(value: Any, *, name: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} must contain exactly: {', '.join(sorted(fields))}")
    return value


def _positive_int(value: Any, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bounded_pair(value: Any, *, name: str) -> tuple[int, int]:
    section = _mapping(value, name=name, fields={"min_items", "max_items"})
    minimum = _positive_int(section["min_items"], name=f"{name}.min_items")
    maximum = _positive_int(section["max_items"], name=f"{name}.max_items")
    if minimum > maximum:
        raise ValueError(f"{name}.min_items cannot exceed max_items")
    return minimum, maximum


def _normalized_string(value: Any, *, name: str, maximum: int = 500) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or " ".join(value.split()) != value
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
        or len(value) > maximum
    ):
        raise ValueError(f"{name} must be a non-empty normalized string")
    return value


def _string_list(value: Any, *, name: str, maximum: int = 100) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    output: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = _normalized_string(raw, name=f"{name}[{index}]", maximum=maximum)
        key = item.casefold()
        if key in seen:
            raise ValueError(f"{name} contains a duplicate value")
        output.append(item)
        seen.add(key)
    return tuple(output)


def _parse_constraints(value: Any) -> SemanticConstraints:
    constraints = _mapping(value, name="constraints", fields=_CONSTRAINT_FIELDS)
    summary = _mapping(
        constraints["summary"],
        name="constraints.summary",
        fields={"min_chars", "max_chars"},
    )
    summary_min = _positive_int(summary["min_chars"], name="constraints.summary.min_chars")
    summary_max = _positive_int(summary["max_chars"], name="constraints.summary.max_chars")
    if summary_min > summary_max:
        raise ValueError("constraints.summary.min_chars cannot exceed max_chars")
    categories_min, categories_max = _bounded_pair(
        constraints["categories"], name="constraints.categories"
    )
    keywords = _mapping(
        constraints["keywords"],
        name="constraints.keywords",
        fields={"min_items", "max_items", "max_chars", "disallowed"},
    )
    keywords_min = _positive_int(keywords["min_items"], name="constraints.keywords.min_items")
    keywords_max = _positive_int(keywords["max_items"], name="constraints.keywords.max_items")
    if keywords_min > keywords_max:
        raise ValueError("constraints.keywords.min_items cannot exceed max_items")
    keyword_max_chars = _positive_int(
        keywords["max_chars"], name="constraints.keywords.max_chars"
    )
    disallowed = _string_list(
        keywords["disallowed"],
        name="constraints.keywords.disallowed",
        maximum=keyword_max_chars,
    )
    return SemanticConstraints(
        summary_min_chars=summary_min,
        summary_max_chars=summary_max,
        categories_min_items=categories_min,
        categories_max_items=categories_max,
        keywords_min_items=keywords_min,
        keywords_max_items=keywords_max,
        keyword_max_chars=keyword_max_chars,
        disallowed_keywords=frozenset(item.casefold() for item in disallowed),
    )


def _parse_categories(value: Any) -> tuple[ArticleCategory, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("categories must be a non-empty list")
    output: list[ArticleCategory] = []
    ids: set[str] = set()
    labels: set[str] = set()
    signals: set[str] = set()
    for index, raw in enumerate(value):
        item = _mapping(raw, name=f"categories[{index}]", fields=_CATEGORY_FIELDS)
        category_id = _normalized_string(item["id"], name=f"categories[{index}].id", maximum=64)
        if _TOKEN.fullmatch(category_id) is None or category_id in ids:
            raise ValueError("category ids must be unique lowercase tokens")
        label = _normalized_string(item["label"], name=f"categories[{index}].label", maximum=100)
        label_key = label.casefold()
        if label_key in labels:
            raise ValueError("category labels must be unique")
        description = _normalized_string(
            item["description"], name=f"categories[{index}].description", maximum=500
        )
        category_signals = _string_list(
            item["signals"], name=f"categories[{index}].signals", maximum=64
        )
        if any(_TOKEN.fullmatch(signal) is None or signal in signals for signal in category_signals):
            raise ValueError("category signals must be unique lowercase tokens")
        output.append(
            ArticleCategory(
                id=category_id,
                label=label,
                description=description,
                signals=category_signals,
            )
        )
        ids.add(category_id)
        labels.add(label_key)
        signals.update(category_signals)
    return tuple(output)


def load_article_taxonomy(path: str | Path = DEFAULT_TAXONOMY_PATH) -> ArticleTaxonomy:
    taxonomy_path = Path(path)
    try:
        with taxonomy_path.open("rb") as source:
            raw = source.read(MAX_TAXONOMY_BYTES + 1)
    except OSError as exc:
        raise ValueError("taxonomy file cannot be read") from exc
    if not raw or len(raw) > MAX_TAXONOMY_BYTES:
        raise ValueError("taxonomy file is empty or exceeds its size limit")
    try:
        payload = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("taxonomy must be valid UTF-8 YAML") from exc
    root = _mapping(payload, name="taxonomy", fields=_TOP_LEVEL_FIELDS)
    if root["schema_version"] != TAXONOMY_SCHEMA_VERSION:
        raise ValueError("unsupported taxonomy schema_version")
    taxonomy_id = _normalized_string(root["taxonomy_id"], name="taxonomy_id", maximum=64)
    if _TOKEN.fullmatch(taxonomy_id) is None or taxonomy_id != DEFAULT_TAXONOMY_ID:
        raise ValueError("unsupported taxonomy_id")
    sha256 = hashlib.sha256(raw).hexdigest()
    if (
        taxonomy_path.resolve() == DEFAULT_TAXONOMY_PATH.resolve()
        and sha256 != DEFAULT_TAXONOMY_SHA256
    ):
        raise ValueError("default taxonomy does not match the immutable v1 SHA-256")
    return ArticleTaxonomy(
        schema_version=TAXONOMY_SCHEMA_VERSION,
        taxonomy_id=taxonomy_id,
        sha256=sha256,
        categories=_parse_categories(root["categories"]),
        constraints=_parse_constraints(root["constraints"]),
    )


def validate_semantic_bundle(
    value: Any,
    *,
    taxonomy: ArticleTaxonomy | None = None,
) -> dict[str, Any]:
    selected = taxonomy or load_article_taxonomy()
    bundle = _mapping(
        value,
        name="semantic bundle",
        fields={
            "schema_version",
            "taxonomy_id",
            "taxonomy_sha256",
            "summary",
            "categories",
            "keywords",
        },
    )
    if bundle["schema_version"] != "article-semantic-bundle.v1":
        raise ValueError("unsupported semantic bundle schema_version")
    if bundle["taxonomy_id"] != selected.taxonomy_id:
        raise ValueError("semantic bundle taxonomy_id does not match the configured taxonomy")
    if bundle["taxonomy_sha256"] != selected.sha256:
        raise ValueError("semantic bundle taxonomy_sha256 does not match the configured taxonomy")
    summary = _normalized_string(
        bundle["summary"],
        name="summary",
        maximum=selected.constraints.summary_max_chars,
    )
    if len(summary) < selected.constraints.summary_min_chars:
        raise ValueError("summary is shorter than the configured minimum")
    categories = _semantic_values(
        bundle["categories"],
        name="categories",
        minimum=selected.constraints.categories_min_items,
        maximum=selected.constraints.categories_max_items,
        item_maximum=100,
        allowed=selected.allowed_labels,
    )
    keywords = _semantic_values(
        bundle["keywords"],
        name="keywords",
        minimum=selected.constraints.keywords_min_items,
        maximum=selected.constraints.keywords_max_items,
        item_maximum=selected.constraints.keyword_max_chars,
    )
    if any(item.casefold() in selected.constraints.disallowed_keywords for item in keywords):
        raise ValueError("keywords contain a disallowed generic term")
    if any("," in item or ";" in item for item in keywords):
        raise ValueError("keywords cannot contain Markdown metadata separators")
    return {
        "schema_version": "article-semantic-bundle.v1",
        "taxonomy_id": selected.taxonomy_id,
        "taxonomy_sha256": selected.sha256,
        "summary": summary,
        "categories": list(categories),
        "keywords": list(keywords),
    }


def _semantic_values(
    value: Any,
    *,
    name: str,
    minimum: int,
    maximum: int,
    item_maximum: int,
    allowed: frozenset[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} must contain between {minimum} and {maximum} values")
    output: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = _normalized_string(raw, name=f"{name}[{index}]", maximum=item_maximum)
        key = item.casefold()
        if key in seen:
            raise ValueError(f"{name} must be case-insensitively unique")
        if allowed is not None and item not in allowed:
            raise ValueError(f"{name} contains a value outside the configured taxonomy")
        output.append(item)
        seen.add(key)
    return tuple(output)
