from __future__ import annotations

import hashlib
import json
import re
import socket
from datetime import date
from pathlib import Path

import pytest

from climate_monitor import semantic_bundle
from climate_monitor.models import CandidateItem
from climate_monitor.orchestrator import run_monitor
from climate_monitor.semantic_bundle import (
    SemanticBundleError,
    article_identity,
    build_sidecar_payload,
    commit_report_with_semantics,
    derive_semantic_bundle,
    recover_pending_commit,
    rendered_article_urls,
    select_semantic_articles,
    semantic_sidecar_path,
    serialize_sidecar,
    verify_semantic_sidecar,
)
from climate_monitor.taxonomy import (
    DEFAULT_TAXONOMY_PATH,
    DEFAULT_TAXONOMY_SHA256,
    load_article_taxonomy,
)


REPORT_URL_LINE = re.compile(r"^\*\*URL:\*\* (.+?) <br>$", re.MULTILINE)


def _write_source_config(path: Path) -> None:
    path.write_text(
        "sources:\n"
        "  - key: iais\n"
        "    abbreviation: IAIS\n"
        "    full_name: International Association of Insurance Supervisors\n"
        "    url: https://www.iais.org/\n",
        encoding="utf-8",
    )


def _write_run_config(
    path: Path,
    *,
    source_dir: Path,
    wiki_dir: Path,
    state: Path,
    max_items: int = 12,
    write_empty_report: bool = False,
) -> None:
    path.write_text(
        f"""
report_title: Weekly Climate & Actuarial Monitor
max_items_per_report: {max_items}
climate_keywords: [climate, flood, wildfire]
actuarial_keywords: [insurance, supervision, capital]
research_lane:
  lookback_days: 30
  queries: [climate insurance report]
output:
  source_dir: {source_dir.as_posix()}
  wiki_dir: {wiki_dir.as_posix()}
  write_empty_report: {str(write_empty_report).lower()}
dedupe:
  url_tracking_path: {(state / "seen_urls.json").as_posix()}
  title_tracking_path: {(state / "seen_titles.json").as_posix()}
""".strip(),
        encoding="utf-8",
    )


def _write_manifest(path: Path, *, count: int = 2) -> None:
    items = []
    for index in range(count):
        items.append(
            {
                "item_id": str(index + 1),
                "item_type": "page",
                "url": f"https://www.iais.org/climate-supervision-{index + 1}",
                "title": f"Climate supervision update {index + 1}",
                "summary": "Insurance supervisors discuss climate risk and capital.",
                "status": "new",
                "observed_at": "2026-05-14T00:00:00Z",
            }
        )
    path.write_text(
        json.dumps(
            {
                "schema_version": "web-listening-manifest.v1",
                "source": {"source_id": "iais", "site_name": "IAIS"},
                "discovered_items": items,
                "downloaded_assets": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_research(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "title": "Climate risk and insurance capital study",
                    "url": "https://example.org/study",
                    "summary": "A study about climate risk and insurance capital.",
                    "source_name": "Example Research",
                    "published": "2026-05-01",
                }
            ],
            indent=2,
        ),
        encoding="utf-8",
    )


def _run(tmp_path: Path, *, name: str = "run", max_items: int = 12, manifest_count: int = 2):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    source_config = root / "sources.yaml"
    run_config = root / "run_config.yaml"
    manifest = root / "manifest.json"
    research = root / "research.json"
    source_dir = root / "sources"
    wiki_dir = root / "wiki"
    state = root / "state"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=wiki_dir, state=state, max_items=max_items)
    _write_manifest(manifest, count=manifest_count)
    _write_research(research)
    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 14),
        manifest_fixture_path=manifest,
        research_fixture_path=research,
        state_dir=state,
        sync=False,
    )
    return result, source_dir


def _item(**overrides) -> CandidateItem:
    payload = {
        "title": "Climate supervision update",
        "url": "https://www.iais.org/climate-supervision",
        "summary": "Insurance supervisors discuss climate risk and capital adequacy.",
        "source_name": "IAIS",
        "lane": "website",
        "climate_related": True,
        "actuarial_related": True,
        "climate_signal": "physical_risk",
        "actuarial_signal": "insurance_risk",
        "topics": ("climate", "insurance", "capital"),
        "categories": ("Physical Risk", "Insurance Risk"),
        "keywords": ("flood", "pricing", "catastrophe model"),
    }
    payload.update(overrides)
    return CandidateItem(**payload)


# --------------------------------------------------------------------------
# Producer wiring: only finally selected articles, 1:1 with canonical Markdown
# --------------------------------------------------------------------------


def test_run_monitor_writes_a_semantic_sidecar_beside_the_canonical_report(tmp_path):
    result, source_dir = _run(tmp_path)

    report_path = Path(result.report_path)
    sidecar_path = semantic_sidecar_path(report_path)
    assert sidecar_path.name == "climate-monitor-2026-05-14.semantics.json"
    assert sidecar_path.exists()

    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    report_bytes = report_path.read_bytes()
    assert payload["schema_version"] == "article-semantic-sidecar.v1"
    assert payload["report"]["sha256"] == hashlib.sha256(report_bytes).hexdigest()
    assert payload["report"]["date"] == "2026-05-14"
    assert payload["report"]["filename"] == "climate-monitor-2026-05-14.md"
    assert payload["taxonomy"]["taxonomy_id"] == "climate-actuarial-v1"
    assert payload["taxonomy"]["sha256"] == DEFAULT_TAXONOMY_SHA256
    assert payload["article_count"] == len(payload["articles"]) == len(result.items)


