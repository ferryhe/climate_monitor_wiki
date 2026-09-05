from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from climate_monitor.article_candidate_contract import adapt_article_changes, adapt_pillar_b
from climate_monitor import (
    candidate_aggregation,
    orchestrator,
    semantic_bundle,
    web_listening_adapter,
)
from climate_monitor.candidate_aggregation import (
    combine_candidate_collections,
    combine_current_artifacts,
    combine_runtime_items,
    commit_combined_candidates,
    combined_candidates_path,
    serialize_combined_candidates,
    validate_combined_candidates,
)
from climate_monitor.candidate_snapshot import (
    candidate_item_snapshot_path,
    verify_candidate_item_snapshot,
)
from climate_monitor.models import CandidateItem, MonitorSource
from climate_monitor.dedupe import canonical_url
from climate_monitor.orchestrator import run_monitor
from climate_monitor.seen_state import (
    commit_seen_url_delta,
    pending_seen_url_delta_path,
    prepare_seen_url_delta,
)
from climate_monitor.semantic_bundle import semantic_sidecar_path, verify_semantic_sidecar


def _pillar_a(*items: dict[str, object]) -> dict[str, object]:
    return {
        "date": "2026-09-07",
        "pillar": "A",
        "sites_with_changes": 1,
        "orgs_with_articles": 1 if items else 0,
        "baseline_urls": 4,
        "new_articles": len(items),
        "seen_before": 2,
        "generated_at": "2026-09-07T08:10:00Z",
        "articles": [{"org": "Example Org", "items": list(items)}] if items else [],
    }


def _a_item(title: str, url: str) -> dict[str, object]:
    return {"title": title, "url": url, "categories": ["financial_risk"]}


def _b_item(title: str, url: str) -> dict[str, object]:
    return {
        "title": title,
        "url": url,
        "source": "web",
        "summary": "Search-result evidence for climate insurance risk.",
    }


def _sha(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def test_current_artifacts_merge_only_by_canonical_url_and_retain_origins():
    pillar_a = _pillar_a(
        _a_item("Shared title", "https://example.org/report?edition=2025"),
        _a_item("Shared title", "https://example.org/report?edition=2026"),
        _a_item(
            "Tracking copy",
            "https://example.org/shared?utm_source=mail#findings",
        ),
    )
    pillar_b = [
        _b_item("Different discovery title", "https://example.org/shared?fbclid=abc"),
        _b_item("Shared title", "https://example.net/different-url"),
    ]

    result = combine_current_artifacts(
        pillar_a,
        pillar_b,
        report_date="2026-09-07",
        pillar_a_artifact_id="article_changes_2026-09-07.json",
        pillar_a_artifact_sha256=_sha(pillar_a),
        pillar_b_artifact_id="pillar_b_2026-09-07.json",
        pillar_b_artifact_sha256=_sha(pillar_b),
        pillar_b_discovered_at="2026-09-07T00:00:00Z",
        seen_urls=set(),
    )

    artifact = result.artifact
    assert artifact["counts"] == {
        "pillar_a_rows": 3,
        "pillar_b_rows": 2,
        "unique_urls": 4,
        "cross_pillar_merges": 1,
        "history_skips": 0,
        "invalid_rows": 0,
    }
    assert len(artifact["items"]) == 4
    shared = next(
        item for item in artifact["items"] if item["canonical_url"] == "https://example.org/shared"
    )
    assert shared["display_pillar"] == "A"
    assert [origin["pillar"] for origin in shared["origins"]] == ["A", "B"]
    assert {
        origin["original_title"] for origin in shared["origins"]
    } == {"Tracking copy", "Different discovery title"}

    validated = validate_combined_candidates(artifact)
    assert serialize_combined_candidates(validated) == result.artifact_bytes


def test_combined_artifact_bytes_do_not_depend_on_collection_order():
    pillar_a = _pillar_a(_a_item("A title", "https://example.org/shared?utm_medium=email"))
    pillar_b = [_b_item("B title", "https://example.org/shared#section")]
    a_candidates = adapt_article_changes(
        pillar_a, artifact_id="a.json", artifact_sha256=_sha(pillar_a)
    )
    b_candidates = adapt_pillar_b(
        pillar_b,
        artifact_id="b.json",
        artifact_sha256=_sha(pillar_b),
        discovered_at="2026-09-07T00:00:00Z",
    )

    first = combine_candidate_collections(
        a_candidates,
        b_candidates,
        report_date="2026-09-07",
        seen_urls=set(),
    )
    swapped = combine_candidate_collections(
        b_candidates,
        a_candidates,
        report_date="2026-09-07",
        seen_urls=set(),
    )

    assert first.artifact_bytes == swapped.artifact_bytes
    assert first.artifact["counts"] == swapped.artifact["counts"]


def test_combined_artifact_records_recomputable_history_skips():
    pillar_a = _pillar_a(_a_item("A title", "https://example.org/seen?utm_source=x"))
    result = combine_current_artifacts(
        pillar_a,
        [],
        report_date="2026-09-07",
        pillar_a_artifact_id="a.json",
        pillar_a_artifact_sha256=_sha(pillar_a),
        pillar_b_artifact_id="b.json",
        pillar_b_artifact_sha256=_sha([]),
        pillar_b_discovered_at="2026-09-07T00:00:00Z",
        seen_urls={"https://example.org/seen"},
    )

    assert result.candidates == ()
    assert len(result.history_skips) == 1
    assert result.artifact["counts"]["unique_urls"] == 1
    assert result.artifact["counts"]["history_skips"] == 1
    assert len(result.artifact["history_skips"]) == 1


def test_single_combined_artifact_commit_is_atomic_on_interruption(tmp_path, monkeypatch):
    result = combine_current_artifacts(
        _pillar_a(_a_item("A title", "https://example.org/current")),
        [],
        report_date="2026-09-07",
        pillar_a_artifact_id="a.json",
        pillar_a_artifact_sha256="a" * 64,
        pillar_b_artifact_id="b.json",
        pillar_b_artifact_sha256="b" * 64,
        pillar_b_discovered_at="2026-09-07T00:00:00Z",
        seen_urls=set(),
    )
    destination = tmp_path / "combined-candidates_2026-09-07.json"
    destination.write_bytes(b"previous evidence\n")

    monkeypatch.setattr(
        candidate_aggregation.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("simulated single-artifact interruption")
        ),
    )
    with pytest.raises(KeyboardInterrupt, match="single-artifact"):
        commit_combined_candidates(destination, result.artifact_bytes)

    assert destination.read_bytes() == b"previous evidence\n"
    assert not destination.with_name(destination.name + ".tmp").exists()


def test_runtime_invalid_row_count_is_recomputable_without_copying_row_content():
    valid = CandidateItem(
        title="Valid title",
        url="https://example.org/valid",
        summary="Valid climate insurance evidence.",
        source_name="Example",
        lane="website",
    )
    invalid = replace(valid, title="Invalid", url="", lane="research")

    result = combine_runtime_items(
        [valid],
        [invalid],
        report_date="2026-09-07",
        pillar_a_artifact_id="a.json",
        pillar_a_artifact_sha256="a" * 64,
        pillar_b_artifact_id="b.json",
        pillar_b_artifact_sha256="b" * 64,
        discovered_at="2026-09-07T00:00:00Z",
        seen_urls=set(),
    ).combined

    assert result.artifact["counts"] == {
        "pillar_a_rows": 1,
        "pillar_b_rows": 1,
        "unique_urls": 1,
        "cross_pillar_merges": 0,
        "history_skips": 0,
        "invalid_rows": 1,
    }
    assert result.artifact["invalid_rows"] == [
        {
            "pillar": "B",
            "input_artifact": {"artifact_id": "b.json", "sha256": "b" * 64},
            "row": "/0",
            "reasons": ["invalid_candidate"],
        }
    ]


