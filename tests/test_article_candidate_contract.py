from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from climate_monitor.article_candidate_contract import (
    ARTICLE_CANDIDATE_BATCH_SCHEMA_VERSION,
    ARTICLE_CANDIDATE_SCHEMA_VERSION,
    ArtifactIdentity,
    CandidateContractError,
    adapt_article_changes,
    adapt_pillar_b,
    batch_digest,
    build_candidate,
    build_candidate_batch,
    candidate_digest,
    merge_candidates,
    serialize_candidate,
    serialize_candidate_batch,
    validate_candidate,
    validate_candidate_batch,
)
from climate_monitor.dedupe import canonical_url
from climate_monitor.semantic_bundle import article_identity


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "monitoring" / "schemas"
FIXTURE_DIR = ROOT / "monitoring" / "fixtures" / "url_first_article_candidate_v1"
CANDIDATE_SCHEMA_PATH = SCHEMA_DIR / "url_first_article_candidate_v1.schema.json"
BATCH_SCHEMA_PATH = SCHEMA_DIR / "url_first_article_candidate_batch_v1.schema.json"


def _artifact(name: str = "input.json", digest: str = "a" * 64) -> ArtifactIdentity:
    return ArtifactIdentity(artifact_id=name, sha256=digest)


def _origin(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "pillar": "B",
        "source": "web",
        "url": "https://example.org/articles/climate-risk",
        "input_artifact": _artifact().model_dump(mode="json"),
        "row": "/0",
        "discovered_at": "2026-09-05T12:00:00Z",
    }
    payload.update(overrides)
    return payload


def _candidate(**overrides: object):
    url = str(overrides.pop("url", "https://example.org/articles/climate-risk"))
    origin = _origin(url=url)
    return build_candidate(url=url, origins=[origin], **overrides)


def _schema_registry(candidate_schema: dict[str, object]) -> Registry:
    return Registry().with_resource(
        candidate_schema["$id"], Resource.from_contents(candidate_schema)
    )


def test_contract_uses_existing_url_and_article_identity_authorities():
    raw = "HTTPS://Example.ORG/report/?edition=2026&utm_source=mail#findings"
    candidate = _candidate(url=raw)

    assert candidate.canonical_url == canonical_url(raw)
    assert candidate.canonical_url == "https://example.org/report?edition=2026"
    assert candidate.article_id == article_identity({"url": raw})


