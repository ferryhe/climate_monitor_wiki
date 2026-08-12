import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from .config import load_delivery_config
from .delivery import deliver
from .errors import DeliveryError, GenerationError, LockStateError
from .io import atomic_write_bytes, atomic_write_json, exclusive_lock
from .paths import validate_run_paths
from .pdf import render_pdf
from .report import parse_weekly_report
from .summary import build_summary, write_summary


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise GenerationError(f"could not hash generated artifact {path.name}") from exc


def _manifest(
    summary: dict[str, Any],
    delivery: dict[str, Any],
    *,
    summary_sha256: str,
    pdf_name: str,
    pdf_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "report": {
            "date": summary["report"]["date"],
            "filename": f"climate-monitor-{summary['report']['date']}.md",
            "sha256": summary["report"]["sha256"],
        },
        "artifacts": {
            "summary": {"path": "summary.json", "sha256": summary_sha256},
            "pdf": {"path": pdf_name, "sha256": pdf_sha256},
            "manifest": {"path": "manifest.json"},
        },
        "delivery": {
            "status": delivery["status"],
            "recipients": delivery["recipients"],
        },
    }


def _failure_status(recipients: list[dict[str, str]], error: Exception) -> str:
    if isinstance(error, LockStateError) or any(item["status"] in {"sending", "unknown"} for item in recipients):
        return "ambiguous"
    return "failed"


def _recipient_snapshot(state_path: Path, recipients_config) -> list[dict[str, str]]:
    recipient_ids = [item.id for item in recipients_config]
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        recipients = state["recipients"]
        if not isinstance(recipients, dict):
            raise ValueError
        return [
            {
                "id": recipient_id,
                "status": recipients[recipient_id]["status"]
                if recipients.get(recipient_id, {}).get("status") in {"pending", "sending", "sent", "failed", "unknown"}
                else "unknown",
            }
            for recipient_id in recipient_ids
        ]
    except (AttributeError, OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return [
            {
                "id": recipient_id,
                "status": "unknown",
            }
            for recipient_id in recipient_ids
        ]


def _validate_or_install_artifacts(candidates: list[tuple[Path, Path, str]]) -> None:
    for _candidate, destination, expected_sha256 in candidates:
        if destination.exists():
            try:
                actual = _file_sha256(destination)
            except GenerationError as exc:
                raise LockStateError(f"existing artifact {destination.name} is unreadable") from exc
            if actual != expected_sha256:
                raise LockStateError(f"existing artifact {destination.name} does not match generated content")
    for candidate, destination, _expected_sha256 in candidates:
        if not destination.exists():
            try:
                data = candidate.read_bytes()
            except OSError as exc:
                raise GenerationError(f"could not read generated artifact {candidate.name}") from exc
            if hashlib.sha256(data).hexdigest() != _expected_sha256:
                raise GenerationError(f"generated artifact {candidate.name} changed before installation")
            atomic_write_bytes(destination, data)
    for _candidate, destination, expected_sha256 in candidates:
        try:
            actual = _file_sha256(destination)
        except GenerationError as exc:
            raise LockStateError(f"installed artifact {destination.name} is unreadable") from exc
        if actual != expected_sha256:
            raise LockStateError(f"installed artifact {destination.name} changed unexpectedly")


def run_delivery(
    report_path: Path,
    output_dir: Path,
    state_dir: Path,
    config_path: Path,
    *,
    dry_run: bool = False,
    smtp_factory=None,
    clock=None,
) -> dict[str, Any]:
    report_path, output_dir, state_dir, config_path = validate_run_paths(
        report_path,
        output_dir,
        state_dir,
        config_path,
    )
    report = parse_weekly_report(report_path)
    config = load_delivery_config(config_path)
    summary = build_summary(report)
    artifact_dir = output_dir / report.report_date / report.sha256
    summary_path = artifact_dir / "summary.json"
    pdf_name = f"climate-monitor-{report.report_date}.pdf"
    pdf_path = artifact_dir / pdf_name
    manifest_path = artifact_dir / "manifest.json"

    with tempfile.TemporaryDirectory(prefix="climate-delivery-") as temporary:
        temporary_dir = Path(temporary)
        candidate_summary = temporary_dir / "summary.json"
        candidate_pdf = temporary_dir / pdf_name
        write_summary(summary, candidate_summary)
        render_pdf(summary, candidate_pdf)
        summary_sha256 = _file_sha256(candidate_summary)
        pdf_sha256 = _file_sha256(candidate_pdf)

        with exclusive_lock(state_dir, report.sha256):
            _validate_or_install_artifacts(
                [
                    (candidate_summary, summary_path, summary_sha256),
                    (candidate_pdf, pdf_path, pdf_sha256),
                ]
            )
            try:
                delivery = deliver(
                    summary,
                    pdf_path,
                    config,
                    state_dir,
                    dry_run=dry_run,
                    smtp_factory=smtp_factory,
                    acquire_lock=False,
                    summary_artifact_sha256=summary_sha256,
                    clock=clock,
                )
            except (DeliveryError, LockStateError) as original_error:
                state_path = state_dir / f"{report.sha256}.json"
                recipients = (
                    _recipient_snapshot(state_path, config.recipients)
                    if state_path.exists()
                    else [
                        {
                            "id": item.id,
                            "status": "pending",
                        }
                        for item in config.recipients
                    ]
                )
                try:
                    atomic_write_json(
                        manifest_path,
                        _manifest(
                            summary,
                            {"status": _failure_status(recipients, original_error), "recipients": recipients},
                            summary_sha256=summary_sha256,
                            pdf_name=pdf_name,
                            pdf_sha256=pdf_sha256,
                        ),
                    )
                except Exception as manifest_error:
                    raise original_error.with_traceback(original_error.__traceback__) from manifest_error
                raise
            atomic_write_json(
                manifest_path,
                _manifest(
                    summary,
                    delivery,
                    summary_sha256=summary_sha256,
                    pdf_name=pdf_name,
                    pdf_sha256=pdf_sha256,
                ),
            )
    return {
        "status": delivery["status"],
        "report_date": report.report_date,
        "report_sha256": report.sha256,
        "artifacts": {"summary": "summary.json", "pdf": pdf_name, "manifest": "manifest.json"},
    }
