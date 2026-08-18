from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import climate_delivery.artifacts as artifact_module
from climate_delivery.artifacts import ARTIFACT_ONLY_DELIVERY_STATUS, load_report_artifact
from report_artifact_fixtures import (
    CANONICAL_REPORT_SHA256,
    canonical_report_bytes,
    write_canonical_artifact,
)


REPORT_DATE = "2026-08-17"
REPORT_FILENAME = f"climate-monitor-{REPORT_DATE}.md"
REPORT_TITLE = "Weekly Climate Monitor — 17 August 2026"
REPORT_SHA256 = "a" * 64
PDF_FILENAME = f"climate-monitor-{REPORT_DATE}.pdf"


def _write_artifact(root: Path) -> tuple[Path, dict, bytes]:
    artifact_dir = root / REPORT_DATE / REPORT_SHA256
    artifact_dir.mkdir(parents=True)
    summary = {
        "schema_version": 1,
        "report": {
            "date": REPORT_DATE,
            "title": REPORT_TITLE,
            "sha256": REPORT_SHA256,
            "sites": {"checked": 57, "succeeded": 56, "failed": 1},
        },
        "executive_summary": ["A narrative paragraph.", "A second paragraph."],
        "monitoring_notes": ["One source could not be reached."],
        "highlights": [
            {
                "pillar": "A",
                "title": "A monitored update",
                "summary": "Summary A.",
                "url": "https://example.test/a",
            },
            {
                "pillar": "B",
                "title": "Wider intelligence",
                "summary": "Summary B.",
                "url": "https://example.test/b",
            },
        ],
        "original_links": ["https://example.test/a", "https://example.test/b"],
    }
    summary_bytes = (json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    pdf_bytes = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    (artifact_dir / "summary.json").write_bytes(summary_bytes)
    (artifact_dir / PDF_FILENAME).write_bytes(pdf_bytes)
    manifest = {
        "schema_version": 1,
        "report": {
            "date": REPORT_DATE,
            "filename": REPORT_FILENAME,
            "sha256": REPORT_SHA256,
        },
        "artifacts": {
            "summary": {
                "path": "summary.json",
                "sha256": hashlib.sha256(summary_bytes).hexdigest(),
            },
            "pdf": {
                "path": PDF_FILENAME,
                "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            },
            "manifest": {"path": "manifest.json"},
        },
        "delivery": {
            "status": "sent",
            "recipients": [{"id": "private-id", "status": "sent"}],
        },
    }
    (artifact_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return artifact_dir, manifest, pdf_bytes


def _load(root: Path | str | None, *, include_pdf_bytes: bool = True):
    return load_report_artifact(
        root,
        report_date=REPORT_DATE,
        report_filename=REPORT_FILENAME,
        report_title=REPORT_TITLE,
        report_sha256=REPORT_SHA256,
        include_pdf_bytes=include_pdf_bytes,
    )


def _rewrite_manifest(artifact_dir: Path, manifest: dict) -> None:
    (artifact_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_valid_artifact_projects_only_briefing_and_pdf_bytes(tmp_path):
    artifact_dir, _manifest, pdf_bytes = _write_artifact(tmp_path)

    artifact = _load(tmp_path.resolve())

    assert artifact is not None
    assert artifact.briefing == {
        "executive_summary": ["A narrative paragraph.", "A second paragraph."],
        "monitoring_snapshot": {
            "sites_checked": 57,
            "sites_succeeded": 56,
            "sites_failed": 1,
            "pillar_a_updates": 1,
            "pillar_b_updates": 1,
            "notes": ["One source could not be reached."],
        },
    }
    assert artifact.pdf_filename == PDF_FILENAME
    assert artifact.pdf_bytes == pdf_bytes
    assert "private-id" not in repr(artifact)
    assert str(artifact_dir) not in repr(artifact)


def test_metadata_mode_streams_pdf_validation_without_retaining_bytes(
    tmp_path, monkeypatch
):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    pdf_path = artifact_dir / PDF_FILENAME
    pdf_bytes = b"%PDF-1.4\n" + (b"0" * (2 * 1024 * 1024)) + b"\n%%EOF\n"
    pdf_path.write_bytes(pdf_bytes)
    manifest["artifacts"]["pdf"]["sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
    _rewrite_manifest(artifact_dir, manifest)
    original_read_limited = artifact_module._read_limited

    def reject_full_pdf_read(path, limit):
        if path.suffix == ".pdf":
            raise AssertionError("metadata mode must stream PDF validation")
        return original_read_limited(path, limit)

    monkeypatch.setattr("climate_delivery.artifacts._read_limited", reject_full_pdf_read)

    artifact = _load(tmp_path.resolve(), include_pdf_bytes=False)

    assert artifact is not None
    assert not hasattr(artifact, "pdf_bytes")
    assert artifact.pdf_filename == PDF_FILENAME


def test_canonical_2026_08_17_artifact_is_readable_end_to_end(tmp_path):
    canonical_raw = canonical_report_bytes()
    fixture = write_canonical_artifact(tmp_path)

    assert b"\r\n" not in canonical_raw
    assert fixture.report.sha256 == CANONICAL_REPORT_SHA256
    assert (fixture.report.checked, fixture.report.succeeded, fixture.report.failed) == (57, 57, 0)
    assert len([item for item in fixture.report.highlights if item.pillar == "A"]) == 9
    assert len([item for item in fixture.report.highlights if item.pillar == "B"]) == 17
    assert len(fixture.summary["executive_summary"]) == 4
    assert len(fixture.summary["monitoring_notes"]) == 3

    artifact = load_report_artifact(
        tmp_path.resolve(),
        report_date=fixture.report.report_date,
        report_filename=fixture.report.filename,
        report_title=fixture.report.title,
        report_sha256=fixture.report.sha256,
        include_pdf_bytes=True,
    )

    assert artifact is not None
    assert artifact.briefing["executive_summary"] == fixture.summary["executive_summary"]
    assert artifact.briefing["monitoring_snapshot"] == {
        "sites_checked": 57,
        "sites_succeeded": 57,
        "sites_failed": 0,
        "pillar_a_updates": 9,
        "pillar_b_updates": 17,
        "notes": fixture.summary["monitoring_notes"],
    }
    assert artifact.pdf_bytes == fixture.pdf_bytes


def test_artifact_only_manifest_status_is_valid_for_backfill(tmp_path):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    manifest["delivery"] = {
        "status": ARTIFACT_ONLY_DELIVERY_STATUS,
        "recipients": [],
    }
    _rewrite_manifest(artifact_dir, manifest)

    assert _load(tmp_path.resolve()) is not None


@pytest.mark.parametrize(
    ("delivery_status", "recipient_status"),
    [
        ("sent", "sent"),
        ("already-sent", "sent"),
        ("dry-run", "pending"),
        ("failed", "failed"),
        ("ambiguous", "unknown"),
    ],
)
def test_existing_delivery_manifest_statuses_remain_valid(
    tmp_path, delivery_status, recipient_status
):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    manifest["delivery"] = {
        "status": delivery_status,
        "recipients": [{"id": "alpha", "status": recipient_status}],
    }
    _rewrite_manifest(artifact_dir, manifest)

    assert _load(tmp_path.resolve()) is not None


@pytest.mark.parametrize(
    "status",
    ["", "backfill", "unknown", "sending", "SENT", 1, None],
)
def test_invalid_manifest_delivery_status_fails_closed(tmp_path, status):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    manifest["delivery"]["status"] = status
    _rewrite_manifest(artifact_dir, manifest)

    assert _load(tmp_path.resolve()) is None


@pytest.mark.parametrize(
    "recipients",
    [
        [{}],
        [{"id": "alpha"}],
        [{"status": "sent"}],
        [{"id": "", "status": "sent"}],
        [{"id": "   ", "status": "sent"}],
        [{"id": "Alpha", "status": "sent"}],
        [{"id": "alpha ", "status": "sent"}],
        [{"id": 1, "status": "sent"}],
        [{"id": "alpha", "status": "invalid"}],
        [{"id": "alpha", "status": 1}],
        [{"id": "alpha", "status": "sent", "email": "secret@example.test"}],
        [
            {"id": "alpha", "status": "sent"},
            {"id": "alpha", "status": "failed"},
        ],
    ],
)
def test_invalid_manifest_recipient_schema_fails_closed(tmp_path, recipients):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    manifest["delivery"]["recipients"] = recipients
    _rewrite_manifest(artifact_dir, manifest)

    assert _load(tmp_path.resolve()) is None


def test_artifact_only_status_rejects_recipient_entries(tmp_path):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    manifest["delivery"] = {
        "status": "artifact-only",
        "recipients": [{"id": "alpha", "status": "pending"}],
    }
    _rewrite_manifest(artifact_dir, manifest)

    assert _load(tmp_path.resolve()) is None


@pytest.mark.parametrize(
    "delivery_status",
    ["sent", "already-sent", "dry-run", "failed", "ambiguous"],
)
def test_standard_delivery_statuses_reject_empty_recipients(tmp_path, delivery_status):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    manifest["delivery"] = {"status": delivery_status, "recipients": []}
    _rewrite_manifest(artifact_dir, manifest)

    assert _load(tmp_path.resolve()) is None


def test_manifest_delivery_rejects_extra_fields(tmp_path):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    manifest["delivery"]["smtp_config"] = "private"
    _rewrite_manifest(artifact_dir, manifest)

    assert _load(tmp_path.resolve()) is None


@pytest.mark.parametrize("root", [None, "", "relative-output"])
def test_unconfigured_or_relative_root_fails_closed(root):
    assert _load(root) is None


def test_missing_and_unreadable_artifacts_fail_closed(tmp_path, monkeypatch):
    assert _load((tmp_path / "missing").resolve()) is None
    artifact_dir, _manifest, _pdf = _write_artifact(tmp_path)

    def unreadable(*_args, **_kwargs):
        raise PermissionError("private host path")

    monkeypatch.setattr("climate_delivery.artifacts._read_limited", unreadable)
    assert _load(tmp_path.resolve()) is None
    assert artifact_dir.exists()


@pytest.mark.parametrize(
    ("target", "replacement"),
    [
        ("manifest", b"not-json"),
        ("manifest-schema", None),
        ("manifest-delivery", None),
        ("summary", b"not-json"),
        ("summary-schema", None),
    ],
)
def test_invalid_json_or_schema_fails_closed(tmp_path, target, replacement):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    if target == "manifest":
        (artifact_dir / "manifest.json").write_bytes(replacement)
    elif target == "manifest-schema":
        manifest["schema_version"] = 2
        _rewrite_manifest(artifact_dir, manifest)
    elif target == "manifest-delivery":
        manifest["delivery"] = {"status": "sent", "recipients": "not-a-list"}
        _rewrite_manifest(artifact_dir, manifest)
    elif target == "summary":
        (artifact_dir / "summary.json").write_bytes(replacement)
        manifest["artifacts"]["summary"]["sha256"] = hashlib.sha256(replacement).hexdigest()
        _rewrite_manifest(artifact_dir, manifest)
    else:
        summary_path = artifact_dir / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["executive_summary"] = "not-a-list"
        raw = json.dumps(summary).encode()
        summary_path.write_bytes(raw)
        manifest["artifacts"]["summary"]["sha256"] = hashlib.sha256(raw).hexdigest()
        _rewrite_manifest(artifact_dir, manifest)
    assert _load(tmp_path.resolve()) is None


@pytest.mark.parametrize(
    ("where", "field", "bad_value"),
    [
        ("manifest", "date", "2026-08-10"),
        ("manifest", "filename", "climate-monitor-2026-08-10.md"),
        ("manifest", "sha256", "b" * 64),
        ("summary", "date", "2026-08-10"),
        ("summary", "title", "A different report"),
        ("summary", "sha256", "b" * 64),
    ],
)
def test_report_identity_mismatch_fails_closed(tmp_path, where, field, bad_value):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    if where == "manifest":
        manifest["report"][field] = bad_value
    else:
        summary_path = artifact_dir / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["report"][field] = bad_value
        raw = json.dumps(summary).encode()
        summary_path.write_bytes(raw)
        manifest["artifacts"]["summary"]["sha256"] = hashlib.sha256(raw).hexdigest()
    _rewrite_manifest(artifact_dir, manifest)
    assert _load(tmp_path.resolve()) is None


@pytest.mark.parametrize("artifact_name", ["summary", "pdf"])
def test_artifact_hash_mismatch_fails_closed(tmp_path, artifact_name):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    manifest["artifacts"][artifact_name]["sha256"] = "0" * 64
    _rewrite_manifest(artifact_dir, manifest)
    assert _load(tmp_path.resolve()) is None


@pytest.mark.parametrize(
    ("artifact_name", "unsafe_path"),
    [
        ("summary", "../summary.json"),
        ("summary", "/private/summary.json"),
        ("summary", "C:\\private\\summary.json"),
        ("pdf", "../report.pdf"),
        ("pdf", "/private/report.pdf"),
        ("pdf", "C:\\private\\report.pdf"),
    ],
)
def test_absolute_and_traversal_manifest_paths_fail_closed(tmp_path, artifact_name, unsafe_path):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    manifest["artifacts"][artifact_name]["path"] = unsafe_path
    _rewrite_manifest(artifact_dir, manifest)
    assert _load(tmp_path.resolve()) is None


@pytest.mark.parametrize(
    "sites",
    [
        {"checked": 57, "succeeded": 56, "failed": 0},
        {"checked": -1, "succeeded": 0, "failed": 0},
        {"checked": True, "succeeded": 1, "failed": 0},
        {"checked": 57, "succeeded": "56", "failed": 1},
    ],
)
def test_invalid_monitoring_statistics_fail_closed(tmp_path, sites):
    artifact_dir, manifest, _pdf = _write_artifact(tmp_path)
    summary_path = artifact_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["report"]["sites"] = sites
    raw = json.dumps(summary).encode()
    summary_path.write_bytes(raw)
    manifest["artifacts"]["summary"]["sha256"] = hashlib.sha256(raw).hexdigest()
    _rewrite_manifest(artifact_dir, manifest)
    assert _load(tmp_path.resolve()) is None


def test_symlink_escape_fails_closed(tmp_path):
    root = tmp_path / "root"
    artifact_dir, manifest, _pdf = _write_artifact(root)
    outside = tmp_path / "outside.json"
    outside.write_bytes((artifact_dir / "summary.json").read_bytes())
    summary_path = artifact_dir / "summary.json"
    summary_path.unlink()
    try:
        summary_path.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable for this user/platform")
    manifest["artifacts"]["summary"]["sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    _rewrite_manifest(artifact_dir, manifest)

    assert _load(root.resolve()) is None