@pytest.mark.parametrize(
    "url",
    [
        "https://[not-an-ip]/x",
        "https://bad..example/x",
        "https://-bad.example/x",
        "https://example.org:abc/x",
    ],
)
def test_common_malformed_http_urls_fail_python_and_schema(url):
    with pytest.raises(CandidateContractError, match="URL"):
        build_candidate(
            url=url,
            origins=[_origin(url=url)],
        )

    schema = json.loads(CANDIDATE_SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = json.loads(
        (FIXTURE_DIR / "positive" / "candidate__url_only.json").read_text(
            encoding="utf-8"
        )
    )
    payload["url"] = url
    payload["canonical_url"] = url
    payload["origins"][0]["url"] = url
    assert list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload)
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/x",
        "http://example.org:8080/x?query=1",
        "https://8.8.8.8/x",
        "https://[2001:4860:4860::8888]/x",
        "https://example.org:8443/x",
    ],
)
def test_supported_http_host_and_port_forms_pass_python_and_schema(url):
    candidate = build_candidate(
        url=url,
        origins=[_origin(url=url)],
    )

    schema = json.loads(CANDIDATE_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(
        candidate.model_dump(mode="json")
    )


def test_url_only_candidate_is_valid_and_url_label_has_explicit_basis():
    url_only = _candidate()
    labelled = _candidate(title="climate-risk", title_basis="url")

    assert url_only.title is None
    assert url_only.summary is None
    assert url_only.categories is None
    assert validate_candidate(url_only.model_dump(mode="json")) == url_only
    assert labelled.title_basis == "url"
    assert labelled.origins[0].original_title is None


@pytest.mark.parametrize(
    ("display_fields", "message"),
    [
        (
            {"title": "Unsupported title", "title_basis": "search_result"},
            "title evidence.*retained origin",
        ),
        (
            {"summary": "Unsupported summary", "summary_basis": "search_result"},
            "summary evidence.*retained origin",
        ),
    ],
)
def test_non_url_display_evidence_must_match_a_retained_origin(display_fields, message):
    with pytest.raises(CandidateContractError, match=message):
        build_candidate(
            url="https://example.org/unsupported-evidence",
            origins=[_origin(url="https://example.org/unsupported-evidence")],
            **display_fields,
        )


@pytest.mark.parametrize("categories", [["zeta", "alpha"], ("zeta", "alpha")])
def test_build_candidate_accepts_category_sequences_in_canonical_order(categories):
    candidate = _candidate(
        categories=categories,
        categories_basis="upstream_classification",
    )

    assert candidate.categories == ["alpha", "zeta"]


@pytest.mark.parametrize(
    "categories",
    ["risk", b"risk", {"risk": True}, {"risk"}, 42],
)
def test_build_candidate_rejects_non_category_sequences(categories):
    with pytest.raises(CandidateContractError, match="categories"):
        _candidate(
            categories=categories,
            categories_basis="upstream_classification",
        )


def test_same_title_and_same_input_bytes_do_not_merge_different_urls():
    first_url = "https://example.org/articles/climate-risk"
    first = build_candidate(
        url=first_url,
        origins=[
            _origin(
                url=first_url,
                original_title="Shared title",
                title_basis="search_result",
            )
        ],
        title="Shared title",
        title_basis="search_result",
    )
    second_url = "https://example.net/articles/climate-risk"
    second = build_candidate(
        url=second_url,
        origins=[
            _origin(
                url=second_url,
                input_artifact=_artifact(digest="a" * 64).model_dump(mode="json"),
                original_title="Shared title",
                title_basis="search_result",
            )
        ],
        title="Shared title",
        title_basis="search_result",
    )

    merged = merge_candidates([first, second])

    assert len(merged) == 2
    assert first.article_id != second.article_id


def test_same_url_a_and_b_preserves_origins_and_prefers_display_pillar_a():
    url = "https://example.org/report?edition=2026"
    pillar_a = build_candidate(
        url=url,
        origins=[
            _origin(
                pillar="A",
                source="Example Org",
                url=url,
                row="/articles/0/items/0",
                original_title="Original A title",
                title_basis="upstream_artifact",
            )
        ],
        categories=["financial_risk"],
        categories_basis="upstream_classification",
    )
    pillar_b = build_candidate(
        url=f"{url}&utm_source=search#result",
        origins=[
            _origin(
                url=f"{url}&utm_source=search#result",
                original_title="Search title",
                title_basis="search_result",
                original_summary="Search snippet summary",
                summary_basis="search_result",
            )
        ],
        title="Search title",
        title_basis="search_result",
        summary="Search snippet summary",
        summary_basis="search_result",
    )

    (merged,) = merge_candidates([pillar_b], [pillar_a])

    assert merged.display_pillar == "A"
    assert len(merged.origins) == 2
    assert [origin.pillar for origin in merged.origins] == ["A", "B"]
    assert merged.title == "Search title"
    assert merged.categories == ["financial_risk"]


def test_merge_deduplicates_only_identical_origin_identities():
    original = _candidate()
    same = _candidate()
    different_row = build_candidate(
        url=original.url,
        origins=[_origin(row="/1")],
    )

    (deduplicated,) = merge_candidates([original], [same])
    (both_origins,) = merge_candidates([original], [different_row])

    assert len(deduplicated.origins) == 1
    assert len(both_origins.origins) == 2

    conflicting = build_candidate(
        url=original.url,
        origins=[
            _origin(
                original_title="conflict",
                title_basis="search_result",
            )
        ],
    )
    with pytest.raises(CandidateContractError, match="conflicting duplicate origin"):
        merge_candidates([original], [conflicting])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("url", "file:///tmp/report", "URL"),
        ("canonical_url", "https://example.org/wrong", "canonical_url"),
        ("article_id", "0" * 64, "article_id"),
        ("display_pillar", "A", "display_pillar"),
        ("candidate_digest", "0" * 64, "candidate_digest"),
        ("schema_version", "url-first-article-candidate.v2", "schema_version"),
    ],
)
def test_candidate_validation_fails_closed_on_tampering(field, value, message):
    payload = _candidate().model_dump(mode="json")
    payload[field] = value

    with pytest.raises(CandidateContractError, match=message):
        validate_candidate(payload)


