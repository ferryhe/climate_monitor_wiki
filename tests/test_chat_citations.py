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
    names = ("escapeHtml", "inlineFmt", "safeSourceUrl", "sourceCitationHref")
    functions = []
    for name in names:
        start = source.index(f"function {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    script = "\n".join(functions) + r'''
const html = inlineFmt('Single [1], merged [1, 2], code `[1, 2]`, link [1, 2](https://example.org/[3]), URL https://example.org/[4].');
const article = sourceCitationHref({path: "wiki/article-x.md", canonical_url: "https://publisher.example/article"});
const report = sourceCitationHref({path: "sources/weekly.md", url: "https://github.example/report", source_urls: ["https://unrelated.example"]});
process.stdout.write(JSON.stringify({html, article, report}));
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
