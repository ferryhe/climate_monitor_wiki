from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from climate_monitor.ai_filter import CATEGORY_LABELS
from climate_monitor.taxonomy import (
    DEFAULT_TAXONOMY_PATH,
    load_article_taxonomy,
    validate_semantic_bundle,
)
from climate_registry.annotations import ALLOWED_CATEGORIES, DISALLOWED_KEYWORDS


EXPECTED_LABELS = {
    "Physical Risk",
    "Transition Risk",
    "Adaptation & Resilience",
    "Climate Risk",
    "Insurance Risk",
    "Capital & Solvency",
    "Supervision & Disclosure",
    "Actuarial Modelling",
}
EXPECTED_TAXONOMY_SHA256 = "3deefa1cc0df7a2e1ce8ef538271c0ab4465bb928f20e6418aafc8a834794d94"
EXPECTED_SIGNAL_LABELS = {
    "physical_risk": "Physical Risk",
    "transition_risk": "Transition Risk",
    "adaptation_resilience": "Adaptation & Resilience",
    "general_climate": "Climate Risk",
    "insurance_risk": "Insurance Risk",
    "capital_solvency": "Capital & Solvency",
    "supervision_disclosure": "Supervision & Disclosure",
    "actuarial_modeling": "Actuarial Modelling",
}


def test_versioned_taxonomy_is_the_shared_category_authority():
    taxonomy = load_article_taxonomy()

    assert taxonomy.taxonomy_id == "climate-actuarial-v1"
    assert taxonomy.allowed_labels == EXPECTED_LABELS
    assert CATEGORY_LABELS == EXPECTED_SIGNAL_LABELS
    assert ALLOWED_CATEGORIES == EXPECTED_LABELS
    assert DISALLOWED_KEYWORDS == {"article", "news", "report", "update"}
    assert taxonomy.sha256 == EXPECTED_TAXONOMY_SHA256
    assert taxonomy.sha256 == hashlib.sha256(DEFAULT_TAXONOMY_PATH.read_bytes()).hexdigest()


def test_semantic_bundle_is_normalized_against_the_versioned_taxonomy():
    validated = validate_semantic_bundle(
        {
            "schema_version": "article-semantic-bundle.v1",
            "taxonomy_id": "climate-actuarial-v1",
            "taxonomy_sha256": EXPECTED_TAXONOMY_SHA256,
            "summary": "Observed wildfire losses affect catastrophe pricing assumptions.",
            "categories": ["Physical Risk", "Insurance Risk"],
            "keywords": ["wildfire", "catastrophe pricing", "insured losses"],
        }
    )

    assert validated["categories"] == ["Physical Risk", "Insurance Risk"]
    assert validated["keywords"] == ["wildfire", "catastrophe pricing", "insured losses"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("taxonomy_id", "other-v1", "taxonomy_id"),
        ("taxonomy_sha256", "0" * 64, "taxonomy_sha256"),
        ("categories", ["Unknown"], "configured taxonomy"),
        ("categories", ["Physical Risk", "physical risk"], "case-insensitively unique"),
        ("keywords", ["flood", "Flood", "pricing"], "case-insensitively unique"),
        ("keywords", ["flood", "pricing"], "between 3 and 8"),
        ("keywords", ["flood", "pricing", "report"], "disallowed"),
        ("keywords", ["flood", "risk, pricing", "insurance"], "separators"),
        ("keywords", ["flood", "risk\npricing", "insurance"], "normalized"),
        ("summary", " summary ", "normalized"),
        ("summary", "summary\nmetadata", "normalized"),
        ("summary", "summary\tmetadata", "normalized"),
        ("summary", "not NFC: e\u0301", "normalized"),
    ],
)
def test_invalid_semantic_bundle_fails_closed(field, value, message):
    bundle = {
        "schema_version": "article-semantic-bundle.v1",
        "taxonomy_id": "climate-actuarial-v1",
        "taxonomy_sha256": EXPECTED_TAXONOMY_SHA256,
        "summary": "Source-backed summary.",
        "categories": ["Climate Risk"],
        "keywords": ["climate", "scenario", "pricing"],
    }
    bundle[field] = value

    with pytest.raises(ValueError, match=message):
        validate_semantic_bundle(bundle)


def test_taxonomy_loader_rejects_duplicate_labels(tmp_path: Path):
    raw = DEFAULT_TAXONOMY_PATH.read_text(encoding="utf-8")
    path = tmp_path / "taxonomy.yaml"
    path.write_text(raw.replace("Transition Risk", "Physical Risk", 1), encoding="utf-8")

    with pytest.raises(ValueError, match="labels must be unique"):
        load_article_taxonomy(path)


def test_taxonomy_loader_rejects_duplicate_yaml_keys(tmp_path: Path):
    raw = DEFAULT_TAXONOMY_PATH.read_text(encoding="utf-8")
    path = tmp_path / "taxonomy.yaml"
    path.write_text(
        raw.replace(
            "taxonomy_id: climate-actuarial-v1",
            "taxonomy_id: other-v1\ntaxonomy_id: climate-actuarial-v1",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate YAML key"):
        load_article_taxonomy(path)


def test_json_schema_snapshot_matches_the_yaml_authority():
    taxonomy = load_article_taxonomy()
    schema_path = DEFAULT_TAXONOMY_PATH.parents[1] / "schemas" / "article_semantic_bundle_v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    properties = schema["properties"]

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version",
        "taxonomy_id",
        "taxonomy_sha256",
        "summary",
        "categories",
        "keywords",
    }
    assert properties["taxonomy_id"]["const"] == taxonomy.taxonomy_id
    assert properties["taxonomy_sha256"]["const"] == taxonomy.sha256
    assert properties["summary"]["minLength"] == taxonomy.constraints.summary_min_chars
    assert properties["summary"]["maxLength"] == taxonomy.constraints.summary_max_chars
    assert properties["categories"]["minItems"] == taxonomy.constraints.categories_min_items
    assert properties["categories"]["maxItems"] == taxonomy.constraints.categories_max_items
    assert properties["keywords"]["minItems"] == taxonomy.constraints.keywords_min_items
    assert properties["keywords"]["maxItems"] == taxonomy.constraints.keywords_max_items
    assert properties["keywords"]["items"]["maxLength"] == taxonomy.constraints.keyword_max_chars