def test_candidate_validation_rejects_unknown_fields_and_duplicate_origin_identity():
    payload = _candidate().model_dump(mode="json")
    payload["future_breaking_field"] = True
    with pytest.raises(CandidateContractError):
        validate_candidate(payload)

    payload = _candidate().model_dump(mode="json")
    payload["origins"].append(copy.deepcopy(payload["origins"][0]))
    with pytest.raises(CandidateContractError, match="duplicate origin"):
        validate_candidate(payload)


def test_candidate_validation_rejects_unpaired_evidence_and_wrong_origin_url():
    candidate = build_candidate(
        url="https://example.org/articles/climate-risk",
        origins=[
            _origin(
                original_title="Search title",
                title_basis="search_result",
            )
        ],
        title="Search title",
        title_basis="search_result",
    )
    payload = candidate.model_dump(mode="json")
    payload["title_basis"] = None
    with pytest.raises(CandidateContractError, match="title.*title_basis"):
        validate_candidate(payload)

    payload = _candidate().model_dump(mode="json")
    payload["origins"][0]["url"] = "https://example.net/different"
    with pytest.raises(CandidateContractError, match="origin URL"):
        validate_candidate(payload)


def test_batch_validation_rejects_duplicates_order_count_digest_and_version():
    first = _candidate()
    second_url = "https://example.net/report"
    second = build_candidate(url=second_url, origins=[_origin(url=second_url, row="/1")])
    batch = build_candidate_batch([first, second])
    assert batch.schema_version == ARTICLE_CANDIDATE_BATCH_SCHEMA_VERSION

    for mutate, message in [
        (
            lambda value: (
                value["candidates"].append(copy.deepcopy(value["candidates"][0])),
                value.update(candidate_count=value["candidate_count"] + 1),
            ),
            "duplicate",
        ),
        (lambda value: value["candidates"].reverse(), "order"),
        (lambda value: value.update(candidate_count=99), "candidate_count"),
        (lambda value: value.update(batch_digest="0" * 64), "batch_digest"),
        (
            lambda value: value.update(schema_version="url-first-article-candidate-batch.v2"),
            "schema_version",
        ),
    ]:
        payload = batch.model_dump(mode="json")
        mutate(payload)
        with pytest.raises(CandidateContractError, match=message):
            validate_candidate_batch(payload)


def test_batch_rejects_one_artifact_row_assigned_to_different_candidates():
    first = _candidate()
    second_url = "https://example.net/different"
    second = build_candidate(url=second_url, origins=[_origin(url=second_url)])

    with pytest.raises(CandidateContractError, match="duplicate origin"):
        build_candidate_batch([first, second])


