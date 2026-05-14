from __future__ import annotations

import json
import os
import subprocess
import sys
from textwrap import dedent


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