def test_runtime_carry_uses_item_matching_merged_display_pillar():
    url = "https://example.org/shared"
    prior_a_payload = _pillar_a(_a_item("Prior A title", url))
    prior_a_payload["articles"][0]["org"] = "WebOrg"
    prior_a = combine_current_artifacts(
        prior_a_payload,
        [],
        report_date="2026-09-07",
        pillar_a_artifact_id="prior-a.json",
        pillar_a_artifact_sha256="a" * 64,
        pillar_b_artifact_id="prior-b.json",
        pillar_b_artifact_sha256="b" * 64,
        pillar_b_discovered_at="2026-09-07T00:00:00Z",
        seen_urls=set(),
    ).candidates[0]
    semantics = {
        "summary": "Previously authored summary.",
        "categories": ["Supervision & Disclosure"],
        "keywords": ["climate risk", "insurance capital", "supervision"],
    }
    carried_a = CandidateItem(
        title="Prior A title",
        url=url,
        summary="Previously authored summary.",
        source_name="WebOrg",
        lane="website",
        semantics=semantics,
    )
    current_b = CandidateItem(
        title="Current B title",
        url=f"{url}?utm_source=search#details",
        summary="Current research summary.",
        source_name="Research search",
        lane="research",
    )

    merged = combine_runtime_items(
        [],
        [current_b],
        report_date="2026-09-07",
        pillar_a_artifact_id="current-a.json",
        pillar_a_artifact_sha256="c" * 64,
        pillar_b_artifact_id="current-b.json",
        pillar_b_artifact_sha256="d" * 64,
        discovered_at="2026-09-07T01:00:00Z",
        seen_urls=set(),
        carry_forward_candidates=(prior_a,),
        carry_forward_items=(carried_a,),
    )

    assert len(merged.combined.candidates) == 1
    candidate = merged.combined.candidates[0]
    assert candidate.display_pillar == "A"
    assert [origin.pillar for origin in candidate.origins] == ["A", "B"]
    assert len(merged.items) == 1
    assert merged.items[0].source_name == "WebOrg"
    assert merged.items[0].lane == "website"
    assert merged.items[0].semantics == semantics

    prior_b = combine_current_artifacts(
        _pillar_a(),
        [_b_item("Prior B title", url)],
        report_date="2026-09-07",
        pillar_a_artifact_id="prior-a-empty.json",
        pillar_a_artifact_sha256="e" * 64,
        pillar_b_artifact_id="prior-b-only.json",
        pillar_b_artifact_sha256="f" * 64,
        pillar_b_discovered_at="2026-09-07T00:00:00Z",
        seen_urls=set(),
    ).candidates[0]
    carried_b = replace(
        carried_a,
        title="Prior B title",
        source_name="Research search",
        lane="research",
    )
    current_a = CandidateItem(
        title="Current A title",
        url=url,
        summary="Current A summary.",
        source_name="Current A Org",
        lane="document",
    )

    symmetric = combine_runtime_items(
        [current_a],
        [],
        report_date="2026-09-07",
        pillar_a_artifact_id="current-a-only.json",
        pillar_a_artifact_sha256="1" * 64,
        pillar_b_artifact_id="current-b-empty.json",
        pillar_b_artifact_sha256="2" * 64,
        discovered_at="2026-09-07T01:00:00Z",
        seen_urls=set(),
        carry_forward_candidates=(prior_b,),
        carry_forward_items=(carried_b,),
    )

    assert symmetric.combined.candidates[0].display_pillar == "A"
    assert symmetric.items[0].source_name == "Current A Org"
    assert symmetric.items[0].lane == "document"
    assert symmetric.items[0].semantics is None


@pytest.mark.parametrize("reverse", [False, True])
def test_runtime_item_matches_exact_deterministic_display_origin(reverse):
    url = "https://example.org/shared"
    alpha_semantics = {
        "summary": "Alpha authored summary.",
        "categories": ["Supervision & Disclosure"],
        "keywords": ["alpha climate", "alpha insurance", "alpha evidence"],
    }
    alpha = CandidateItem(
        title="Alpha title",
        url=f"{url}?utm_source=alpha#details",
        summary="Alpha source summary.",
        source_name="Alpha",
        lane="website",
        content_hash="alpha-content",
        semantics=alpha_semantics,
    )
    zeta_semantics = {
        "summary": "Zeta authored summary.",
        "categories": ["Financial Risk & Investment"],
        "keywords": ["zeta climate", "zeta insurance", "zeta evidence"],
    }
    zeta = CandidateItem(
        title="Zeta title",
        url=url,
        summary="Zeta source summary.",
        source_name="Zeta",
        lane="document",
        content_hash="zeta-content",
        asset_id="zeta-asset",
        asset_filename="zeta.pdf",
        semantics=zeta_semantics,
    )
    raw = [alpha, zeta] if reverse else [zeta, alpha]

    result = combine_runtime_items(
        raw,
        [],
        report_date="2026-09-07",
        pillar_a_artifact_id="current-a.json",
        pillar_a_artifact_sha256=("a" if reverse else "b") * 64,
        pillar_b_artifact_id="current-b.json",
        pillar_b_artifact_sha256="c" * 64,
        discovered_at="2026-09-07T01:00:00Z",
        seen_urls=set(),
    )

    assert len(result.combined.candidates) == 1
    candidate = result.combined.candidates[0]
    assert candidate.display_pillar == "A"
    assert [origin.source for origin in candidate.origins] == ["Alpha", "Zeta"]
    assert len(result.items) == 1
    item = result.items[0]
    assert item.source_name == "Alpha"
    assert item.lane == "website"
    assert item.summary == "Alpha source summary."
    assert item.content_hash == "alpha-content"
    assert item.semantics == alpha_semantics
    assert item.asset_id == ""
    assert item.asset_filename == ""


def test_seen_url_delta_is_two_phase_atomic_and_idempotent(tmp_path, monkeypatch):
    state = tmp_path / "seen_urls.json"
    state.write_bytes(b'["https://example.org/existing"]\n')
    before = state.read_bytes()
    pending = prepare_seen_url_delta(
        state,
        [
            "https://example.org/new?utm_source=mail#fragment",
            "https://example.org/new",
        ],
        report_date="2026-09-07",
        combined_sha256="a" * 64,
        report_sha256="b" * 64,
    )

    assert pending == pending_seen_url_delta_path(state)
    assert state.read_bytes() == before

    real_replace = __import__("os").replace

    def interrupted_replace(source, target):
        if Path(target) == state:
            raise KeyboardInterrupt("simulated interruption")
        return real_replace(source, target)

    monkeypatch.setattr("climate_monitor.seen_state.os.replace", interrupted_replace)
    with pytest.raises(KeyboardInterrupt):
        commit_seen_url_delta(state)
    assert state.read_bytes() == before

    monkeypatch.setattr("climate_monitor.seen_state.os.replace", real_replace)
    assert commit_seen_url_delta(state) is True
    committed = state.read_bytes()
    assert json.loads(committed) == [
        "https://example.org/existing",
        "https://example.org/new",
    ]
    assert commit_seen_url_delta(state) is False
    assert state.read_bytes() == committed


