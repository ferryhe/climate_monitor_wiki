from __future__ import annotations

import hashlib
import json
import sys
import unicodedata
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import climate_monitor.taxonomy as taxonomy_module
from climate_monitor.ai_filter import CATEGORY_LABELS
from climate_monitor.taxonomy import (
    DEFAULT_TAXONOMY_ID,
    DEFAULT_TAXONOMY_PATH,
    DEFAULT_TAXONOMY_SHA256,
    MAX_TAXONOMY_BYTES,
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
    "Decarbonisation", "Climate Finance & Investment", "Climate & Health",
    "Climate & Nature", "Science & Data", "Food & Water Security",
}
EXPECTED_TAXONOMY_SHA256 = hashlib.sha256(DEFAULT_TAXONOMY_PATH.read_bytes()).hexdigest()
EXPECTED_SIGNAL_LABELS = {
    "physical_risk": "Physical Risk",
    "transition_risk": "Transition Risk",
    "adaptation_resilience": "Adaptation & Resilience",
    "general_climate": "Climate Risk",
    "insurance_risk": "Insurance Risk",
    "capital_solvency": "Capital & Solvency",
    "supervision_disclosure": "Supervision & Disclosure",
    "actuarial_modeling": "Actuarial Modelling",
    "mitigation_decarbonisation": "Decarbonisation", "climate_finance": "Climate Finance & Investment",
    "climate_health": "Climate & Health", "climate_nature": "Climate & Nature",
    "climate_science_data": "Science & Data", "climate_food_water": "Food & Water Security",
}


def _valid_semantic_bundle() -> dict[str, object]:
    return {
        "schema_version": "article-semantic-bundle.v1",
        "taxonomy_id": DEFAULT_TAXONOMY_ID,
        "taxonomy_sha256": EXPECTED_TAXONOMY_SHA256,
        "summary": "Source-backed summary.",
        "categories": ["Climate Risk"],
        "keywords": ["climate", "scenario", "pricing"],
    }


def _semantic_schema() -> dict[str, object]:
    schema_path = (
        DEFAULT_TAXONOMY_PATH.parents[1]
        / "schemas"
        / "article_semantic_bundle_v1.schema.json"
    )
    return json.loads(schema_path.read_text(encoding="utf-8"))


def test_versioned_taxonomy_is_the_shared_category_authority():
    taxonomy = load_article_taxonomy()

    assert len(taxonomy.categories) == 14
    assert taxonomy.taxonomy_id == DEFAULT_TAXONOMY_ID == "climate-actuarial-v2"
    assert taxonomy.allowed_labels == EXPECTED_LABELS
    assert taxonomy.labels_by_signal == EXPECTED_SIGNAL_LABELS
    assert CATEGORY_LABELS == EXPECTED_SIGNAL_LABELS
    assert ALLOWED_CATEGORIES == EXPECTED_LABELS
    assert DISALLOWED_KEYWORDS == {"article", "news", "report", "update"}
    assert taxonomy.sha256 == DEFAULT_TAXONOMY_SHA256 == EXPECTED_TAXONOMY_SHA256
    assert taxonomy.sha256 == hashlib.sha256(DEFAULT_TAXONOMY_PATH.read_bytes()).hexdigest()


def test_semantic_bundle_is_normalized_against_the_versioned_taxonomy():
    validated = validate_semantic_bundle(
        {
            "schema_version": "article-semantic-bundle.v1",
            "taxonomy_id": "climate-actuarial-v2",
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
        ("keywords", ["flood", "risk; pricing", "insurance"], "separators"),
        ("keywords", ["flood", "risk\npricing", "insurance"], "normalized"),
        ("keywords", ["flood", "risk\u0085pricing", "insurance"], "normalized"),
        ("summary", "", "normalized"),
        ("summary", " summary ", "normalized"),
        ("summary", "summary  metadata", "normalized"),
        ("summary", "summary\nmetadata", "normalized"),
        ("summary", "summary\tmetadata", "normalized"),
        ("summary", "summary\x00metadata", "normalized"),
        ("summary", "summary\ud800metadata", "normalized"),
        ("summary", "summary\u202emetadata", "normalized"),
        ("summary", "summary\u034fmetadata", "normalized"),
        ("categories", ["Climate\ufe0f Risk"], "normalized"),
        ("keywords", ["climate", "re\u200bport", "pricing"], "normalized"),
        ("keywords", ["climate", "re\u034fport", "pricing"], "normalized"),
        ("keywords", ["climate", "re\ufe0fport", "pricing"], "normalized"),
        ("keywords", ["climate", "risk\u202e", "pricing"], "normalized"),
        ("summary", "not NFC: e\u0301", "normalized"),
    ],
)
def test_invalid_semantic_bundle_fails_closed(field, value, message):
    bundle = {
        "schema_version": "article-semantic-bundle.v1",
        "taxonomy_id": "climate-actuarial-v2",
        "taxonomy_sha256": EXPECTED_TAXONOMY_SHA256,
        "summary": "Source-backed summary.",
        "categories": ["Climate Risk"],
        "keywords": ["climate", "scenario", "pricing"],
    }
    bundle[field] = value

    with pytest.raises(ValueError, match=message):
        validate_semantic_bundle(bundle)


