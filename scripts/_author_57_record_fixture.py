"""Author the canonical 57-record v2 fixture for Issue #87 AC-2/AC-5.

Run from the repository root:

    python3 scripts/_author_57_record_fixture.py

Outputs three files under tests/fixtures/issue87/:

* 57_article_evidence.json     -- ``article-evidence.v1`` envelope
* 57_stats.json                -- the canonical 6-key stats dict
* 57_record_fixture.json       -- placeholder response (the dry-run script
                                  builds a real v2 response at runtime via
                                  the production driver's request emitter so
                                  the request_sha256 is always consistent).

The fixture represents the Issue #87 acceptance criteria: 33 updated + 9
unchanged + 14 blocked + 1 failed + 0 unresolved = 57 total; report 57/42/15.

It deliberately references the known-leaky ADB and WRI URLs from the Issue
so the migration probes the exact Issue #87 cases.

Only the canonical-URL + title + basis fields are populated; the
article-evidence envelope is the minimum shape the driver accepts.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REPORT_DATE = "2026-09-07"
STATS = {
    "total": 57,
    "updated": 33,
    "unchanged": 9,
    "blocked": 14,
    "failed": 1,
    "unresolved": 0,
}


def _evidence(
    *,
    article_id: str,
    url: str,
    title: str,
    status: str,
    selected_method: str | None,
    content_type: str | None,
    content_ref: str | None,
    content_hash: str | None,
    summary_basis: str,
    title_basis: str = "upstream_artifact",
    display_pillar: str = "A",
    origins: list[dict] | None = None,
    failure_reason: str | None = None,
    search_snippet: str | None = None,
) -> dict:
    record = {
        "article_id": article_id,
        "requested_url": url,
        "final_url": url,
        "title": title,
        "status": status,
        "attempts": [{"tool": selected_method}] if selected_method else [],
        "selected_method": selected_method,
        "content_type": content_type,
        "content_ref": content_ref,
        "content_hash": content_hash,
        "summary_basis": summary_basis,
        "title_basis": title_basis,
        "display_pillar": display_pillar,
        "origins": origins or [{"pillar": display_pillar, "source": "fixture", "url": url}],
    }
    if search_snippet is not None:
        record["search_snippet"] = search_snippet
    if failure_reason is not None:
        record["failure_reason"] = failure_reason
    return record


def _build_articles() -> list[dict]:
    """Build the kept-article evidence records.

    The orchestrator keeps at most ``max_items_per_report`` candidates, so
    the dry-run fixture is intentionally tiny (one kept article) — the
    canonical 57/42/15 split is encoded in the v2 stats dict on the
    response, not in the article set. This matches the existing
    ``test_v2_authoring_response_exposes_canonical_57_42_15_split`` test.

    The URL is the same IAIS URL used by
    ``tests/weekly_monitor/test_weekly_driver.py``'s manifest fixture so
    the orchestrator's ``kept`` set picks it up and the response's
    canonical URL matches the orchestrator's expected identity.
    """
    iais_url = "https://www.iais.org/climate-supervision"
    adb_url = "https://www.adb.org/news/asia-climate-resilience-2026"
    wri_url = "https://www.wri.org/insights/restoring-nature-fights-wildfires-heat-floods"
    body = "IAIS climate supervision update content for fixture."
    digest = hashlib.sha256(body.encode()).hexdigest()

    return [
        _evidence(
            article_id="iais-climate-supervision-2026",
            url=iais_url,
            title="IAIS climate supervision update",
            status="ok",
            selected_method="http",
            content_type="text/html",
            content_ref=f"memory:{digest}",
            content_hash=digest,
            summary_basis="article_content",
            origins=[
                # The kept URL is the IAIS URL so it matches the manifest
                # fixture's kept set; the additional origins surface the
                # ADB and WRI leaky URLs as sibling candidates the
                # orchestrator should dedup by canonical URL.
                {"pillar": "A", "source": "iais", "url": iais_url},
                {"pillar": "A", "source": "adb", "url": adb_url},
                {"pillar": "A", "source": "wri-mirror", "url": wri_url},
            ],
        )
    ]


def main() -> int:
    out_dir = ROOT / "tests" / "fixtures" / "issue87"
    out_dir.mkdir(parents=True, exist_ok=True)
    articles = _build_articles()

    article_evidence = {
        "schema_version": "article-evidence.v1",
        "records": articles,
    }
    (out_dir / "57_article_evidence.json").write_text(
        json.dumps(article_evidence, indent=2, sort_keys=True), encoding="utf-8"
    )

    (out_dir / "57_stats.json").write_text(
        json.dumps(STATS, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Placeholder v2 response. The dry-run script builds the real response
    # at runtime via the production driver so the request_sha256 always
    # matches the emitted request. The shape here documents the contract
    # for offline inspection only.
    placeholder = {
        "_placeholder": True,
        "_note": (
            "Real v2 response is built at runtime by the dry-run script via "
            "build_authoring_request so request_sha256 stays consistent. "
            "The fixture exists only for documentation; the dry-run script "
            "ignores this file."
        ),
        "schema_version": "weekly-monitor-authoring-response.v2",
        "contract_version": "weekly-monitor-authoring.v2",
        "stats": STATS,
    }
    (out_dir / "57_record_fixture.json").write_text(
        json.dumps(placeholder, indent=2, sort_keys=True), encoding="utf-8"
    )

    print(f"wrote {out_dir}/57_article_evidence.json")
    print(f"wrote {out_dir}/57_stats.json")
    print(f"wrote {out_dir}/57_record_fixture.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())