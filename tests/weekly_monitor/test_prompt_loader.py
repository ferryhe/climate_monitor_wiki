from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from climate_monitor.weekly_monitor.prompt_loader import load_weekly_monitor_prompt


ROOT = Path(__file__).resolve().parents[2]
JOB_ROOT = ROOT / "monitoring" / "jobs" / "weekly-climate-monitor-08h"
EXPECTED_PROMPT_SHA256 = "a6769950d5a23e28df9fe5f5d801e5a836fe1326ce6db521724af91a8491088d"
EXPECTED_PROMPT_CHARS = 17451
EXPECTED_PROMPT_LINES = 294
EXPECTED_PROMPT_BYTES = 17459


def test_prompt_loader_returns_exact_versioned_prompt_bytes_and_stable_sha():
    prompt = load_weekly_monitor_prompt()
    prompt_path = JOB_ROOT / "prompts" / "weekly-monitor-v1.prompt.md"
    meta = json.loads(
        (JOB_ROOT / "prompts" / "weekly-monitor-v1.meta.json").read_text(
            encoding="utf-8"
        )
    )

    assert prompt.prompt_id == "weekly_monitor" == meta["prompt_id"]
    assert prompt.version == "v1" == meta["version"]
    assert prompt.path == prompt_path
    assert prompt.raw_bytes == prompt_path.read_bytes()
    assert prompt.sha256 == hashlib.sha256(prompt.raw_bytes).hexdigest()
    decoded = prompt.raw_bytes.decode("utf-8")
    assert prompt.sha256 == meta["sha256"] == EXPECTED_PROMPT_SHA256
    assert len(decoded) == EXPECTED_PROMPT_CHARS
    assert len(decoded.splitlines()) == EXPECTED_PROMPT_LINES
    assert len(prompt.raw_bytes) == EXPECTED_PROMPT_BYTES
    assert b"OPENAI_API_KEY" not in prompt.raw_bytes
    assert b"sk-" not in prompt.raw_bytes


def test_pinned_prompt_artifacts_are_lf_normalized():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()

    assert (
        "monitoring/jobs/weekly-climate-monitor-08h/prompts/*.prompt.md text eol=lf"
        in attrs
    )


def test_weekly_monitor_management_metadata_is_not_live_hermes_config():
    manifest = json.loads((JOB_ROOT / "manifest.json").read_text(encoding="utf-8"))
    driver = json.loads(
        (JOB_ROOT / "driver" / "driver.v1.json").read_text(encoding="utf-8")
    )

    assert manifest["not_live_scheduler_config"] is True
    assert manifest["runtime_owner"] == "Hermes"
    assert manifest["prompt"]["artifact_committed"] is True
    assert manifest["prompt"]["sha256"] == EXPECTED_PROMPT_SHA256
    assert manifest["prompt"]["chars"] == EXPECTED_PROMPT_CHARS
    assert manifest["prompt"]["lines"] == EXPECTED_PROMPT_LINES
    assert manifest["boundaries"]["does_not_store_credentials"] is True
    assert (
        manifest["boundaries"]["does_not_store_host_paths_except_exact_prompt_artifact"]
        is True
    )
    assert "does_not_store_host_paths" not in manifest["boundaries"]
    assert manifest["provenance"]["raw_job_json_status"]["committed"] is False
    assert manifest["provenance"]["raw_job_json_status"]["available_externally"] is True
    assert manifest["provenance"]["raw_job_json_status"]["tarball_missing_job_json"] is True
    assert driver["runtime_boundary"]["not_live_scheduler_config"] is True
    assert driver["runtime_boundary"]["contains_credentials"] is False
    assert not (JOB_ROOT / "job-08h-monitor.json").exists()


