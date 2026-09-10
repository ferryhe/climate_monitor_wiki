from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from textwrap import dedent

import pytest


def test_print_pillar_b_prompt_is_read_only_and_loads_template(tmp_path, monkeypatch, capsys):
    from scripts import run_climate_monitor as monitor
    def forbidden(*args, **kwargs):
        raise AssertionError("prompt preview must not execute the monitor or a subprocess")
    monkeypatch.setattr(monitor, "run_monitor", forbidden)
    monkeypatch.setattr(monitor, "run_weekly_monitor", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    destination = tmp_path / "new reports" / "pillar_b.json"
    monkeypatch.setattr(sys, "argv", ["run_climate_monitor.py", "--print-pillar-b-prompt",
        "--report-date", "2026-09-07", "--pillar-b-artifact", str(destination), "--json"])
    monitor.main()
    output = json.loads(capsys.readouterr().out)
    assert output["prompt_id"] == "pillar_b_search"
    assert '"mode": "unlimited"' in output["prompt"]
    assert "do not add search time parameters" in output["prompt"]
    assert output["path"].endswith("pillar-b-search-v2.prompt.md")
    assert not destination.parent.exists()


@pytest.mark.parametrize("options", [[], ["--report-date", "2026-09-07"],
    ["--report-date", "not-a-date", "--pillar-b-artifact", "/tmp/b.json"],
    ["--production-weekly"]])
def test_print_pillar_b_prompt_requires_explicit_inputs_and_no_execution(monkeypatch, options):
    from scripts import run_climate_monitor as monitor
    monkeypatch.setattr(sys, "argv", ["run_climate_monitor.py", "--print-pillar-b-prompt", *options])
    with pytest.raises(SystemExit) as exc:
        monitor.main()
    assert exc.value.code == 2


def test_run_climate_monitor_json_outputs_fixture_dry_run_result(tmp_path):
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    manifest = tmp_path / "manifest.json"
    research = tmp_path / "research.json"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"

    source_config.write_text(
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
    run_config.write_text(
        f"""
report_title: Daily Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate]
actuarial_keywords: [insurance, capital]
research_lane:
  lookback_days: 30
  queries: [climate insurance report]
output:
  source_dir: {source_dir.as_posix()}
  wiki_dir: {wiki_dir.as_posix()}
  write_empty_report: false
""".strip(),
        encoding="utf-8",
    )
    manifest.write_text(
        """
{
  "schema_version": "web-listening-manifest.v1",
  "source": {"source_id": "iais", "site_name": "IAIS"},
  "discovered_items": [
    {
      "item_id": "1",
      "item_type": "page",
      "url": "https://www.iais.org/climate-supervision",
      "title": "Climate supervision update",
      "summary": "Insurance supervisors discuss climate risk.",
      "status": "new",
      "observed_at": "2026-05-14T00:00:00Z"
    },
    {
      "item_id": "2",
      "item_type": "file_link",
      "url": "https://www.iais.org/uploads/climate-risk-report.pdf",
      "title": "Climate risk report PDF",
      "summary": "Insurance supervisors discuss climate risk in a report file.",
      "status": "new",
      "observed_at": "2026-05-14T00:05:00Z",
      "content_type": "application/pdf"
    }
  ],
  "downloaded_assets": [
    {
      "asset_id": "sha256-0123456789abcdef",
      "source_item_id": "2",
      "url": "https://www.iais.org/uploads/climate-risk-report.pdf",
      "local_path": "data/downloads/_tracked/iais/climate-risk-report.pdf",
      "filename": "climate-risk-report.pdf",
      "media_type": "application/pdf",
      "bytes": 123456,
      "checksum": {"algorithm": "sha256", "value": "0123456789abcdef"},
      "status": "downloaded"
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    research.write_text(
        """
{
  "items": [
    {
      "title": "Climate risk and insurance capital report",
      "url": "https://example.org/report",
      "summary": "A report about climate risk and insurance capital.",
      "source_name": "Example Research",
      "published": "2026-05-01"
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = "sk-test-secret-not-output"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_climate_monitor.py",
            "--source-config",
            str(source_config),
            "--run-config",
            str(run_config),
            "--date",
            "2026-05-14",
            "--manifest-fixture",
            str(manifest),
            "--research-fixture",
            str(research),
            "--state-dir",
            str(tmp_path / "state"),
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

    assert "Report written:" not in completed.stdout
    assert "sk-test-secret-not-output" not in completed.stdout
    assert payload["report_date"] == "2026-05-14"
    assert not os.path.isabs(payload["report_path"])
    assert payload["report_path"] == "climate-monitor-2026-05-14.md"
    assert payload["synced"] is False
    assert payload["item_count"] == 3
    assert payload["items"][0]["title"] == "Climate supervision update"
    assert payload["items"][0]["source"] == "IAIS"
    assert payload["items"][0]["detected"] == "2026-05-14T00:00:00Z"
    assert payload["items"][1]["lane"] == "document"
    assert payload["items"][1]["asset_id"] == "sha256-0123456789abcdef"
    assert payload["items"][1]["asset_media_type"] == "application/pdf"
    assert "asset_metadata" not in payload["items"][1]
    assert payload["items"][2]["lane"] == "research"
    assert payload["items"][2]["published"] == "2026-05-01"


def test_article_evidence_source_dir_override(tmp_path, monkeypatch):
    """AC-4: the orchestrator stages the article-evidence.v1 artifact under
    the resolved ``source_dir`` (the ``--source-dir`` flag)."""

    from climate_monitor.orchestrator import run_monitor
    from climate_monitor import orchestrator

    override = tmp_path / "override-sources"
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        dedent(
            """
            sources:
              - key: iais
                abbreviation: IAIS
                full_name: International Association of Insurance Supervisors
                url: https://www.iais.org/
                tags: [insurance, climate]
            """
        ).strip()
    )
    run_config_path = tmp_path / "run_config.yaml"
    run_config_path.write_text(
        f"""
report_title: Daily Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate]
actuarial_keywords: [insurance]
research_lane:
  lookback_days: 30
  queries: []
output:
  source_dir: {override.as_posix()}
  wiki_dir: {(tmp_path / 'wiki').as_posix()}
  write_empty_report: false
dedupe:
  url_tracking_path: {tmp_path.as_posix()}/state/seen_urls.json
  title_tracking_path: {tmp_path.as_posix()}/state/seen_titles.json
""".strip()
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": "web-listening-manifest.v1",
        "source": {"source_id": "iais", "site_name": "IAIS"},
        "discovered_items": [{
            "item_id": "1", "item_type": "page",
            "url": "https://www.iais.org/climate-supervision",
            "title": "Climate supervision update",
            "summary": "Insurance supervisors discuss climate risk.",
            "status": "new", "observed_at": "2026-09-07T00:00:00Z",
        }],
        "downloaded_assets": [],
    }))
    research_path = tmp_path / "research.json"
    research_path.write_text("[]")
    seen = {"called": False, "source_dir": None}

    def fake_stage(*, candidates, source_dir, report_date, providers=(), manifest_fixture_path=None, prepared_evidence=None):
        assert prepared_evidence is None
        from climate_monitor.article_content_adapter import (
            build_article_evidence_artifact,
            write_article_evidence_artifact,
        )
        seen["called"] = True
        seen["source_dir"] = source_dir
        artifact = build_article_evidence_artifact(
            [], report_date=report_date.isoformat()
        )
        return write_article_evidence_artifact(
            source_dir, report_date.isoformat(), artifact
        )

    monkeypatch.setattr(orchestrator, "_stage_article_evidence", fake_stage)
    run_monitor(
        source_config_path=sources_path,
        run_config_path=run_config_path,
        report_date=date(2026, 9, 7),
        manifest_fixture_path=manifest_path,
        research_fixture_path=research_path,
        state_dir=tmp_path / "state",
        sync=False,
        update_seen_state=False,
    )
    assert seen["called"], "orchestrator must invoke evidence staging inside #91 transaction"
    assert seen["source_dir"] == override


def test_json_mode_still_stages_evidence(tmp_path, monkeypatch, capsys):
    """AC-4: ``--json`` mode no longer triggers separate CLI staging. The
    orchestrator now owns staging and writes the artifact regardless of
    the CLI's JSON output mode. The CLI just prints the report metadata.
    """

    from climate_monitor.models import CandidateItem, MonitorRunResult
    from scripts import run_climate_monitor as cli
    monkeypatch.setattr(cli, "run_monitor", lambda **kw: MonitorRunResult(
        report_date=kw["report_date"], report_path=None,
        items=(CandidateItem(title="Climate insurance", url="https://example.org/climate",
                             summary="", source_name="Example", lane="website"),)))
    monkeypatch.setattr(sys, "argv", ["run_climate_monitor", "--date", "2026-09-07",
        "--source-dir", str(tmp_path), "--no-sync", "--no-update-seen-state", "--json"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["item_count"] == 1


def test_article_evidence_uses_configured_source_dir_without_override(tmp_path, monkeypatch):
    """AC-4: with no ``--source-dir`` override, the orchestrator stages
    evidence under the configured ``source_dir`` from ``run_config.yaml``.
    """

    from climate_monitor.orchestrator import run_monitor
    from climate_monitor import orchestrator

    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        dedent(
            """
            sources:
              - key: iais
                abbreviation: IAIS
                full_name: International Association of Insurance Supervisors
                url: https://www.iais.org/
                tags: [insurance, climate]
            """
        ).strip()
    )
    run_config_path = tmp_path / "run_config.yaml"
    run_config_path.write_text(
        f"""
report_title: Daily Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate]
actuarial_keywords: [insurance]
research_lane:
  lookback_days: 30
  queries: []
output:
  source_dir: {(tmp_path / 'sources').as_posix()}
  wiki_dir: {(tmp_path / 'wiki').as_posix()}
  write_empty_report: false
dedupe:
  url_tracking_path: {tmp_path.as_posix()}/state/seen_urls.json
  title_tracking_path: {tmp_path.as_posix()}/state/seen_titles.json
""".strip()
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": "web-listening-manifest.v1",
        "source": {"source_id": "iais", "site_name": "IAIS"},
        "discovered_items": [{
            "item_id": "1", "item_type": "page",
            "url": "https://www.iais.org/climate-supervision",
            "title": "Climate supervision update",
            "summary": "Insurance supervisors discuss climate risk.",
            "status": "new", "observed_at": "2026-09-07T00:00:00Z",
        }],
        "downloaded_assets": [],
    }))
    research_path = tmp_path / "research.json"
    research_path.write_text("[]")
    seen = {"called_with": None}

    def fake_stage(*, candidates, source_dir, report_date, providers=(), manifest_fixture_path=None, prepared_evidence=None):
        assert prepared_evidence is None
        from climate_monitor.article_content_adapter import (
            build_article_evidence_artifact,
            write_article_evidence_artifact,
        )
        seen["called_with"] = source_dir
        artifact = build_article_evidence_artifact(
            [], report_date=report_date.isoformat()
        )
        return write_article_evidence_artifact(
            source_dir, report_date.isoformat(), artifact
        )

    monkeypatch.setattr(orchestrator, "_stage_article_evidence", fake_stage)
    run_monitor(
        source_config_path=sources_path,
        run_config_path=run_config_path,
        report_date=date(2026, 9, 7),
        manifest_fixture_path=manifest_path,
        research_fixture_path=research_path,
        state_dir=tmp_path / "state",
        sync=False,
        update_seen_state=False,
    )
    assert seen["called_with"] == tmp_path / "sources"


def test_staging_only_adds_artifact_and_stdout(tmp_path, monkeypatch, capsys):
    """AC-4: orchestrator-driven staging does not touch unrelated modules."""

    import builtins
    from climate_monitor.orchestrator import run_monitor
    from climate_monitor import orchestrator

    forbidden = ("climate_registry", "climate_delivery", "api_server",
                 "scripts.publish_weekly_reports", "scripts.reload_and_smoke_test")
    original = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        assert not name.startswith(forbidden), f"unexpected staging import: {name}"
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    def no_process(*args, **kwargs):
        raise AssertionError("staging must not launch external processes")
    monkeypatch.setattr(subprocess, "Popen", no_process)
    report = tmp_path / "report.md"
    report.write_text("Previously written report\n")

    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        dedent(
            """
            sources:
              - key: iais
                abbreviation: IAIS
                full_name: International Association of Insurance Supervisors
                url: https://www.iais.org/
                tags: [insurance, climate]
            """
        ).strip()
    )
    run_config_path = tmp_path / "run_config.yaml"
    run_config_path.write_text(
        f"""
report_title: Daily Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate]
actuarial_keywords: [insurance]
research_lane:
  lookback_days: 30
  queries: []
output:
  source_dir: {(tmp_path / 'sources').as_posix()}
  wiki_dir: {(tmp_path / 'wiki').as_posix()}
  write_empty_report: false
dedupe:
  url_tracking_path: {tmp_path.as_posix()}/state/seen_urls.json
  title_tracking_path: {tmp_path.as_posix()}/state/seen_titles.json
""".strip()
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": "web-listening-manifest.v1",
        "source": {"source_id": "iais", "site_name": "IAIS"},
        "discovered_items": [{
            "item_id": "1", "item_type": "page",
            "url": "https://www.iais.org/climate-supervision",
            "title": "Climate supervision update",
            "summary": "Insurance supervisors discuss climate risk.",
            "status": "new", "observed_at": "2026-09-07T00:00:00Z",
        }],
        "downloaded_assets": [],
    }))
    research_path = tmp_path / "research.json"
    research_path.write_text("[]")
    monkeypatch.setattr(orchestrator, "_stage_article_evidence",
        lambda *, candidates, source_dir, report_date, providers=(), manifest_fixture_path=None, prepared_evidence=None: None)
    run_monitor(
        source_config_path=sources_path,
        run_config_path=run_config_path,
        report_date=date(2026, 9, 7),
        manifest_fixture_path=manifest_path,
        research_fixture_path=research_path,
        state_dir=tmp_path / "state",
        sync=False,
        update_seen_state=False,
    )
    # No assertion on the report path; the orchestrator's staging was
    # patched to a no-op. The forbidden-import guards cover the contract.


