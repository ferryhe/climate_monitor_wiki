from __future__ import annotations

import copy
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from climate_monitor.models import CandidateItem
from climate_monitor.semantic_bundle import article_identity
from climate_monitor.taxonomy import (
    DEFAULT_TAXONOMY_ID,
    DEFAULT_TAXONOMY_SHA256,
    load_article_taxonomy,
)
from climate_monitor.weekly_monitor.authoring_contract import (
    AUTHORING_CONTRACT_VERSION,
    AUTHORING_CONTRACT_VERSION_V2,
    AUTHORING_REQUEST_SCHEMA_VERSION_V2,
    AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
    AUTHORING_RESPONSE_SCHEMA_VERSION,
    AuthoringContractError,
    build_authoring_request,
    load_authoring_response,
    validate_authoring_response,
)
from climate_monitor.weekly_monitor.prompt_loader import load_weekly_monitor_prompt


ROOT = Path(__file__).resolve().parents[2]
JOB_ROOT = ROOT / "monitoring" / "jobs" / "weekly-climate-monitor-08h"
FIXTURE_DIR = JOB_ROOT / "contracts" / "fixtures"


def _item(**overrides) -> CandidateItem:
    payload = {
        "title": "Climate supervision update",
        "url": "https://www.iais.org/climate-supervision",
        "summary": "Initial source summary.",
        "source_name": "IAIS",
        "lane": "website",
        "climate_related": True,
        "actuarial_related": True,
        "climate_signal": "physical_risk",
        "actuarial_signal": "insurance_risk",
        "topics": ("climate", "insurance", "capital"),
        "categories": ("Physical Risk", "Insurance Risk"),
        "keywords": ("climate", "insurance", "capital"),
    }
    payload.update(overrides)
    return CandidateItem(**payload)


def _evidence_record(
    *,
    item: CandidateItem,
    title_basis: str = "upstream_artifact",
    display_pillar: str = "A",
    summary_basis: str = "page",
    status: str = "ok",
    content: str | None = "Complete climate insurance article evidence.",
    content_ref: str | None = None,
    content_hash: str | None = None,
    extra: dict | None = None,
    origins: list[dict] | None = None,
) -> dict:
    body = content if content is not None else ""
    digest = hashlib.sha256(body.encode()).hexdigest() if body else None
    return {
        "article_id": article_identity(item),
        "requested_url": item.url,
        "final_url": item.url,
        "status": status,
        "attempts": [{"tool": "http"}] if status == "ok" else [],
        "selected_method": "http" if status == "ok" else None,
        "content_type": "text/html" if status == "ok" else None,
        "content_ref": content_ref or (f"memory:{digest}" if digest else None),
        "content_hash": content_hash or digest,
        "summary_basis": summary_basis,
        "title_basis": title_basis,
        "display_pillar": display_pillar,
        "origins": origins
        or [{"pillar": display_pillar, "source": item.source_name, "url": item.url}],
        "content": body,
        "extra": extra or {},
    }