def test_redacted_hermes_capture_records_safe_metadata_only():
    capture_path = JOB_ROOT / "provenance" / "captures" / "hermes-job-f5259a8ec2d9.redacted.json"
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    encoded = capture_path.read_text(encoding="utf-8")

    assert capture["not_live_scheduler_config"] is True
    assert capture["job"]["id"] == "f5259a8ec2d9"
    assert capture["job"]["name"] == "Weekly Climate & Actuarial Monitor (IAA CSC Supras)"
    assert capture["job"]["enabled"] is True
    assert capture["job"]["state"] == "scheduled"
    assert capture["job"]["created_at"] == "2026-07-26T19:53:13.667513-04:00"
    assert capture["job"]["next_run_at"] == "2026-08-24T04:00:00-04:00"
    assert capture["job"]["last_run_at"] == "2026-08-17T08:28:13.098952-04:00"
    assert capture["job"]["last_status"] == "ok"
    assert capture["job"]["schedule"] == {
        "kind": "cron",
        "expr": "0 8 * * 1",
        "display": "0 8 * * 1",
        "timezone": "UTC",
    }
    assert capture["tarball_reference"]["tarball_missing_job_json"] is True
    assert capture["raw_job_json"] == {
        "raw_job_json_committed": False,
        "raw_job_json_available_externally": True,
        "raw_job_json_source": "user-provided local attachment, not committed",
        "raw_job_json_filename": "job-08h-monitor.json",
        "file_bytes": 18122,
        "file_sha256": "de237bb2719ec8812718776d64626fbab9d1e3fe8ddca55eb6955244455255f8",
        "physical_line_count": 46,
        "parsed_top_level_property_count": 31,
    }
    assert capture["prompt"]["raw_prompt_committed"] is True
    assert (
        capture["prompt"]["prompt_artifact_path"]
        == "prompts/weekly-monitor-v1.prompt.md"
    )
    assert capture["prompt"]["body_redacted_from_capture"] is True
    assert capture["prompt"]["prompt_chars"] == 16406
    assert capture["prompt"]["prompt_lines"] == 276
    assert (
        capture["prompt"]["prompt_sha256"]
        == "543f8c5b2d30f8b51dc2253a69cdb516d58dfd37cb9b2389e94dd5f5ebbd14b6"
    )
    assert (
        capture["prompt"]["matches_snapshot"]
        == "monitor-post-pr44adapter2-f5259a8ec2d9.snapshot.json"
    )
    assert capture["runtime_shape"] == {
        "script": None,
        "no_agent": False,
        "model": None,
        "workdir": None,
    }
    assert capture["redactions"]["delivery_channels"] == ["discord", "feishu"]
    assert capture["redactions"]["host_paths_redacted"] is True
    assert capture["redactions"]["origin_identifiers_redacted"] is True
    inventory = {
        item["snapshot"]: item for item in capture["prompt"]["snapshot_inventory"]
    }
    assert inventory[capture["prompt"]["matches_snapshot"]]["prompt_hash"] == capture[
        "prompt"
    ]["prompt_sha256"]
    assert inventory[capture["prompt"]["matches_snapshot"]]["prompt_chars"] == capture[
        "prompt"
    ]["prompt_chars"]
    assert [item["prompt_hash"] for item in capture["prompt"]["snapshot_inventory"]] == [
        "5fa51bb90c7b9a4452f50108c3dbafaaa763b1df0c7d18d97ce27ef855bd0186",
        "eb9afc5cbda5cb49fad95c058defcae2ea36fc977cdc708ef5287a17c6f89ab0",
        "543f8c5b2d30f8b51dc2253a69cdb516d58dfd37cb9b2389e94dd5f5ebbd14b6",
        "f3e014d8184162a943f71d1c2505effaebe745b5da6175ebf68e24a6f832d324",
        "f3e014d8184162a943f71d1c2505effaebe745b5da6175ebf68e24a6f832d324",
    ]
    assert not (JOB_ROOT / "job-08h-monitor.json").exists()
    for forbidden in (
        "/home/ubuntu",
        "discord:",
        "oc_",
        "ou_",
        "Follow these steps",
        "Step 1",
        "You are ",
        "```",
    ):
        assert forbidden not in encoded
    assert not re.search(r"\b\d{12,}\b", encoded)
    assert "prompt_body" not in encoded