def test_sidecar_articles_correspond_one_to_one_with_the_rendered_markdown(tmp_path):
    result, _ = _run(tmp_path)
    report_path = Path(result.report_path)
    payload = json.loads(semantic_sidecar_path(report_path).read_text(encoding="utf-8"))

    rendered_urls = REPORT_URL_LINE.findall(report_path.read_text(encoding="utf-8"))
    sidecar_urls = [article["url"] for article in payload["articles"]]
    assert sidecar_urls == rendered_urls
    assert len(set(article["article_id"] for article in payload["articles"])) == len(sidecar_urls)
    assert [article["position"] for article in payload["articles"]] == list(range(1, len(sidecar_urls) + 1))


def test_only_finally_selected_articles_receive_semantics(tmp_path):
    result, _ = _run(tmp_path, max_items=1, manifest_count=3)
    report_path = Path(result.report_path)
    payload = json.loads(semantic_sidecar_path(report_path).read_text(encoding="utf-8"))

    assert len(result.items) == 1
    assert payload["article_count"] == 1
    assert REPORT_URL_LINE.findall(report_path.read_text(encoding="utf-8")) == [
        payload["articles"][0]["url"]
    ]


def test_run_monitor_skips_publication_when_semantic_selection_drops_everything(tmp_path, monkeypatch):
    root = tmp_path / "all-dropped"
    root.mkdir(parents=True, exist_ok=True)
    source_config = root / "sources.yaml"
    run_config = root / "run_config.yaml"
    source_dir = root / "sources"
    wiki_dir = root / "wiki"
    state = root / "state"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=wiki_dir, state=state)

    state.mkdir(parents=True)
    seen_urls = state / "seen_urls.json"
    seen_titles = state / "seen_titles.json"
    seen_urls.write_text('["https://existing.example/item"]\n', encoding="utf-8")
    seen_titles.write_text('["existing title"]\n', encoding="utf-8")
    seen_urls_before = seen_urls.read_bytes()
    seen_titles_before = seen_titles.read_bytes()

    source_dir.mkdir(parents=True)
    report_path = source_dir / "climate-monitor-2026-05-14.md"
    sidecar_path = semantic_sidecar_path(report_path)
    report_path.write_text("previous report\n", encoding="utf-8")
    sidecar_path.write_text("previous sidecar\n", encoding="utf-8")
    report_before = report_path.read_bytes()
    sidecar_before = sidecar_path.read_bytes()

    warning = "iais seed https://www.iais.org/broken/: timeout"
    drop_note = (
        "dropped non-semantic article (unvalidatable bundle): "
        "Climate insurance capital update [https://www.iais.org/all-filtered] "
        "(semantic bundle is not contract-valid: test)"
    )
    sync_calls = []

    def fake_collect(sources, *, state_dir, manifest_fixture_path=None, site_scopes=None):
        return [
            CandidateItem(
                title="Climate insurance capital update",
                url="https://www.iais.org/all-filtered",
                summary="Climate risk and insurance capital supervision.",
                source_name="IAIS",
                lane="website",
            )
        ], [warning]

    def fake_select(items):
        assert [item.title for item in items] == ["Climate insurance capital update"]
        return [], [drop_note]

    monkeypatch.setattr("climate_monitor.orchestrator.collect_website_items", fake_collect)
    monkeypatch.setattr("climate_monitor.orchestrator.select_semantic_articles", fake_select)
    monkeypatch.setattr(
        "climate_monitor.orchestrator.sync_source_wiki",
        lambda *args, **kwargs: sync_calls.append((args, kwargs)),
    )

    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 14),
        site_scopes_path=None,
        state_dir=state,
        sync=True,
    )

    assert result.report_path is None
    assert result.semantics_path is None
    assert result.report_sha256 == ""
    assert result.items == ()
    assert result.dedup_notes == (drop_note,)
    assert result.warnings == (warning,)
    assert result.synced is False
    assert report_path.read_bytes() == report_before
    assert sidecar_path.read_bytes() == sidecar_before
    assert seen_urls.read_bytes() == seen_urls_before
    assert seen_titles.read_bytes() == seen_titles_before
    assert sync_calls == []


def test_run_monitor_publishes_remaining_items_after_partial_semantic_drop(tmp_path, monkeypatch):
    root = tmp_path / "partial-drop"
    root.mkdir(parents=True, exist_ok=True)
    source_config = root / "sources.yaml"
    run_config = root / "run_config.yaml"
    source_dir = root / "sources"
    wiki_dir = root / "wiki"
    state = root / "state"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=wiki_dir, state=state)

    drop_note = "dropped non-semantic article (unvalidatable bundle): filtered"

    def fake_collect(sources, *, state_dir, manifest_fixture_path=None, site_scopes=None):
        return [
            CandidateItem(
                title="Dropped climate insurance update",
                url="https://www.iais.org/dropped",
                summary="Climate risk and insurance capital supervision.",
                source_name="IAIS",
                lane="website",
            ),
            CandidateItem(
                title="Kept climate insurance update",
                url="https://www.iais.org/kept",
                summary="Climate risk and insurance capital supervision.",
                source_name="IAIS",
                lane="website",
            ),
        ], []

    def fake_select(items):
        assert [item.url for item in items] == [
            "https://www.iais.org/dropped",
            "https://www.iais.org/kept",
        ]
        return [items[1]], [drop_note]

    monkeypatch.setattr("climate_monitor.orchestrator.collect_website_items", fake_collect)
    monkeypatch.setattr("climate_monitor.orchestrator.select_semantic_articles", fake_select)

    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 14),
        site_scopes_path=None,
        state_dir=state,
        sync=False,
    )

    assert result.report_path is not None
    assert [item.url for item in result.items] == ["https://www.iais.org/kept"]
    assert result.dedup_notes == (drop_note,)
    report_path = Path(result.report_path)
    report_text = report_path.read_text(encoding="utf-8")
    payload = json.loads(semantic_sidecar_path(report_path).read_text(encoding="utf-8"))
    rendered_urls = rendered_article_urls(report_text)
    sidecar_urls = [article["url"] for article in payload["articles"]]
    assert rendered_urls == ["https://www.iais.org/kept"]
    assert sidecar_urls == rendered_urls
    assert payload["article_count"] == 1


