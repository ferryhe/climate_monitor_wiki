from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .config import RECIPIENT_ID
from .delivery import _validate_summary
from .errors import InputError


MAX_MANIFEST_BYTES = 256 * 1024
MAX_SUMMARY_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 32 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
# ``artifact-only`` is the stable no-email manifest status for historical
# backfill. It deliberately carries no recipient entries.
ARTIFACT_ONLY_DELIVERY_STATUS = "artifact-only"
DELIVERY_STATUSES = frozenset(
    {
        "sent",
        "already-sent",
        "dry-run",
        "failed",
        "ambiguous",
        ARTIFACT_ONLY_DELIVERY_STATUS,
    }
)
RECIPIENT_STATUSES = frozenset({"pending", "sending", "sent", "failed", "unknown"})


class _InvalidArtifact(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedReportArtifact:
    briefing: dict[str, Any]
    pdf_filename: str
    pdf_bytes: bytes = field(repr=False)


def _read_limited(path: Path, limit: int) -> bytes:
    if path.stat().st_size > limit:
        raise _InvalidArtifact("artifact exceeds size limit")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise _InvalidArtifact("artifact exceeds size limit")
    return data


def _contained_file(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _InvalidArtifact("artifact escapes configured root") from exc
    if not resolved.is_file():
        raise _InvalidArtifact("artifact is not a file")
    return resolved


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _InvalidArtifact("invalid sha256")
    return value


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _InvalidArtifact("invalid object")
    return value


def _manifest_artifact(value: Any, *, expected_path: str) -> tuple[str, str]:
    artifact = _mapping(value)
    if artifact.get("path") != expected_path:
        raise _InvalidArtifact("invalid artifact path")
    return expected_path, _sha256(artifact.get("sha256"))


def _validate_identity(
    *, report_date: str, report_filename: str, report_title: str, report_sha256: str
) -> None:
    try:
        parsed_date = date.fromisoformat(report_date)
    except (TypeError, ValueError) as exc:
        raise _InvalidArtifact("invalid report identity") from exc
    if (
        parsed_date.isoformat() != report_date
        or report_filename != f"climate-monitor-{report_date}.md"
        or not isinstance(report_title, str)
        or not report_title.strip()
    ):
        raise _InvalidArtifact("invalid report identity")
    _sha256(report_sha256)


def _load_validated(
    root_value: str | Path,
    *,
    report_date: str,
    report_filename: str,
    report_title: str,
    report_sha256: str,
) -> ValidatedReportArtifact:
    configured_root = Path(root_value)
    if not configured_root.is_absolute():
        raise _InvalidArtifact("artifact root must be absolute")
    root = configured_root.resolve(strict=True)
    if not root.is_dir():
        raise _InvalidArtifact("artifact root is not a directory")

    _validate_identity(
        report_date=report_date,
        report_filename=report_filename,
        report_title=report_title,
        report_sha256=report_sha256,
    )
    artifact_dir = (root / report_date / report_sha256).resolve(strict=True)
    try:
        artifact_dir.relative_to(root)
    except ValueError as exc:
        raise _InvalidArtifact("artifact directory escapes configured root") from exc
    if not artifact_dir.is_dir():
        raise _InvalidArtifact("artifact directory is unavailable")

    manifest_path = _contained_file(root, artifact_dir / "manifest.json")
    manifest_raw = _read_limited(manifest_path, MAX_MANIFEST_BYTES)
    manifest = _mapping(json.loads(manifest_raw.decode("utf-8")))
    if manifest.get("schema_version") != 1:
        raise _InvalidArtifact("invalid manifest schema")
    manifest_report = _mapping(manifest.get("report"))
    if (
        manifest_report.get("date") != report_date
        or manifest_report.get("filename") != report_filename
        or manifest_report.get("sha256") != report_sha256
    ):
        raise _InvalidArtifact("manifest report identity mismatch")

    artifacts = _mapping(manifest.get("artifacts"))
    summary_name, expected_summary_sha256 = _manifest_artifact(
        artifacts.get("summary"), expected_path="summary.json"
    )
    pdf_filename = f"climate-monitor-{report_date}.pdf"
    pdf_name, expected_pdf_sha256 = _manifest_artifact(
        artifacts.get("pdf"), expected_path=pdf_filename
    )
    manifest_entry = _mapping(artifacts.get("manifest"))
    if manifest_entry.get("path") != "manifest.json":
        raise _InvalidArtifact("invalid manifest path")
    delivery = _mapping(manifest.get("delivery"))
    if set(delivery) != {"status", "recipients"}:
        raise _InvalidArtifact("invalid delivery schema")
    delivery_status = delivery.get("status")
    if delivery_status not in DELIVERY_STATUSES:
        raise _InvalidArtifact("invalid delivery status")
    recipients = delivery.get("recipients")
    if not isinstance(recipients, list):
        raise _InvalidArtifact("invalid delivery recipients")
    recipient_ids: set[str] = set()
    for recipient in recipients:
        if not isinstance(recipient, dict) or set(recipient) != {"id", "status"}:
            raise _InvalidArtifact("invalid delivery recipient")
        recipient_id, recipient_status = recipient["id"], recipient["status"]
        if (
            not isinstance(recipient_id, str)
            or RECIPIENT_ID.fullmatch(recipient_id) is None
            or recipient_status not in RECIPIENT_STATUSES
            or recipient_id in recipient_ids
        ):
            raise _InvalidArtifact("invalid delivery recipient")
        recipient_ids.add(recipient_id)
    if delivery_status == ARTIFACT_ONLY_DELIVERY_STATUS and recipients:
        raise _InvalidArtifact("artifact-only delivery must not have recipients")
    if delivery_status != ARTIFACT_ONLY_DELIVERY_STATUS and not recipients:
        raise _InvalidArtifact("delivery manifest must have recipients")

    summary_path = _contained_file(root, artifact_dir / summary_name)
    summary_raw = _read_limited(summary_path, MAX_SUMMARY_BYTES)
    summary = _mapping(json.loads(summary_raw.decode("utf-8")))
    _validate_summary(summary)
    actual_summary_sha256 = hashlib.sha256(summary_raw).hexdigest()
    if actual_summary_sha256 != expected_summary_sha256:
        raise _InvalidArtifact("summary hash mismatch")
    summary_report = _mapping(summary.get("report"))
    if (
        summary_report.get("date") != report_date
        or summary_report.get("title") != report_title
        or summary_report.get("sha256") != report_sha256
    ):
        raise _InvalidArtifact("summary report identity mismatch")

    sites = _mapping(summary_report.get("sites"))
    counts = tuple(sites.get(key) for key in ("checked", "succeeded", "failed"))
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise _InvalidArtifact("invalid monitoring statistics")
    checked, succeeded, failed = counts
    if succeeded + failed != checked:
        raise _InvalidArtifact("inconsistent monitoring statistics")

    narratives = summary["executive_summary"]
    if not narratives or any(not value.strip() for value in narratives):
        raise _InvalidArtifact("executive summary is empty")
    notes = summary.get("monitoring_notes", [])
    highlights = summary["highlights"]

    pdf_path = _contained_file(root, artifact_dir / pdf_name)
    pdf_bytes = _read_limited(pdf_path, MAX_PDF_BYTES)
    if hashlib.sha256(pdf_bytes).hexdigest() != expected_pdf_sha256:
        raise _InvalidArtifact("PDF hash mismatch")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise _InvalidArtifact("invalid PDF content")

    return ValidatedReportArtifact(
        briefing={
            "executive_summary": list(narratives),
            "monitoring_snapshot": {
                "sites_checked": checked,
                "sites_succeeded": succeeded,
                "sites_failed": failed,
                "pillar_a_updates": sum(item["pillar"] == "A" for item in highlights),
                "pillar_b_updates": sum(item["pillar"] == "B" for item in highlights),
                "notes": list(notes),
            },
        },
        pdf_filename=pdf_filename,
        pdf_bytes=pdf_bytes,
    )


def load_report_artifact(
    output_dir: str | Path | None,
    *,
    report_date: str,
    report_filename: str,
    report_title: str,
    report_sha256: str,
) -> ValidatedReportArtifact | None:
    """Load one exact content-addressed delivery artifact, failing closed."""
    if output_dir is None or not str(output_dir).strip():
        return None
    try:
        return _load_validated(
            output_dir,
            report_date=report_date,
            report_filename=report_filename,
            report_title=report_title,
            report_sha256=report_sha256,
        )
    except (
        InputError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        RuntimeError,
        _InvalidArtifact,
    ):
        return None