@pytest.mark.parametrize(
    ("summary", "keywords"),
    [
        ("Κλιματικοί κίνδυνοι επηρεάζουν την τιμολόγηση των ασφαλίστρων.", ["κλίμα", "ασφάλιση", "τιμολόγηση"]),
        ("Résumé des risques climatiques.", ["résilience", "assurance", "modélisation"]),
        ("Climate risk affects pricing.", ["climate", "insurance", "pricing"]),
    ],
)
def test_valid_nfc_unicode_is_strict_utf8_encodable(summary, keywords):
    bundle = _valid_semantic_bundle()
    bundle["summary"] = summary
    bundle["keywords"] = keywords

    validated = validate_semantic_bundle(bundle)

    assert validated["summary"].encode("utf-8", errors="strict")
    assert all(item.encode("utf-8", errors="strict") for item in validated["keywords"])


def test_unicode_policy_rejects_every_format_character():
    format_characters = (
        chr(codepoint)
        for codepoint in range(sys.maxunicode + 1)
        if unicodedata.category(chr(codepoint)) == "Cf"
    )
    for character in format_characters:
        with pytest.raises(ValueError, match="normalized"):
            taxonomy_module._validate_unicode_scalar_text(
                character, name="semantic text"
            )


@pytest.mark.parametrize(
    "codepoint",
    [0x034F, 0xFE0F, 0x180B, 0x180F, 0x2060, 0x3164, 0xFFA0, 0xE0100],
)
def test_unicode_policy_rejects_default_ignorable_probes(codepoint):
    assert taxonomy_module._is_default_ignorable(codepoint)
    with pytest.raises(ValueError, match="normalized"):
        taxonomy_module._validate_unicode_scalar_text(chr(codepoint), name="semantic text")


def test_frozen_default_ignorable_ranges_are_complete_and_well_formed():
    ranges = taxonomy_module._DEFAULT_IGNORABLE_RANGES

    assert taxonomy_module._DEFAULT_IGNORABLE_UNICODE_VERSION == "17.0.0"
    assert isinstance(ranges, tuple)
    assert sum(end - start + 1 for start, end in ranges) == 4_174
    canonical_ranges = "\n".join(
        f"{start:06X}..{end:06X}" for start, end in ranges
    ).encode("ascii")
    assert hashlib.sha256(canonical_ranges).hexdigest() == (
        "5205ae076909257d0fd58182a3a24b554abd099e1ed6340e3a7e1ad0f576bf72"
    )
    assert all(isinstance(item, tuple) and len(item) == 2 for item in ranges)
    assert all(start <= end for start, end in ranges)
    assert all(
        left_end < right_start
        for (_, left_end), (right_start, _) in zip(ranges, ranges[1:])
    )

    checked_neighbors = 0
    for start, end in ranges:
        for codepoint in {start, end}:
            assert taxonomy_module._is_default_ignorable(codepoint)
            with pytest.raises(ValueError, match="normalized"):
                taxonomy_module._validate_unicode_scalar_text(
                    chr(codepoint), name="semantic text"
                )
        for codepoint in (start - 1, end + 1):
            if not 0 <= codepoint <= sys.maxunicode:
                continue
            character = chr(codepoint)
            if (
                taxonomy_module._is_default_ignorable(codepoint)
                or unicodedata.category(character) in {"Cc", "Cf", "Cn", "Cs"}
            ):
                continue
            taxonomy_module._validate_unicode_scalar_text(character, name="semantic text")
            checked_neighbors += 1
    assert checked_neighbors > 0