def test_batch_rejects_artifact_row_reused_by_different_candidate_origins():
    shared_artifact = _artifact(name="shared.json", digest="b" * 64).model_dump(
        mode="json"
    )
    first = build_candidate(
        url="https://example.org/first",
        origins=[
            _origin(
                pillar="A",
                source="Example A",
                url="https://example.org/first",
                input_artifact=shared_artifact,
                row="/items/0",
            )
        ],
    )
    second = build_candidate(
        url="https://example.net/second",
        origins=[
            _origin(
                pillar="B",
                source="web",
                url="https://example.net/second",
                input_artifact=shared_artifact,
                row="/items/0",
            )
        ],
    )

    with pytest.raises(CandidateContractError, match="artifact row"):
        build_candidate_batch([first, second])

    payload = {
        "schema_version": ARTICLE_CANDIDATE_BATCH_SCHEMA_VERSION,
        "candidate_count": 2,
        "candidates": [second.model_dump(mode="json"), first.model_dump(mode="json")],
        "batch_digest": "0" * 64,
    }
    with pytest.raises(CandidateContractError, match="artifact row"):
        validate_candidate_batch(payload)


def test_batch_allows_artifact_row_on_distinct_origins_within_one_candidate():
    url = "https://example.org/shared-discovery"
    shared_artifact = _artifact(name="shared.json", digest="c" * 64).model_dump(
        mode="json"
    )
    candidate = build_candidate(
        url=url,
        origins=[
            _origin(
                pillar="A",
                source="Example A",
                url=url,
                input_artifact=shared_artifact,
                row="/items/0",
            ),
            _origin(
                pillar="B",
                source="web",
                url=url,
                input_artifact=shared_artifact,
                row="/items/0",
            ),
        ],
    )

    batch = build_candidate_batch([candidate])

    assert batch.candidates[0].origins == candidate.origins


def test_canonical_serialization_is_exact_and_repeatable():
    candidate = build_candidate(
        url="https://example.org/articles/climate-risk",
        origins=[
            _origin(
                original_title="Résumé",
                title_basis="search_result",
            )
        ],
        title="Résumé",
        title_basis="search_result",
    )
    batch = build_candidate_batch([candidate])

    candidate_bytes = serialize_candidate(candidate)
    batch_bytes = serialize_candidate_batch(batch)

    assert candidate_bytes == serialize_candidate(json.loads(candidate_bytes))
    assert batch_bytes == serialize_candidate_batch(json.loads(batch_bytes))
    assert candidate_digest(candidate) == candidate.candidate_digest
    assert batch_digest(batch) == batch.batch_digest
    assert candidate_bytes.endswith(b"\n") and b"\r" not in candidate_bytes
    assert batch_bytes.endswith(b"\n") and b"\r" not in batch_bytes
    assert b'"title":"R\xc3\xa9sum\xc3\xa9"' in candidate_bytes


def test_article_changes_adapter_is_read_only_strict_and_preserves_mappable_values():
    payload = {
        "date": "2026-09-07",
        "pillar": "A",
        "sites_with_changes": 2,
        "orgs_with_articles": 1,
        "baseline_urls": 20,
        "new_articles": 1,
        "seen_before": 3,
        "generated_at": "2026-09-07T08:10:00+00:00",
        "articles": [
            {
                "org": "Example Org",
                "items": [
                    {
                        "title": "Climate-risk report",
                        "url": "https://example.org/report?edition=2026",
                        "categories": ["financial_risk"],
                    }
                ],
            }
        ],
    }
    before = copy.deepcopy(payload)

    (candidate,) = adapt_article_changes(
        payload,
        artifact_id="article_changes_2026-09-07.json",
        artifact_sha256="b" * 64,
    )

    assert payload == before
    assert candidate.display_pillar == "A"
    assert candidate.title is None
    assert candidate.categories == ["financial_risk"]
    origin = candidate.origins[0]
    assert origin.source == "Example Org"
    assert origin.row == "/articles/0/items/0"
    assert origin.original_title == "Climate-risk report"
    assert origin.title_basis == "upstream_artifact"
    assert origin.discovered_at == payload["generated_at"]


