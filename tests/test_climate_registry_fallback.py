from __future__ import annotations

import json
from pathlib import Path

from climate_registry.fallback import select_fallback_bundle
from climate_registry.reports import ParsedArticle, ParsedReport


def _report(article: ParsedArticle) -> ParsedReport:
    return ParsedReport(
        path=Path("climate-monitor-2026-08-17.md"),
        report_date="2026-08-17",
        title="Weekly Climate Monitor",
        sha256="a" * 64,
        cadence="weekly",
        report_format="weekly-monitor-v2",
        sites_checked=1,
        sites_succeeded=0,
        sites_failed=1,
        executive_summary=("Summary",),
        articles=(article,),
        warnings=(),
    )


def _article(**overrides) -> ParsedArticle:
    values = {
        "section": "pillar-a",
        "pillar": "A",
        "title": "Publisher update",
        "summary": "A complete report summary.",
        "url": "https://example.com/update",
        "categories": ("Climate Risk",),
        "keywords": ("climate", "insurance", "capital"),
    }
    values.update(overrides)
    return ParsedArticle(**values)


def test_absent_json_uses_one_complete_report_bundle(tmp_path):
    article = _article()
    decision = select_fallback_bundle(
        metadata_dir=tmp_path / "absent",
        report=_report(article),
        article=article,
        expected_canonical_url=article.url,
    )
    assert decision.status == "complete"
    assert decision.bundle is not None
    assert decision.bundle.source == "source_report"
    assert json.loads(decision.bundle.bundle_json)["identity"]["report_sha256"] == "a" * 64


def test_present_invalid_json_blocks_report_fallback(tmp_path):
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "articles-001-001.json").write_text("{}", encoding="utf-8")
    article = _article()
    decision = select_fallback_bundle(
        metadata_dir=metadata,
        report=_report(article),
        article=article,
        expected_canonical_url=article.url,
    )
    assert decision.status == "invalid"
    assert decision.reason == "annotation_schema_invalid"


def test_valid_json_without_the_url_allows_complete_report_bundle(tmp_path):
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "articles-001-001.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "annotation_method": "subagent-original-content-v1",
                "source_scope": "linked-original-content-with-report-fallback",
                "generated_on": "2026-08-17",
                "articles": [
                    {
                        "canonical_url": "https://other.example/item",
                        "source_url": "https://other.example/item",
                        "title": "Other",
                        "source_basis": "original_content",
                        "summary": "A complete unrelated annotation.",
                        "categories": ["Climate Risk"],
                        "keywords": ["climate", "insurance", "capital"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    article = _article()
    decision = select_fallback_bundle(
        metadata_dir=metadata,
        report=_report(article),
        article=article,
        expected_canonical_url=article.url,
    )
    assert decision.status == "complete"
    assert decision.bundle is not None
    assert decision.bundle.source == "source_report"


def test_json_claim_for_target_with_wrong_canonical_identity_blocks_report(tmp_path):
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "articles-001-001.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "annotation_method": "subagent-original-content-v1",
                "source_scope": "linked-original-content-with-report-fallback",
                "generated_on": "2026-08-17",
                "articles": [
                    {
                        "canonical_url": "https://other.example/item",
                        "source_url": "https://example.com/update",
                        "title": "Wrong identity",
                        "source_basis": "publisher_excerpt",
                        "summary": "A complete but wrongly bound annotation.",
                        "categories": ["Climate Risk"],
                        "keywords": ["climate", "insurance", "capital"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    article = _article()
    decision = select_fallback_bundle(
        metadata_dir=metadata,
        report=_report(article),
        article=article,
        expected_canonical_url=article.url,
    )
    assert decision.status == "invalid"
    assert decision.reason == "annotation_identity_mismatch"


def test_report_bundle_is_not_spliced_when_metadata_is_incomplete(tmp_path):
    article = _article(keywords=())
    decision = select_fallback_bundle(
        metadata_dir=tmp_path / "absent",
        report=_report(article),
        article=article,
        expected_canonical_url=article.url,
    )
    assert decision.status == "invalid"
    assert decision.reason == "report_bundle_incomplete"


def test_report_canonical_identity_mismatch_fails_closed(tmp_path):
    article = _article(url="https://example.com/other")
    decision = select_fallback_bundle(
        metadata_dir=tmp_path / "absent",
        report=_report(article),
        article=article,
        expected_canonical_url="https://example.com/update",
    )
    assert decision.status == "invalid"
    assert decision.reason == "report_identity_mismatch"


def test_report_sha_change_invalidates_report_fallback_bundle(tmp_path):
    article = _article()
    first_report = _report(article)
    second_report = ParsedReport(
        **{**first_report.__dict__, "sha256": "b" * 64}
    )
    first = select_fallback_bundle(
        metadata_dir=tmp_path / "absent",
        report=first_report,
        article=article,
        expected_canonical_url=article.url,
    )
    second = select_fallback_bundle(
        metadata_dir=tmp_path / "absent",
        report=second_report,
        article=article,
        expected_canonical_url=article.url,
    )
    assert first.bundle is not None and second.bundle is not None
    assert first.bundle.bundle_sha256 != second.bundle.bundle_sha256
