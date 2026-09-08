from __future__ import annotations

import hashlib
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

# Issue #93 evidence-based authoring: a single repository-owned pass that
# carries per-article evidence (article content / search snippet / none) and
# validates the response against the request's deterministic identity and
# the same summary-basis rules. v1 readers/fixtures keep working unchanged.
AUTHORING_CONTRACT_VERSION_V2 = "weekly-monitor-authoring.v2"
AUTHORING_REQUEST_SCHEMA_VERSION_V2 = "weekly-monitor-authoring-request.v2"
AUTHORING_RESPONSE_SCHEMA_VERSION_V2 = "weekly-monitor-authoring-response.v2"

_VALID_SUMMARY_BASIS = frozenset(
    {"article_content", "search_snippet", "page", "search_result",
     "change_event", "upstream_artifact", "legacy_v1"}
)

_RESPONSE_FIELDS = frozenset(
    {"schema_version", "contract_version", "article_count", "articles"}
)
_ARTICLE_FIELDS = frozenset({"article_id", "semantics"})

_V2_RESPONSE_FIELDS = frozenset(
    {"schema_version", "contract_version", "request_sha256",
     "article_count", "articles", "executive_summary", "stats"}
)
_V2_ARTICLE_AUTHORED_FIELDS = frozenset(
    {"relevant", "summary", "summary_basis", "evidence_hash",
     "categories", "keywords"}
)


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
    article_evidence: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    stats: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a single authoring request.

    When ``article_evidence`` is provided the v2 contract is used: the
    request carries per-article URL/origins/display_pillar/title/title_basis,
    evidence (status / attempts / selected_method / content_type /
    content_ref / content_hash / search_snippet), summary_basis, code-
    computed stats, and the prompt/taxonomy identity. Otherwise the v1
    contract is used unchanged so existing readers and fixtures keep working.
    """
    selected = taxonomy or load_article_taxonomy()
    ordered = _ordered_items(items)
    if article_evidence is not None:
        return _build_evidence_request(
            report_date=report_date,
            ordered=ordered,
            prompt=prompt,
            taxonomy=selected,
            article_evidence=article_evidence,
            stats=stats or {},
        )
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
    request: Mapping[str, Any] | None = None,
) -> AuthoringValidationResult:
    """Validate a single authoring response.

    v2 responses are dispatched to ``_validate_evidence_response`` and must
    carry their originating request so the validator can rebind the response
    article identities to the exact input set, deterministic stats, and the
    basis rules. v1 responses follow the original strict contract.
    """
    if response.get("schema_version") == AUTHORING_RESPONSE_SCHEMA_VERSION_V2:
        return _validate_evidence_response(
            items, response, taxonomy=taxonomy, request=request
        )
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


# ---------------------------------------------------------------------------
# v2: evidence-based authoring (Issue #93)
# ---------------------------------------------------------------------------


def _canonical_json_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_records(
    article_evidence: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    if isinstance(article_evidence, Mapping):
        records = article_evidence.get("records")
    else:
        records = article_evidence
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise AuthoringContractError("article evidence records must be a list")
    out: list[Mapping[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise AuthoringContractError("article evidence record must be an object")
        out.append(record)
    return out


def _build_evidence_request(
    *,
    report_date: date,
    ordered: Sequence[CandidateItem],
    prompt: LoadedPrompt,
    taxonomy: ArticleTaxonomy,
    article_evidence: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
) -> dict[str, Any]:
    records = _normalize_records(article_evidence)
    by_url: dict[str, Mapping[str, Any]] = {}
    for record in records:
        # Keep the URL identity assigned before acquisition. Different inputs
        # may redirect to the same landing page; the target is fetch metadata,
        # not permission to merge their candidates or lose their origins.
        identity_url = canonical_url(
            str(record.get("requested_url") or record.get("final_url") or "")
        )
        if not identity_url:
            raise AuthoringContractError("article evidence has missing URL identity")
        if identity_url in by_url:
            raise AuthoringContractError("article evidence has duplicate URL identity")
        by_url[identity_url] = record

    articles: list[dict[str, Any]] = []
    for item in ordered:
        canonical = _canonical_identity_url(item)
        record = by_url.pop(canonical, None)
        if record is None:
            raise AuthoringContractError("missing article evidence identity")
        extra_obj = record.get("extra")
        extra = extra_obj if isinstance(extra_obj, Mapping) else {}
        origins_raw = record.get("origins") or []
        if not isinstance(origins_raw, list):
            raise AuthoringContractError("article evidence origins must be a list")
        origins: list[dict[str, Any]] = []
        for entry in origins_raw:
            if not isinstance(entry, Mapping):
                raise AuthoringContractError("article evidence origin must be an object")
            origins.append({key: value for key, value in entry.items()})
        article_evidence_block = {
            "status": record.get("status"),
            "attempts": record.get("attempts"),
            "selected_method": record.get("selected_method"),
            "content_type": record.get("content_type"),
            "content_ref": record.get("content_ref"),
            "content_hash": record.get("content_hash"),
            "search_snippet": extra.get("search_snippet"),
            # upstream summary_basis is informational only; the response
            # authoritatively declares the authored basis it used.
            "upstream_summary_basis": record.get("summary_basis"),
        }
        # display_pillar fallback: explicit record value wins; otherwise
        # "A wins when any A origin exists, B otherwise" per the candidate
        # contract. This avoids mis-rendering cross-pillar merges when the
        # evidence record omits display_pillar.
        explicit_pillar = record.get("display_pillar")
        if explicit_pillar not in {None, "A", "B"}:
            raise AuthoringContractError("display_pillar must be A or B")
        if explicit_pillar in {"A", "B"}:
            display_pillar = explicit_pillar
        else:
            origins_pillars = {
                entry.get("pillar")
                for entry in origins
                if isinstance(entry, Mapping)
            }
            if "A" in origins_pillars:
                display_pillar = "A"
            else:
                display_pillar = "B" if item.lane == "research" else "A"
        if display_pillar not in {"A", "B"}:
            raise AuthoringContractError("display_pillar must be A or B")
        record_title = record.get("title")
        title = str(record_title) if record_title else item.title
        title_basis = record.get("title_basis") or (
            "url" if title == item.url else "upstream_artifact"
        )
        if title_basis not in {"page", "search_result", "upstream_artifact", "url"}:
            raise AuthoringContractError("title_basis must be one of page/search_result/upstream_artifact/url")
        articles.append(
            {
                "article_id": article_identity(item),
                "url": item.url,
                "canonical_url": canonical,
                "origins": origins,
                "display_pillar": display_pillar,
                "title": title,
                "title_basis": title_basis,
                "evidence": article_evidence_block,
            }
        )
    if by_url:
        raise AuthoringContractError("unknown article evidence identity")

    payload: dict[str, Any] = {
        "schema_version": AUTHORING_REQUEST_SCHEMA_VERSION_V2,
        "contract_version": AUTHORING_CONTRACT_VERSION_V2,
        "report_date": report_date.isoformat(),
        "prompt": {
            "id": prompt.prompt_id,
            "version": prompt.version,
            "sha256": prompt.sha256,
        },
        "taxonomy": {
            "schema_version": taxonomy.schema_version,
            "taxonomy_id": taxonomy.taxonomy_id,
            "sha256": taxonomy.sha256,
            "allowed_categories": [category.label for category in taxonomy.categories],
        },
        "stats": dict(stats),
        "articles": articles,
    }
    payload["request_sha256"] = _canonical_json_digest(
        _identity_only_payload(payload)
    )
    return payload


def _identity_only_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the request payload reduced to the identity-binding fields.

    ``classifier`` is excluded because the pre-classifier metadata
    (climate_signal, actuarial_signal, etc.) is an internal artifact of
    ``classify_candidate`` that may differ across two shell-based views of
    the same evidence. Per-article evidence and contract integrity are
    enforced by the strict field-by-field comparison in
    :func:`_validate_evidence_response`; ``request_sha256`` only needs to
    bind the response to the input set, prompt, taxonomy, stats, and evidence.
    """

    articles: list[dict[str, Any]] = []
    for article in payload.get("articles", []):
        articles.append(
            {
                "article_id": article.get("article_id"),
                "url": article.get("url"),
                "canonical_url": article.get("canonical_url"),
                "origins": article.get("origins"),
                "display_pillar": article.get("display_pillar"),
                "title": article.get("title"),
                "title_basis": article.get("title_basis"),
                "evidence": article.get("evidence"),
            }
        )
    return {
        "schema_version": payload.get("schema_version"),
        "contract_version": payload.get("contract_version"),
        "report_date": payload.get("report_date"),
        "prompt": payload.get("prompt"),
        "taxonomy": payload.get("taxonomy"),
        "stats": payload.get("stats"),
        "articles": articles,
    }


