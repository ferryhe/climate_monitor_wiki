from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..dedupe import canonical_url
from ..models import CandidateItem
from ..semantic_bundle import article_identity, render_order
from ..taxonomy import ArticleTaxonomy, load_article_taxonomy, validate_semantic_bundle
from .prompt_loader import LoadedPrompt


AUTHORING_CONTRACT_VERSION = "weekly-monitor-authoring.v1"
AUTHORING_REQUEST_SCHEMA_VERSION = "weekly-monitor-authoring-request.v1"
AUTHORING_RESPONSE_SCHEMA_VERSION = "weekly-monitor-authoring-response.v1"

_RESPONSE_FIELDS = frozenset(
    {"schema_version", "contract_version", "article_count", "articles"}
)
_ARTICLE_FIELDS = frozenset({"article_id", "semantics"})


class AuthoringContractError(ValueError):
    """The weekly authoring response does not match the selected article set."""


@dataclass(frozen=True)
class AuthoringValidationResult:
    items: tuple[CandidateItem, ...]
    article_identities: tuple[str, ...]
    article_count: int


def load_authoring_response(path: str | Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except AuthoringContractError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise AuthoringContractError("authoring response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthoringContractError("authoring response must be an object")
    return payload


def build_authoring_request(
    *,
    report_date: date,
    items: Sequence[CandidateItem],
    prompt: LoadedPrompt,
    taxonomy: ArticleTaxonomy | None = None,
) -> dict[str, Any]:
    selected = taxonomy or load_article_taxonomy()
    ordered = _ordered_items(items)
    return {
        "schema_version": AUTHORING_REQUEST_SCHEMA_VERSION,
        "contract_version": AUTHORING_CONTRACT_VERSION,
        "report_date": report_date.isoformat(),
        "prompt": {
            "id": prompt.prompt_id,
            "version": prompt.version,
            "sha256": prompt.sha256,
        },
        "taxonomy": {
            "schema_version": selected.schema_version,
            "taxonomy_id": selected.taxonomy_id,
            "sha256": selected.sha256,
            "allowed_categories": [category.label for category in selected.categories],
        },
        "articles": [_request_article(item) for item in ordered],
    }


def validate_authoring_response(
    items: Sequence[CandidateItem],
    response: Mapping[str, Any],
    *,
    taxonomy: ArticleTaxonomy | None = None,
) -> AuthoringValidationResult:
    selected = taxonomy or load_article_taxonomy()
    ordered = _ordered_items(items)
    expected_ids = tuple(article_identity(item) for item in ordered)
    if len(set(expected_ids)) != len(expected_ids):
        raise AuthoringContractError("final selected articles contain a duplicate article identity")

    _exact_fields(response, expected=_RESPONSE_FIELDS, label="authoring response")
    if response["schema_version"] != AUTHORING_RESPONSE_SCHEMA_VERSION:
        raise AuthoringContractError("unsupported authoring response schema_version")
    if response["contract_version"] != AUTHORING_CONTRACT_VERSION:
        raise AuthoringContractError("unsupported authoring contract_version")

    articles = response["articles"]
    if not isinstance(articles, list):
        raise AuthoringContractError("authoring response articles must be a list")
    article_count = response["article_count"]
    if type(article_count) is not int or article_count < 0:
        raise AuthoringContractError("authoring response article_count must be a non-negative integer")
    if article_count != len(articles):
        raise AuthoringContractError("authoring response article_count does not match articles")

    by_id: dict[str, dict[str, Any]] = {}
    for raw_article in articles:
        _exact_fields(raw_article, expected=_ARTICLE_FIELDS, label="authoring article")
        article_id = raw_article["article_id"]
        if not isinstance(article_id, str) or not article_id:
            raise AuthoringContractError("authoring article_id must be a non-empty string")
        if article_id in by_id:
            raise AuthoringContractError("duplicate article identity in authoring response")
        by_id[article_id] = dict(raw_article)

    expected_set = set(expected_ids)
    actual_set = set(by_id)
    unknown = actual_set - expected_set
    if unknown:
        raise AuthoringContractError("unknown article identity in authoring response")
    missing = expected_set - actual_set
    if missing or article_count < len(expected_ids):
        raise AuthoringContractError("missing article identity in authoring response")
    if article_count > len(expected_ids):
        raise AuthoringContractError("unknown article identity in authoring response")

    validated_items: list[CandidateItem] = []
    for item, identity in zip(ordered, expected_ids):
        try:
            bundle = validate_semantic_bundle(by_id[identity]["semantics"], taxonomy=selected)
        except ValueError as exc:
            raise AuthoringContractError(
                "semantic bundle failed taxonomy validation"
            ) from exc
        validated_items.append(
            replace(
                item,
                summary=str(bundle["summary"]),
                categories=tuple(bundle["categories"]),
                keywords=tuple(bundle["keywords"]),
                semantics=bundle,
            )
        )

    return AuthoringValidationResult(
        items=tuple(validated_items),
        article_identities=expected_ids,
        article_count=len(validated_items),
    )


def _ordered_items(items: Sequence[CandidateItem]) -> list[CandidateItem]:
    ordered = render_order(items)
    if len(ordered) != len(items):
        raise AuthoringContractError("every selected article must belong to a rendered lane")
    return ordered


def _request_article(item: CandidateItem) -> dict[str, Any]:
    return {
        "article_id": article_identity(item),
        "url": item.url,
        "canonical_url": _canonical_identity_url(item),
        "title": item.title,
        "source": item.source_name,
        "lane": item.lane,
        "content_hash": item.content_hash,
        "source_summary": item.summary,
        "detected_at": item.detected_at,
        "published": item.published,
        "classifier": {
            "climate_signal": item.climate_signal,
            "actuarial_signal": item.actuarial_signal,
            "categories": list(item.categories),
            "keywords": list(item.keywords),
            "topics": list(item.topics),
        },
    }


def _canonical_identity_url(item: CandidateItem) -> str:
    return canonical_url(item.url)


def _exact_fields(raw: Any, *, expected: frozenset[str], label: str) -> None:
    if not isinstance(raw, Mapping):
        raise AuthoringContractError(f"{label} must be an object")
    unexpected = set(raw) - expected
    if unexpected:
        raise AuthoringContractError(f"unexpected {label} fields")
    missing = expected - set(raw)
    if missing:
        raise AuthoringContractError(f"missing {label} fields")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthoringContractError("authoring response contains duplicate JSON keys")
        result[key] = value
    return result
