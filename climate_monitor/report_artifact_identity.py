from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .run_ledger import LedgerError, read_bounded_file


MAX_MANIFEST_BYTES = 256 * 1024
MAX_SUMMARY_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 32 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactIdentityError(ValueError):
    """An artifact cannot be bound to one exact report identity."""


@dataclass(frozen=True)
class ArtifactIdentity:
    artifact_dir: Path
    manifest_sha256: str
    summary_sha256: str
    pdf_sha256: str


def _is_link_or_reparse(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse
    )


def _safe_directory(path: Path, *, root: Path | None = None) -> Path:
    if not path.is_absolute():
        raise ArtifactIdentityError("artifact path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ArtifactIdentityError("artifact directory is unavailable") from exc
    if resolved != path:
        raise ArtifactIdentityError("artifact path is not canonical")
    if root is not None:
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ArtifactIdentityError("artifact path escapes configured root") from exc
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise ArtifactIdentityError("artifact path is unavailable") from exc
        if _is_link_or_reparse(current):
            raise ArtifactIdentityError("artifact path contains a link or reparse point")
        if current != resolved and not stat.S_ISDIR(metadata.st_mode):
            raise ArtifactIdentityError("artifact path is invalid")
    if not stat.S_ISDIR(os.lstat(resolved).st_mode):
        raise ArtifactIdentityError("artifact path is not a directory")
    return resolved


def _read_regular(path: Path, *, root: Path, limit: int) -> bytes:
    try:
        resolved_parent = _safe_directory(path.parent, root=root)
        candidate = resolved_parent / path.name
    except OSError as exc:
        raise ArtifactIdentityError("artifact file is unavailable") from exc
    try:
        return read_bounded_file(candidate, max_bytes=limit)
    except LedgerError as exc:
        raise ArtifactIdentityError("artifact file is unavailable") from exc


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ArtifactIdentityError(f"invalid {label}")
    return value


def _json(raw: bytes, *, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise ArtifactIdentityError(f"duplicate {label} field")
            output[key] = value
        return output

    try:
        return _object(
            json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates),
            label=label,
        )
    except ArtifactIdentityError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ArtifactIdentityError(f"invalid {label}") from exc


def _sha(value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ArtifactIdentityError("invalid artifact sha256")
    return value


def validate_report_artifact_identity(
    artifact_root: str | Path,
    *,
    report_date: str,
    report_filename: str,
    report_sha256: str,
) -> ArtifactIdentity:
    root = _safe_directory(Path(artifact_root))
    artifact_dir = _safe_directory(root / report_date / report_sha256, root=root)
    manifest_raw = _read_regular(
        artifact_dir / "manifest.json", root=root, limit=MAX_MANIFEST_BYTES
    )
    manifest = _json(manifest_raw, label="manifest")
    if manifest.get("schema_version") != 1:
        raise ArtifactIdentityError("invalid manifest schema")
    report = _object(manifest.get("report"), label="manifest report")
    if (
        report.get("date") != report_date
        or report.get("filename") != report_filename
        or report.get("sha256") != report_sha256
    ):
        raise ArtifactIdentityError("manifest report identity mismatch")
    artifacts = _object(manifest.get("artifacts"), label="manifest artifacts")
    summary_entry = _object(artifacts.get("summary"), label="summary artifact")
    pdf_entry = _object(artifacts.get("pdf"), label="PDF artifact")
    manifest_entry = _object(artifacts.get("manifest"), label="manifest artifact")
    pdf_name = f"climate-monitor-{report_date}.pdf"
    if (
        summary_entry.get("path") != "summary.json"
        or pdf_entry.get("path") != pdf_name
        or manifest_entry.get("path") != "manifest.json"
    ):
        raise ArtifactIdentityError("invalid artifact path")
    expected_summary_sha256 = _sha(summary_entry.get("sha256"))
    expected_pdf_sha256 = _sha(pdf_entry.get("sha256"))

    summary_raw = _read_regular(
        artifact_dir / "summary.json", root=root, limit=MAX_SUMMARY_BYTES
    )
    summary_sha256 = hashlib.sha256(summary_raw).hexdigest()
    if summary_sha256 != expected_summary_sha256:
        raise ArtifactIdentityError("summary hash mismatch")
    summary = _json(summary_raw, label="summary")
    summary_report = _object(summary.get("report"), label="summary report")
    if (
        summary_report.get("date") != report_date
        or summary_report.get("sha256") != report_sha256
    ):
        raise ArtifactIdentityError("summary report identity mismatch")

    pdf_raw = _read_regular(
        artifact_dir / pdf_name, root=root, limit=MAX_PDF_BYTES
    )
    if not pdf_raw.startswith(b"%PDF-"):
        raise ArtifactIdentityError("invalid PDF content")
    pdf_sha256 = hashlib.sha256(pdf_raw).hexdigest()
    if pdf_sha256 != expected_pdf_sha256:
        raise ArtifactIdentityError("PDF hash mismatch")
    return ArtifactIdentity(
        artifact_dir=artifact_dir,
        manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        summary_sha256=summary_sha256,
        pdf_sha256=pdf_sha256,
    )