@pytest.mark.parametrize("keyword", ["report", "re\u034fport", "re\ufe0fport"])
def test_generic_keyword_cannot_be_hidden_with_default_ignorables(keyword):
    bundle = _valid_semantic_bundle()
    bundle["keywords"] = ["climate", keyword, "pricing"]

    with pytest.raises(ValueError):
        validate_semantic_bundle(bundle)


def test_taxonomy_strings_share_default_ignorable_validation():
    raw = DEFAULT_TAXONOMY_PATH.read_text(encoding="utf-8")
    modified = raw.replace("description: ", "description: \u034f", 1)

    with pytest.raises(ValueError, match="normalized"):
        taxonomy_module._parse_article_taxonomy_bytes(modified.encode("utf-8"))


def test_taxonomy_loader_rejects_duplicate_labels():
    raw = DEFAULT_TAXONOMY_PATH.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="labels must be unique"):
        taxonomy_module._parse_article_taxonomy_bytes(
            raw.replace("Transition Risk", "Physical Risk", 1).encode("utf-8")
        )


def test_taxonomy_loader_rejects_duplicate_yaml_keys():
    raw = DEFAULT_TAXONOMY_PATH.read_text(encoding="utf-8")
    modified = raw.replace(
        "taxonomy_id: climate-actuarial-v2",
        "taxonomy_id: other-v1\ntaxonomy_id: climate-actuarial-v2",
        1,
    )

    with pytest.raises(ValueError, match="duplicate YAML key"):
        taxonomy_module._parse_article_taxonomy_bytes(modified.encode("utf-8"))


def test_taxonomy_loader_wraps_missing_file_errors(tmp_path: Path):
    with pytest.raises(ValueError, match="taxonomy file cannot be read"):
        load_article_taxonomy(tmp_path / "missing.yaml")


