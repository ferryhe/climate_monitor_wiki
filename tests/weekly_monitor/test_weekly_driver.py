from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest
from jsonschema import Draft202012Validator

from climate_monitor.models import CandidateItem
from climate_monitor.orchestrator import run_monitor
from climate_monitor.semantic_bundle import article_identity, semantic_sidecar_path
from climate_monitor.taxonomy import DEFAULT_TAXONOMY_ID, DEFAULT_TAXONOMY_SHA256
from climate_monitor.weekly_monitor.authoring_contract import (
    AUTHORING_CONTRACT_VERSION,
    AUTHORING_CONTRACT_VERSION_V2,
    AUTHORING_REQUEST_SCHEMA_VERSION_V2,
    AUTHORING_RESPONSE_SCHEMA_VERSION,
    AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
    AuthoringContractError,
    build_authoring_request,
)
from climate_monitor.weekly_monitor.driver import DRIVER_VERSION, run_weekly_monitor
from climate_monitor.weekly_monitor.prompt_loader import load_weekly_monitor_prompt
from climate_monitor.weekly_monitor.provenance import PROVENANCE_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[2]
JOB_ROOT = ROOT / "monitoring" / "jobs" / "weekly-climate-monitor-08h"


def _item(**overrides) -> CandidateItem:
    payload = {
        "title": "Climate supervision update",
        "url": "https://www.iais.org/climate-supervision",
        "summary": "Initial source summary.",
        "source_name": "IAIS",
        "lane": "website",
        "climate_related": True,
        "actuarial_related": True,
        "climate_signal": "physical_risk",
        "actuarial_signal": "insurance_risk",
        "topics": ("climate", "insurance", "capital"),
        "categories": ("Physical Risk", "Insurance Risk"),
        "keywords": ("climate", "insurance", "capital"),
    }
    payload.update(overrides)
    return CandidateItem(**payload)


def _bundle(
    *,
    summary: str = "IAIS describes climate supervision implications for insurers.",
    categories: list[str] | None = None,
    keywords: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "article-semantic-bundle.v1",
        "taxonomy_id": DEFAULT_TAXONOMY_ID,
        "taxonomy_sha256": DEFAULT_TAXONOMY_SHA256,
        "summary": summary,
        "categories": categories or ["Supervision & Disclosure"],
        "keywords": keywords
        or ["supervisory review", "climate scenario", "insurance supervision"],
    }


def _response(items: list[CandidateItem]) -> dict[str, object]:
    return {
        "schema_version": AUTHORING_RESPONSE_SCHEMA_VERSION,
        "contract_version": AUTHORING_CONTRACT_VERSION,
        "article_count": len(items),
        "articles": [
            {"article_id": article_identity(item), "semantics": _bundle()}
            for item in items
        ],
    }


def _write_source_config(path: Path) -> None:
    path.write_text(
        dedent(
            """
            sources:
              - key: iais
                abbreviation: IAIS
                full_name: International Association of Insurance Supervisors
                url: https://www.iais.org/
            """
        ).strip(),
        encoding="utf-8",
    )


def _write_run_config(path: Path, *, source_dir: Path, wiki_dir: Path, state: Path) -> None:
    path.write_text(
        f"""
report_title: Weekly Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate, flood, wildfire]
actuarial_keywords: [insurance, supervision, capital]
research_lane:
  lookback_days: 30
  queries: []
output:
  source_dir: {source_dir.as_posix()}
  wiki_dir: {wiki_dir.as_posix()}
  write_empty_report: false
dedupe:
  url_tracking_path: {(state / "seen_urls.json").as_posix()}
  title_tracking_path: {(state / "seen_titles.json").as_posix()}
""".strip(),
        encoding="utf-8",
    )