def _write_modern_config(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    sources = root / "sources.yaml"
    config = root / "run.yaml"
    source_dir = root / "reports"
    wiki_dir = root / "wiki"
    state_dir = root / "state"
    sources.write_text("sources: []\n", encoding="utf-8")
    config.write_text(
        f"""
report_title: Weekly Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate]
actuarial_keywords: [insurance]
research_lane:
  lookback_days: 30
  queries: []
output:
  source_dir: {source_dir.as_posix()}
  wiki_dir: {wiki_dir.as_posix()}
  write_empty_report: false
dedupe:
  url_tracking_path: {(state_dir / 'seen_urls.json').as_posix()}
  title_tracking_path: {(state_dir / 'seen_titles.json').as_posix()}
""".strip(),
        encoding="utf-8",
    )
    return sources, config, source_dir, wiki_dir, state_dir


def test_orchestrator_merges_before_classification_and_never_touches_seen_titles(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    seen_urls = state_dir / "seen_urls.json"
    seen_titles = state_dir / "seen_titles.json"
    seen_urls.write_text("[]\n", encoding="utf-8")
    seen_titles.write_bytes(b'["same title"]\r\n')
    title_before = seen_titles.read_bytes()
    calls: list[str] = []
    a_item = CandidateItem(
        title="Same title",
        url="https://example.org/shared?utm_source=site#part",
        summary="Climate insurance capital evidence from the website.",
        source_name="Example Org",
        lane="website",
        detected_at="2026-09-07T08:10:00Z",
    )
    b_item = CandidateItem(
        title="Search title",
        url="https://example.org/shared?fbclid=abc",
        summary="Climate insurance capital evidence from research search.",
        source_name="Research search",
        lane="research",
        published="2026-09-01",
    )

    monkeypatch.setattr(
        "climate_monitor.orchestrator.collect_website_items",
        lambda *args, **kwargs: ([a_item], []),
    )
    monkeypatch.setattr(
        "climate_monitor.orchestrator.search_recent_research",
        lambda *args, **kwargs: [b_item],
    )

    def fake_classify(item, run_config):
        calls.append(item.url)
        return replace(
            item,
            climate_related=True,
            actuarial_related=True,
            categories=("Supervision & Disclosure",),
            keywords=("climate risk", "insurance capital", "supervisory review"),
        )

    monkeypatch.setattr("climate_monitor.orchestrator.classify_candidate", fake_classify)

    result = run_monitor(
        source_config_path=sources,
        run_config_path=config,
        report_date=date(2026, 9, 7),
        state_dir=state_dir,
        sync=False,
    )

    assert len(calls) == 1
    assert len(result.items) == 1
    assert seen_titles.read_bytes() == title_before
    assert json.loads(seen_urls.read_text(encoding="utf-8")) == ["https://example.org/shared"]
    combined = json.loads(
        (source_dir / "combined-candidates_2026-09-07.json").read_text(encoding="utf-8")
    )
    assert combined["counts"]["cross_pillar_merges"] == 1
    assert [origin["pillar"] for origin in combined["items"][0]["origins"]] == ["A", "B"]


def test_orchestrator_dry_run_leaves_seen_state_and_pending_delta_unchanged(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    before = state.read_bytes()
    item = CandidateItem(
        title="Climate insurance report",
        url="https://example.org/new",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    monkeypatch.setattr(
        "climate_monitor.orchestrator.collect_website_items",
        lambda *args, **kwargs: ([item], []),
    )
    monkeypatch.setattr(
        "climate_monitor.orchestrator.search_recent_research",
        lambda *args, **kwargs: [],
    )

    run_monitor(
        source_config_path=sources,
        run_config_path=config,
        report_date=date(2026, 9, 7),
        state_dir=state_dir,
        sync=False,
        update_seen_state=False,
    )

    assert state.read_bytes() == before
    assert not pending_seen_url_delta_path(state).exists()


def test_orchestrator_report_commit_failure_leaves_canonical_state_unchanged(
    tmp_path, monkeypatch
):
    sources, config, _, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b'["https://example.org/existing"]\r\n')
    before = state.read_bytes()
    item = CandidateItem(
        title="Climate insurance report",
        url="https://example.org/new",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    monkeypatch.setattr(
        "climate_monitor.orchestrator.collect_website_items",
        lambda *args, **kwargs: ([item], []),
    )
    monkeypatch.setattr(
        "climate_monitor.orchestrator.search_recent_research",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "climate_monitor.orchestrator.commit_report_with_semantics",
        lambda **kwargs: (_ for _ in ()).throw(OSError("simulated report commit failure")),
    )

    with pytest.raises(OSError, match="simulated report commit failure"):
        run_monitor(
            source_config_path=sources,
            run_config_path=config,
            report_date=date(2026, 9, 7),
            state_dir=state_dir,
            sync=False,
        )

    assert state.read_bytes() == before


def test_live_checkpoint_reemits_candidate_after_report_commit_interruption(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    source = MonitorSource(
        key="example",
        abbreviation="EXAMPLE",
        full_name="Example",
        url="https://example.org/",
    )
    sources.write_text(
        "sources:\n"
        "  - key: example\n"
        "    abbreviation: EXAMPLE\n"
        "    full_name: Example\n"
        "    url: https://example.org/\n",
        encoding="utf-8",
    )
    state_dir.mkdir()
    (state_dir / "seen_urls.json").write_bytes(b"[]\n")
    current_links: list[str] = []

    class Page:
        final_url = "https://example.org/"
        fit_markdown = "Climate insurance page"
        markdown = ""
        content_text = ""
        raw_html = ""
        status_code = 200

        @property
        def metadata_json(self):
            return {"links": list(current_links)}

    class FakeCrawler:
        def __init__(self, *, fetch_mode):
            self.fetch_mode = fetch_mode

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetch_page(self, url, *, fetch_mode, fetch_config_json=None):
            return Page()

    diff = {
        "compute_hash": lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "extract_links": lambda html, base_url: [],
        "find_document_links": lambda links: [],
        "find_new_links": lambda previous, current: [
            link for link in current if link not in previous
        ],
        "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
    }
    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(web_listening_adapter, "_load_web_listening", lambda: (FakeCrawler, diff))
    web_state_dir = state_dir / "websites"
    baseline_items, baseline_warnings = web_listening_adapter.collect_source_items(
        source=source,
        state_dir=web_state_dir,
    )
    assert baseline_items == []
    assert baseline_warnings == []
    checkpoint = next(web_state_dir.glob("*.json"))
    baseline_checkpoint = checkpoint.read_bytes()
    candidate_url = "https://example.org/climate-insurance-update"
    current_links.append(candidate_url)
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }
    with monkeypatch.context() as crash:
        crash.setattr(
            orchestrator,
            "commit_report_with_semantics",
            lambda **kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt("simulated live report interruption")
            ),
        )
        with pytest.raises(KeyboardInterrupt, match="live report"):
            run_monitor(**arguments)

    assert checkpoint.read_bytes() == baseline_checkpoint

    recovered = run_monitor(**arguments)

    assert [item.url for item in recovered.items] == [candidate_url]
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["links"] == [
        candidate_url
    ]
    assert json.loads((state_dir / "seen_urls.json").read_text(encoding="utf-8")) == [
        candidate_url
    ]
    assert Path(recovered.report_path).parent == source_dir

    report_path = Path(recovered.report_path)
    sidecar_path = semantic_sidecar_path(report_path)
    combined_path = combined_candidates_path(source_dir, "2026-09-07")
    first_bundle = {
        "report": report_path.read_bytes(),
        "sidecar": sidecar_path.read_bytes(),
        "combined": combined_path.read_bytes(),
        "state": (state_dir / "seen_urls.json").read_bytes(),
    }
    first_combined = json.loads(first_bundle["combined"])

    unchanged = run_monitor(**arguments)

    assert [item.url for item in unchanged.items] == [candidate_url]
    assert {
        "report": report_path.read_bytes(),
        "sidecar": sidecar_path.read_bytes(),
        "combined": combined_path.read_bytes(),
        "state": (state_dir / "seen_urls.json").read_bytes(),
    } == first_bundle

    incremental_url = "https://example.org/climate-insurance-incremental"
    current_links.append(incremental_url)
    incremental = run_monitor(**arguments)

    assert [item.url for item in incremental.items] == [candidate_url, incremental_url]
    combined = json.loads(combined_path.read_text(encoding="utf-8"))
    assert combined["counts"] == {
        "pillar_a_rows": 2,
        "pillar_b_rows": 0,
        "unique_urls": 2,
        "cross_pillar_merges": 0,
        "history_skips": 0,
        "invalid_rows": 0,
    }
    assert [item["canonical_url"] for item in combined["items"]] == [
        incremental_url,
        candidate_url,
    ]
    assert all(len(item["origins"]) == 1 for item in combined["items"])
    carried = next(
        item for item in combined["items"] if item["canonical_url"] == candidate_url
    )
    assert carried["origins"] == first_combined["items"][0]["origins"]
    report_text = report_path.read_text(encoding="utf-8")
    assert candidate_url in report_text
    assert incremental_url in report_text
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert [article["canonical_url"] for article in sidecar["articles"]] == [
        candidate_url,
        incremental_url,
    ]
    assert json.loads((state_dir / "seen_urls.json").read_text(encoding="utf-8")) == [
        candidate_url,
        incremental_url,
    ]
    incremental_bundle = {
        "report": report_path.read_bytes(),
        "sidecar": sidecar_path.read_bytes(),
        "combined": combined_path.read_bytes(),
        "state": (state_dir / "seen_urls.json").read_bytes(),
    }

    incremental_replay = run_monitor(**arguments)

    assert [item.url for item in incremental_replay.items] == [
        candidate_url,
        incremental_url,
    ]
    assert {
        "report": report_path.read_bytes(),
        "sidecar": sidecar_path.read_bytes(),
        "combined": combined_path.read_bytes(),
        "state": (state_dir / "seen_urls.json").read_bytes(),
    } == incremental_bundle

    rejected_url = "https://example.org/climate-insurance-rejected"
    current_links.append(rejected_url)
    monkeypatch.setattr(orchestrator, "classify_candidate", lambda item, config: item)
    no_report = run_monitor(**{**arguments, "report_date": date(2026, 9, 14)})

    assert no_report.report_path is None
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["links"] == [
        candidate_url,
        incremental_url,
    ]
    assert json.loads((state_dir / "seen_urls.json").read_text(encoding="utf-8")) == [
        candidate_url,
        incremental_url,
    ]

    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)
    retry = run_monitor(**{**arguments, "report_date": date(2026, 9, 14)})

    assert [item.url for item in retry.items] == [rejected_url]
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["links"] == [
        candidate_url,
        incremental_url,
        rejected_url,
    ]

    read_only_url = "https://example.org/climate-insurance-read-only"
    current_links.append(read_only_url)
    checkpoint_before_read_only = checkpoint.read_bytes()
    seen_before_read_only = (state_dir / "seen_urls.json").read_bytes()
    read_only = run_monitor(
        **{
            **arguments,
            "report_date": date(2026, 9, 21),
            "update_seen_state": False,
        }
    )

    assert [item.url for item in read_only.items] == [read_only_url]
    assert checkpoint.read_bytes() == checkpoint_before_read_only
    assert (state_dir / "seen_urls.json").read_bytes() == seen_before_read_only
    assert not list(web_state_dir.glob("*.pending-run.json"))


def test_orchestrator_invalidates_stale_combined_evidence_before_current_artifact_failure(
    tmp_path,
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    (state_dir / "seen_urls.json").write_text("[]\n", encoding="utf-8")
    source_dir.mkdir()
    stale = combined_candidates_path(source_dir, "2026-09-07")
    stale.write_bytes(b"stale combined evidence")
    pillar_a = _pillar_a(_a_item("Invalid", ""))
    pillar_b: list[dict[str, object]] = []
    pillar_a_path = tmp_path / "article_changes.json"
    pillar_b_path = tmp_path / "pillar_b.json"
    pillar_a_path.write_text(json.dumps(pillar_a), encoding="utf-8")
    pillar_b_path.write_text(json.dumps(pillar_b), encoding="utf-8")

    with pytest.raises(ValueError):
        run_monitor(
            source_config_path=sources,
            run_config_path=config,
            report_date=date(2026, 9, 7),
            article_changes_artifact_path=pillar_a_path,
            pillar_b_artifact_path=pillar_b_path,
            state_dir=state_dir,
            sync=False,
        )

    assert not stale.exists()


@pytest.mark.parametrize("crash_timing", ["manifest_applied", "bundle_clean"])
def test_orchestrator_recovers_committed_bundle_state_before_reacquisition(
    tmp_path, monkeypatch, crash_timing
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    before = state.read_bytes()
    item = CandidateItem(
        title="Climate insurance recovery report",
        url="https://example.org/recovery?utm_source=mail#section",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: ([item], []),
    )
    monkeypatch.setattr(
        orchestrator,
        "search_recent_research",
        lambda *args, **kwargs: [],
    )
    candidate_path = combined_candidates_path(source_dir, "2026-09-07")

    if crash_timing == "bundle_clean":
        with monkeypatch.context() as crash:
            crash.setattr(
                orchestrator,
                "commit_seen_url_delta",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    KeyboardInterrupt("simulated pre-state interruption")
                ),
            )
            with pytest.raises(KeyboardInterrupt, match="pre-state"):
                run_monitor(
                    source_config_path=sources,
                    run_config_path=config,
                    report_date=date(2026, 9, 7),
                    state_dir=state_dir,
                    sync=False,
                )
    else:
        real_replace = semantic_bundle.os.replace
        with monkeypatch.context() as crash:
            def interrupted_replace(source, target):
                if Path(target) == candidate_path:
                    raise KeyboardInterrupt("simulated bundle interruption")
                return real_replace(source, target)

            crash.setattr(semantic_bundle.os, "replace", interrupted_replace)
            with pytest.raises(KeyboardInterrupt, match="bundle interruption"):
                run_monitor(
                    source_config_path=sources,
                    run_config_path=config,
                    report_date=date(2026, 9, 7),
                    state_dir=state_dir,
                    sync=False,
                )

    assert state.read_bytes() == before
    pending_combined = candidate_path.with_name(candidate_path.name + ".pending")
    expected_combined = (
        pending_combined.read_bytes() if pending_combined.exists() else candidate_path.read_bytes()
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must not reacquire candidates")
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "search_recent_research",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must not repeat research")
        ),
    )

    recovered = run_monitor(
        source_config_path=sources,
        run_config_path=config,
        report_date=date(2026, 9, 7),
        state_dir=state_dir,
        sync=False,
    )

    assert json.loads(state.read_text(encoding="utf-8")) == ["https://example.org/recovery"]
    assert not pending_seen_url_delta_path(state).exists()
    assert recovered.report_path is not None
    assert len(recovered.items) == 1
    assert candidate_path.read_bytes() == expected_combined
    validate_combined_candidates(json.loads(expected_combined.decode("utf-8")))
    verify_semantic_sidecar(recovered.report_path)
    assert not list(source_dir.glob("*pending*"))


def test_orchestrator_discards_unmatched_pending_with_unchanged_base_and_reacquires(
    tmp_path, monkeypatch
):
    sources, config, _, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b'[]\n')
    before = state.read_bytes()
    pending = prepare_seen_url_delta(
        state,
        ["https://example.org/orphaned"],
        report_date="2026-09-07",
        combined_sha256="a" * 64,
        report_sha256="b" * 64,
    )
    item = CandidateItem(
        title="Climate insurance replacement report",
        url="https://example.org/replacement",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: ([item], []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)

    result = run_monitor(
        source_config_path=sources,
        run_config_path=config,
        report_date=date(2026, 9, 7),
        state_dir=state_dir,
        sync=False,
    )

    assert [candidate.url for candidate in result.items] == [item.url]
    assert json.loads(state.read_text(encoding="utf-8")) == [
        "https://example.org/replacement"
    ]
    assert state.read_bytes() != before
    assert not pending.exists()


def test_orchestrator_unmatched_pending_base_conflict_fails_closed(
    tmp_path, monkeypatch
):
    sources, config, _, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    pending = prepare_seen_url_delta(
        state,
        ["https://example.org/orphaned"],
        report_date="2026-09-07",
        combined_sha256="a" * 64,
        report_sha256="b" * 64,
    )
    pending_before = pending.read_bytes()
    state.write_bytes(b'["https://example.org/concurrent"]\n')
    state_before = state.read_bytes()
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("base conflict must fail before acquisition")
        ),
    )

    with pytest.raises(ValueError, match="state changed after the pending delta"):
        run_monitor(
            source_config_path=sources,
            run_config_path=config,
            report_date=date(2026, 9, 7),
            state_dir=state_dir,
            sync=False,
        )

    assert state.read_bytes() == state_before
    assert pending.read_bytes() == pending_before