def test_taxonomy_loader_bounds_file_reads(tmp_path: Path):
    path = tmp_path / "taxonomy.yaml"
    path.write_bytes(b"x" * (MAX_TAXONOMY_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds its size limit"):
        load_article_taxonomy(path)


def test_taxonomy_loader_does_not_cache_stale_path_contents(tmp_path: Path):
    raw = DEFAULT_TAXONOMY_PATH.read_bytes()
    path = tmp_path / "taxonomy.yaml"
    path.write_bytes(raw)
    assert load_article_taxonomy(path).allowed_labels == EXPECTED_LABELS

    path.write_bytes(raw.replace(b"Transition Risk", b"Physical Risk", 1))
    with pytest.raises(ValueError, match="labels must be unique"):
        load_article_taxonomy(path)


def test_editable_taxonomy_changes_identity_without_python_changes(tmp_path, monkeypatch):
    raw = DEFAULT_TAXONOMY_PATH.read_bytes()
    path = tmp_path / "taxonomy.yaml"
    path.write_bytes(raw.replace(b"Physical Risk", b"Physical Hazard", 1))
    monkeypatch.setattr(taxonomy_module, "DEFAULT_TAXONOMY_PATH", path)
    selected = load_article_taxonomy()
    assert "Physical Hazard" in selected.allowed_labels
    assert selected.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    bundle = _valid_semantic_bundle()
    bundle.update(taxonomy_sha256=selected.sha256, categories=["Physical Hazard"])
    assert validate_semantic_bundle(bundle)["categories"] == ["Physical Hazard"]
    old_identity = {"taxonomy_id": selected.taxonomy_id, "sha256": selected.sha256,
                    "definition_yaml": selected.definition_yaml}
    path.write_bytes(raw)
    assert taxonomy_module.frozen_article_taxonomy(old_identity) == selected
    with pytest.raises(ValueError, match="frozen identity"):
        taxonomy_module.frozen_article_taxonomy({**old_identity, "sha256": "0" * 64})


def test_legacy_taxonomy_is_validated_from_the_immutable_archive():
    archive = load_article_taxonomy(DEFAULT_TAXONOMY_PATH.with_name("article_categories_v1.yaml"))
    assert len(archive.categories) == 8
    assert archive.sha256 == "3deefa1cc0df7a2e1ce8ef538271c0ab4465bb928f20e6418aafc8a834794d94"
    assert taxonomy_module.frozen_article_taxonomy({"taxonomy_id": archive.taxonomy_id, "sha256": archive.sha256}) == archive
    bundle = _valid_semantic_bundle()
    bundle.update(taxonomy_id=archive.taxonomy_id, taxonomy_sha256=archive.sha256)
    validate_semantic_bundle(bundle, taxonomy=archive)


@pytest.mark.parametrize("categories", [["Unknown"], ["Physical Risk", "Physical Risk"],
    ["Physical Risk", "Transition Risk", "Climate Risk", "Insurance Risk", "Climate & Health"]])
def test_editable_taxonomy_still_rejects_unknown_duplicate_and_five_categories(categories):
    bundle = _valid_semantic_bundle()
    bundle["categories"] = categories
    with pytest.raises(ValueError):
        validate_semantic_bundle(bundle)


def test_bundle_validator_rejects_forged_taxonomy_objects():
    canonical = load_article_taxonomy()

    wrong_sha = replace(canonical, sha256="0" * 64)
    wrong_sha_bundle = _valid_semantic_bundle()
    wrong_sha_bundle["taxonomy_sha256"] = wrong_sha.sha256
    with pytest.raises(ValueError, match="supported taxonomy"):
        validate_semantic_bundle(wrong_sha_bundle, taxonomy=wrong_sha)

    altered_categories = replace(canonical, categories=canonical.categories[:-1])
    with pytest.raises(ValueError, match="supported taxonomy"):
        validate_semantic_bundle(_valid_semantic_bundle(), taxonomy=altered_categories)

    unsupported = replace(canonical, taxonomy_id="other-v1", sha256="f" * 64)
    unsupported_bundle = _valid_semantic_bundle()
    unsupported_bundle["taxonomy_id"] = unsupported.taxonomy_id
    unsupported_bundle["taxonomy_sha256"] = unsupported.sha256
    with pytest.raises(ValueError, match="supported taxonomy"):
        validate_semantic_bundle(unsupported_bundle, taxonomy=unsupported)


def test_taxonomy_loader_rejects_unhashable_yaml_mapping_keys():
    with pytest.raises(ValueError, match="invalid YAML mapping key"):
        taxonomy_module._parse_article_taxonomy_bytes(b"? [complex, key]\n: value\n")


def test_json_schema_snapshot_matches_the_yaml_authority():
    taxonomy = load_article_taxonomy()
    schema = _semantic_schema()
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
    assert "schema alone does not express" in schema["description"]
    assert properties["schema_version"]["const"] == "article-semantic-bundle.v1"
    assert "const" not in properties["taxonomy_id"]
    assert "const" not in properties["taxonomy_sha256"]
    assert properties["summary"]["minLength"] == taxonomy.constraints.summary_min_chars
    assert properties["summary"]["maxLength"] == taxonomy.constraints.summary_max_chars
    assert properties["categories"]["minItems"] == taxonomy.constraints.categories_min_items
    assert "maxItems" not in properties["categories"]  # Runtime enforces the configured bound.
    assert properties["keywords"]["minItems"] == taxonomy.constraints.keywords_min_items
    assert properties["keywords"]["maxItems"] == taxonomy.constraints.keywords_max_items
    assert properties["keywords"]["items"]["maxLength"] == taxonomy.constraints.keyword_max_chars
    assert properties["categories"]["uniqueItems"] is True
    assert properties["keywords"]["uniqueItems"] is True
    assert "Python validator" in schema["$comment"]


def test_json_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(_semantic_schema())


def test_jsonschema_test_dependency_is_directly_declared():
    requirements = (
        DEFAULT_TAXONOMY_PATH.parents[2] / "requirements.txt"
    ).read_text(encoding="utf-8").splitlines()

    assert "jsonschema>=4.23,<5" in requirements


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("summary", "safe\n"),
        ("categories", ["Climate Risk\n"]),
        ("keywords", ["climate\n", "scenario", "pricing"]),
    ],
)
def test_json_schema_rejects_trailing_line_feed(field, value):
    validator = Draft202012Validator(_semantic_schema())
    bundle = _valid_semantic_bundle()
    bundle[field] = value

    assert not validator.is_valid(bundle)


@pytest.mark.parametrize("field", ["summary", "categories", "keywords"])
def test_json_schema_rejects_every_c0_c1_control(field):
    validator = Draft202012Validator(_semantic_schema())
    for codepoint in (*range(0x20), *range(0x7F, 0xA0)):
        bundle = _valid_semantic_bundle()
        text = f"safe{chr(codepoint)}"
        if field == "summary":
            bundle[field] = text
        elif field == "categories":
            bundle[field] = [text]
        else:
            bundle[field] = [text, "scenario", "pricing"]
        assert not validator.is_valid(bundle), f"{field} accepted U+{codepoint:04X}"