def test_run_monitor_can_intentionally_publish_empty_semantic_report(tmp_path, monkeypatch):
    root = tmp_path / "intentional-empty"
    root.mkdir(parents=True, exist_ok=True)
    source_config = root / "sources.yaml"
    run_config = root / "run_config.yaml"
    source_dir = root / "sources"
    wiki_dir = root / "wiki"
    state = root / "state"
    _write_source_config(source_config)
    _write_run_config(
        run_config,
        source_dir=source_dir,
        wiki_dir=wiki_dir,
        state=state,
        write_empty_report=True,
    )

    drop_note = "dropped non-semantic article (unvalidatable bundle): filtered"

    def fake_collect(sources, *, state_dir, manifest_fixture_path=None, site_scopes=None):
        return [
            CandidateItem(
                title="Climate insurance capital update",
                url="https://www.iais.org/all-filtered",
                summary="Climate risk and insurance capital supervision.",
                source_name="IAIS",
                lane="website",
            )
        ], []

    monkeypatch.setattr("climate_monitor.orchestrator.collect_website_items", fake_collect)
    monkeypatch.setattr(
        "climate_monitor.orchestrator.select_semantic_articles",
        lambda items: ([], [drop_note]),
    )

    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 14),
        site_scopes_path=None,
        state_dir=state,
        sync=False,
    )

    assert result.report_path is not None
    assert result.items == ()
    assert result.dedup_notes == (drop_note,)
    report_path = Path(result.report_path)
    report_text = report_path.read_text(encoding="utf-8")
    payload = json.loads(semantic_sidecar_path(report_path).read_text(encoding="utf-8"))
    assert rendered_article_urls(report_text) == []
    assert payload["article_count"] == 0
    assert payload["articles"] == []
    assert payload["report"]["filename"] == report_path.name


def test_every_final_article_carries_a_validated_semantic_bundle(tmp_path):
    result, _ = _run(tmp_path)
    payload = json.loads(semantic_sidecar_path(Path(result.report_path)).read_text(encoding="utf-8"))
    taxonomy = load_article_taxonomy()

    for article in payload["articles"]:
        bundle = article["semantics"]
        assert bundle["schema_version"] == "article-semantic-bundle.v1"
        assert bundle["summary"]
        assert 1 <= len(bundle["categories"]) <= 3
        assert 3 <= len(bundle["keywords"]) <= 8
        assert set(bundle["categories"]) <= set(taxonomy.allowed_labels)


def test_monitor_result_reports_the_semantic_artifact_identity(tmp_path):
    result, _ = _run(tmp_path)
    report_bytes = Path(result.report_path).read_bytes()

    assert result.report_sha256 == hashlib.sha256(report_bytes).hexdigest()
    assert result.semantics_path is not None
    assert Path(result.semantics_path).name == "climate-monitor-2026-05-14.semantics.json"
    payload = json.loads(result.to_json())
    assert payload["report_sha256"] == result.report_sha256
    assert payload["semantics_path"] == "climate-monitor-2026-05-14.semantics.json"


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_sidecar_bytes_are_identical_across_independent_runs(tmp_path):
    first_result, _ = _run(tmp_path, name="a")
    second_result, _ = _run(tmp_path, name="b")

    first = semantic_sidecar_path(Path(first_result.report_path)).read_bytes()
    second = semantic_sidecar_path(Path(second_result.report_path)).read_bytes()
    assert first == second
    assert first.endswith(b"\n")


def test_serialization_is_stable_and_sorted(tmp_path):
    taxonomy = load_article_taxonomy()
    payload = build_sidecar_payload(
        report_date=date(2026, 5, 14),
        report_filename="climate-monitor-2026-05-14.md",
        report_sha256="a" * 64,
        items=[_item()],
        taxonomy=taxonomy,
    )
    raw = serialize_sidecar(payload)
    assert raw == serialize_sidecar(json.loads(raw.decode("utf-8")))
    decoded = json.loads(raw.decode("utf-8"))
    assert list(decoded) == sorted(decoded)
    assert list(decoded["articles"][0]) == sorted(decoded["articles"][0])
    assert raw.endswith(b"\n")
    assert b"\r" not in raw


def test_article_identity_is_stable_and_url_derived():
    identity = article_identity(_item())
    assert identity == article_identity(_item())
    assert identity == article_identity(_item(url="https://www.iais.org/climate-supervision/?utm_source=x"))
    assert identity != article_identity(_item(url="https://www.iais.org/other"))
    assert re.fullmatch(r"[0-9a-f]{64}", identity)