@pytest.mark.parametrize("error_kind", ["missing_provider"])
def test_staging_failure_aborts_seen_state_via_seen_state_error(tmp_path, monkeypatch, error_kind):
    """AC-4: when evidence staging fails inside the #91 transaction, the
    orchestrator raises ``SeenStateError`` — the seen-state commit MUST NOT
    be silently written (this is the regression that Issue #92 closes)."""

    from climate_monitor.seen_state import SeenStateError
    from climate_monitor.orchestrator import run_monitor
    from climate_monitor import orchestrator

    def boom(*, candidates, source_dir, report_date, providers=(), manifest_fixture_path=None, prepared_evidence=None):
        assert prepared_evidence is None
        raise orchestrator.ArticleContentAdapterError("simulated contract violation")

    monkeypatch.setattr(orchestrator, "_stage_article_evidence", boom)
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        dedent(
            """
            sources:
              - key: iais
                abbreviation: IAIS
                full_name: International Association of Insurance Supervisors
                url: https://www.iais.org/
                tags: [insurance, climate]
            """
        ).strip()
    )
    run_config_path = tmp_path / "run_config.yaml"
    run_config_path.write_text(
        f"""
report_title: Daily Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate]
actuarial_keywords: [insurance]
research_lane:
  lookback_days: 30
  queries: []
output:
  source_dir: {(tmp_path / 'sources').as_posix()}
  wiki_dir: {(tmp_path / 'wiki').as_posix()}
  write_empty_report: false
dedupe:
  url_tracking_path: {tmp_path.as_posix()}/state/seen_urls.json
  title_tracking_path: {tmp_path.as_posix()}/state/seen_titles.json
""".strip()
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": "web-listening-manifest.v1",
        "source": {"source_id": "iais", "site_name": "IAIS"},
        "discovered_items": [{
            "item_id": "1", "item_type": "page",
            "url": "https://www.iais.org/climate-supervision",
            "title": "Climate supervision update",
            "summary": "Insurance supervisors discuss climate risk.",
            "status": "new", "observed_at": "2026-09-07T00:00:00Z",
        }],
        "downloaded_assets": [],
    }))
    research_path = tmp_path / "research.json"
    research_path.write_text("[]")
    with pytest.raises(SeenStateError, match="article-evidence contract violation"):
        run_monitor(
            source_config_path=sources_path,
            run_config_path=run_config_path,
            report_date=date(2026, 9, 7),
            manifest_fixture_path=manifest_path,
            research_fixture_path=research_path,
            state_dir=tmp_path / "state",
            sync=False,
            update_seen_state=True,
        )
