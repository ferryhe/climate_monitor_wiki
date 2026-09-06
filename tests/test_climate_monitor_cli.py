from __future__ import annotations

import json
import os
import subprocess
import sys
from textwrap import dedent

import pytest


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
    from types import SimpleNamespace
    from climate_monitor import config
    from climate_monitor.models import CandidateItem, MonitorRunResult
    from scripts import run_climate_monitor as cli

    fallback = tmp_path / "configured-sources"
    override = tmp_path / "override-sources"
    monkeypatch.setattr(config, "load_run_config", lambda _: SimpleNamespace(source_dir=fallback))
    monkeypatch.setattr(cli, "run_monitor", lambda **kw: MonitorRunResult(
        report_date=kw["report_date"], report_path=None,
        items=(CandidateItem(title="Climate insurance", url="https://example.org/climate",
                             summary="", source_name="Example", lane="website"),)))
    monkeypatch.setattr(sys, "argv", ["run_climate_monitor", "--date", "2026-09-07",
        "--source-dir", str(override), "--no-sync", "--no-update-seen-state"])
    cli.main()
    assert (override / "article-evidence.v1_2026-09-07.json").exists()
    assert not fallback.exists()


def test_json_mode_still_stages_evidence(tmp_path, monkeypatch, capsys):
    from climate_monitor.models import CandidateItem, MonitorRunResult
    from scripts import run_climate_monitor as cli
    monkeypatch.setattr(cli, "run_monitor", lambda **kw: MonitorRunResult(
        report_date=kw["report_date"], report_path=None,
        items=(CandidateItem(title="Climate insurance", url="https://example.org/climate",
                             summary="", source_name="Example", lane="website"),)))
    monkeypatch.setattr(sys, "argv", ["run_climate_monitor", "--date", "2026-09-07",
        "--source-dir", str(tmp_path), "--no-sync", "--no-update-seen-state", "--json"])
    cli.main()
    assert (tmp_path / "article-evidence.v1_2026-09-07.json").exists()
    assert json.loads(capsys.readouterr().out)["item_count"] == 1


def test_article_evidence_uses_configured_source_dir_without_override(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from climate_monitor import config
    from climate_monitor.models import CandidateItem, MonitorRunResult
    from scripts import run_climate_monitor as cli
    monkeypatch.setattr(config, "load_run_config", lambda _: SimpleNamespace(source_dir=tmp_path))
    monkeypatch.setattr(cli, "run_monitor", lambda **kw: MonitorRunResult(
        report_date=kw["report_date"], report_path=None,
        items=(CandidateItem(title="Climate insurance", url="https://example.org/climate",
                             summary="", source_name="Example", lane="website"),)))
    monkeypatch.setattr(sys, "argv", ["run_climate_monitor", "--date", "2026-09-07",
        "--no-sync", "--no-update-seen-state"])
    cli.main()
    assert (tmp_path / "article-evidence.v1_2026-09-07.json").exists()


def test_staging_only_adds_artifact_and_stdout(tmp_path, monkeypatch, capsys):
    import builtins
    from datetime import date
    from climate_monitor.models import CandidateItem
    from scripts import run_climate_monitor as cli
    from tests.fixtures.article_content.providers import loopback_success_provider

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
    item = CandidateItem(title="Climate insurance", url="https://example.org/climate",
                         summary="", source_name="Example", lane="website")
    path = cli._stage_article_evidence(items=(item,), report_date=date(2026, 9, 7),
        source_dir=tmp_path, providers=(loopback_success_provider,))
    assert {p.name for p in tmp_path.iterdir()} == {"report.md", path.name}
    assert report.read_text() == "Previously written report\n"
    assert capsys.readouterr().out == ""  # The CLI prints the returned artifact path.


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("provider", ["loopback_damaged_content_ref_provider", "missing_provider"])
def test_staging_failure_preserves_report_and_surfaces_warning(tmp_path, monkeypatch, capsys, json_mode, provider):
    from climate_monitor.models import CandidateItem, MonitorRunResult
    from scripts import run_climate_monitor as cli
    report = tmp_path / "climate-monitor-2026-09-07.md"
    def run_monitor(**kw):
        report.write_text("Report remains intact\n")
        return MonitorRunResult(report_date=kw["report_date"], report_path=str(report),
            items=(CandidateItem(title="Climate insurance", url="https://example.org/climate",
                                 summary="", source_name="Example", lane="website"),))
    monkeypatch.setattr(cli, "run_monitor", run_monitor)
    args = ["run_climate_monitor", "--date", "2026-09-07", "--source-dir", str(tmp_path),
        "--no-sync", "--no-update-seen-state", "--article-evidence-loopback",
        f"tests.fixtures.article_content.providers:{provider}"]
    monkeypatch.setattr(sys, "argv", args + (["--json"] if json_mode else []))
    cli.main()
    output = capsys.readouterr().out
    assert "article-evidence staging failed:" in output
    if json_mode:
        assert json.loads(output)["warnings"]
    else:
        assert f"Report written: {report}" in output
    assert report.read_text() == "Report remains intact\n"
    assert {p.name for p in tmp_path.iterdir()} == {report.name}
