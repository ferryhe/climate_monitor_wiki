from __future__ import annotations

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
from climate_monitor.semantic_bundle import article_identity, semantic_sidecar_path
from climate_monitor.taxonomy import DEFAULT_TAXONOMY_ID, DEFAULT_TAXONOMY_SHA256
from climate_monitor.weekly_monitor.authoring_contract import (
    AUTHORING_CONTRACT_VERSION,
    AUTHORING_RESPONSE_SCHEMA_VERSION,
    AuthoringContractError,
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
    report_path.write_text("# previous canonical report\n", encoding="utf-8")
    sidecar_path.write_text("previous sidecar\n", encoding="utf-8")
    report_before = report_path.read_bytes()
    sidecar_before = sidecar_path.read_bytes()

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

    assert report_path.read_bytes() == report_before
    assert sidecar_path.read_bytes() == sidecar_before
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