def _validate_evidence_response(
    items: Sequence[CandidateItem],
    response: Mapping[str, Any],
    *,
    taxonomy: ArticleTaxonomy | None,
    request: Mapping[str, Any] | None,
) -> AuthoringValidationResult:
    if request is None or request.get("schema_version") != AUTHORING_REQUEST_SCHEMA_VERSION_V2:
        raise AuthoringContractError("v2 authoring response requires its request")
    if set(response) != _V2_RESPONSE_FIELDS:
        raise AuthoringContractError("unexpected or missing v2 authoring response fields")
    if response.get("contract_version") != AUTHORING_CONTRACT_VERSION_V2:
        raise AuthoringContractError("unsupported authoring contract_version")
    if response.get("request_sha256") != request.get("request_sha256"):
        raise AuthoringContractError("authoring request identity was mutated")
    if response.get("stats") != request.get("stats"):
        raise AuthoringContractError("authoring deterministic stats were mutated")

    raw_articles = response.get("articles")
    if not isinstance(raw_articles, list):
        raise AuthoringContractError("authoring response articles must be a list")
    article_count = response.get("article_count")
    if type(article_count) is not int or article_count < 0:
        raise AuthoringContractError("authoring response article_count must be a non-negative integer")
    if article_count != len(raw_articles):
        raise AuthoringContractError("authoring response article_count does not match articles")

    # Issue #87 AC-2: enforce the canonical 6-key v2 stats shape (total/updated/
    # unchanged/blocked/failed/unresolved) and the deterministic mapping
    # ``total == updated + unchanged + blocked + failed + unresolved`` on every
    # v2 response. The driver repeats this validation before invoking the
    # orchestrator so ``MonitorRunResult.stats`` exposes the validated 57/42/15
    # split even if downstream validation is skipped.
    _validate_v2_stats_shape(response.get("stats"))

    requested = request.get("articles")
    if not isinstance(requested, list):
        raise AuthoringContractError("invalid v2 authoring request articles")
    request_by_id: dict[str, Mapping[str, Any]] = {}
    for article in requested:
        if not isinstance(article, Mapping):
            raise AuthoringContractError("v2 authoring request article must be an object")
        identity = article.get("article_id")
        if not isinstance(identity, str) or not identity:
            raise AuthoringContractError("v2 authoring request article_id must be a non-empty string")
        if identity in request_by_id:
            raise AuthoringContractError("duplicate article identity in v2 authoring request")
        request_by_id[identity] = article

    selected = taxonomy or load_article_taxonomy()
    ordered = _ordered_items(items)
    # Issue #87 AC-2 + AC-3: the v2 contract binds the response to the
    # exact articles emitted in the request (the full kept set from
    # ``article-evidence.v1``), not to the orchestrator's classified
    # subset. ``items`` may be a strict subset for a consumer; the response must mirror every request
    # article so the validator cannot accept a truncated response.
    request_ids = set(request_by_id)
    expected_ids = tuple(
        identity for identity in (article.get("article_id") for article in requested)
        if isinstance(identity, str) and identity in request_ids
    )
    if len(set(expected_ids)) != len(expected_ids):
        raise AuthoringContractError("final selected articles contain a duplicate article identity")

    # The v2 contract binds the response to the exact articles emitted in
    # the request. The response must cover every request article and may
    # not include extras. The orchestrator's classified ``kept`` subset is
    # accepted only when it equals the full request article set; when
    # ``items`` is a strict subset of the request, the validator fails
    # closed rather than silently downgrading the contract.
    request_ids = set(request_by_id)
    kept_ids = set(expected_ids)
    if kept_ids != request_ids:
        raise AuthoringContractError(
            "v2 authoring request does not cover the selected items; "
            "the response must mirror every request article"
        )
    if article_count != len(kept_ids):
        raise AuthoringContractError(
            "authoring response article_count does not match the request article set"
        )

    response_by_id: dict[str, Mapping[str, Any]] = {}
    for article in raw_articles:
        if not isinstance(article, Mapping):
            raise AuthoringContractError("authoring article must be an object")
        identity = article.get("article_id")
        if not isinstance(identity, str) or not identity:
            raise AuthoringContractError("authoring article_id must be a non-empty string")
        if identity in response_by_id:
            raise AuthoringContractError("duplicate article identity in authoring response")
        expected = request_by_id.get(identity)
        if expected is None:
            raise AuthoringContractError("unknown article identity in authoring response")
        unexpected = set(article) - set(expected) - _V2_ARTICLE_AUTHORED_FIELDS
        if unexpected:
            raise AuthoringContractError("unexpected v2 authoring article fields")
        missing = (set(expected) | _V2_ARTICLE_AUTHORED_FIELDS) - set(article)
        if missing:
            raise AuthoringContractError("missing v2 authoring article fields")
        for key, value in expected.items():
            if article.get(key) != value:
                raise AuthoringContractError(
                    f"authoring article input or evidence was mutated: key={key!r} "
                    f"expected={value!r} got={article.get(key)!r}"
                )
        response_by_id[identity] = article

    if set(response_by_id) != kept_ids:
        raise AuthoringContractError("missing article identity in authoring response")

    accepted: list[CandidateItem] = []
    accepted_ids: list[str] = []
    for item in ordered:
        # Final rendering may select a subset of the full URL request. Bind by
        # URL identity, never by that subset's position in the original batch.
        identity = article_identity(item)
        if identity not in response_by_id:
            raise AuthoringContractError("selected article is missing from the authoring request")
        article = response_by_id[identity]
        if type(article.get("relevant")) is not bool:
            raise AuthoringContractError("authoring relevance must be boolean")
        summary = article.get("summary")
        basis = article.get("summary_basis")
        evidence_hash = article.get("evidence_hash")
        evidence = article["evidence"]

        if basis == "none":
            if summary not in {None, ""}:
                raise AuthoringContractError("summary_basis none cannot carry summary evidence")
            if evidence_hash is not None:
                raise AuthoringContractError("summary_basis none cannot carry summary evidence")
            validated_summary = ""
        elif basis == "article_content":
            if evidence.get("status") != "ok" or not evidence.get("content_ref"):
                raise AuthoringContractError("article_content summary lacks content evidence")
            if evidence_hash != evidence.get("content_hash"):
                raise AuthoringContractError("article_content summary hash mismatch")
            if not isinstance(summary, str) or not summary.strip():
                raise AuthoringContractError("article_content summary must be non-empty")
            validated_summary = summary
        elif basis == "search_snippet":
            if not evidence.get("search_snippet"):
                raise AuthoringContractError("search_snippet summary lacks snippet evidence")
            if evidence_hash is not None:
                raise AuthoringContractError("search_snippet summary cannot pretend to be content")
            if not isinstance(summary, str) or not summary.strip():
                raise AuthoringContractError("search_snippet summary must be non-empty")
            validated_summary = summary
        elif basis in _VALID_SUMMARY_BASIS:
            if not isinstance(summary, str) or not summary.strip():
                raise AuthoringContractError("summary_basis summary must be non-empty")
            validated_summary = summary
        else:
            raise AuthoringContractError("unsupported summary_basis")

        if not article["relevant"]:
            # Irrelevant items keep their audit record but never enter the
            # accepted set, so their summary/basis are reported as-is.
            continue

        # Taxonomy validation remains the authority. URL-only relevant records
        # use a temporary validation summary, then retain an honestly empty one.
        taxonomy_summary = validated_summary or "Relevant URL retained without summary evidence."
        try:
            bundle = validate_semantic_bundle(
                {
                    "schema_version": "article-semantic-bundle.v1",
                    "taxonomy_id": selected.taxonomy_id,
                    "taxonomy_sha256": selected.sha256,
                    "summary": taxonomy_summary,
                    "categories": article.get("categories"),
                    "keywords": article.get("keywords"),
                },
                taxonomy=selected,
            )
        except ValueError as exc:
            raise AuthoringContractError("semantic bundle failed taxonomy validation") from exc
        bundle["summary"] = validated_summary
        accepted.append(
            replace(
                item,
                title=article["title"] or item.title,
                climate_related=True,
                actuarial_related=True,
                summary=validated_summary,
                categories=tuple(bundle["categories"]),
                keywords=tuple(bundle["keywords"]),
                semantics=bundle,
            )
        )
        accepted_ids.append(identity)

    return AuthoringValidationResult(
        items=tuple(accepted),
        article_identities=tuple(accepted_ids),
        article_count=len(accepted),
    )


