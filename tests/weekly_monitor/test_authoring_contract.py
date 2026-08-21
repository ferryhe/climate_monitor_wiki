from __future__ import annotations

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
    assert DEFAULT_TAXONOMY_ID == "climate-actuarial-v1"
    assert DEFAULT_TAXONOMY_SHA256 == load_article_taxonomy().sha256