def test_article_identity_requires_a_canonical_url():
    with pytest.raises(SemanticBundleError):
        article_identity(_item(url="   "))


# --------------------------------------------------------------------------
# Single agent pass: no second per-article model call
# --------------------------------------------------------------------------


def test_producer_module_makes_no_model_or_network_call():
    source = Path(semantic_bundle.__file__).read_text(encoding="utf-8")
    for forbidden in ("openai", "requests", "urllib.request", "httpx", "http.client", "getenv", "environ"):
        assert forbidden not in source


def test_pipeline_produces_semantics_without_any_socket(tmp_path, monkeypatch):
    def blocked(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("semantic production must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    result, _ = _run(tmp_path)
    assert result.report_sha256


def test_agent_supplied_bundle_is_used_verbatim_for_the_selected_article():
    taxonomy = load_article_taxonomy()
    supplied = {
        "summary": "Supervisors published a climate scenario exercise for insurers.",
        "categories": ["Supervision & Disclosure"],
        "keywords": ["scenario exercise", "supervisory review", "solvency"],
    }
    bundle = derive_semantic_bundle(_item(semantics=supplied), taxonomy=taxonomy)
    assert bundle["summary"] == supplied["summary"]
    assert bundle["categories"] == supplied["categories"]
    assert bundle["keywords"] == supplied["keywords"]
    assert bundle["taxonomy_sha256"] == DEFAULT_TAXONOMY_SHA256


def test_manifest_semantics_flow_through_the_single_existing_pass(tmp_path):
    root = tmp_path / "seam"
    root.mkdir()
    source_config = root / "sources.yaml"
    run_config = root / "run_config.yaml"
    manifest = root / "manifest.json"
    source_dir = root / "sources"
    state = root / "state"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=root / "wiki", state=state)
    manifest.write_text(
        json.dumps(
            {
                "source": {"site_name": "IAIS"},
                "discovered_items": [
                    {
                        "item_id": "1",
                        "item_type": "page",
                        "url": "https://www.iais.org/climate-supervision",
                        "title": "Climate supervision update",
                        "summary": "Insurance supervisors discuss climate risk.",
                        "status": "new",
                        "semantics": {
                            "summary": "One authoring pass produced this supervisory summary.",
                            "categories": ["Supervision & Disclosure"],
                            "keywords": ["supervisory review", "climate scenario", "disclosure"],
                        },
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 14),
        manifest_fixture_path=manifest,
        state_dir=state,
        sync=False,
    )
    payload = json.loads(semantic_sidecar_path(Path(result.report_path)).read_text(encoding="utf-8"))
    bundle = payload["articles"][0]["semantics"]
    assert bundle["summary"] == "One authoring pass produced this supervisory summary."
    assert bundle["categories"] == ["Supervision & Disclosure"]
    assert payload["articles"][0]["semantics_provenance"] == "agent_bundle"


def test_derived_fallback_is_marked_as_deterministic_provenance(tmp_path):
    result, _ = _run(tmp_path)
    payload = json.loads(semantic_sidecar_path(Path(result.report_path)).read_text(encoding="utf-8"))
    assert {article["semantics_provenance"] for article in payload["articles"]} == {"pipeline_derived"}


def test_derived_keywords_top_up_from_pipeline_categories_deterministically():
    taxonomy = load_article_taxonomy()
    bundle = derive_semantic_bundle(
        _item(topics=("climate",), keywords=("climate",), categories=("Physical Risk", "Insurance Risk")),
        taxonomy=taxonomy,
    )
    assert bundle["keywords"] == ["climate", "physical risk", "insurance risk"]
    assert bundle["keywords"] == derive_semantic_bundle(
        _item(topics=("climate",), keywords=("climate",), categories=("Physical Risk", "Insurance Risk")),
        taxonomy=taxonomy,
    )["keywords"]


def test_derived_categories_outside_the_taxonomy_are_not_emitted():
    bundle = derive_semantic_bundle(
        _item(categories=("Physical Risk", "Made Up Label"), keywords=("flood", "pricing", "solvency")),
        taxonomy=load_article_taxonomy(),
    )
    assert bundle["categories"] == ["Physical Risk"]


def test_derived_keywords_respect_the_taxonomy_bounds():
    bundle = derive_semantic_bundle(
        _item(keywords=tuple(f"term-{index}" for index in range(20)), topics=()),
        taxonomy=load_article_taxonomy(),
    )
    assert bundle["keywords"] == [f"term-{index}" for index in range(8)]


def test_derived_summary_is_clipped_to_the_taxonomy_maximum():
    long_summary = " ".join(["climate"] * 600)
    bundle = derive_semantic_bundle(_item(summary=long_summary), taxonomy=load_article_taxonomy())
    assert len(bundle["summary"]) <= 2000
    assert long_summary.startswith(bundle["summary"])


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "supplied",
    [
        {"summary": "A valid summary.", "categories": ["Not A Category"], "keywords": ["a", "b", "c"]},
        {"summary": "A valid summary.", "categories": ["Physical Risk"], "keywords": ["report", "b", "c"]},
        {"summary": "A valid summary.", "categories": ["Physical Risk", "Physical Risk"], "keywords": ["a", "b", "c"]},
        {"summary": "A valid summary.", "categories": ["Physical Risk"], "keywords": ["a", "b"]},
        {"summary": "A valid summary.", "categories": [], "keywords": ["a", "b", "c"]},
        {"summary": "", "categories": ["Physical Risk"], "keywords": ["a", "b", "c"]},
        {"summary": "Bad\u200bsummary.", "categories": ["Physical Risk"], "keywords": ["a", "b", "c"]},
        {"summary": "Bad\x07summary.", "categories": ["Physical Risk"], "keywords": ["a", "b", "c"]},
        {"summary": "A valid summary.", "categories": ["Physical Risk"], "keywords": ["a", "b", "c", "d", "e", "f", "g", "h", "i"]},
        {"summary": "A valid summary.", "categories": ["Physical Risk"], "keywords": ["a,b", "c", "d"]},
        {"summary": "A valid summary.", "categories": ["Physical Risk"], "keywords": ["A", "a", "c"]},
        {"summary": "A valid summary.", "categories": ["Physical Risk"], "keywords": ["a", "b", "c"], "extra": 1},
        {"summary": "A valid summary.", "categories": ["Physical Risk"]},
    ],
)
def test_invalid_agent_semantics_fail_closed(supplied):
    with pytest.raises(SemanticBundleError):
        derive_semantic_bundle(_item(semantics=supplied), taxonomy=load_article_taxonomy())


def test_agent_supplied_taxonomy_binding_mismatch_is_rejected():
    supplied = {
        "schema_version": "article-semantic-bundle.v1",
        "taxonomy_id": "climate-actuarial-v1",
        "taxonomy_sha256": "0" * 64,
        "summary": "A valid summary.",
        "categories": ["Physical Risk"],
        "keywords": ["flood", "pricing", "solvency"],
    }
    with pytest.raises(SemanticBundleError):
        derive_semantic_bundle(_item(semantics=supplied), taxonomy=load_article_taxonomy())


def test_derivation_fails_closed_when_no_category_can_be_assigned():
    with pytest.raises(SemanticBundleError):
        derive_semantic_bundle(
            _item(
                categories=(),
                climate_signal="none",
                actuarial_signal="none",
                climate_related=False,
                actuarial_related=False,
            ),
            taxonomy=load_article_taxonomy(),
        )


def test_derivation_fails_closed_when_too_few_keywords_can_be_derived():
    with pytest.raises(SemanticBundleError):
        derive_semantic_bundle(
            _item(topics=(), keywords=(), categories=("Physical Risk",)),
            taxonomy=load_article_taxonomy(),
        )


def test_derivation_fails_closed_on_an_empty_summary():
    with pytest.raises(SemanticBundleError):
        derive_semantic_bundle(_item(summary="   "), taxonomy=load_article_taxonomy())


def test_duplicate_article_identity_is_rejected():
    with pytest.raises(SemanticBundleError):
        build_sidecar_payload(
            report_date=date(2026, 5, 14),
            report_filename="climate-monitor-2026-05-14.md",
            report_sha256="a" * 64,
            items=[_item(), _item(title="Other title")],
            taxonomy=load_article_taxonomy(),
        )


def test_validation_failure_does_not_overwrite_an_existing_canonical_pair(tmp_path):
    report_path = tmp_path / "climate-monitor-2026-05-14.md"
    sidecar_path = semantic_sidecar_path(report_path)
    report_path.write_text("# previous canonical report\n", encoding="utf-8")
    sidecar_path.write_text("previous sidecar\n", encoding="utf-8")

    with pytest.raises(SemanticBundleError):
        commit_report_with_semantics(
            report_path=report_path,
            report_date=date(2026, 5, 14),
            report_text="# new report\n**URL:** https://www.iais.org/climate-supervision <br>\n",
            items=[_item(summary="  ")],
        )

    assert report_path.read_text(encoding="utf-8") == "# previous canonical report\n"
    assert sidecar_path.read_text(encoding="utf-8") == "previous sidecar\n"
    assert not list(tmp_path.glob("*.pending"))


# --------------------------------------------------------------------------
# Taxonomy identity by ID + SHA, independent of path
# --------------------------------------------------------------------------


def test_taxonomy_copied_to_another_path_still_validates_by_identity(tmp_path):
    copied = tmp_path / "nested" / "copy.yaml"
    copied.parent.mkdir(parents=True)
    copied.write_bytes(DEFAULT_TAXONOMY_PATH.read_bytes())

    taxonomy = load_article_taxonomy(copied)
    assert taxonomy.taxonomy_id == "climate-actuarial-v1"
    assert taxonomy.sha256 == DEFAULT_TAXONOMY_SHA256

    bundle = derive_semantic_bundle(_item(), taxonomy=taxonomy)
    assert bundle["taxonomy_sha256"] == DEFAULT_TAXONOMY_SHA256


def test_tampered_taxonomy_copy_is_rejected(tmp_path):
    copied = tmp_path / "tampered.yaml"
    raw = DEFAULT_TAXONOMY_PATH.read_bytes()
    copied.write_bytes(raw.replace(b"label: Actuarial Modelling", b"label: Actuarial Modeling"))

    with pytest.raises(ValueError):
        load_article_taxonomy(copied)


# --------------------------------------------------------------------------
# Verification: report SHA mismatch, stale sidecar, partial write, recovery
# --------------------------------------------------------------------------


def test_verify_accepts_a_freshly_committed_pair(tmp_path):
    result, _ = _run(tmp_path)
    assert verify_semantic_sidecar(Path(result.report_path))["article_count"] == len(result.items)


def test_verify_rejects_a_report_sha_mismatch(tmp_path):
    result, _ = _run(tmp_path)
    report_path = Path(result.report_path)
    report_path.write_text(report_path.read_text(encoding="utf-8") + "\n<!-- tampered -->\n", encoding="utf-8")

    with pytest.raises(SemanticBundleError):
        verify_semantic_sidecar(report_path)


def test_verify_rejects_a_stale_sidecar_from_a_previous_report(tmp_path):
    first, _ = _run(tmp_path, name="first", manifest_count=2)
    second, _ = _run(tmp_path, name="second", manifest_count=1)
    stale = semantic_sidecar_path(Path(first.report_path)).read_bytes()
    target = semantic_sidecar_path(Path(second.report_path))
    target.write_bytes(stale)

    with pytest.raises(SemanticBundleError):
        verify_semantic_sidecar(Path(second.report_path))


def test_verify_rejects_a_missing_sidecar(tmp_path):
    result, _ = _run(tmp_path)
    semantic_sidecar_path(Path(result.report_path)).unlink()

    with pytest.raises(SemanticBundleError):
        verify_semantic_sidecar(Path(result.report_path))


def test_verify_rejects_a_sidecar_missing_a_rendered_article(tmp_path):
    result, _ = _run(tmp_path)
    sidecar_path = semantic_sidecar_path(Path(result.report_path))
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["articles"] = payload["articles"][:-1]
    payload["article_count"] = len(payload["articles"])
    sidecar_path.write_bytes(serialize_sidecar(payload))

    with pytest.raises(SemanticBundleError):
        verify_semantic_sidecar(Path(result.report_path))


def test_verify_rejects_a_sidecar_with_a_stale_extra_article(tmp_path):
    result, _ = _run(tmp_path)
    sidecar_path = semantic_sidecar_path(Path(result.report_path))
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    extra = dict(payload["articles"][0])
    extra["url"] = "https://www.iais.org/removed-last-week"
    extra["article_id"] = "f" * 64
    extra["position"] = len(payload["articles"]) + 1
    payload["articles"].append(extra)
    payload["article_count"] = len(payload["articles"])
    sidecar_path.write_bytes(serialize_sidecar(payload))

    with pytest.raises(SemanticBundleError):
        verify_semantic_sidecar(Path(result.report_path))


def test_partial_write_is_repaired_by_recovery(tmp_path, monkeypatch):
    report_path = tmp_path / "climate-monitor-2026-05-14.md"
    sidecar_path = semantic_sidecar_path(report_path)
    report_path.write_text("# previous canonical report\n", encoding="utf-8")
    sidecar_path.write_text("previous sidecar\n", encoding="utf-8")
    report_text = "# new report\n**URL:** https://www.iais.org/climate-supervision <br>\n"

    real_replace = semantic_bundle.os.replace
    calls = {"count": 0}

    def crashing_replace(source, target):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated crash between the two commits")
        return real_replace(source, target)

    monkeypatch.setattr(semantic_bundle.os, "replace", crashing_replace)
    with pytest.raises(OSError):
        commit_report_with_semantics(
            report_path=report_path,
            report_date=date(2026, 5, 14),
            report_text=report_text,
            items=[_item()],
        )
    monkeypatch.undo()

    # Crash left the Markdown updated and the sidecar stale: verification must fail.
    assert report_path.read_text(encoding="utf-8") == report_text
    with pytest.raises(SemanticBundleError):
        verify_semantic_sidecar(report_path)

    assert recover_pending_commit(report_path) == "applied"
    verify_semantic_sidecar(report_path)
    assert not list(tmp_path.glob("*.pending"))


def test_recovery_applies_a_fully_staged_pair(tmp_path, monkeypatch):
    report_path = tmp_path / "climate-monitor-2026-05-14.md"
    report_text = "# new report\n**URL:** https://www.iais.org/climate-supervision <br>\n"

    def crashing_replace(source, target):
        raise OSError("simulated crash before any commit")

    monkeypatch.setattr(semantic_bundle.os, "replace", crashing_replace)
    with pytest.raises(OSError):
        commit_report_with_semantics(
            report_path=report_path,
            report_date=date(2026, 5, 14),
            report_text=report_text,
            items=[_item()],
        )
    monkeypatch.undo()

    assert not report_path.exists()
    assert recover_pending_commit(report_path) == "applied"
    assert report_path.read_text(encoding="utf-8") == report_text
    verify_semantic_sidecar(report_path)
    assert not list(tmp_path.glob("*.pending"))


def test_recovery_discards_an_inconsistent_staged_pair(tmp_path):
    report_path = tmp_path / "climate-monitor-2026-05-14.md"
    report_path.write_text("# previous canonical report\n", encoding="utf-8")
    pending_report = report_path.with_name(report_path.name + ".pending")
    pending_sidecar = semantic_sidecar_path(report_path).with_name(
        semantic_sidecar_path(report_path).name + ".pending"
    )
    pending_report.write_text("# torn report\n", encoding="utf-8")
    pending_sidecar.write_text("{ truncated", encoding="utf-8")

    assert recover_pending_commit(report_path) == "discarded"
    assert report_path.read_text(encoding="utf-8") == "# previous canonical report\n"
    assert not pending_report.exists()
    assert not pending_sidecar.exists()


def test_recovery_is_a_no_op_on_a_clean_tree(tmp_path):
    report_path = tmp_path / "climate-monitor-2026-05-14.md"
    assert recover_pending_commit(report_path) == "clean"


def test_run_monitor_recovers_stale_pending_files_before_committing(tmp_path):
    root = tmp_path / "recovery"
    source_dir = root / "sources"
    source_dir.mkdir(parents=True)
    stale_pending = source_dir / "climate-monitor-2026-05-14.md.pending"
    stale_pending.write_text("# stale staging\n", encoding="utf-8")

    source_config = root / "sources.yaml"
    run_config = root / "run_config.yaml"
    manifest = root / "manifest.json"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=root / "wiki", state=root / "state")
    _write_manifest(manifest, count=1)

    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 14),
        manifest_fixture_path=manifest,
        state_dir=root / "state",
        sync=False,
    )

    assert not stale_pending.exists()
    verify_semantic_sidecar(Path(result.report_path))