def _write_manifest(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "web-listening-manifest.v1",
                "source": {"source_id": "iais", "site_name": "IAIS"},
                "discovered_items": [
                    {
                        "item_id": "iais-1",
                        "item_type": "page",
                        "url": "https://www.iais.org/climate-supervision",
                        "title": "Climate supervision update",
                        "summary": "Initial climate insurance capital summary.",
                        "status": "new",
                        "observed_at": "2026-05-18T08:00:00Z",
                    }
                ],
                "downloaded_assets": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_run_monitor_rejects_partial_provenance_before_writing_artifacts(tmp_path):
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    manifest = tmp_path / "manifest.json"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=wiki_dir, state=state)
    _write_manifest(manifest)

    with pytest.raises(ValueError, match="provenance metadata is incomplete"):
        run_monitor(
            source_config_path=source_config,
            run_config_path=run_config,
            report_date=date(2026, 5, 18),
            manifest_fixture_path=manifest,
            state_dir=state,
            prompt_provenance={
                "id": "weekly_monitor",
                "version": "v1",
                "sha256": load_weekly_monitor_prompt().sha256,
            },
        )

    assert not source_dir.exists()
    assert not wiki_dir.exists()


def test_weekly_driver_fails_before_artifacts_seen_state_or_sync_on_invalid_authoring(tmp_path):
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    manifest = tmp_path / "manifest.json"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"
    state = tmp_path / "state"
    authoring = tmp_path / "invalid_authoring_response.json"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=wiki_dir, state=state)
    _write_manifest(manifest)
    invalid_response = _response([_item()])
    invalid_response["articles"][0]["semantics"]["categories"] = ["Unknown Category"]
    authoring.write_text(json.dumps(invalid_response, indent=2) + "\n", encoding="utf-8")

    state.mkdir()
    seen_urls = state / "seen_urls.json"
    seen_titles = state / "seen_titles.json"
    seen_urls.write_text('["https://existing.example/item"]\n', encoding="utf-8")
    seen_titles.write_text('["existing title"]\n', encoding="utf-8")
    seen_urls_before = seen_urls.read_bytes()
    seen_titles_before = seen_titles.read_bytes()

    source_dir.mkdir()
    report_path = source_dir / "climate-monitor-2026-05-18.md"
    sidecar_path = semantic_sidecar_path(report_path)

    with pytest.raises(AuthoringContractError):
        run_weekly_monitor(
            source_config_path=source_config,
            run_config_path=run_config,
            report_date=date(2026, 5, 18),
            manifest_fixture_path=manifest,
            state_dir=state,
            authoring_response_path=authoring,
            sync=True,
            repository_commit_sha="a" * 40,
        )

    assert not report_path.exists()
    assert not sidecar_path.exists()
    assert seen_urls.read_bytes() == seen_urls_before
    assert seen_titles.read_bytes() == seen_titles_before
    assert not wiki_dir.exists()


