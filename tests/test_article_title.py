from __future__ import annotations

import copy
import hashlib

import pytest

from climate_monitor.article_title import extract_page_title
from climate_monitor import article_content_adapter as adapter
from tests.fixtures.article_content import providers as loopbacks


@pytest.mark.parametrize("html,expected", [
    ('<title>SEO title | Site</title><header><h1>Site</h1></header>'
     '<main><h1>Europe’s <em>Heat</em> &amp; the IPCC</h1></main>',
     ("Europe’s Heat & the IPCC", "h1")),
    ('<nav><h1>Menu</h1></nav><h1 hidden>Hidden</h1>'
     '<h1 aria-hidden="true">Hidden too</h1><h1>Visible headline</h1>',
     ("Visible headline", "h1")),
    ('<meta property="og:title" content="Original headline"><title>SEO | Site</title>',
     ("Original headline", "og:title")),
    ('<title>NASA climate &amp; insurance</title>', ("NASA climate & insurance", "title")),
    ('<main><p>No headline here.</p></main>', None),
])
def test_extract_original_headline_without_case_rewriting(html, expected):
    assert extract_page_title(html) == expected


@pytest.mark.parametrize("enabled", [True, False])
def test_adapter_binds_title_to_verified_body_preserving_discovery(enabled):
    html = '<html><title>SEO title | Site</title><main><h1>Original H1</h1></main></html>'
    body_hash = hashlib.sha256(html.encode()).hexdigest()
    calls = []

    def provider(article_id, url):
        calls.append(url)
        result = loopbacks.loopback_success_provider(article_id, url)
        ref, digest = loopbacks.in_process_resolver.put(html.encode())
        # Exercise the long-body ref-only path; the plugin must see resolved HTML.
        result["data"].pop("full_text")
        result["data"].update(content_ref=ref, sha256=digest, content_hash=digest)
        return result

    provider.content_resolver = loopbacks.in_process_resolver
    article = {"article_id": "a", "url": "https://example.org/article", "title": "",
               "title_basis": None, "origins": [{"pillar": "B", "source": "Publisher"}]}
    original = copy.deepcopy(article)
    artifact = adapter.build_article_evidence_artifact(
        [article], providers=(provider,), report_date="2026-09-07",
        include_verified_content=True, title_extractor=extract_page_title if enabled else None)
    record = artifact["records"][0]
    assert calls == [article["url"]]
    assert record["content_hash"] == body_hash
    assert record["content"] == html
    assert record["origins"] == original["origins"]
    assert article == original
    assert record["title"] == ("Original H1" if enabled else "")
    if enabled:
        assert record["title_basis"] == "page"
        assert record["extra"]["page_title_evidence"] == {
            "source": "h1", "content_hash": body_hash,
            "discovery_title": "", "discovery_title_basis": None}
    adapter.validate_retained_article_evidence(artifact, report_date="2026-09-07", urls=[article["url"]])


def test_adapter_retains_discovery_title_when_no_page_title():
    artifact = adapter.build_article_evidence_artifact(
        [{"article_id": "a", "url": "https://example.org/article", "title": "Search title"}],
        providers=(loopbacks.loopback_success_provider,), report_date="2026-09-07",
        include_verified_content=True, title_extractor=extract_page_title)
    assert artifact["records"][0]["title"] == "Search title"
    assert "page_title_evidence" not in artifact["records"][0]["extra"]
