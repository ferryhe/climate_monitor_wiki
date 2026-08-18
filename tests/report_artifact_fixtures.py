from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from climate_delivery.artifacts import ARTIFACT_ONLY_DELIVERY_STATUS
from climate_delivery.io import atomic_write_json
from climate_delivery.pdf import render_pdf
from climate_delivery.pipeline import _manifest
from climate_delivery.report import WeeklyReport, parse_weekly_report
from climate_delivery.summary import build_summary, write_summary


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REPORT_PATH = ROOT / "sources" / "climate-monitor-2026-08-17.md"
CANONICAL_REPORT_SHA256 = "ed19d7b8c8fbe99a5f66b333b5e2d5fbee63c3f41cf927d79d812888fc333972"


@dataclass(frozen=True)
class CanonicalArtifactFixture:
    report: WeeklyReport
    summary: dict[str, Any]
    artifact_dir: Path
    pdf_bytes: bytes


def canonical_report() -> WeeklyReport:
    canonical_raw = canonical_report_bytes()
    return parse_weekly_report(CANONICAL_REPORT_PATH, raw=canonical_raw)


def canonical_report_bytes() -> bytes:
    canonical_raw = CANONICAL_REPORT_PATH.read_bytes().replace(b"\r\n", b"\n")
    assert b"\r\n" not in canonical_raw
    assert hashlib.sha256(canonical_raw).hexdigest() == CANONICAL_REPORT_SHA256
    return canonical_raw


def write_canonical_artifact(output_dir: Path) -> CanonicalArtifactFixture:
    report = canonical_report()
    summary = build_summary(report)
    artifact_dir = output_dir / report.report_date / report.sha256
    summary_path = artifact_dir / "summary.json"
    pdf_name = f"climate-monitor-{report.report_date}.pdf"
    pdf_path = artifact_dir / pdf_name
    write_summary(summary, summary_path)
    render_pdf(summary, pdf_path)
    summary_sha256 = hashlib.sha256(summary_path.read_bytes()).hexdigest()
    pdf_bytes = pdf_path.read_bytes()
    atomic_write_json(
        artifact_dir / "manifest.json",
        _manifest(
            summary,
            {"status": ARTIFACT_ONLY_DELIVERY_STATUS, "recipients": []},
            summary_sha256=summary_sha256,
            pdf_name=pdf_name,
            pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        ),
    )
    return CanonicalArtifactFixture(
        report=report,
        summary=summary,
        artifact_dir=artifact_dir,
        pdf_bytes=pdf_bytes,
    )