def test_authoring_schemas_are_valid_and_accept_valid_values():
    bundle_schema = json.loads(
        (ROOT / "monitoring" / "schemas" / "article_semantic_bundle_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    response_schema = json.loads(
        (JOB_ROOT / "contracts" / "authoring-response.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    request_schema = json.loads(
        (JOB_ROOT / "contracts" / "authoring-request.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(response_schema)
    Draft202012Validator.check_schema(request_schema)

    registry = Registry().with_resource(
        bundle_schema["$id"], Resource.from_contents(bundle_schema)
    )
    Draft202012Validator(response_schema, registry=registry).validate(
        load_authoring_response(FIXTURE_DIR / "valid-response.json")
    )
    request = build_authoring_request(
        report_date=date(2026, 5, 18),
        items=[_item()],
        prompt=load_weekly_monitor_prompt(),
        taxonomy=load_article_taxonomy(),
    )
    Draft202012Validator(request_schema).validate(request)


def test_valid_authoring_response_binds_one_bundle_per_final_article():
    items = [
        _item(),
        _item(
            title="Climate risk report PDF",
            url="https://www.iais.org/uploads/climate-risk-report.pdf",
            lane="document",
        ),
    ]

    result = validate_authoring_response(
        items,
        load_authoring_response(FIXTURE_DIR / "valid-response.json"),
    )

    assert result.article_count == 2
    assert result.article_identities == tuple(article_identity(item) for item in items)
    assert [item.summary for item in result.items] == [
        "IAIS published a climate supervision update relevant to insurance supervisors.",
        "IAIS published a climate risk report PDF relevant to insurance supervisors.",
    ]
    assert result.items[0].semantics["categories"] == ["Supervision & Disclosure"]
    assert result.items[1].keywords == (
        "climate risk",
        "capital adequacy",
        "insurance supervision",
    )


def test_authoring_request_exposes_the_final_article_input_contract_without_prompt_bytes():
    items = [_item()]
    prompt = load_weekly_monitor_prompt()
    taxonomy = load_article_taxonomy()

    request = build_authoring_request(
        report_date=date(2026, 5, 18),
        items=items,
        prompt=prompt,
        taxonomy=taxonomy,
    )

    encoded = json.dumps(request)
    assert request["schema_version"] == "weekly-monitor-authoring-request.v1"
    assert request["contract_version"] == AUTHORING_CONTRACT_VERSION
    assert request["prompt"] == {
        "id": prompt.prompt_id,
        "version": prompt.version,
        "sha256": prompt.sha256,
    }
    assert request["taxonomy"]["taxonomy_id"] == "climate-actuarial-v1"
    assert request["articles"][0]["article_id"] == article_identity(items[0])
    assert "raw_bytes" not in encoded
    assert str(ROOT) not in encoded


@pytest.mark.parametrize(
    ("fixture_name", "message"),
    [
        ("invalid-missing-article.json", "missing article"),
        ("invalid-duplicate-identity.json", "duplicate article"),
        ("invalid-extra-article.json", "unknown article"),
        ("invalid-unknown-category.json", "taxonomy"),
        ("invalid-malformed.json", "unexpected"),
    ],
)
def test_invalid_authoring_responses_fail_closed(fixture_name: str, message: str):
    items = [
        _item(),
        _item(
            title="Climate risk report PDF",
            url="https://www.iais.org/uploads/climate-risk-report.pdf",
            lane="document",
        ),
    ]

    with pytest.raises(AuthoringContractError, match=message):
        validate_authoring_response(
            items,
            load_authoring_response(FIXTURE_DIR / fixture_name),
        )


def test_response_constants_match_taxonomy_identity():
    assert AUTHORING_RESPONSE_SCHEMA_VERSION == "weekly-monitor-authoring-response.v1"
    assert AUTHORING_CONTRACT_VERSION == "weekly-monitor-authoring.v1"
    assert AUTHORING_RESPONSE_SCHEMA_VERSION_V2 == "weekly-monitor-authoring-response.v2"
    assert AUTHORING_REQUEST_SCHEMA_VERSION_V2 == "weekly-monitor-authoring-request.v2"
    assert AUTHORING_CONTRACT_VERSION_V2 == "weekly-monitor-authoring.v2"
    assert DEFAULT_TAXONOMY_ID == "climate-actuarial-v1"
    assert DEFAULT_TAXONOMY_SHA256 == load_article_taxonomy().sha256


def test_evidence_authoring_v2_binds_basis_identity_and_stats():
    item = _item(summary="", content_hash="")
    prompt = load_weekly_monitor_prompt()
    body = "Complete climate insurance article evidence."
    digest = hashlib.sha256(body.encode()).hexdigest()
    evidence = {
        "records": [
            _evidence_record(
                item=item,
                content=body,
                content_hash=digest,
                content_ref=f"memory:{digest}",
            )
        ]
    }
    stats = {
        "total": 57,
        "updated": 54,
        "unchanged": 0,
        "blocked": 0,
        "failed": 3,
        "unresolved": 0,
    }
    request = build_authoring_request(
        report_date=date(2026, 5, 18),
        items=[item],
        prompt=prompt,
        article_evidence=evidence,
        stats=stats,
    )
    assert request["schema_version"] == AUTHORING_REQUEST_SCHEMA_VERSION_V2
    article = request["articles"][0]
    assert article["evidence"]["content_hash"] == digest
    assert article["evidence"]["content_ref"] == f"memory:{digest}"
    assert article["display_pillar"] == "A"
    assert article["origins"] == [
        {"pillar": "A", "source": item.source_name, "url": item.url}
    ]
    assert request["stats"] == stats
    response_article = copy.deepcopy(article)
    response_article.update(
        {
            "relevant": True,
            "summary": "Evidence-backed climate insurance summary.",
            "summary_basis": "article_content",
            "evidence_hash": digest,
            "categories": ["Supervision & Disclosure"],
            "keywords": ["climate", "insurance", "capital"],
        }
    )
    response = {
        "schema_version": AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
        "contract_version": AUTHORING_CONTRACT_VERSION_V2,
        "request_sha256": request["request_sha256"],
        "article_count": 1,
        "articles": [response_article],
        "executive_summary": "57 total; 54 updated; 0 unchanged; 0 blocked; 3 failed; 0 unresolved.",
        "stats": stats,
    }
    result = validate_authoring_response([item], response, request=request)
    assert result.items[0].summary == "Evidence-backed climate insurance summary."
    assert result.items[0].semantics["summary"] == "Evidence-backed climate insurance summary."

    # Finalize rebuilds candidates from discovery, where the title may still be
    # a URL. Use the immutable prepared page title, never a model-invented title.
    from dataclasses import replace
    url_only_item = replace(item, title=item.url)
    titled = validate_authoring_response([url_only_item], response, request=request)
    assert titled.items[0].title == article["title"] == item.title
    assert titled.article_identities == result.article_identities

    # Mutation categories: each one must fail closed. The "mutated" message
    # covers request-identity/evidence tampering; "missing"/"duplicate"
    # cover identity-set violations.
    for description, mutate in (
        ("mutated-url", lambda v: v["articles"][0].__setitem__(
            "url", "https://evil.invalid"
        )),
        ("mutated-title", lambda v: v["articles"][0].__setitem__(
            "title", "Model-invented replacement title"
        )),
        ("mutated-hash", lambda v: v["articles"][0]["evidence"].__setitem__(
            "content_hash", "0" * 64
        )),
        ("mutated-identity", lambda v: v.__setitem__(
            "request_sha256", "0" * 64
        )),
        ("missing-id", lambda v: v["articles"].pop(0)),
        ("extra-fields", lambda v: v.__setitem__(
            "articles", list(v["articles"]) + [v["articles"][0]]
        )),
    ):
        changed = copy.deepcopy(response)
        mutate(changed)
        with pytest.raises(AuthoringContractError):
            validate_authoring_response([item], changed, request=request)

    # Stats are deterministic input; mutating them fails with a specific
    # message so the validator reports the right category.
    changed = copy.deepcopy(response)
    changed["stats"] = {**stats, "failed": 4, "total": 58}
    with pytest.raises(AuthoringContractError, match="stats"):
        validate_authoring_response([item], changed, request=request)

    # article_count larger than the input set is rejected as unknown ID.
    changed = copy.deepcopy(response)
    changed["article_count"] = 2
    with pytest.raises(AuthoringContractError, match="article_count"):
        validate_authoring_response([item], changed, request=request)


def test_evidence_authoring_v2_handles_snippet_and_irrelevant():
    item = _item(summary="")
    prompt = load_weekly_monitor_prompt()
    evidence = {
        "records": [
            _evidence_record(
                item=item,
                status="ok",
                content="Honest climate insurance article body.",
                summary_basis="search_snippet",
                extra={"search_snippet": "Snippet excerpt for IAIS guidance."},
            )
        ]
    }
    request = build_authoring_request(
        report_date=date(2026, 5, 18),
        items=[item],
        prompt=prompt,
        article_evidence=evidence,
        stats={"total": 1, "updated": 1, "unchanged": 0, "blocked": 0, "failed": 0, "unresolved": 0},
    )
    article = copy.deepcopy(request["articles"][0])
    article.update(
        {
            "relevant": True,
            "summary": "Snippet excerpt for IAIS guidance.",
            "summary_basis": "search_snippet",
            "evidence_hash": None,
            "categories": ["Supervision & Disclosure"],
            "keywords": ["climate", "insurance", "capital"],
        }
    )
    response = {
        "schema_version": AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
        "contract_version": AUTHORING_CONTRACT_VERSION_V2,
        "request_sha256": request["request_sha256"],
        "article_count": 1,
        "articles": [article],
        "executive_summary": "",
        "stats": request["stats"],
    }
    result = validate_authoring_response([item], response, request=request)
    assert result.items and result.items[0].summary == "Snippet excerpt for IAIS guidance."

    # Snippet evidence_hash must remain None (snippet cannot pretend to be
    # content). Anything else is rejected.
    fraud = copy.deepcopy(response)
    fraud["articles"][0]["evidence_hash"] = "f" * 64
    with pytest.raises(AuthoringContractError, match="cannot pretend to be content"):
        validate_authoring_response([item], fraud, request=request)

    # article_content evidence with a mismatching evidence_hash is rejected.
    content_evidence = {
        "records": [
            _evidence_record(
                item=item,
                status="ok",
                content="Honest climate insurance article body.",
            )
        ]
    }
    content_request = build_authoring_request(
        report_date=date(2026, 5, 18),
        items=[item],
        prompt=prompt,
        article_evidence=content_evidence,
        stats={"total": 1, "updated": 1, "unchanged": 0, "blocked": 0, "failed": 0, "unresolved": 0},
    )
    content_article = copy.deepcopy(content_request["articles"][0])
    digest = hashlib.sha256(b"Honest climate insurance article body.").hexdigest()
    content_article.update(
        {
            "relevant": True,
            "summary": "Honest content summary.",
            "summary_basis": "article_content",
            "evidence_hash": "0" * 64,
            "categories": ["Supervision & Disclosure"],
            "keywords": ["climate", "insurance", "capital"],
        }
    )
    content_response = {
        "schema_version": AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
        "contract_version": AUTHORING_CONTRACT_VERSION_V2,
        "request_sha256": content_request["request_sha256"],
        "article_count": 1,
        "articles": [content_article],
        "executive_summary": "",
        "stats": content_request["stats"],
    }
    with pytest.raises(AuthoringContractError, match="hash mismatch"):
        validate_authoring_response([item], content_response, request=content_request)

    # Irrelevant record: no item enters the accepted set. The validator still
    # binds the article fields, so the irrelevant entry uses summary_basis
    # "none" with empty summary.
    irrelevant = copy.deepcopy(response)
    irrelevant["articles"][0]["relevant"] = False
    irrelevant["articles"][0]["summary"] = ""
    irrelevant["articles"][0]["summary_basis"] = "none"
    irrelevant["articles"][0]["evidence_hash"] = None
    irrelevant["executive_summary"] = ""
    irrelevant_result = validate_authoring_response(
        [item], irrelevant, request=request
    )
    assert irrelevant_result.items == ()
    assert irrelevant_result.article_count == 0


def test_evidence_authoring_v2_url_only_relevant_keeps_link_drops_summary():
    item = _item(summary="", url="https://example.org/only-url", lane="website")
    prompt = load_weekly_monitor_prompt()
    evidence = {
        "records": [
            _evidence_record(
                item=item,
                status="unavailable",
                content=None,
                content_hash=None,
                content_ref=None,
                summary_basis="none",
            )
        ]
    }
    request = build_authoring_request(
        report_date=date(2026, 5, 18),
        items=[item],
        prompt=prompt,
        article_evidence=evidence,
        stats={"total": 1, "updated": 0, "unchanged": 0, "blocked": 0, "failed": 1, "unresolved": 0},
    )
    article = copy.deepcopy(request["articles"][0])
    article.update(
        {
            "relevant": True,
            "summary": "",
            "summary_basis": "none",
            "evidence_hash": None,
            "categories": ["Supervision & Disclosure"],
            "keywords": ["climate", "insurance", "capital"],
        }
    )
    response = {
        "schema_version": AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
        "contract_version": AUTHORING_CONTRACT_VERSION_V2,
        "request_sha256": request["request_sha256"],
        "article_count": 1,
        "articles": [article],
        "executive_summary": "",
        "stats": request["stats"],
    }
    result = validate_authoring_response([item], response, request=request)
    assert len(result.items) == 1
    assert result.items[0].summary == ""

    # none must not carry any summary.
    bogus = copy.deepcopy(response)
    bogus["articles"][0]["summary"] = "fabricated"
    with pytest.raises(AuthoringContractError, match="none"):
        validate_authoring_response([item], bogus, request=request)


def test_evidence_authoring_v2_rejects_pillar_title_basis_outside_allowlist():
    item = _item()
    prompt = load_weekly_monitor_prompt()
    evidence = {
        "records": [
            _evidence_record(item=item, display_pillar="C")
        ]
    }
    with pytest.raises(AuthoringContractError, match="display_pillar"):
        build_authoring_request(
            report_date=date(2026, 5, 18),
            items=[item],
            prompt=prompt,
            article_evidence=evidence,
        )

    bad_basis = {
        "records": [
            _evidence_record(item=item, title_basis="bogus")
        ]
    }
    with pytest.raises(AuthoringContractError, match="title_basis"):
        build_authoring_request(
            report_date=date(2026, 5, 18),
            items=[item],
            prompt=prompt,
            article_evidence=bad_basis,
        )


def test_evidence_authoring_v2_dedupes_same_canonical_url_in_evidence():
    item = _item()
    prompt = load_weekly_monitor_prompt()
    duplicate = {
        "records": [
            _evidence_record(item=item),
            _evidence_record(item=item, status="unavailable", content=None,
                             content_hash=None, content_ref=None),
        ]
    }
    with pytest.raises(AuthoringContractError, match="duplicate"):
        build_authoring_request(
            report_date=date(2026, 5, 18),
            items=[item],
            prompt=prompt,
            article_evidence=duplicate,
        )


def test_redirect_targets_do_not_replace_candidate_url_identities():
    from climate_monitor.weekly_monitor.driver import _candidate_items_from_evidence

    items = [
        _item(url="https://example.org/climate-policy", title="Climate policy"),
        _item(url="https://example.org/insurance", title="Insurance research"),
    ]
    evidence = {"records": [_evidence_record(item=item) for item in items]}
    for record in evidence["records"]:
        record["final_url"] = "https://example.org/landing"
    before = copy.deepcopy(evidence)
    shells = _candidate_items_from_evidence(None, evidence)
    assert {item.url for item in shells} == {item.url for item in items}
    request = build_authoring_request(
        report_date=date(2026, 9, 7), items=shells,
        prompt=load_weekly_monitor_prompt(), article_evidence=evidence,
        stats={"total": 2, "updated": 2, "unchanged": 0,
               "blocked": 0, "failed": 0, "unresolved": 0},
    )
    assert {article["url"] for article in request["articles"]} == {item.url for item in items}
    assert len({article["article_id"] for article in request["articles"]}) == 2
    for article in request["articles"]:
        assert article["origins"][0]["url"] == article["url"]
    assert evidence == before


def test_v2_selected_subset_receives_its_own_url_summary():
    items = [_item(url=f'https://example.org/climate-{index}') for index in range(3)]
    request = build_authoring_request(report_date=date(2026, 9, 7), items=items,
        prompt=load_weekly_monitor_prompt(),
        article_evidence={'records': [_evidence_record(item=item) for item in items]},
        stats={'total': 1, 'updated': 1, 'unchanged': 0, 'blocked': 0, 'failed': 0, 'unresolved': 0})
    response = dict(schema_version=AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
        contract_version=AUTHORING_CONTRACT_VERSION_V2, request_sha256=request['request_sha256'],
        stats=request['stats'], article_count=3, executive_summary='',
        articles=[{**article, 'relevant': True, 'summary': f"Own summary for {article['url']}.",
            'summary_basis': 'article_content', 'evidence_hash': article['evidence']['content_hash'],
            'categories': ['Supervision & Disclosure'], 'keywords': ['insurance', 'supervision', 'disclosure']}
            for article in request['articles']])
    selected = items[-1]
    result = validate_authoring_response([selected], response, request=request)
    assert result.items[0].summary == f'Own summary for {selected.url}.'
    assert result.article_identities == (article_identity(selected),)