def test_repository_offline_fixtures_produce_a_verifiable_pair(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source_config = root / "monitoring" / "supranational_sources.yaml"
    manifest = root / "monitoring" / "fixtures" / "web_listening_manifest_sample.json"
    research = root / "monitoring" / "fixtures" / "research_results_sample.json"
    run_config = tmp_path / "run_config.yaml"
    source_dir = tmp_path / "sources"
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=tmp_path / "wiki", state=tmp_path / "state")

    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 14),
        manifest_fixture_path=manifest,
        research_fixture_path=research,
        state_dir=tmp_path / "state",
        sync=False,
    )

    payload = verify_semantic_sidecar(Path(result.report_path))
    assert payload["article_count"] == len(result.items) >= 1
    provenance = {article["semantics_provenance"] for article in payload["articles"]}
    assert "agent_bundle" in provenance
    authored = next(
        article for article in payload["articles"] if article["semantics_provenance"] == "agent_bundle"
    )
    assert authored["semantics"]["categories"] == ["Supervision & Disclosure"]
    assert authored["semantics"]["keywords"] == [
        "supervisory review",
        "climate scenario",
        "insurance supervision",
    ]


def test_sidecar_json_schema_is_a_valid_structural_contract(tmp_path):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    schemas = Path(__file__).resolve().parents[1] / "monitoring" / "schemas"
    sidecar_schema = json.loads((schemas / "article_semantic_sidecar_v1.schema.json").read_text(encoding="utf-8"))
    bundle_schema = json.loads((schemas / "article_semantic_bundle_v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(sidecar_schema)

    registry = Registry().with_resource(bundle_schema["$id"], Resource.from_contents(bundle_schema))
    validator = Draft202012Validator(sidecar_schema, registry=registry)

    result, _ = _run(tmp_path)
    payload = json.loads(semantic_sidecar_path(Path(result.report_path)).read_text(encoding="utf-8"))
    validator.validate(payload)

    payload["articles"][0]["semantics_provenance"] = "handwritten"
    with pytest.raises(Exception):
        validator.validate(payload)


def test_sidecar_schema_pins_the_immutable_taxonomy_identity():
    schemas = Path(__file__).resolve().parents[1] / "monitoring" / "schemas"
    schema = json.loads((schemas / "article_semantic_sidecar_v1.schema.json").read_text(encoding="utf-8"))
    taxonomy = schema["properties"]["taxonomy"]["properties"]

    assert taxonomy["taxonomy_id"]["const"] == "climate-actuarial-v1"
    assert taxonomy["sha256"]["const"] == DEFAULT_TAXONOMY_SHA256
    assert schema["properties"]["schema_version"]["const"] == "article-semantic-sidecar.v1"
    assert schema["additionalProperties"] is False


# --------------------------------------------------------------------------
# Reviewer regressions (iteration 2)
#   HIGH-1 / residual: benign per-item oddities (blank URL, sparse/unvalidatable
#   bundle) become per-item DROPS, never a whole-run abort that writes zero
#   artifacts. The Markdown and the sidecar are built over the same dropped
#   set, so they stay 1:1.
#   HIGH-2: a URL containing a space must round-trip through commit + verify
#   (1:1, byte-stable).
# --------------------------------------------------------------------------


def test_run_monitor_publishes_report_and_excludes_blank_url_research_item(tmp_path):
    # A blank-URL research item previously made article_identity raise inside
    # commit_report_with_semantics, aborting the entire run and writing zero
    # artifacts. It must now be dropped per-item and the rest of the report
    # published (Markdown + sidecar 1:1 over the same dropped set).
    root = tmp_path / "blank"
    root.mkdir(parents=True, exist_ok=True)
    source_config = root / "sources.yaml"
    run_config = root / "run_config.yaml"
    manifest = root / "manifest.json"
    research = root / "research.json"
    source_dir = root / "sources"
    wiki_dir = root / "wiki"
    state = root / "state"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=wiki_dir, state=state, max_items=12)
    _write_manifest(manifest, count=1)
    research.write_text(
        json.dumps(
            [
                {
                    "title": "Valid climate insurance capital study",
                    "url": "https://example.org/valid-study",
                    "summary": "Insurance supervisors discuss climate risk and capital adequacy.",
                    "source_name": "Example Research",
                    "published": "2026-05-01",
                },
                {
                    "title": "Blank URL climate insurance note",
                    "url": "",
                    "summary": "Climate and insurance capital requirements for supervision and disclosure.",
                    "source_name": "Example Research",
                    "published": "2026-05-01",
                },
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 14),
        manifest_fixture_path=manifest,
        research_fixture_path=research,
        state_dir=state,
        sync=False,
    )

    # The run did not abort: both the canonical Markdown and the sidecar exist.
    assert result.report_path is not None
    report_path = Path(result.report_path)
    report_text = report_path.read_text(encoding="utf-8")
    assert semantic_sidecar_path(report_path).exists()
    payload = json.loads(semantic_sidecar_path(report_path).read_text(encoding="utf-8"))

    # The valid items are published; the blank-URL item is excluded.
    assert "Valid climate insurance capital study" in report_text
    assert "Climate supervision update 1" in report_text
    # The blank-URL item is NOT published as an article (it only appears in the
    # drop note below, which is expected). It must be absent from the sidecar.
    assert not any(
        article["title"] == "Blank URL climate insurance note"
        for article in payload["articles"]
    )

    # The exclusion is recorded as a drop note.
    assert any(
        "Blank URL climate insurance note" in note or "no canonical URL" in note
        for note in result.dedup_notes
    )

    # The sidecar is 1:1 with the rendered Markdown (no orphan entry).
    rendered = rendered_article_urls(report_text)
    sidecar_urls = [article["url"] for article in payload["articles"]]
    assert sidecar_urls == rendered
    assert len(payload["articles"]) == 2
    verify_semantic_sidecar(report_path)


def test_select_semantic_articles_drops_blank_url_and_sparse_items():
    taxonomy = load_article_taxonomy()
    blank = _item(url="   ", title="No URL")
    sparse = _item(
        title="Sparse",
        url="https://example.org/sparse",
        categories=(),
        climate_signal="none",
        actuarial_signal="none",
        climate_related=False,
        actuarial_related=False,
    )
    good = _item()

    kept, notes = select_semantic_articles([blank, sparse, good], taxonomy=taxonomy)
    assert [item.title for item in kept] == ["Climate supervision update"]
    assert len(notes) == 2
    assert any("No URL" in note for note in notes)
    assert any("Sparse" in note for note in notes)


def test_select_semantic_articles_keeps_everything_when_all_valid():
    items = [_item(), _item(title="Second", url="https://example.org/second")]
    kept, notes = select_semantic_articles(items)
    assert len(kept) == 2
    assert notes == []


def test_article_with_space_in_url_round_trips_through_commit_and_verify(tmp_path):
    # HIGH-2: a URL containing a space previously truncated at the first space
    # (the old \S+ regex) and failed the 1:1 check. The non-greedy (.+?) regex
    # must capture the full URL and round-trip it byte-stable into the sidecar.
    report_path = tmp_path / "climate-monitor-2026-05-14.md"
    spaced_url = "https://www.iais.org/climate supervision"
    item = _item(url=spaced_url)
    report_text = f"# report\n**URL:** {spaced_url} <br>\n"

    commit = commit_report_with_semantics(
        report_path=report_path,
        report_date=date(2026, 5, 14),
        report_text=report_text,
        items=[item],
    )
    sidecar_path = semantic_sidecar_path(report_path)
    assert sidecar_path.exists()
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    # The full URL (spaces included) round-trips unchanged into the sidecar.
    assert payload["articles"][0]["url"] == spaced_url
    rendered = rendered_article_urls(report_text)
    assert rendered == [spaced_url]
    assert [article["url"] for article in payload["articles"]] == rendered
    # Verification accepts the committed pair (1:1, byte-stable).
    verified = verify_semantic_sidecar(report_path)
    assert verified["article_count"] == 1
    # A byte-identical re-run is deterministic.
    report_path2 = tmp_path / "climate-monitor-2026-05-14-b.md"
    commit2 = commit_report_with_semantics(
        report_path=report_path2,
        report_date=date(2026, 5, 14),
        report_text=report_text,
        items=[item],
    )
    assert commit2["report_sha256"] == commit["report_sha256"]


def test_rendered_article_urls_accepts_lf_and_crlf_without_broadening_shape():
    lf_report = (
        "**URL:** https://example.test/first <br>\n"
        "**URL:** https://example.test/second <br>\n"
    )
    crlf_report = lf_report.replace("\n", "\r\n")

    expected = ["https://example.test/first", "https://example.test/second"]
    assert rendered_article_urls(lf_report) == expected
    assert rendered_article_urls(crlf_report) == expected

    assert rendered_article_urls(" **URL:** https://example.test/first <br>\n") == []
    assert rendered_article_urls("**URL:** https://example.test/first <br> \n") == []
    assert rendered_article_urls("**URL:** https://example.test/first <br />\n") == []


def test_verify_accepts_crlf_report_bound_to_matching_sidecar(tmp_path):
    report_path = tmp_path / "climate-monitor-2026-05-14.md"
    report_text = (
        "# report\r\n"
        "**URL:** https://www.iais.org/climate-supervision <br>\r\n"
    )
    report_bytes = report_text.encode("utf-8")
    report_path.write_bytes(report_bytes)
    payload = build_sidecar_payload(
        report_date=date(2026, 5, 14),
        report_filename=report_path.name,
        report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        items=[_item()],
        taxonomy=load_article_taxonomy(),
    )
    semantic_sidecar_path(report_path).write_bytes(serialize_sidecar(payload))

    verified = verify_semantic_sidecar(report_path)

    assert [article["url"] for article in verified["articles"]] == [
        "https://www.iais.org/climate-supervision"
    ]