# ---------------------------------------------------------------------------
# v2 stats shape (Issue #87 AC-2)
# ---------------------------------------------------------------------------

V2_STATS_FIELDS = (
    "total",
    "updated",
    "unchanged",
    "blocked",
    "failed",
    "unresolved",
)


def _validate_v2_stats_shape(raw: Any) -> dict[str, int]:
    """Validate the canonical 6-key v2 stats shape and the deterministic sum.

    Every v2 authoring response must carry exactly the keys
    ``{total, updated, unchanged, blocked, failed, unresolved}``. All values
    must be non-negative integers and ``total`` must equal
    ``updated + unchanged + blocked + failed + unresolved``.

    Returns the validated dict so callers can pin it on a result object
    (the driver writes it onto ``MonitorRunResult.stats``).
    """

    if not isinstance(raw, Mapping):
        raise AuthoringContractError("v2 authoring response stats must be an object")
    keys = set(raw)
    expected = frozenset(V2_STATS_FIELDS)
    if keys != expected:
        unexpected = keys - expected
        missing = expected - keys
        detail: list[str] = []
        if missing:
            detail.append(f"missing={sorted(missing)}")
        if unexpected:
            detail.append(f"unexpected={sorted(unexpected)}")
        raise AuthoringContractError(
            "v2 authoring response stats must declare the canonical 6-key shape: "
            + ", ".join(detail)
        )
    normalized: dict[str, int] = {}
    for field in V2_STATS_FIELDS:
        value = raw[field]
        if type(value) is not int or value < 0:
            raise AuthoringContractError(
                f"v2 authoring response stats.{field} must be a non-negative integer"
            )
        normalized[field] = value
    total = sum(
        normalized[field]
        for field in ("updated", "unchanged", "blocked", "failed", "unresolved")
    )
    if normalized["total"] != total:
        raise AuthoringContractError(
            "v2 authoring response stats.total must equal "
            "updated + unchanged + blocked + failed + unresolved "
            f"(got total={normalized['total']}, components_sum={total})"
        )
    return normalized