def test_orchestrator_damaged_pending_delta_fails_closed_before_acquisition(
    tmp_path, monkeypatch
):
    sources, config, _, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    pending = pending_seen_url_delta_path(state)
    pending.write_bytes(b"{not-json\n")
    state_before = state.read_bytes()
    pending_before = pending.read_bytes()
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("damaged pending delta must fail before acquisition")
        ),
    )

    with pytest.raises(ValueError, match="not valid UTF-8 JSON"):
        run_monitor(
            source_config_path=sources,
            run_config_path=config,
            report_date=date(2026, 9, 7),
            state_dir=state_dir,
            sync=False,
        )

    assert state.read_bytes() == state_before
    assert pending.read_bytes() == pending_before


def test_orchestrator_no_update_seen_state_preserves_existing_cross_date_pending(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    pending = prepare_seen_url_delta(
        state,
        ["https://example.org/prior"],
        report_date="2026-08-31",
        combined_sha256="a" * 64,
        report_sha256="b" * 64,
    )
    state_before = state.read_bytes()
    pending_before = pending.read_bytes()
    monkeypatch.setattr(orchestrator, "collect_website_items", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])

    result = run_monitor(
        source_config_path=sources,
        run_config_path=config,
        report_date=date(2026, 9, 7),
        state_dir=state_dir,
        sync=False,
        update_seen_state=False,
    )

    assert result.report_path is None
    assert state.read_bytes() == state_before
    assert pending.read_bytes() == pending_before
    assert combined_candidates_path(source_dir, "2026-09-07").exists()