def test_weekly_driver_json_result_records_safe_provenance(tmp_path):
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    manifest = tmp_path / "manifest.json"
    authoring = tmp_path / "authoring_response.json"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=wiki_dir, state=state)
    _write_manifest(manifest)
    item = _item()
    authoring.write_text(json.dumps(_response([item]), indent=2) + "\n", encoding="utf-8")

    result = run_weekly_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 18),
        manifest_fixture_path=manifest,
        state_dir=state,
        authoring_response_path=authoring,
        sync=False,
        repository_commit_sha="b" * 40,
        model_provider="openai",
        model="gpt-5-mini",
        temperature=0.2,
        max_output_tokens=4000,
    )
    payload = json.loads(result.to_json())
    provenance = payload["provenance"]
    report_path = Path(result.report_path)
    sidecar_path = semantic_sidecar_path(report_path)
    encoded = json.dumps(payload)

    assert provenance["schema_version"] == PROVENANCE_SCHEMA_VERSION
    assert provenance["repository"]["commit_sha"] == "b" * 40
    assert provenance["prompt"]["id"] == "weekly_monitor"
    assert provenance["prompt"]["version"] == "v1"
    assert provenance["prompt"]["sha256"] == load_weekly_monitor_prompt().sha256
    assert provenance["driver"]["version"] == DRIVER_VERSION
    assert provenance["driver"]["contract_version"] == AUTHORING_CONTRACT_VERSION
    assert provenance["taxonomy"] == {
        "taxonomy_id": DEFAULT_TAXONOMY_ID,
        "sha256": DEFAULT_TAXONOMY_SHA256,
    }
    assert provenance["report"] == {
        "filename": report_path.name,
        "sha256": result.report_sha256,
    }
    assert provenance["semantic_sidecar"] == {
        "filename": sidecar_path.name,
        "sha256": hashlib.sha256(sidecar_path.read_bytes()).hexdigest(),
    }
    assert provenance["final_articles"] == {
        "count": 1,
        "identities": [article_identity(item)],
    }
    assert provenance["model"] == {
        "provider": "openai",
        "model": "gpt-5-mini",
        "settings": {"max_output_tokens": 4000, "temperature": 0.2},
    }
    Draft202012Validator(
        json.loads(
            (JOB_ROOT / "contracts" / "provenance.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
    ).validate(provenance)
    assert str(tmp_path) not in encoded
    assert "sk-test-secret" not in encoded


def test_cli_production_weekly_requires_authoring_mode_at_parse_time():
    """Issue #87: --production-weekly now requires --authoring-mode
    {prepare,finalize}; the legacy --authoring-response/--article-evidence/
    --stats triple is no longer accepted because the same-run chain must
    build its own bundle from #67 outcome + manifest + Pillar B."""
    completed = subprocess.run(
        [sys.executable, "scripts/run_climate_monitor.py", "--production-weekly"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "--production-weekly requires --authoring-mode" in completed.stderr
    assert "production weekly driver requires an authoring response file" not in completed.stderr


def test_cli_production_weekly_path_uses_repo_prompt_without_printing_prompt_or_secrets(tmp_path):
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    manifest = tmp_path / "manifest.json"
    authoring = tmp_path / "authoring_response.json"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=wiki_dir, state=state)
    _write_manifest(manifest)
    authoring.write_text(json.dumps(_response([_item()]), indent=2) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = "sk-test-secret-not-output"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_climate_monitor.py",
            "--production-weekly",
            "--source-config",
            str(source_config),
            "--run-config",
            str(run_config),
            "--date",
            "2026-05-18",
            "--manifest-fixture",
            str(manifest),
            "--state-dir",
            str(state),
            "--authoring-response",
            str(authoring),
            "--model-provider",
            "openai",
            "--model",
            "gpt-5-mini",
            "--temperature",
            "0.2",
            "--max-output-tokens",
            "4000",
            "--no-sync",
            "--no-update-seen-state",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    payload = json.loads(completed.stdout)
    encoded = json.dumps(payload)
    assert payload["provenance"]["prompt"]["sha256"] == load_weekly_monitor_prompt().sha256
    assert payload["provenance"]["model"]["provider"] == "openai"
    assert "sk-test-secret-not-output" not in completed.stdout
    assert "You are the Weekly Climate" not in completed.stdout
    assert str(tmp_path) not in encoded


def test_weekly_driver_v2_path_emits_request_and_validates_response(tmp_path):
    """AC-6: the production driver is the single caller of the request emitter
    and the response validator/apply. v2 evidence is required and the response
    is bound to it."""
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    manifest = tmp_path / "manifest.json"
    authoring = tmp_path / "authoring_response.json"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=wiki_dir, state=state)
    _write_manifest(manifest)

    item = _item()
    body = "Honest climate insurance article body."
    digest = hashlib.sha256(body.encode()).hexdigest()
    article_evidence = {
        "records": [
            {
                "article_id": article_identity(item),
                "requested_url": item.url,
                "final_url": item.url,
                "title": item.title,
                "status": "ok",
                "attempts": [{"tool": "http"}],
                "selected_method": "http",
                "content_type": "text/html",
                "content_ref": f"memory:{digest}",
                "content_hash": digest,
                "summary_basis": "page",
                "title_basis": "upstream_artifact",
                "display_pillar": "A",
                "origins": [{"pillar": "A", "source": item.source_name, "url": item.url}],
                "extra": {},
            }
        ]
    }
    stats = {
        "total": 1,
        "updated": 1,
        "unchanged": 0,
        "blocked": 0,
        "failed": 0,
        "unresolved": 0,
    }
    authoring.write_text(json.dumps(_response([item])) + "\n", encoding="utf-8")
    with pytest.raises(AuthoringContractError, match="v2 evidence path requires a v2"):
        # The default _response() returns a v1 envelope; the v2 driver must
        # reject mismatched schemas before any artifact is written.
        run_weekly_monitor(
            source_config_path=source_config,
            run_config_path=run_config,
            report_date=date(2026, 5, 18),
            manifest_fixture_path=manifest,
            state_dir=state,
            authoring_response_path=authoring,
            sync=False,
            repository_commit_sha="c" * 40,
            article_evidence=article_evidence,
            stats=stats,
        )

    # Now build a matching v2 response and confirm the driver emits and
    # validates without touching the production checkout.
    from climate_monitor.weekly_monitor.authoring_contract import (
        build_authoring_request,
    )
    from climate_monitor.weekly_monitor.prompt_loader import load_weekly_monitor_prompt
    prompt = load_weekly_monitor_prompt()
    request = build_authoring_request(
        report_date=date(2026, 5, 18),
        items=[item],
        prompt=prompt,
        article_evidence=article_evidence,
        stats=stats,
    )
    response_article = json.loads(json.dumps(request["articles"][0]))
    response_article.update(
        {
            "relevant": True,
            "summary": "Honest content summary.",
            "summary_basis": "article_content",
            "evidence_hash": digest,
            "categories": ["Supervision & Disclosure"],
            "keywords": ["climate", "insurance", "capital"],
        }
    )
    v2_response = {
        "schema_version": AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
        "contract_version": AUTHORING_CONTRACT_VERSION_V2,
        "request_sha256": request["request_sha256"],
        "article_count": 1,
        "articles": [response_article],
        "executive_summary": "1 total; 1 updated; 0 unchanged; 0 blocked; 0 failed; 0 unresolved.",
        "stats": stats,
    }
    authoring.write_text(json.dumps(v2_response) + "\n", encoding="utf-8")
    result = run_weekly_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 18),
        manifest_fixture_path=manifest,
        state_dir=state,
        authoring_response_path=authoring,
        sync=False,
        repository_commit_sha="d" * 40,
        article_evidence=article_evidence,
        stats=stats,
    )
    payload = json.loads(result.to_json())
    provenance = payload["provenance"]
    assert provenance["driver"]["contract_version"] == AUTHORING_CONTRACT_VERSION_V2


def test_cli_production_weekly_path_forwards_v2_evidence_to_driver(tmp_path):
    """AC-6: the production CLI actually wires --article-evidence + --stats
    through to the driver's v2 request emitter and validator. Without this,
    the CLI silently falls back to v1 even though the driver supports v2."""
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    manifest = tmp_path / "manifest.json"
    authoring = tmp_path / "authoring_response.json"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"
    state = tmp_path / "state"
    article_evidence_path = tmp_path / "article_evidence.json"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=wiki_dir, state=state)
    _write_manifest(manifest)

    item = _item(title="Climate insurance supervision update for actuarial risk")
    body = "Honest climate insurance article body."
    digest = hashlib.sha256(body.encode()).hexdigest()
    aid = article_identity(item)
    article_evidence = {
        "schema_version": "article-evidence.v1",
        "report_date": "2026-05-18",
        "generated_at": "",
        "dependency_status": {},
        "record_count": 1,
        "records": [
            {
                "article_id": aid,
                "requested_url": item.url,
                "final_url": item.url,
                "title": item.title,
                "status": "ok",
                "attempts": [{"tool": "http"}],
                "selected_method": "http",
                "content_type": "text/html",
                "content_ref": f"memory:{digest}",
                "content_hash": digest,
                "summary_basis": "page",
                "title_basis": "upstream_artifact",
                "display_pillar": "A",
                "origins": [
                    {"pillar": "A", "source": item.source_name, "url": item.url}
                ],
                "extra": {},
            }
        ],
        "artifact_digest": "0" * 64,
    }
    article_evidence_path.write_text(json.dumps(article_evidence), encoding="utf-8")

    stats = {
        "total": 1,
        "updated": 1,
        "unchanged": 0,
        "blocked": 0,
        "failed": 0,
        "unresolved": 0,
    }
    request = build_authoring_request(
        report_date=date(2026, 5, 18),
        items=[item],
        prompt=load_weekly_monitor_prompt(),
        article_evidence=article_evidence,
        stats=stats,
    )
    response_article = json.loads(json.dumps(request["articles"][0]))
    response_article.update(
        {
            "relevant": True,
            "summary": "Honest content summary.",
            "summary_basis": "article_content",
            "evidence_hash": digest,
            "categories": ["Supervision & Disclosure"],
            "keywords": ["climate", "insurance", "capital"],
        }
    )
    v2_response = {
        "schema_version": AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
        "contract_version": AUTHORING_CONTRACT_VERSION_V2,
        "request_sha256": request["request_sha256"],
        "article_count": 1,
        "articles": [response_article],
        "executive_summary": "1 total; 1 updated; 0 unchanged; 0 blocked; 0 failed; 0 unresolved.",
        "stats": stats,
    }
    authoring.write_text(json.dumps(v2_response), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_climate_monitor.py",
            "--production-weekly",
            "--source-config",
            str(source_config),
            "--run-config",
            str(run_config),
            "--date",
            "2026-05-18",
            "--manifest-fixture",
            str(manifest),
            "--state-dir",
            str(state),
            "--source-dir",
            str(source_dir),
            "--wiki-dir",
            str(wiki_dir),
            "--authoring-response",
            str(authoring),
            "--article-evidence",
            str(article_evidence_path),
            "--stats",
            json.dumps(stats),
            "--no-sync",
            "--no-update-seen-state",
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["provenance"]["driver"]["contract_version"] == AUTHORING_CONTRACT_VERSION_V2


# ---------------------------------------------------------------------------
# Issue #87 AC-2: the v2 authoring response carries the canonical 6-key stats
# dict (total/updated/unchanged/blocked/failed/unresolved) and the driver
# exposes the validated mapping on ``MonitorRunResult.stats``. The mapping
# ``total == updated + unchanged + blocked + failed + unresolved`` is enforced
# by the validator before any artifact is written.
# ---------------------------------------------------------------------------


def _v2_evidence_record(
    item: CandidateItem,
    *,
    body: str,
    summary_basis: str = "article_content",
    extra: dict | None = None,
    status: str = "ok",
    origins: tuple[dict, ...] | None = None,
) -> dict:
    digest = hashlib.sha256(body.encode()).hexdigest()
    record = {
        "article_id": article_identity(item),
        "requested_url": item.url,
        "final_url": item.url,
        "title": item.title,
        "status": status,
        "attempts": [{"tool": "http"}],
        "selected_method": "http" if status == "ok" else None,
        "content_type": "text/html" if status == "ok" else None,
        "content_ref": f"memory:{digest}" if status == "ok" else None,
        "content_hash": digest if status == "ok" else None,
        "summary_basis": summary_basis,
        "title_basis": "upstream_artifact",
        "display_pillar": "A",
        "origins": list(origins)
        if origins is not None
        else [{"pillar": "A", "source": item.source_name, "url": item.url}],
        "extra": extra or {},
    }
    if status != "ok":
        record["failure_reason"] = "auth_required"
    return record


def test_v2_authoring_response_exposes_canonical_57_42_15_split(tmp_path):
    """AC-2: feeding a v2 response with literal counts {updated:33, unchanged:9,
    blocked:14, failed:1, unresolved:0, total:57} makes the driver expose the
    validated mapping on ``MonitorRunResult.stats``. The canonical
    ``57 / 42 succeeded (33 updated + 9 unchanged) / 15 failed (14 blocked +
    1 failed)`` split is what the orchestrator and downstream consumers read.

    The v2 authoring contract enforces the deterministic sum
    ``total == updated + unchanged + blocked + failed + unresolved`` before any
    artifact is written.

    The orchestrator keeps ``max_items_per_report`` of the manifest-fixture
    candidates; we author a single kept article here and bind the response's
    ``stats`` dict to the canonical 57-record split. The split is what the
    driver validates, exposes, and that downstream consumers (Hermes
    wrappers, 09:00 climate_delivery, AC-5 dry-run) read.
    """
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    manifest = tmp_path / "manifest.json"
    authoring = tmp_path / "authoring_response.json"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_source_config(source_config)
    _write_run_config(run_config, source_dir=source_dir, wiki_dir=wiki_dir, state=state)
    _write_manifest(manifest)

    stats = {
        "total": 57,
        "updated": 33,
        "unchanged": 9,
        "blocked": 14,
        "failed": 1,
        "unresolved": 0,
    }

    # One kept item (the manifest fixture only declares one). The kept set
    # binds to the request via article_identity; the canonical 6-key stats
    # dict is validated independently by the driver.
    items = [_item()]
    records = [_v2_evidence_record(items[0], body="Body 0")]
    article_evidence = {"records": records}
    request = build_authoring_request(
        report_date=date(2026, 5, 18),
        items=items,
        prompt=load_weekly_monitor_prompt(),
        article_evidence=article_evidence,
        stats=stats,
    )

    response_article = json.loads(json.dumps(request["articles"][0]))
    response_article.update(
        {
            "relevant": True,
            "summary": "Honest content summary.",
            "summary_basis": "article_content",
            "evidence_hash": items[0].url
            and hashlib.sha256(b"Body 0").hexdigest(),
            "categories": ["Supervision & Disclosure"],
            "keywords": ["climate", "insurance", "capital"],
        }
    )

    v2_response = {
        "schema_version": AUTHORING_RESPONSE_SCHEMA_VERSION_V2,
        "contract_version": AUTHORING_CONTRACT_VERSION_V2,
        "request_sha256": request["request_sha256"],
        "article_count": 1,
        "articles": [response_article],
        "executive_summary": (
            f"{stats['total']} total; {stats['updated']} updated; "
            f"{stats['unchanged']} unchanged; {stats['blocked']} blocked; "
            f"{stats['failed']} failed; {stats['unresolved']} unresolved."
        ),
        "stats": stats,
    }
    authoring.write_text(json.dumps(v2_response), encoding="utf-8")

    # Mutation guard: the driver fails closed when the deterministic sum is
    # tampered with, even though all 6 keys are present.
    with pytest.raises(AuthoringContractError, match="stats.total"):
        tampered = copy.deepcopy(v2_response)
        tampered["stats"] = {**stats, "total": stats["total"] + 1}
        tampered_path = tmp_path / "tampered.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        run_weekly_monitor(
            source_config_path=source_config,
            run_config_path=run_config,
            report_date=date(2026, 5, 18),
            manifest_fixture_path=manifest,
            state_dir=state,
            authoring_response_path=tampered_path,
            sync=False,
            repository_commit_sha="e" * 40,
            article_evidence=article_evidence,
            stats=stats,
        )

    result = run_weekly_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 18),
        manifest_fixture_path=manifest,
        state_dir=state,
        authoring_response_path=authoring,
        sync=False,
        repository_commit_sha="f" * 40,
        article_evidence=article_evidence,
        stats=stats,
    )

    payload = json.loads(result.to_json())

    # The driver's exposed stats are exactly the canonical 6-key dict the
    # response carried; the orchestrator must not silently drop the keys.
    assert payload["stats"] == {
        "total": 57,
        "updated": 33,
        "unchanged": 9,
        "blocked": 14,
        "failed": 1,
        "unresolved": 0,
    }
    # 57 / 42 succeeded (33 updated + 9 unchanged) / 15 failed
    # (14 blocked + 1 failed) with 0 unresolved (not double-counted).
    succeeded = payload["stats"]["updated"] + payload["stats"]["unchanged"]
    failed = payload["stats"]["blocked"] + payload["stats"]["failed"]
    assert succeeded == 42
    assert failed == 15
    assert payload["stats"]["total"] == succeeded + failed + payload["stats"]["unresolved"]
    assert payload["stats"]["total"] == 57
    assert payload["stats"]["unresolved"] == 0
