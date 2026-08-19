from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from climate_monitor.dedupe import canonical_url

from .annotations import ArticleAnnotation, load_article_annotations_strict
from .reports import ParsedArticle, ParsedReport


@dataclass(frozen=True)
class FallbackBundle:
    canonical_url: str
    summary: str
    categories: tuple[str, ...]
    keywords: tuple[str, ...]
    source: str
    provenance: str
    bundle_json: str
    bundle_sha256: str


@dataclass(frozen=True)
class FallbackDecision:
    status: str
    bundle: FallbackBundle | None = None
    reason: str | None = None


def _canonical_bundle(
    *,
    expected_url: str,
    summary: str,
    categories: tuple[str, ...],
    keywords: tuple[str, ...],
    source: str,
    provenance: str,
    identity: dict[str, object],
) -> FallbackBundle | None:
    if (
        not summary
        or summary != summary.strip()
        or not categories
        or not keywords
        or any(not value or value != value.strip() for value in (*categories, *keywords))
    ):
        return None
    payload = {
        "schema_version": 1,
        "canonical_url": expected_url,
        "summary": summary,
        "categories": list(categories),
        "keywords": list(keywords),
        "source": source,
        "provenance": provenance,
        "identity": identity,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return FallbackBundle(
        canonical_url=expected_url,
        summary=summary,
        categories=categories,
        keywords=keywords,
        source=source,
        provenance=provenance,
        bundle_json=encoded,
        bundle_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def select_fallback_bundle(
    *,
    metadata_dir: str | Path | None,
    report: ParsedReport,
    article: ParsedArticle,
    expected_canonical_url: str,
    annotation_catalog: tuple[str, Mapping[str, ArticleAnnotation]] | None = None,
) -> FallbackDecision:
    """Select one complete bundle; JSON and report fields are never spliced."""
    try:
        expected = canonical_url(expected_canonical_url)
        report_url = canonical_url(article.url)
    except (TypeError, UnicodeError, ValueError):
        return FallbackDecision("invalid", reason="canonical_url_invalid")
    if expected != expected_canonical_url or report_url != expected:
        return FallbackDecision("invalid", reason="report_identity_mismatch")

    annotation_status, annotations = (
        annotation_catalog
        if annotation_catalog is not None
        else load_article_annotations_strict(metadata_dir)
    )
    if annotation_status == "invalid":
        return FallbackDecision("invalid", reason="annotation_schema_invalid")
    annotation = annotations.get(expected) if annotation_status == "valid" else None
    if annotation is None and annotation_status == "valid":
        for candidate in annotations.values():
            try:
                candidate_source = canonical_url(candidate.source_url)
            except (TypeError, UnicodeError, ValueError):
                return FallbackDecision("invalid", reason="annotation_identity_mismatch")
            if candidate_source == expected and candidate.canonical_url != expected:
                return FallbackDecision("invalid", reason="annotation_identity_mismatch")
    if annotation is not None:
        bundle = _canonical_bundle(
            expected_url=expected,
            summary=annotation.summary,
            categories=annotation.categories,
            keywords=annotation.keywords,
            source="json_annotation",
            provenance=annotation.provenance,
            identity={
                "schema_version": 1,
                "annotation_method": "subagent-original-content-v1",
                "source_scope": "linked-original-content-with-report-fallback",
                "source_url": annotation.source_url,
                "title": annotation.title,
                "source_basis": annotation.source_basis,
                "generated_on": annotation.generated_on,
            },
        )
        return (
            FallbackDecision("complete", bundle=bundle)
            if bundle is not None
            else FallbackDecision("invalid", reason="annotation_bundle_incomplete")
        )

    bundle = _canonical_bundle(
        expected_url=expected,
        summary=article.summary,
        categories=article.categories,
        keywords=article.keywords,
        source="source_report",
        provenance="source_report",
        identity={
            "report_date": report.report_date,
            "report_sha256": report.sha256,
            "report_title": report.title,
            "article_title": article.title,
            "source_url": article.url,
        },
    )
    return (
        FallbackDecision("complete", bundle=bundle)
        if bundle is not None
        else FallbackDecision("invalid", reason="report_bundle_incomplete")
    )


def resolution_id(
    *, report_id: str, report_sha256: str, article_id: str, fetch_id: str,
    bundle_sha256: str,
) -> str:
    raw = "\0".join((report_id, report_sha256, article_id, fetch_id, bundle_sha256))
    return "resolution-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