def test_orchestrator_no_update_same_date_pending_returns_bound_bundle_read_only(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    old = CandidateItem(
        title="Climate insurance protected report",
        url="https://example.org/protected",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    new = replace(
        old,
        title="Climate insurance replacement report",
        url="https://example.org/replacement",
    )
    current = [old]
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: (list(current), []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }
    with monkeypatch.context() as crash:
        crash.setattr(
            orchestrator,
            "commit_seen_url_delta",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt("simulated protected pre-state interruption")
            ),
        )
        with pytest.raises(KeyboardInterrupt, match="protected"):
            run_monitor(**arguments)

    report = source_dir / "climate-monitor-2026-09-07.md"
    sidecar = semantic_sidecar_path(report)
    combined = combined_candidates_path(source_dir, "2026-09-07")
    pending = pending_seen_url_delta_path(state)
    protected = {
        "state": state.read_bytes(),
        "pending": pending.read_bytes(),
        "report": report.read_bytes(),
        "sidecar": sidecar.read_bytes(),
        "combined": combined.read_bytes(),
    }
    current.append(new)

    read_only = run_monitor(**arguments, update_seen_state=False)

    assert [item.url for item in read_only.items] == ["https://example.org/protected"]
    assert {
        "state": state.read_bytes(),
        "pending": pending.read_bytes(),
        "report": report.read_bytes(),
        "sidecar": sidecar.read_bytes(),
        "combined": combined.read_bytes(),
    } == protected

    recovered = run_monitor(**arguments)

    assert [item.url for item in recovered.items] == ["https://example.org/protected"]
    assert json.loads(state.read_text(encoding="utf-8")) == [
        "https://example.org/protected"
    ]
    assert not pending.exists()
    assert (
        report.read_bytes(),
        sidecar.read_bytes(),
        combined.read_bytes(),
    ) == (
        protected["report"],
        protected["sidecar"],
        protected["combined"],
    )


def _classify_as_relevant(item, run_config):
    return replace(
        item,
        climate_related=True,
        actuarial_related=True,
        categories=("Supervision & Disclosure",),
        keywords=("climate risk", "insurance capital", "supervisory review"),
    )


def test_orchestrator_same_day_replay_keeps_report_bundle_and_state_stable(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    item = CandidateItem(
        title="Climate insurance replay report",
        url="https://example.org/replay?utm_source=mail#findings",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    calls: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: ([item], []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])

    def classify(candidate, run_config):
        calls.append(candidate.url)
        return _classify_as_relevant(candidate, run_config)

    monkeypatch.setattr(orchestrator, "classify_candidate", classify)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }

    first = run_monitor(**arguments)
    report_path = Path(first.report_path)
    sidecar_path = semantic_sidecar_path(report_path)
    combined_path = combined_candidates_path(source_dir, "2026-09-07")
    first_bytes = {
        "report": report_path.read_bytes(),
        "sidecar": sidecar_path.read_bytes(),
        "combined": combined_path.read_bytes(),
        "state": state.read_bytes(),
    }

    second = run_monitor(**arguments)

    assert second.items == first.items
    assert report_path.read_bytes() == first_bytes["report"]
    assert sidecar_path.read_bytes() == first_bytes["sidecar"]
    assert combined_path.read_bytes() == first_bytes["combined"]
    assert state.read_bytes() == first_bytes["state"]
    assert json.loads(state.read_text(encoding="utf-8")) == ["https://example.org/replay"]
    combined = json.loads(combined_path.read_text(encoding="utf-8"))
    assert combined["counts"]["history_skips"] == 0
    assert [row["canonical_url"] for row in combined["items"]] == [
        "https://example.org/replay"
    ]
    assert calls == [item.url, item.url]


def test_orchestrator_same_day_replay_keeps_old_and_adds_only_new_url_state(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    old = CandidateItem(
        title="Climate insurance old report",
        url="https://example.org/old",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    new = replace(old, title="Climate insurance new report", url="https://example.org/new")
    current = [old]
    calls: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: (list(current), []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])

    def classify(candidate, run_config):
        calls.append(candidate.url)
        return _classify_as_relevant(candidate, run_config)

    monkeypatch.setattr(orchestrator, "classify_candidate", classify)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }

    run_monitor(**arguments)
    state_after_first = state.read_bytes()
    current.append(new)
    second = run_monitor(**arguments)

    assert json.loads(state_after_first) == ["https://example.org/old"]
    assert json.loads(state.read_text(encoding="utf-8")) == [
        "https://example.org/old",
        "https://example.org/new",
    ]
    assert {item.url for item in second.items} == {
        "https://example.org/old",
        "https://example.org/new",
    }
    combined = json.loads(
        combined_candidates_path(source_dir, "2026-09-07").read_text(encoding="utf-8")
    )
    assert combined["counts"]["unique_urls"] == 2
    assert combined["counts"]["history_skips"] == 0
    assert {row["canonical_url"] for row in combined["items"]} == {
        "https://example.org/old",
        "https://example.org/new",
    }
    assert calls == [
        "https://example.org/old",
        "https://example.org/old",
        "https://example.org/new",
    ]


def test_orchestrator_snapshot_preserves_full_document_item_on_incremental_carry(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    semantics = {
        "summary": "Climate insurance document evidence for supervisors.",
        "categories": ["Supervision & Disclosure"],
        "keywords": ["climate risk", "insurance capital", "supervisory review"],
    }
    old = CandidateItem(
        title="Climate insurance document report",
        url="https://example.org/document-old?utm_source=monitor#download",
        summary=semantics["summary"],
        source_name="Example Documents",
        lane="document",
        published="2026-09-01",
        detected_at="2026-09-07T01:02:03Z",
        content_hash="old-content-hash",
        evidence_text="Complete extracted climate insurance evidence.",
        climate_related=True,
        actuarial_related=True,
        relevance_reason="Direct climate and solvency evidence.",
        climate_signal="climate",
        actuarial_signal="insurance",
        confidence=0.97,
        evidence_snippet="Climate risk affects insurance capital.",
        source_item_id="document-row-1",
        asset_id="asset-1",
        asset_local_path="downloads/document-old.pdf",
        asset_canonical_blob_path="blobs/sha256/old.pdf",
        asset_tracked_path="reports/assets/document-old.pdf",
        asset_filename="document-old.pdf",
        asset_media_type="application/pdf",
        asset_bytes=4321,
        asset_checksum_algorithm="sha256",
        asset_checksum_value="d" * 64,
        asset_metadata={"page_count": 17, "producer": "Example"},
        topics=("climate risk", "insurance capital"),
        categories=("Supervision & Disclosure",),
        keywords=("climate risk", "insurance capital", "supervisory review"),
        semantics=semantics,
    )
    new = CandidateItem(
        title="Climate insurance incremental update",
        url="https://example.org/new",
        summary="New climate insurance evidence for supervisors.",
        source_name="Example",
        lane="website",
        climate_related=True,
        actuarial_related=True,
        relevance_reason="New climate and solvency evidence.",
        climate_signal="climate",
        actuarial_signal="insurance",
        confidence=0.91,
        evidence_snippet="New climate insurance evidence.",
        categories=("Supervision & Disclosure",),
        keywords=("climate risk", "insurance capital", "supervisory review"),
        semantics={
            **semantics,
            "summary": "New climate insurance evidence for supervisors.",
        },
    )
    current = [old]
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: (list(current), []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", lambda item, config: item)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }

    first = run_monitor(**arguments)
    current[:] = [new]
    second = run_monitor(**arguments)

    carried = next(
        item
        for item in second.items
        if canonical_url(item.url) == canonical_url(old.url)
    )
    assert carried == old
    report_path = Path(second.report_path)
    report_text = report_path.read_text(encoding="utf-8")
    for expected in (
        "**Published:** 2026-09-01 <br>",
        "**Confidence:** 0.97 <br>",
        "**Evidence:** Climate risk affects insurance capital. <br>",
        "**Document file:** document-old.pdf <br>",
        "**Source item ID:** document-row-1 <br>",
        f"**Checksum:** sha256: {'d' * 64} <br>",
    ):
        assert expected in report_text
    combined_path = combined_candidates_path(source_dir, "2026-09-07")
    snapshot_path = candidate_item_snapshot_path(source_dir, "2026-09-07")
    snapshot_items = verify_candidate_item_snapshot(
        snapshot_path,
        combined_path=combined_path,
        report_path=report_path,
        report_date="2026-09-07",
    )
    snapshotted_old = next(
        item for item in snapshot_items if canonical_url(item.url) == canonical_url(old.url)
    )
    assert snapshotted_old == carried
    assert json.loads(state.read_text(encoding="utf-8")) == [
        "https://example.org/document-old",
        "https://example.org/new",
    ]
    stable = {
        path: path.read_bytes()
        for path in (
            report_path,
            semantic_sidecar_path(report_path),
            combined_path,
            snapshot_path,
            state,
        )
    }

    replay = run_monitor(**arguments)

    assert replay.items == second.items
    assert {path: path.read_bytes() for path in stable} == stable


def test_current_artifact_replay_preserves_snapshot_item_with_shared_b_overlay(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    (state_dir / "seen_urls.json").write_bytes(b"[]\n")
    old_semantics = {
        "summary": "Authored climate insurance document evidence.",
        "categories": ["Supervision & Disclosure"],
        "keywords": ["climate risk", "insurance capital", "supervisory review"],
    }
    old = CandidateItem(
        title="A-origin document title",
        url="https://example.org/shared-document?utm_source=monitor#download",
        summary="A-origin document summary.",
        source_name="Example Documents",
        lane="document",
        published="2026-09-01",
        detected_at="2026-09-07T01:02:03Z",
        content_hash="shared-document-content",
        evidence_text="Full retained document evidence.",
        climate_related=True,
        actuarial_related=True,
        relevance_reason="Direct climate insurance evidence.",
        climate_signal="climate",
        actuarial_signal="insurance",
        confidence=0.96,
        evidence_snippet="Climate risk affects insurance capital.",
        source_item_id="document-row-1",
        asset_id="asset-1",
        asset_local_path="downloads/shared.pdf",
        asset_canonical_blob_path="blobs/sha256/shared.pdf",
        asset_tracked_path="reports/assets/shared.pdf",
        asset_filename="shared.pdf",
        asset_media_type="application/pdf",
        asset_bytes=4321,
        asset_checksum_algorithm="sha256",
        asset_checksum_value="e" * 64,
        asset_metadata={"page_count": 17},
        topics=("climate risk", "insurance capital"),
        categories=("Supervision & Disclosure",),
        keywords=("climate risk", "insurance capital", "supervisory review"),
        semantics=old_semantics,
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: ([old], []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])

    def classify(item, config):
        return replace(
            item,
            climate_related=True,
            actuarial_related=True,
            categories=item.categories or ("Supervision & Disclosure",),
            keywords=item.keywords
            or ("climate risk", "insurance capital", "supervisory review"),
        )

    monkeypatch.setattr(orchestrator, "classify_candidate", classify)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }
    run_monitor(**arguments)
    article_changes_path = tmp_path / "article_changes.json"
    pillar_b_path = tmp_path / "pillar_b.json"
    article_changes_path.write_text(json.dumps(_pillar_a()), encoding="utf-8")
    pillar_b_path.write_text(
        json.dumps(
            [
                _b_item(
                    "B search-result title",
                    "https://example.org/shared-document?fbclid=search#abstract",
                )
            ]
        ),
        encoding="utf-8",
    )

    replay = run_monitor(
        **arguments,
        article_changes_artifact_path=article_changes_path,
        pillar_b_artifact_path=pillar_b_path,
    )

    assert len(replay.items) == 1
    item = replay.items[0]
    assert item.title == "B search-result title"
    assert item.summary == "Search-result evidence for climate insurance risk."
    assert item.source_name == "Example Documents"
    assert item.lane == "document"
    assert item.published == old.published
    assert item.detected_at == old.detected_at
    assert item.content_hash == old.content_hash
    assert item.evidence_text == old.evidence_text
    assert item.evidence_snippet == old.evidence_snippet
    assert item.confidence == old.confidence
    assert item.source_item_id == old.source_item_id
    assert item.asset_metadata == old.asset_metadata
    assert item.asset_checksum_value == old.asset_checksum_value
    assert item.semantics == old_semantics
    report = Path(replay.report_path)
    report_text = report.read_text(encoding="utf-8")
    assert "**Published:** 2026-09-01 <br>" in report_text
    assert "**Document file:** shared.pdf <br>" in report_text
    assert "**Evidence:** Climate risk affects insurance capital. <br>" in report_text
    snapshot_items = verify_candidate_item_snapshot(
        candidate_item_snapshot_path(source_dir, "2026-09-07"),
        combined_path=combined_candidates_path(source_dir, "2026-09-07"),
        report_path=report,
        report_date="2026-09-07",
    )
    assert snapshot_items == (item,)
    assert json.loads((state_dir / "seen_urls.json").read_text(encoding="utf-8")) == [
        "https://example.org/shared-document"
    ]


def test_orchestrator_snapshot_covers_non_rendered_combined_items(tmp_path, monkeypatch):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "max_items_per_report: 12",
            "max_items_per_report: 1",
        ),
        encoding="utf-8",
    )
    first = CandidateItem(
        title="Climate insurance first report",
        url="https://example.org/first",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    second = replace(first, title="Climate insurance second report", url="https://example.org/second")
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: ([first, second], []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)

    result = run_monitor(
        source_config_path=sources,
        run_config_path=config,
        report_date=date(2026, 9, 7),
        state_dir=state_dir,
        sync=False,
    )

    assert len(result.items) == 1
    combined = combined_candidates_path(source_dir, "2026-09-07")
    snapshot_items = verify_candidate_item_snapshot(
        candidate_item_snapshot_path(source_dir, "2026-09-07"),
        combined_path=combined,
        report_path=Path(result.report_path),
        report_date="2026-09-07",
    )
    assert {canonical_url(item.url) for item in snapshot_items} == {
        "https://example.org/first",
        "https://example.org/second",
    }


def test_orchestrator_missing_or_corrupt_snapshot_blocks_incremental_rewrite(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    (state_dir / "seen_urls.json").write_bytes(b"[]\n")
    old = CandidateItem(
        title="Climate insurance old report",
        url="https://example.org/old",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    new = replace(old, title="Climate insurance new report", url="https://example.org/new")
    current = [old]
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: (list(current), []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }
    first = run_monitor(**arguments)
    report = Path(first.report_path)
    combined = combined_candidates_path(source_dir, "2026-09-07")
    snapshot = candidate_item_snapshot_path(source_dir, "2026-09-07")
    protected = {
        path: path.read_bytes()
        for path in (
            report,
            semantic_sidecar_path(report),
            combined,
            state_dir / "seen_urls.json",
        )
    }
    snapshot.unlink()
    current.clear()

    unchanged = run_monitor(**arguments)

    assert [canonical_url(item.url) for item in unchanged.items] == [
        "https://example.org/old"
    ]
    assert {path: path.read_bytes() for path in protected} == protected
    current[:] = [new]

    with pytest.raises(ValueError, match="candidate item snapshot"):
        run_monitor(**arguments)

    assert {path: path.read_bytes() for path in protected} == protected
    snapshot.write_bytes(b"{\"schema_version\":\"candidate-items-snapshot.v1\"}\n")
    corrupt = snapshot.read_bytes()

    with pytest.raises(ValueError, match="same-date report bundle is incomplete or inconsistent"):
        run_monitor(**arguments)

    assert snapshot.read_bytes() == corrupt
    assert {path: path.read_bytes() for path in protected} == protected


def test_orchestrator_same_day_replay_fails_closed_on_inconsistent_bundle(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    item = CandidateItem(
        title="Climate insurance report",
        url="https://example.org/report",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    monkeypatch.setattr(orchestrator, "collect_website_items", lambda *args, **kwargs: ([item], []))
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }
    first = run_monitor(**arguments)
    report_path = Path(first.report_path)
    sidecar_path = semantic_sidecar_path(report_path)
    combined_path = combined_candidates_path(source_dir, "2026-09-07")
    state_before = state.read_bytes()
    report_before = report_path.read_bytes()
    sidecar_before = sidecar_path.read_bytes()
    combined_path.write_bytes(b"corrupt combined evidence\n")
    corrupt_before = combined_path.read_bytes()
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("inconsistent same-day bundle must fail before acquisition")
        ),
    )

    with pytest.raises(ValueError, match="same-date report bundle is incomplete or inconsistent"):
        run_monitor(**arguments)

    assert state.read_bytes() == state_before
    assert report_path.read_bytes() == report_before
    assert sidecar_path.read_bytes() == sidecar_before
    assert combined_path.read_bytes() == corrupt_before


@pytest.mark.parametrize("interruption", ["manifest", "artifact"])
def test_orchestrator_retries_after_incomplete_bundle_staging_without_losing_candidate(
    tmp_path, monkeypatch, interruption
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    before = state.read_bytes()
    item = CandidateItem(
        title="Climate insurance interrupted report",
        url="https://example.org/interrupted",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    calls: list[str] = []
    monkeypatch.setattr(orchestrator, "collect_website_items", lambda *args, **kwargs: ([item], []))
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])

    def classify(candidate, run_config):
        calls.append(candidate.url)
        return _classify_as_relevant(candidate, run_config)

    monkeypatch.setattr(orchestrator, "classify_candidate", classify)
    real_write = semantic_bundle._write_pending
    writes = 0

    def interrupted_write(path, payload):
        nonlocal writes
        writes += 1
        failure_write = 1 if interruption == "manifest" else 2
        if writes == failure_write:
            raise KeyboardInterrupt("simulated artifact staging interruption")
        return real_write(path, payload)

    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }
    with monkeypatch.context() as crash:
        crash.setattr(semantic_bundle, "_write_pending", interrupted_write)
        with pytest.raises(KeyboardInterrupt, match="artifact staging"):
            run_monitor(**arguments)

    assert state.read_bytes() == before
    assert pending_seen_url_delta_path(state).exists()

    result = run_monitor(**arguments)

    assert [candidate.url for candidate in result.items] == [
        "https://example.org/interrupted"
    ]
    assert json.loads(state.read_text(encoding="utf-8")) == [
        "https://example.org/interrupted"
    ]
    assert not pending_seen_url_delta_path(state).exists()
    assert combined_candidates_path(source_dir, "2026-09-07").exists()
    assert calls == ["https://example.org/interrupted"] * 2


def test_orchestrator_recovers_snapshot_interruption_before_seen_state_commit(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    item = CandidateItem(
        title="Climate insurance interrupted document",
        url="https://example.org/interrupted-document",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="document",
        published="2026-09-01",
        detected_at="2026-09-07T01:00:00Z",
        content_hash="document-content",
        source_item_id="document-1",
        asset_id="asset-1",
        asset_filename="interrupted.pdf",
        asset_media_type="application/pdf",
        asset_bytes=123,
        asset_checksum_algorithm="sha256",
        asset_checksum_value="a" * 64,
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: ([item], []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }
    snapshot = candidate_item_snapshot_path(source_dir, "2026-09-07")
    real_replace = semantic_bundle.os.replace

    def interrupted_replace(source, target):
        if Path(target) == snapshot:
            raise KeyboardInterrupt("simulated snapshot promotion interruption")
        return real_replace(source, target)

    with monkeypatch.context() as crash:
        crash.setattr(semantic_bundle.os, "replace", interrupted_replace)
        with pytest.raises(KeyboardInterrupt, match="snapshot promotion"):
            run_monitor(**arguments)

    assert state.read_bytes() == b"[]\n"
    assert pending_seen_url_delta_path(state).exists()
    assert not snapshot.exists()

    recovered = run_monitor(**arguments)

    assert recovered.items[0].published == "2026-09-01"
    assert recovered.items[0].source_item_id == "document-1"
    assert recovered.items[0].asset_filename == "interrupted.pdf"
    assert not pending_seen_url_delta_path(state).exists()
    assert json.loads(state.read_text(encoding="utf-8")) == [
        "https://example.org/interrupted-document"
    ]
    verify_candidate_item_snapshot(
        snapshot,
        combined_path=combined_candidates_path(source_dir, "2026-09-07"),
        report_path=Path(recovered.report_path),
        report_date="2026-09-07",
    )


def test_orchestrator_pending_new_bundle_requires_its_snapshot_before_state_commit(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    item = CandidateItem(
        title="Climate insurance pending report",
        url="https://example.org/pending-snapshot",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: ([item], []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }
    with monkeypatch.context() as crash:
        crash.setattr(
            orchestrator,
            "commit_seen_url_delta",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt("simulated pre-state interruption")
            ),
        )
        with pytest.raises(KeyboardInterrupt, match="pre-state"):
            run_monitor(**arguments)

    pending = pending_seen_url_delta_path(state)
    snapshot = candidate_item_snapshot_path(source_dir, "2026-09-07")
    state_before = state.read_bytes()
    pending_before = pending.read_bytes()
    assert json.loads(pending_before)["snapshot_sha256"] == hashlib.sha256(
        snapshot.read_bytes()
    ).hexdigest()
    snapshot.unlink()
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid pending recovery must fail before acquisition")
        ),
    )

    with pytest.raises(ValueError, match="complete matching committed report bundle"):
        run_monitor(**arguments)

    assert state.read_bytes() == state_before
    assert pending.read_bytes() == pending_before


def test_orchestrator_recovers_prior_date_pending_bundle_before_current_run(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    first_item = CandidateItem(
        title="Climate insurance prior-week report",
        url="https://example.org/prior-week",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    current = [first_item]
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: (list(current), []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)

    with monkeypatch.context() as crash:
        crash.setattr(
            orchestrator,
            "commit_seen_url_delta",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt("simulated prior-week pre-state interruption")
            ),
        )
        with pytest.raises(KeyboardInterrupt, match="prior-week"):
            run_monitor(
                source_config_path=sources,
                run_config_path=config,
                report_date=date(2026, 8, 31),
                state_dir=state_dir,
                sync=False,
            )

    pending = pending_seen_url_delta_path(state)
    pending_payload = json.loads(pending.read_text(encoding="utf-8"))
    assert pending_payload["report_date"] == "2026-08-31"
    prior_combined = combined_candidates_path(source_dir, "2026-08-31")
    assert pending_payload["combined_sha256"] == hashlib.sha256(
        prior_combined.read_bytes()
    ).hexdigest()
    prior_bytes = prior_combined.read_bytes()
    current.clear()

    current_result = run_monitor(
        source_config_path=sources,
        run_config_path=config,
        report_date=date(2026, 9, 7),
        state_dir=state_dir,
        sync=False,
    )

    assert current_result.report_path is None
    assert json.loads(state.read_text(encoding="utf-8")) == [
        "https://example.org/prior-week"
    ]
    assert not pending.exists()
    assert prior_combined.read_bytes() == prior_bytes
    assert combined_candidates_path(source_dir, "2026-09-07").exists()


def test_orchestrator_retries_pre_manifest_failure_over_an_old_same_date_bundle(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    old = CandidateItem(
        title="Climate insurance old report",
        url="https://example.org/old",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    new = replace(old, title="Climate insurance new report", url="https://example.org/new")
    current = [old]
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: (list(current), []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }
    first = run_monitor(**arguments)
    report_path = Path(first.report_path)
    sidecar_path = semantic_sidecar_path(report_path)
    combined_path = combined_candidates_path(source_dir, "2026-09-07")
    old_bundle = (
        report_path.read_bytes(),
        sidecar_path.read_bytes(),
        combined_path.read_bytes(),
    )
    state_before = state.read_bytes()
    current.append(new)

    with monkeypatch.context() as crash:
        crash.setattr(
            semantic_bundle,
            "_write_pending",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt("simulated pre-manifest interruption over old bundle")
            ),
        )
        with pytest.raises(KeyboardInterrupt, match="pre-manifest"):
            run_monitor(**arguments)

    assert state.read_bytes() == state_before
    assert (
        report_path.read_bytes(),
        sidecar_path.read_bytes(),
        combined_path.read_bytes(),
    ) == old_bundle
    assert pending_seen_url_delta_path(state).exists()

    recovered = run_monitor(**arguments)

    assert {item.url for item in recovered.items} == {
        "https://example.org/old",
        "https://example.org/new",
    }
    assert json.loads(state.read_text(encoding="utf-8")) == [
        "https://example.org/old",
        "https://example.org/new",
    ]
    assert not pending_seen_url_delta_path(state).exists()


def test_orchestrator_retries_pre_manifest_failure_when_only_authored_subset_changes(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    first_item = CandidateItem(
        title="Climate insurance first report",
        url="https://example.org/first",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    second_item = replace(
        first_item,
        title="Climate insurance second report",
        url="https://example.org/second",
    )
    selected = {first_item.url}
    monkeypatch.setattr(
        orchestrator,
        "collect_website_items",
        lambda *args, **kwargs: ([first_item, second_item], []),
    )
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])

    def classify(candidate, run_config):
        if candidate.url in selected:
            return _classify_as_relevant(candidate, run_config)
        return replace(candidate, climate_related=False, actuarial_related=False)

    monkeypatch.setattr(orchestrator, "classify_candidate", classify)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }
    first = run_monitor(**arguments)
    report_path = Path(first.report_path)
    combined_path = combined_candidates_path(source_dir, "2026-09-07")
    old_report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    old_combined_sha256 = hashlib.sha256(combined_path.read_bytes()).hexdigest()
    selected.add(second_item.url)

    with monkeypatch.context() as crash:
        crash.setattr(
            semantic_bundle,
            "_write_pending",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt("simulated authored-subset pre-manifest interruption")
            ),
        )
        with pytest.raises(KeyboardInterrupt, match="authored-subset"):
            run_monitor(**arguments)

    pending = pending_seen_url_delta_path(state)
    pending_payload = json.loads(pending.read_text(encoding="utf-8"))
    assert pending_payload["combined_sha256"] == old_combined_sha256
    assert pending_payload["report_sha256"] != old_report_sha256

    recovered = run_monitor(**arguments)

    assert {item.url for item in recovered.items} == {
        "https://example.org/first",
        "https://example.org/second",
    }
    assert json.loads(state.read_text(encoding="utf-8")) == [
        "https://example.org/first",
        "https://example.org/second",
    ]
    assert not pending.exists()


def test_orchestrator_matching_pending_report_identity_rejects_addition_mismatch(
    tmp_path, monkeypatch
):
    sources, config, source_dir, _, state_dir = _write_modern_config(tmp_path)
    state_dir.mkdir()
    state = state_dir / "seen_urls.json"
    state.write_bytes(b"[]\n")
    item = CandidateItem(
        title="Climate insurance matching report",
        url="https://example.org/matching",
        summary="Climate insurance capital evidence for supervisors.",
        source_name="Example",
        lane="website",
    )
    monkeypatch.setattr(orchestrator, "collect_website_items", lambda *args, **kwargs: ([item], []))
    monkeypatch.setattr(orchestrator, "search_recent_research", lambda *args, **kwargs: [])
    monkeypatch.setattr(orchestrator, "classify_candidate", _classify_as_relevant)
    arguments = {
        "source_config_path": sources,
        "run_config_path": config,
        "report_date": date(2026, 9, 7),
        "state_dir": state_dir,
        "sync": False,
    }
    with monkeypatch.context() as crash:
        crash.setattr(
            orchestrator,
            "commit_seen_url_delta",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt("simulated matching-report pre-state interruption")
            ),
        )
        with pytest.raises(KeyboardInterrupt, match="matching-report"):
            run_monitor(**arguments)

    pending = pending_seen_url_delta_path(state)
    pending_payload = json.loads(pending.read_text(encoding="utf-8"))
    report_path = Path(source_dir) / "climate-monitor-2026-09-07.md"
    assert pending_payload["report_sha256"] == hashlib.sha256(
        report_path.read_bytes()
    ).hexdigest()
    pending_payload["additions"] = ["https://example.org/different"]
    pending.write_text(
        json.dumps(pending_payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    state_before = state.read_bytes()
    pending_before = pending.read_bytes()

    with pytest.raises(ValueError, match="does not match the committed report bundle"):
        run_monitor(**arguments)

    assert state.read_bytes() == state_before
    assert pending.read_bytes() == pending_before


def test_legacy_and_modern_current_artifact_entrypoints_emit_identical_candidates(
    tmp_path, monkeypatch
):
    report_date = "2026-09-07"
    legacy_reports = tmp_path / "legacy"
    legacy_reports.mkdir()
    state_file = tmp_path / "article_state.json"
    state_file.write_text("{}\n", encoding="utf-8")
    pillar_a = _pillar_a(
        _a_item(
            "Pillar A title",
            "https://example.org/shared?utm_source=site#section",
        )
    )
    pillar_b = [
        _b_item("Pillar B title", "https://example.org/shared?fbclid=search")
    ]
    article_changes_path = legacy_reports / f"article_changes_{report_date}.json"
    pillar_b_path = legacy_reports / f"pillar_b_{report_date}.json"
    article_changes_path.write_text(json.dumps(pillar_a), encoding="utf-8")
    pillar_b_path.write_text(json.dumps(pillar_b), encoding="utf-8")
    repository = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "CLIMATE_REPORTS_DIR": str(legacy_reports),
        "CLIMATE_WL_STATE": str(state_file),
    }
    legacy = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "step3_aggregate.py"),
            "--date",
            report_date,
            "--no-update-seen-state",
        ],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert legacy.returncode == 0, legacy.stdout + legacy.stderr

    modern_root = tmp_path / "modern"
    modern_root.mkdir()
    sources, config, modern_reports, _, modern_state = _write_modern_config(modern_root)
    modern_state.mkdir()
    (modern_state / "seen_urls.json").write_text("[]\n", encoding="utf-8")

    calls: list[str] = []

    def fake_classify(item, run_config):
        calls.append(item.url)
        return replace(
            item,
            climate_related=True,
            actuarial_related=True,
            summary=item.summary or "Climate insurance report evidence.",
            categories=("Supervision & Disclosure",),
            keywords=("climate risk", "insurance capital", "supervisory review"),
        )

    monkeypatch.setattr("climate_monitor.orchestrator.classify_candidate", fake_classify)
    modern_result = run_monitor(
        source_config_path=sources,
        run_config_path=config,
        report_date=date.fromisoformat(report_date),
        article_changes_artifact_path=article_changes_path,
        pillar_b_artifact_path=pillar_b_path,
        state_dir=modern_state,
        sync=False,
        update_seen_state=False,
    )

    legacy_bytes = combined_candidates_path(legacy_reports, report_date).read_bytes()
    modern_bytes = combined_candidates_path(modern_reports, report_date).read_bytes()
    assert modern_bytes == legacy_bytes
    payload = json.loads(modern_bytes)
    assert len(payload["items"]) == 1
    assert len(payload["items"][0]["origins"]) == 2
    assert len(json.loads((legacy_reports / f"aggregated_{report_date}.json").read_text())["items"]) == 1
    assert len(calls) == 1
    assert calls[0] == payload["items"][0]["url"]
    assert len(modern_result.items) == 1
    assert Path(modern_result.report_path).read_text(encoding="utf-8").count(
        f"**URL:** {calls[0]} <br>"
    ) == 1

    no_report_root = tmp_path / "modern-no-report"
    no_report_root.mkdir()
    no_sources, no_config, no_reports, _, no_state = _write_modern_config(no_report_root)
    no_state.mkdir()
    (no_state / "seen_urls.json").write_text("[]\n", encoding="utf-8")
    no_state_before = (no_state / "seen_urls.json").read_bytes()
    monkeypatch.setattr(
        orchestrator,
        "classify_candidate",
        lambda item, run_config: replace(
            item,
            climate_related=False,
            actuarial_related=False,
        ),
    )

    no_report_result = run_monitor(
        source_config_path=no_sources,
        run_config_path=no_config,
        report_date=date.fromisoformat(report_date),
        article_changes_artifact_path=article_changes_path,
        pillar_b_artifact_path=pillar_b_path,
        state_dir=no_state,
        sync=False,
    )

    assert no_report_result.report_path is None
    assert combined_candidates_path(no_reports, report_date).read_bytes() == legacy_bytes
    assert (no_state / "seen_urls.json").read_bytes() == no_state_before
    assert not pending_seen_url_delta_path(no_state / "seen_urls.json").exists()
    assert not (no_reports / f"climate-monitor-{report_date}.md").exists()
    assert not semantic_sidecar_path(
        no_reports / f"climate-monitor-{report_date}.md"
    ).exists()