def test_pillar_b_adapter_is_read_only_strict_and_preserves_search_evidence():
    payload = [
        {
            "title": "Insurance transition research",
            "url": "https://example.net/research/insurance-transition",
            "source": "web",
            "summary": "Evidence from the search result.",
        }
    ]
    before = copy.deepcopy(payload)

    (candidate,) = adapt_pillar_b(
        payload,
        artifact_id="pillar_b_2026-09-07.json",
        artifact_sha256="c" * 64,
        discovered_at="2026-09-07T08:20:00Z",
    )

    assert payload == before
    assert candidate.display_pillar == "B"
    assert candidate.title == payload[0]["title"]
    assert candidate.title_basis == "search_result"
    assert candidate.summary == payload[0]["summary"]
    assert candidate.origins[0].summary_basis == "search_result"
    assert candidate.origins[0].row == "/0"


def test_article_changes_adapter_maps_empty_title_to_absent_evidence():
    payload = {
        "date": "2026-09-07",
        "pillar": "A",
        "sites_with_changes": 1,
        "orgs_with_articles": 1,
        "baseline_urls": 20,
        "new_articles": 1,
        "seen_before": 3,
        "generated_at": "2026-09-07T08:10:00Z",
        "articles": [
            {
                "org": "Example Org",
                "items": [
                    {
                        "title": "",
                        "url": "https://example.org/titleless-report",
                        "categories": ["financial_risk"],
                    }
                ],
            }
        ],
    }

    (candidate,) = adapt_article_changes(
        payload,
        artifact_id="article_changes_2026-09-07.json",
        artifact_sha256="b" * 64,
    )

    assert candidate.title is None
    assert candidate.title_basis is None
    assert candidate.origins[0].original_title is None
    assert candidate.origins[0].title_basis is None


def test_pillar_b_adapter_maps_empty_title_to_absent_evidence():
    (candidate,) = adapt_pillar_b(
        [
            {
                "title": "",
                "url": "https://example.net/titleless-result",
                "source": "web",
                "summary": "",
            }
        ],
        artifact_id="pillar_b_2026-09-07.json",
        artifact_sha256="c" * 64,
        discovered_at="2026-09-07T08:20:00Z",
    )

    assert candidate.title is None
    assert candidate.title_basis is None
    assert candidate.origins[0].original_title is None
    assert candidate.origins[0].title_basis is None


@pytest.mark.parametrize("invalid_title", [" ", None])
def test_adapters_reject_titles_other_than_nonempty_or_exactly_empty_strings(
    invalid_title,
):
    article_changes = {
        "date": "2026-09-07",
        "pillar": "A",
        "sites_with_changes": 1,
        "orgs_with_articles": 1,
        "baseline_urls": 20,
        "new_articles": 1,
        "seen_before": 3,
        "generated_at": "2026-09-07T08:10:00Z",
        "articles": [
            {
                "org": "Example Org",
                "items": [
                    {
                        "title": invalid_title,
                        "url": "https://example.org/invalid-title",
                        "categories": ["financial_risk"],
                    }
                ],
            }
        ],
    }
    with pytest.raises(CandidateContractError, match="title"):
        adapt_article_changes(
            article_changes,
            artifact_id="article_changes_2026-09-07.json",
            artifact_sha256="b" * 64,
        )

    with pytest.raises(CandidateContractError, match="title"):
        adapt_pillar_b(
            [
                {
                    "title": invalid_title,
                    "url": "https://example.net/invalid-title",
                    "source": "web",
                    "summary": "",
                }
            ],
            artifact_id="pillar_b_2026-09-07.json",
            artifact_sha256="c" * 64,
            discovered_at="2026-09-07T08:20:00Z",
        )


