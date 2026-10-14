import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_chat_citations_render_and_open_the_matching_reference():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    names = ("escapeHtml", "inlineFmt", "safeSourceUrl", "safeReportSourceUrl", "sourceNavigationTarget", "sourceNavigationAttributes", "sourceNavigationHref", "sourceNavigationFromHash", "sourceCitationHref", "renderSourceCards")
    functions = []
    for name in names:
        start = source.index(f"function {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    script = "\n".join(functions) + r'''
const html = inlineFmt('Single [1], merged [1, 2], code `[1, 2]`, link [1, 2](https://example.org/[3]), URL https://example.org/[4].');
const article = sourceCitationHref({path: "wiki/article-x.md", canonical_url: "https://publisher.example/article"});
const report = sourceCitationHref({path: "sources/weekly.md", url: "https://github.example/report", source_urls: ["https://unrelated.example"]});
const cards = renderSourceCards([
  {index: 1, title: "Meeting", path: "wiki/registry-meetings.md", matched_section: "G20 Leaders' Summit, Miami"},
  {index: 2, title: "Article", path: "wiki/article-article-00de8b1c45986f255c385eb6.md", canonical_url: "https://publisher.example/article", score: 2.75},
  {index: 3, title: "Report", path: "sources/climate-monitor-2026-08-10.md"},
  {index: 4, title: "PDF article", path: "wiki/pdf-article-article-pdf1.md"},
  {index: 5, title: "Fresh page", type: "web", url: "https://publisher.example/article"},
  {index: 6, title: "PDF observation", path: "wiki/registry-source-observation-pdf-article-occurrence-occ-123.md"},
  {index: 7, title: "COP31 meeting", meeting_id: "event-123"},
  {index: 8, title: "Legacy meeting", type: "meeting", heading: "meeting", evidence_id: "event-legacy", title: "Legacy meeting"},
  {index: 9, title: "Legacy article", type: "article", heading: "article", evidence_id: "article-legacy", canonical_url: "https://publisher.example/legacy"},
  {index: 10, title: "Legacy PDF article", type: "pdf_article", heading: "pdf_article", evidence_id: "pdf-article-legacy", url: "https://publisher.example/pdf-legacy"},
  {index: 11, title: "Wiki chunk", type: "wiki", heading: "Article evidence", evidence_id: "wiki:chunk-99", url: "https://publisher.example/wiki-chunk"},
  {index: 12, title: "Web source identity", type: "web", heading: "web", evidence_id: "web-source-99", url: "https://publisher.example/web-fact"},
]);
const window = {location: {hash: sourceNavigationHref({path: "wiki/article-article-00de8b1c45986f255c385eb6.md", canonical_url: "https://publisher.example/article"})}};
const legacyTargets = [
  {type: "meeting", heading: "meeting", evidence_id: "event-legacy", title: "Legacy meeting"},
  {type: "article", heading: "article", evidence_id: "article-legacy", canonical_url: "https://publisher.example/legacy"},
  {type: "pdf_article", heading: "pdf_article", evidence_id: "pdf-article-legacy", url: "https://publisher.example/pdf-legacy"},
  {type: "wiki", heading: "Article evidence", evidence_id: "wiki:chunk-99", path: "wiki/topics/risk.md"},
  {type: "pdf_article", heading: "pdf_article", evidence_id: "wiki:chunk-100", corpus: "wiki"},
  {type: "web", heading: "web", evidence_id: "web-source-99", url: "https://publisher.example/web-fact"},
].map(sourceNavigationTarget);
process.stdout.write(JSON.stringify({html, article, report, cards, articleTarget: sourceNavigationFromHash(), legacyTargets}));
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)
    rendered = json.loads(result.stdout)
    html = rendered["html"]
    assert html.count('class="citation"') == 3
    assert 'data-citation-index="1"' in html and 'data-citation-index="2"' in html
    assert '<code>[1, 2]</code>' in html
    assert '<a href="https://example.org/[3]"' in html
    assert "https://example.org/[4]" in html
    assert 'data-citation-index="3"' not in html and 'data-citation-index="4"' not in html
    assert rendered["article"] == "https://publisher.example/article"
    assert rendered["report"] == "/sources/weekly.md"
    cards = rendered["cards"]
    assert 'data-source-navigation="meeting"' in cards
    assert 'data-meeting-name="G20 Leaders\' Summit, Miami"' in cards
    assert 'data-article-id="article-00de8b1c45986f255c385eb6"' in cards
    assert 'data-source-navigation="report" data-report-date="2026-08-10" data-report-source-url="/sources/climate-monitor-2026-08-10.md"' in cards
    assert 'data-article-id="article-pdf1" data-article-source="pdf"' in cards
    assert 'href="#meeting=name%3AG20%20Leaders\'%20Summit%2C%20Miami"' in cards
    assert 'data-original-url="https://publisher.example/article"' in cards
    assert 'href="#registry-article=registry:article-00de8b1c45986f255c385eb6&amp;url=https%3A%2F%2Fpublisher.example%2Farticle"' in cards
    assert rendered["articleTarget"] == {"kind": "article", "source": "registry", "id": "article-00de8b1c45986f255c385eb6", "url": "https://publisher.example/article"}
    assert 'href="#historical-report=2026-08-10&amp;url=%2Fsources%2Fclimate-monitor-2026-08-10.md"' in cards
    assert 'data-source-navigation="article-url" data-source-url="https://publisher.example/article"' in cards
    assert 'href="#registry-article-url=https%3A%2F%2Fpublisher.example%2Farticle"' in cards
    assert 'data-source-navigation="pdf-observation" data-occurrence-id="pdf-article-occurrence-occ-123"' in cards
    assert 'href="#registry-pdf-occurrence=pdf-article-occurrence-occ-123&amp;search=PDF+observation&amp;url=&amp;snippet="' in cards
    assert 'data-meeting-id="event-123"' in cards
    assert 'href="#meeting=id%3Aevent-123%7Cname%3ACOP31%20meeting"' in cards
    assert 'data-meeting-id="event-legacy"' in cards
    assert 'href="#meeting=id%3Aevent-legacy%7Cname%3ALegacy%20meeting"' in cards
    assert 'data-article-id="article-legacy" data-article-source="registry"' in cards
    assert 'href="#registry-article=registry:article-legacy&amp;url=https%3A%2F%2Fpublisher.example%2Flegacy"' in cards
    assert 'data-article-id="pdf-article-legacy" data-article-source="pdf"' in cards
    assert 'href="#registry-article=pdf:pdf-article-legacy&amp;url=https%3A%2F%2Fpublisher.example%2Fpdf-legacy"' in cards
    assert rendered["legacyTargets"][3] is None
    assert rendered["legacyTargets"][4] is None, "Wiki chunk IDs must not be treated as PDF article identities"
    assert rendered["legacyTargets"][5] == {"kind": "article-url", "url": "https://publisher.example/web-fact"}
    assert "Retrieval score 2.75" in cards and "Retrieval score 3" not in cards
    assert "Retrieval score 2.75%" not in cards
