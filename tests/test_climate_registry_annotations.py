import json
import os
from collections import Counter
from pathlib import Path

import pytest

from climate_monitor.dedupe import canonical_url
from climate_registry.annotations import (
    ALLOWED_CATEGORIES,
    DISALLOWED_KEYWORDS,
    load_article_annotations,
    load_article_annotations_strict,
)
import climate_registry.annotations as annotation_module
from climate_registry.reports import parse_report_directory


def _valid_annotation_payload():
    return {
        "schema_version": 1,
        "annotation_method": "subagent-original-content-v1",
        "source_scope": "linked-original-content-with-report-fallback",
        "generated_on": "2026-08-17",
        "articles": [
            {
                "canonical_url": "https://example.com/flood",
                "source_url": "https://example.com/flood",
                "title": "Flood pricing",
                "source_basis": "original_content",
                "summary": "Insurers reviewed flood pricing.",
                "categories": ["Physical Risk", "Insurance Risk"],
                "keywords": ["flood", "pricing", "insurers"],
            }
        ],
    }


def test_annotation_batches_fail_closed_when_an_entry_is_not_canonical(tmp_path):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    payload = _valid_annotation_payload()
    payload["articles"][0]["canonical_url"] = "https://example.com/wrong"
    path = metadata_dir / "articles-001-001.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_article_annotations(metadata_dir) == {}

    payload["articles"][0]["canonical_url"] = "https://example.com/flood"
    path.write_text(json.dumps(payload), encoding="utf-8")
    annotations = load_article_annotations(metadata_dir)
    assert annotations["https://example.com/flood"].summary == "Insurers reviewed flood pricing."
    assert annotations["https://example.com/flood"].provenance == "original_content_annotation"


def test_annotation_batches_reject_bool_schema_and_non_http_urls(tmp_path):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    path = metadata_dir / "articles-001-001.json"

    payload = _valid_annotation_payload()
    payload["schema_version"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_article_annotations(metadata_dir) == {}

    for invalid_url in ("", "/relative/article"):
        payload = _valid_annotation_payload()
        payload["articles"][0]["source_url"] = invalid_url
        payload["articles"][0]["canonical_url"] = invalid_url
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert load_article_annotations(metadata_dir) == {}


def test_annotation_batches_reject_duplicate_json_members(tmp_path):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    path = metadata_dir / "articles-001-001.json"
    payload = json.dumps(_valid_annotation_payload())
    payload = payload.replace('"schema_version": 1', '"schema_version": 999, "schema_version": 1')
    path.write_text(payload, encoding="utf-8")

    assert load_article_annotations(metadata_dir) == {}


def test_annotation_batches_allow_distinct_official_evidence_urls_only_for_alternates(
    tmp_path,
):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    path = metadata_dir / "articles-001-001.json"

    for source_basis in ("official_replacement", "publisher_excerpt"):
        payload = _valid_annotation_payload()
        payload["articles"][0]["source_basis"] = source_basis
        payload["articles"][0]["source_url"] = "https://publisher.example/replacement"
        path.write_text(json.dumps(payload), encoding="utf-8")
        annotation = load_article_annotations(metadata_dir)["https://example.com/flood"]
        assert annotation.source_url == "https://publisher.example/replacement"
        assert annotation.provenance == f"{source_basis}_annotation"

    payload = _valid_annotation_payload()
    payload["articles"][0]["source_url"] = "https://publisher.example/replacement"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_article_annotations(metadata_dir) == {}


def test_bundled_annotations_cover_each_unique_historical_article_once():
    root = Path(__file__).resolve().parents[1]
    reports = parse_report_directory(root / "sources")
    expected_urls = {
        canonical_url(article.url)
        for report in reports
        if report.report_date <= "2026-08-17"
        for article in report.articles
    }
    annotations = load_article_annotations(root / "article_metadata")

    assert len(expected_urls) == 161
    assert set(annotations) == expected_urls
    assert all(set(item.categories) <= ALLOWED_CATEGORIES for item in annotations.values())
    assert all(
        keyword.casefold() not in DISALLOWED_KEYWORDS
        for item in annotations.values()
        for keyword in item.keywords
    )
    assert Counter(item.source_basis for item in annotations.values()) == {
        "original_content": 155,
        "official_replacement": 4,
        "publisher_excerpt": 2,
    }
    assert all(
        canonical_url(item.source_url) != item.canonical_url
        for item in annotations.values()
        if item.source_basis in {"official_replacement", "publisher_excerpt"}
    )


def test_annotation_loader_rejects_symlinked_batch(tmp_path):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_valid_annotation_payload()), encoding="utf-8")
    link = metadata_dir / "articles-001-001.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    assert load_article_annotations_strict(metadata_dir) == ("invalid", {})


def test_annotation_loader_detects_path_identity_swap(tmp_path, monkeypatch):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    path = metadata_dir / "articles-001-001.json"
    path.write_text(json.dumps(_valid_annotation_payload()), encoding="utf-8")
    real_lstat = annotation_module.os.lstat
    alternate = tmp_path / "alternate.json"
    alternate.write_text(json.dumps(_valid_annotation_payload()) + " ", encoding="utf-8")
    calls = 0

    def swapped_lstat(candidate):
        nonlocal calls
        result = real_lstat(candidate)
        if Path(candidate) == path:
            calls += 1
            if calls > 1:
                return real_lstat(alternate)
        return result

    monkeypatch.setattr(annotation_module.os, "lstat", swapped_lstat)
    assert load_article_annotations_strict(metadata_dir) == ("invalid", {})


def test_annotation_loader_enforces_single_total_file_and_article_bounds(
    tmp_path, monkeypatch
):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    first = json.dumps(_valid_annotation_payload()).encode("utf-8")
    (metadata_dir / "articles-001-001.json").write_bytes(first)

    monkeypatch.setattr(annotation_module, "MAX_ANNOTATION_BYTES", len(first) - 1)
    assert load_article_annotations_strict(metadata_dir) == ("invalid", {})
    monkeypatch.setattr(annotation_module, "MAX_ANNOTATION_BYTES", len(first) + 10)

    second_payload = _valid_annotation_payload()
    second_payload["articles"][0]["canonical_url"] = "https://example.com/second"
    second_payload["articles"][0]["source_url"] = "https://example.com/second"
    second = json.dumps(second_payload).encode("utf-8")
    (metadata_dir / "articles-002-002.json").write_bytes(second)
    monkeypatch.setattr(annotation_module, "MAX_ANNOTATION_FILES", 1)
    assert load_article_annotations_strict(metadata_dir) == ("invalid", {})
    monkeypatch.setattr(annotation_module, "MAX_ANNOTATION_FILES", 2)
    monkeypatch.setattr(annotation_module, "MAX_ANNOTATION_TOTAL_BYTES", len(first) + len(second) - 1)
    assert load_article_annotations_strict(metadata_dir) == ("invalid", {})
    monkeypatch.setattr(annotation_module, "MAX_ANNOTATION_TOTAL_BYTES", len(first) + len(second))
    monkeypatch.setattr(annotation_module, "MAX_ANNOTATION_ARTICLES", 1)
    assert load_article_annotations_strict(metadata_dir) == ("invalid", {})