def test_origin_rows_enforce_rfc6901_escapes_in_python_and_schema():
    schema = json.loads(CANDIDATE_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    with pytest.raises(CandidateContractError, match="RFC 6901"):
        build_candidate(
            url="https://example.org/pointer",
            origins=[
                _origin(
                    url="https://example.org/pointer",
                    row="/bad~2escape",
                )
            ],
        )
    invalid_payload = json.loads(
        (FIXTURE_DIR / "positive" / "candidate__url_only.json").read_text(
            encoding="utf-8"
        )
    )
    invalid_payload["origins"][0]["row"] = "/bad~2escape"
    assert list(validator.iter_errors(invalid_payload))

    valid = build_candidate(
        url="https://example.org/pointer",
        origins=[
            _origin(
                url="https://example.org/pointer",
                row="/a~1b/~0key",
            )
        ],
    )
    validator.validate(valid.model_dump(mode="json"))


def test_multiline_pillar_b_title_round_trips_through_python_and_schema():
    title = "Climate risk\nreport"
    (candidate,) = adapt_pillar_b(
        [
            {
                "title": title,
                "url": "https://example.org/multiline-title",
                "source": "web",
                "summary": "Search evidence.",
            }
        ],
        artifact_id="pillar_b.json",
        artifact_sha256="f" * 64,
        discovered_at="2026-09-05T12:00:00Z",
    )

    serialized = serialize_candidate(candidate)
    round_tripped = json.loads(serialized)
    assert round_tripped["title"] == title
    assert round_tripped["origins"][0]["original_title"] == title

    schema = json.loads(CANDIDATE_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(round_tripped)


@pytest.mark.parametrize(
    "discovered_at",
    [
        "2026-09-05 12:00:00+00:00",
        "20260905T120000Z",
        "2026-W36-6T12:00:00Z",
    ],
)
def test_non_rfc3339_timestamps_fail_python_and_schema_validation(discovered_at):
    with pytest.raises(CandidateContractError, match="RFC 3339"):
        build_candidate(
            url="https://example.org/timestamp",
            origins=[
                _origin(
                    url="https://example.org/timestamp",
                    discovered_at=discovered_at,
                )
            ],
        )
    with pytest.raises(CandidateContractError, match="discovered_at"):
        adapt_pillar_b(
            [
                {
                    "title": "Timestamp example",
                    "url": "https://example.org/timestamp",
                    "source": "web",
                    "summary": "Search evidence.",
                }
            ],
            artifact_id="pillar_b.json",
            artifact_sha256="f" * 64,
            discovered_at=discovered_at,
        )

    schema = json.loads(CANDIDATE_SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = json.loads(
        (FIXTURE_DIR / "positive" / "candidate__url_only.json").read_text(
            encoding="utf-8"
        )
    )
    payload["origins"][0]["discovered_at"] = discovered_at
    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload)
    )
    assert any(error.validator == "format" for error in errors)


@pytest.mark.parametrize(
    "discovered_at",
    [
        "2026-09-05T12:00:00Z",
        "2026-09-05T12:00:00+05:30",
    ],
)
def test_rfc3339_timestamps_pass_python_and_schema_validation(discovered_at):
    candidate = build_candidate(
        url="https://example.org/timestamp",
        origins=[
            _origin(
                url="https://example.org/timestamp",
                discovered_at=discovered_at,
            )
        ],
    )
    (adapted,) = adapt_pillar_b(
        [
            {
                "title": "Timestamp example",
                "url": "https://example.net/timestamp",
                "source": "web",
                "summary": "Search evidence.",
            }
        ],
        artifact_id="pillar_b.json",
        artifact_sha256="f" * 64,
        discovered_at=discovered_at,
    )
    assert candidate.origins[0].discovered_at == discovered_at
    assert adapted.origins[0].discovered_at == discovered_at

    schema = json.loads(CANDIDATE_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(
        candidate.model_dump(mode="json")
    )


def test_adapters_reject_malformed_shapes_totals_and_issue_88_change_events():
    invalid_totals = {
        "date": "2026-09-07",
        "pillar": "A",
        "sites_with_changes": 1,
        "orgs_with_articles": 1,
        "baseline_urls": 0,
        "new_articles": 2,
        "seen_before": 0,
        "generated_at": "2026-09-07T08:10:00Z",
        "articles": [
            {
                "org": "Example",
                "items": [
                    {
                        "title": "Climate report",
                        "url": "https://example.org/report",
                        "categories": ["financial_risk"],
                    }
                ],
            }
        ],
    }
    with pytest.raises(CandidateContractError, match="new_articles"):
        adapt_article_changes(
            invalid_totals, artifact_id="article_changes.json", artifact_sha256="d" * 64
        )

    issue_88 = json.loads(
        (FIXTURE_DIR / "negative" / "adapter_pillar_a__issue_88_change_event.json").read_text(
            encoding="utf-8"
        )
    )
    with pytest.raises(CandidateContractError):
        adapt_article_changes(
            issue_88,
            artifact_id="article_changes_2026-08-31.json",
            artifact_sha256="e" * 64,
        )

    with pytest.raises(CandidateContractError):
        adapt_pillar_b(
            [{"title": "x", "url": "https://example.org/x", "source": "wire", "summary": ""}],
            artifact_id="pillar_b.json",
            artifact_sha256="f" * 64,
            discovered_at="2026-09-07T08:20:00Z",
        )


def test_schemas_meta_validate_and_all_fixtures_have_expected_disposition():
    candidate_schema = json.loads(CANDIDATE_SCHEMA_PATH.read_text(encoding="utf-8"))
    batch_schema = json.loads(BATCH_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(candidate_schema)
    Draft202012Validator.check_schema(batch_schema)
    registry = _schema_registry(candidate_schema)
    format_checker = FormatChecker()
    candidate_validator = Draft202012Validator(
        candidate_schema, format_checker=format_checker
    )
    batch_validator = Draft202012Validator(
        batch_schema, registry=registry, format_checker=format_checker
    )

    positives = sorted((FIXTURE_DIR / "positive").glob("*.json"))
    negatives = sorted((FIXTURE_DIR / "negative").glob("*.json"))
    assert {path.stem for path in positives} == {
        "batch__same_bytes_different_url",
        "batch__same_title_different_url",
        "candidate__pillar_a",
        "candidate__pillar_b",
        "candidate__same_url_a_b",
        "candidate__semantic_query",
        "candidate__url_only",
    }
    assert {path.stem for path in negatives} == {
        "adapter_pillar_a__issue_88_change_event",
        "candidate__article_id_mismatch",
        "candidate__canonical_mismatch",
        "candidate__duplicate_origin",
        "candidate__invalid_url",
        "candidate__malformed_host",
        "candidate__missing_origins",
        "candidate__unknown_breaking_shape",
    }

    for path in positives:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if path.name.startswith("candidate__"):
            candidate_validator.validate(payload)
            validate_candidate(payload)
            assert serialize_candidate(payload) == path.read_bytes()
        else:
            batch_validator.validate(payload)
            validate_candidate_batch(payload)
            assert serialize_candidate_batch(payload) == path.read_bytes()

    for path in negatives:
        if path.name.startswith("adapter_"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        validator = candidate_validator if path.name.startswith("candidate__") else batch_validator
        python_validator = (
            validate_candidate if path.name.startswith("candidate__") else validate_candidate_batch
        )
        schema_failed = bool(list(validator.iter_errors(payload)))
        with pytest.raises(CandidateContractError):
            python_validator(payload)
        # Identity/digest mismatches need semantic recomputation and can be
        # structurally schema-valid; all other negatives must fail both layers.
        if not any(token in path.name for token in ("article_id_mismatch", "canonical_mismatch")):
            assert schema_failed

    same_bytes = json.loads(
        (FIXTURE_DIR / "positive" / "batch__same_bytes_different_url.json").read_text(
            encoding="utf-8"
        )
    )
    first, second = same_bytes["candidates"]
    assert first["origins"][0]["original_snippet"].encode() == second["origins"][0][
        "original_snippet"
    ].encode()
    assert first["article_id"] != second["article_id"]


def test_declared_versions_are_exact_v1_contract_names():
    assert ARTICLE_CANDIDATE_SCHEMA_VERSION == "url-first-article-candidate.v1"
    assert ARTICLE_CANDIDATE_BATCH_SCHEMA_VERSION == "url-first-article-candidate-batch.v1"
