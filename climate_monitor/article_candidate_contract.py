"""Strict URL-first article candidate contract and read-only legacy adapters.

This module defines a portable contract.  It is deliberately not connected to
the runtime aggregation pipeline, state files, fetching, or publication.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from climate_registry.errors import RegistryInputError
from climate_registry.selection import MAX_SUMMARY, MAX_TITLE, MAX_URL, _validate_public_http_url

from .dedupe import canonical_url
from .semantic_bundle import article_identity


ARTICLE_CANDIDATE_SCHEMA_VERSION = "url-first-article-candidate.v1"
ARTICLE_CANDIDATE_BATCH_SCHEMA_VERSION = "url-first-article-candidate-batch.v1"
CANDIDATE_DIGEST_VERSION = "url-first-article-candidate-digest.v1"
BATCH_DIGEST_VERSION = "url-first-article-candidate-batch-digest.v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_PILLAR_A_ROOT_FIELDS = frozenset(
    {
        "date",
        "pillar",
        "sites_with_changes",
        "orgs_with_articles",
        "baseline_urls",
        "new_articles",
        "seen_before",
        "generated_at",
        "articles",
    }
)
_PILLAR_A_GROUP_FIELDS = frozenset({"org", "items"})
_PILLAR_A_ITEM_FIELDS = frozenset({"title", "url", "categories"})
_PILLAR_B_ITEM_FIELDS = frozenset({"title", "url", "source", "summary"})


class CandidateContractError(ValueError):
    """A candidate, batch, or adapter input does not satisfy the v1 contract."""


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


def _nonempty(value: str, *, field: str, maximum: int) -> str:
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be a non-empty, trimmed string of at most {maximum} characters")
    return value


def _optional_text(value: str | None, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _nonempty(value, field=field, maximum=maximum)


def _validate_timestamp(value: str) -> str:
    if not isinstance(value, str) or _RFC3339_TIMESTAMP.fullmatch(value) is None:
        raise ValueError(
            "discovered_at must use RFC 3339 YYYY-MM-DDTHH:MM:SS with Z or a numeric offset"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("discovered_at must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("discovered_at must include an offset")
    return value


def _validate_url(value: str) -> str:
    if len(value) > MAX_URL:
        raise ValueError(f"URL exceeds {MAX_URL} characters")
    try:
        _validate_public_http_url(value)
    except RegistryInputError as exc:
        raise ValueError("URL must be a valid public HTTP(S) URL") from exc
    return value


class ArtifactIdentity(_ContractModel):
    """Stable identity of the exact input artifact read by an adapter."""

    artifact_id: str
    sha256: str

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id(cls, value: str) -> str:
        return _nonempty(value, field="artifact_id", maximum=512)

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("input artifact sha256 must be 64 lowercase hexadecimal characters")
        return value


class CandidateOrigin(_ContractModel):
    """One exact discovery occurrence for a candidate URL."""

    pillar: Literal["A", "B"]
    source: str
    url: str
    input_artifact: ArtifactIdentity
    row: str
    discovered_at: str
    original_title: str | None
    title_basis: Literal["page", "search_result", "upstream_artifact", "url"] | None
    original_summary: str | None
    summary_basis: Literal[
        "page", "search_result", "change_event", "upstream_artifact"
    ] | None
    original_snippet: str | None
    snippet_basis: Literal[
        "page", "search_result", "change_event", "upstream_artifact"
    ] | None

    @field_validator("source")
    @classmethod
    def _source(cls, value: str) -> str:
        return _nonempty(value, field="source", maximum=500)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _validate_url(value)

    @field_validator("row")
    @classmethod
    def _row(cls, value: str) -> str:
        value = _nonempty(value, field="row", maximum=512)
        if not value.startswith("/") or re.search(r"~(?:[^01]|$)", value):
            raise ValueError("row must be an RFC 6901 JSON Pointer")
        return value

    @field_validator("discovered_at")
    @classmethod
    def _discovered_at(cls, value: str) -> str:
        return _validate_timestamp(value)

    @field_validator("original_title")
    @classmethod
    def _original_title(cls, value: str | None) -> str | None:
        return _optional_text(value, field="original_title", maximum=MAX_TITLE)

    @field_validator("original_summary", "original_snippet")
    @classmethod
    def _original_long_text(cls, value: str | None, info: Any) -> str | None:
        return _optional_text(value, field=info.field_name, maximum=MAX_SUMMARY)

    @model_validator(mode="after")
    def _evidence_pairs(self) -> "CandidateOrigin":
        for value_name, basis_name in (
            ("original_title", "title_basis"),
            ("original_summary", "summary_basis"),
            ("original_snippet", "snippet_basis"),
        ):
            if (getattr(self, value_name) is None) != (getattr(self, basis_name) is None):
                raise ValueError(f"{value_name} and {basis_name} must be present together")
        return self


class ArticleCandidate(_ContractModel):
    """One URL identity with every retained discovery origin."""

    schema_version: Literal["url-first-article-candidate.v1"]
    url: str
    canonical_url: str
    article_id: str
    display_pillar: Literal["A", "B"]
    origins: list[CandidateOrigin] = Field(min_length=1)
    title: str | None
    title_basis: Literal["page", "search_result", "upstream_artifact", "url"] | None
    summary: str | None
    summary_basis: Literal[
        "page", "search_result", "change_event", "upstream_artifact"
    ] | None
    categories: list[str] | None
    categories_basis: Literal["upstream_classification"] | None
    candidate_digest: str

    @field_validator("url", "canonical_url")
    @classmethod
    def _urls(cls, value: str) -> str:
        return _validate_url(value)

    @field_validator("article_id", "candidate_digest")
    @classmethod
    def _digests(cls, value: str, info: Any) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError(f"{info.field_name} must be a lowercase SHA-256 digest")
        return value

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        return _optional_text(value, field="title", maximum=MAX_TITLE)

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str | None) -> str | None:
        return _optional_text(value, field="summary", maximum=MAX_SUMMARY)

    @field_validator("categories")
    @classmethod
    def _categories(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("categories must be non-empty when present")
        checked = [
            _nonempty(item, field="category", maximum=100)
            for item in value
        ]
        if len(set(checked)) != len(checked):
            raise ValueError("categories must be unique")
        if checked != sorted(checked):
            raise ValueError("categories must use canonical lexical order")
        return checked

    @model_validator(mode="after")
    def _identity_and_digest(self) -> "ArticleCandidate":
        for value_name, basis_name in (
            ("title", "title_basis"),
            ("summary", "summary_basis"),
            ("categories", "categories_basis"),
        ):
            if (getattr(self, value_name) is None) != (getattr(self, basis_name) is None):
                raise ValueError(f"{value_name} and {basis_name} must be present together")

        if (
            self.title is not None
            and self.title_basis != "url"
            and not any(
                (origin.original_title, origin.title_basis) == (self.title, self.title_basis)
                for origin in self.origins
            )
        ):
            raise ValueError("candidate title evidence must match a retained origin")
        if self.summary is not None and not any(
            (origin.original_summary, origin.summary_basis) == (self.summary, self.summary_basis)
            for origin in self.origins
        ):
            raise ValueError("candidate summary evidence must match a retained origin")

        expected_canonical = canonical_url(self.url)
        if self.canonical_url != expected_canonical:
            raise ValueError("canonical_url does not match climate_monitor.dedupe.canonical_url(url)")
        expected_article_id = article_identity({"url": self.url})
        if self.article_id != expected_article_id:
            raise ValueError("article_id does not match article-identity.v1")

        identities = [_origin_identity(origin) for origin in self.origins]
        if len(set(identities)) != len(identities):
            raise ValueError("candidate contains a duplicate origin identity")
        if identities != sorted(identities):
            raise ValueError("origins are not in canonical identity order")
        for origin in self.origins:
            if canonical_url(origin.url) != self.canonical_url:
                raise ValueError("origin URL does not match the candidate canonical_url")
        if self.url != min(origin.url for origin in self.origins):
            raise ValueError("candidate url must be the canonical lexical representative of origin URLs")

        expected_pillar = "A" if any(origin.pillar == "A" for origin in self.origins) else "B"
        if self.display_pillar != expected_pillar:
            raise ValueError("display_pillar must be A when any A origin exists, otherwise B")
        if self.candidate_digest != _candidate_digest_for_model(self):
            raise ValueError("candidate_digest does not match canonical candidate bytes")
        return self


class ArticleCandidateBatch(_ContractModel):
    """Deterministically ordered candidate collection with a bound digest."""

    schema_version: Literal["url-first-article-candidate-batch.v1"]
    candidate_count: int = Field(ge=0)
    candidates: list[ArticleCandidate]
    batch_digest: str

    @field_validator("candidate_count")
    @classmethod
    def _count_is_not_bool(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("candidate_count must be an integer")
        return value

    @field_validator("batch_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("batch_digest must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _collection_and_digest(self) -> "ArticleCandidateBatch":
        if self.candidate_count != len(self.candidates):
            raise ValueError("candidate_count does not match candidates")
        identities = [candidate.article_id for candidate in self.candidates]
        if len(set(identities)) != len(identities):
            raise ValueError("batch contains a duplicate candidate identity")
        origin_identities = [
            _origin_identity(origin)
            for candidate in self.candidates
            for origin in candidate.origins
        ]
        if len(set(origin_identities)) != len(origin_identities):
            raise ValueError("batch contains a duplicate origin identity")
        artifact_rows: dict[tuple[str, str, str], str] = {}
        for candidate in self.candidates:
            for origin in candidate.origins:
                artifact_row = (
                    origin.input_artifact.artifact_id,
                    origin.input_artifact.sha256,
                    origin.row,
                )
                previous_article = artifact_rows.setdefault(
                    artifact_row, candidate.article_id
                )
                if previous_article != candidate.article_id:
                    raise ValueError(
                        "batch assigns the same input artifact row to different candidates"
                    )
        canonical_order = sorted(self.candidates, key=lambda item: item.canonical_url)
        if identities != [candidate.article_id for candidate in canonical_order]:
            raise ValueError("candidates are not in canonical URL order")
        if self.batch_digest != _batch_digest_for_model(self):
            raise ValueError("batch_digest does not match canonical batch bytes")
        return self


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CandidateContractError("contract contains a value that canonical JSON cannot encode") from exc
    if "\r" in text:
        raise CandidateContractError("canonical JSON must not contain carriage returns")
    return text.encode("utf-8")


def _versioned_digest(version: str, payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(version.encode("ascii"))
    digest.update(b"\n")
    digest.update(_canonical_json_bytes(payload))
    return digest.hexdigest()


def _candidate_digest_for_model(candidate: ArticleCandidate) -> str:
    payload = candidate.model_dump(mode="json", exclude={"candidate_digest"})
    return _versioned_digest(CANDIDATE_DIGEST_VERSION, payload)


def _batch_digest_for_model(batch: ArticleCandidateBatch) -> str:
    payload = batch.model_dump(mode="json", exclude={"batch_digest"})
    return _versioned_digest(BATCH_DIGEST_VERSION, payload)


def _origin_identity(origin: CandidateOrigin) -> tuple[str, str, str, str, str]:
    return (
        origin.pillar,
        origin.source,
        origin.input_artifact.artifact_id,
        origin.input_artifact.sha256,
        origin.row,
    )


def _as_origin(value: CandidateOrigin | Mapping[str, Any]) -> CandidateOrigin:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, CandidateOrigin)
        else dict(value) if isinstance(value, Mapping) else value
    )
    if isinstance(payload, dict):
        for field in (
            "original_title",
            "title_basis",
            "original_summary",
            "summary_basis",
            "original_snippet",
            "snippet_basis",
        ):
            payload.setdefault(field, None)
    try:
        return CandidateOrigin.model_validate(payload)
    except ValidationError as exc:
        raise CandidateContractError(f"origin is invalid: {_validation_message(exc)}") from exc


def _validation_message(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_context=False)
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'contract'}: {error['msg']}"
        for error in errors
    )


def validate_candidate(value: ArticleCandidate | Mapping[str, Any]) -> ArticleCandidate:
    """Validate and recompute all v1 candidate identity and digest bindings."""

    payload = value.model_dump(mode="json") if isinstance(value, ArticleCandidate) else value
    try:
        return ArticleCandidate.model_validate(payload)
    except ValidationError as exc:
        raise CandidateContractError(_validation_message(exc)) from exc


def validate_candidate_batch(
    value: ArticleCandidateBatch | Mapping[str, Any],
) -> ArticleCandidateBatch:
    """Validate a strict v1 batch, including every nested candidate."""

    payload = value.model_dump(mode="json") if isinstance(value, ArticleCandidateBatch) else value
    try:
        return ArticleCandidateBatch.model_validate(payload)
    except ValidationError as exc:
        raise CandidateContractError(_validation_message(exc)) from exc


def serialize_candidate(value: ArticleCandidate | Mapping[str, Any]) -> bytes:
    """Return validated canonical UTF-8 JSON with one trailing LF."""

    candidate = validate_candidate(value)
    return _canonical_json_bytes(candidate.model_dump(mode="json")) + b"\n"


def serialize_candidate_batch(value: ArticleCandidateBatch | Mapping[str, Any]) -> bytes:
    """Return validated canonical UTF-8 batch JSON with one trailing LF."""

    batch = validate_candidate_batch(value)
    return _canonical_json_bytes(batch.model_dump(mode="json")) + b"\n"


def candidate_digest(value: ArticleCandidate | Mapping[str, Any]) -> str:
    """Return the recomputed digest of a validated candidate."""

    return _candidate_digest_for_model(validate_candidate(value))


def batch_digest(value: ArticleCandidateBatch | Mapping[str, Any]) -> str:
    """Return the recomputed digest of a validated batch."""

    return _batch_digest_for_model(validate_candidate_batch(value))


def build_candidate(
    *,
    url: str,
    origins: Sequence[CandidateOrigin | Mapping[str, Any]],
    title: str | None = None,
    title_basis: Literal["page", "search_result", "upstream_artifact", "url"] | None = None,
    summary: str | None = None,
    summary_basis: Literal[
        "page", "search_result", "change_event", "upstream_artifact"
    ] | None = None,
    categories: Sequence[str] | None = None,
    categories_basis: Literal["upstream_classification"] | None = None,
) -> ArticleCandidate:
    """Construct a candidate while deriving every identity/display field."""

    checked_origins = sorted((_as_origin(origin) for origin in origins), key=_origin_identity)
    if not checked_origins:
        raise CandidateContractError("candidate must contain at least one origin")
    canonical = canonical_url(url)
    if any(canonical_url(origin.url) != canonical for origin in checked_origins):
        raise CandidateContractError("all origin URLs must share the candidate canonical URL")
    representative = min(origin.url for origin in checked_origins)
    if url != representative:
        raise CandidateContractError("url must be the lexical representative of origin URLs")

    checked_categories: list[str] | None = None
    if categories is not None:
        if (
            isinstance(categories, (str, bytes, bytearray, Mapping))
            or not isinstance(categories, Sequence)
        ):
            raise CandidateContractError("categories must be a sequence of strings")
        checked_categories = list(categories)
        if any(not isinstance(category, str) for category in checked_categories):
            raise CandidateContractError("categories must be a sequence of strings")

    payload: dict[str, Any] = {
        "schema_version": ARTICLE_CANDIDATE_SCHEMA_VERSION,
        "url": url,
        "canonical_url": canonical,
        "article_id": article_identity({"url": url}),
        "display_pillar": "A" if any(origin.pillar == "A" for origin in checked_origins) else "B",
        "origins": [origin.model_dump(mode="json") for origin in checked_origins],
        "title": title,
        "title_basis": title_basis,
        "summary": summary,
        "summary_basis": summary_basis,
        "categories": sorted(checked_categories) if checked_categories is not None else None,
        "categories_basis": categories_basis,
    }
    payload["candidate_digest"] = _versioned_digest(CANDIDATE_DIGEST_VERSION, payload)
    return validate_candidate(payload)


def build_candidate_batch(
    candidates: Sequence[ArticleCandidate | Mapping[str, Any]],
) -> ArticleCandidateBatch:
    """Construct a strict batch; duplicate URL identities are rejected."""

    checked = sorted((validate_candidate(candidate) for candidate in candidates), key=lambda item: item.canonical_url)
    identities = [candidate.article_id for candidate in checked]
    if len(set(identities)) != len(identities):
        raise CandidateContractError("batch contains a duplicate candidate identity")
    payload: dict[str, Any] = {
        "schema_version": ARTICLE_CANDIDATE_BATCH_SCHEMA_VERSION,
        "candidate_count": len(checked),
        "candidates": [candidate.model_dump(mode="json") for candidate in checked],
    }
    payload["batch_digest"] = _versioned_digest(BATCH_DIGEST_VERSION, payload)
    return validate_candidate_batch(payload)


def _candidate_pair(
    candidates: Sequence[ArticleCandidate], value_name: str, basis_name: str
) -> tuple[Any, Any]:
    priority = {
        "page": 0,
        "search_result": 1,
        "upstream_artifact": 2,
        "change_event": 2,
        "url": 3,
    }
    pairs = [
        (getattr(candidate, value_name), getattr(candidate, basis_name))
        for candidate in candidates
        if getattr(candidate, value_name) is not None
    ]
    if not pairs:
        return None, None
    return min(pairs, key=lambda pair: (priority[pair[1]], json.dumps(pair[0], ensure_ascii=False)))


def merge_candidates(
    *collections: Iterable[ArticleCandidate | Mapping[str, Any]],
) -> list[ArticleCandidate]:
    """Merge equal URL identities, preserving every distinct origin.

    Exact repeated origin identities are collapsed only when their full values
    agree.  Conflicting records for one origin identity fail closed.
    """

    grouped: dict[str, list[ArticleCandidate]] = {}
    for value in (item for collection in collections for item in collection):
        candidate = validate_candidate(value)
        grouped.setdefault(candidate.canonical_url, []).append(candidate)

    merged: list[ArticleCandidate] = []
    for canonical in sorted(grouped):
        candidates = grouped[canonical]
        origins_by_identity: dict[tuple[str, str, str, str, str], CandidateOrigin] = {}
        for candidate in candidates:
            for origin in candidate.origins:
                identity = _origin_identity(origin)
                previous = origins_by_identity.get(identity)
                if previous is not None and previous != origin:
                    raise CandidateContractError("conflicting duplicate origin identity")
                origins_by_identity[identity] = origin
        origins = sorted(origins_by_identity.values(), key=_origin_identity)
        title, title_basis = _candidate_pair(candidates, "title", "title_basis")
        summary, summary_basis = _candidate_pair(candidates, "summary", "summary_basis")
        categories = sorted(
            {
                category
                for candidate in candidates
                for category in (candidate.categories or [])
            }
        ) or None
        merged.append(
            build_candidate(
                url=min(origin.url for origin in origins),
                origins=origins,
                title=title,
                title_basis=title_basis,
                summary=summary,
                summary_basis=summary_basis,
                categories=categories,
                categories_basis="upstream_classification" if categories else None,
            )
        )
    return merged


def _artifact(artifact_id: str, artifact_sha256: str) -> ArtifactIdentity:
    try:
        return ArtifactIdentity.model_validate(
            {"artifact_id": artifact_id, "sha256": artifact_sha256}
        )
    except ValidationError as exc:
        raise CandidateContractError(f"input artifact identity is invalid: {_validation_message(exc)}") from exc


def _exact_mapping(value: Any, fields: frozenset[str], *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CandidateContractError(f"{label} has an invalid field set")
    return value


def _adapter_text(value: Any, *, field: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise CandidateContractError(f"{field} must be a string")
    if allow_empty and value == "":
        return value
    try:
        return _nonempty(value, field=field, maximum=maximum)
    except ValueError as exc:
        raise CandidateContractError(str(exc)) from exc


def _adapter_count(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CandidateContractError(f"{field} must be a non-negative integer")
    return value


def _adapter_date(value: Any) -> str:
    if not isinstance(value, str):
        raise CandidateContractError("article_changes date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CandidateContractError("article_changes date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise CandidateContractError("article_changes date must be YYYY-MM-DD")
    return value


def adapt_article_changes(
    payload: Mapping[str, Any],
    *,
    artifact_id: str,
    artifact_sha256: str,
) -> list[ArticleCandidate]:
    """Read the current ``article_changes_DATE.json`` shape without mutation."""

    root = _exact_mapping(payload, _PILLAR_A_ROOT_FIELDS, label="article_changes artifact")
    _adapter_date(root["date"])
    if root["pillar"] != "A":
        raise CandidateContractError("article_changes artifact must declare pillar A")
    generated_at = root["generated_at"]
    if not isinstance(generated_at, str):
        raise CandidateContractError("article_changes generated_at must be a string")
    try:
        _validate_timestamp(generated_at)
    except ValueError as exc:
        raise CandidateContractError(str(exc)) from exc
    artifact = _artifact(artifact_id, artifact_sha256)

    articles = root["articles"]
    if not isinstance(articles, list):
        raise CandidateContractError("article_changes articles must be an array")
    sites = _adapter_count(root["sites_with_changes"], field="sites_with_changes")
    org_count = _adapter_count(root["orgs_with_articles"], field="orgs_with_articles")
    _adapter_count(root["baseline_urls"], field="baseline_urls")
    new_count = _adapter_count(root["new_articles"], field="new_articles")
    _adapter_count(root["seen_before"], field="seen_before")
    if org_count != len(articles):
        raise CandidateContractError("orgs_with_articles does not match articles")
    if sites < org_count:
        raise CandidateContractError("sites_with_changes cannot be smaller than orgs_with_articles")

    candidates: list[ArticleCandidate] = []
    actual_new = 0
    for group_index, raw_group in enumerate(articles):
        group = _exact_mapping(
            raw_group, _PILLAR_A_GROUP_FIELDS, label=f"article_changes articles[{group_index}]"
        )
        source = _adapter_text(group["org"], field="org", maximum=500)
        items = group["items"]
        if not isinstance(items, list) or not items:
            raise CandidateContractError("article_changes organization items must be a non-empty array")
        actual_new += len(items)
        for item_index, raw_item in enumerate(items):
            item = _exact_mapping(
                raw_item,
                _PILLAR_A_ITEM_FIELDS,
                label=f"article_changes articles[{group_index}].items[{item_index}]",
            )
            title = _adapter_text(
                item["title"], field="title", maximum=MAX_TITLE, allow_empty=True
            )
            url = _adapter_text(item["url"], field="url", maximum=MAX_URL)
            categories = item["categories"]
            if not isinstance(categories, list) or not categories:
                raise CandidateContractError("article_changes categories must be a non-empty array")
            checked_categories = [
                _adapter_text(value, field="category", maximum=100) for value in categories
            ]
            origin = {
                "pillar": "A",
                "source": source,
                "url": url,
                "input_artifact": artifact.model_dump(mode="json"),
                "row": f"/articles/{group_index}/items/{item_index}",
                "discovered_at": generated_at,
            }
            if title:
                origin.update(
                    original_title=title,
                    title_basis="upstream_artifact",
                )
            candidates.append(
                build_candidate(
                    url=url,
                    origins=[origin],
                    categories=checked_categories,
                    categories_basis="upstream_classification",
                )
            )
    if actual_new != new_count:
        raise CandidateContractError("new_articles does not match the number of article rows")
    return merge_candidates(candidates)


def adapt_pillar_b(
    payload: Sequence[Mapping[str, Any]],
    *,
    artifact_id: str,
    artifact_sha256: str,
    discovered_at: str,
) -> list[ArticleCandidate]:
    """Read the current ``pillar_b_DATE.json`` array without mutation."""

    if not isinstance(payload, list):
        raise CandidateContractError("pillar_b artifact must be a JSON array")
    try:
        _validate_timestamp(discovered_at)
    except (TypeError, ValueError) as exc:
        raise CandidateContractError(
            "pillar_b discovered_at must be an RFC 3339 timestamp with an offset"
        ) from exc
    artifact = _artifact(artifact_id, artifact_sha256)
    candidates: list[ArticleCandidate] = []
    for index, raw_item in enumerate(payload):
        item = _exact_mapping(raw_item, _PILLAR_B_ITEM_FIELDS, label=f"pillar_b[{index}]")
        title = _adapter_text(
            item["title"], field="title", maximum=MAX_TITLE, allow_empty=True
        )
        url = _adapter_text(item["url"], field="url", maximum=MAX_URL)
        source = _adapter_text(item["source"], field="source", maximum=500)
        if source != "web":
            raise CandidateContractError("pillar_b source must be web")
        summary = _adapter_text(
            item["summary"], field="summary", maximum=MAX_SUMMARY, allow_empty=True
        )
        origin_payload: dict[str, Any] = {
            "pillar": "B",
            "source": source,
            "url": url,
            "input_artifact": artifact.model_dump(mode="json"),
            "row": f"/{index}",
            "discovered_at": discovered_at,
        }
        if title:
            origin_payload.update(
                original_title=title,
                title_basis="search_result",
            )
        if summary:
            origin_payload.update(
                original_summary=summary,
                summary_basis="search_result",
            )
        candidates.append(
            build_candidate(
                url=url,
                origins=[origin_payload],
                title=title or None,
                title_basis="search_result" if title else None,
                summary=summary or None,
                summary_basis="search_result" if summary else None,
            )
        )
    return merge_candidates(candidates)


__all__ = [
    "ARTICLE_CANDIDATE_BATCH_SCHEMA_VERSION",
    "ARTICLE_CANDIDATE_SCHEMA_VERSION",
    "ArtifactIdentity",
    "ArticleCandidate",
    "ArticleCandidateBatch",
    "BATCH_DIGEST_VERSION",
    "CANDIDATE_DIGEST_VERSION",
    "CandidateContractError",
    "CandidateOrigin",
    "adapt_article_changes",
    "adapt_pillar_b",
    "batch_digest",
    "build_candidate",
    "build_candidate_batch",
    "candidate_digest",
    "merge_candidates",
    "serialize_candidate",
    "serialize_candidate_batch",
    "validate_candidate",
    "validate_candidate_batch",
]
