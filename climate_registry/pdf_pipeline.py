"""Durable handoff from authenticated PDF intake to the Registry and Chat."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from climate_delivery.io import atomic_write_json
from scripts.sync_source_wiki import sync_registry_wiki

from .pdf_intake import persist_pdf_intake
from .persistent import _file_sha256, _read_only_connection, _validate_database


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _external(path: Path, repository_root: Path) -> Path:
    resolved, repository = path.resolve(), repository_root.resolve()
    if resolved == repository or repository in resolved.parents:
        raise ValueError("PDF pipeline storage must be outside the repository")
    return resolved


def _batch_dir(queue_dir: Path, batch_id: str) -> Path:
    if not _SHA256.fullmatch(batch_id):
        raise ValueError("invalid PDF batch ID")
    return queue_dir.resolve() / batch_id


def _bundle_digest(bundle: dict[str, Any]) -> str:
    normalized = dict(bundle)
    normalized.pop("generated_at", None)
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def enqueue_pdf_batch(
    queue_dir: Path,
    bundle: dict[str, Any],
    *,
    repository_root: Path,
) -> dict[str, Any]:
    """Durably enqueue one already parsed PDF without touching the Registry."""
    queue = _external(queue_dir, repository_root)
    if not queue.is_dir():
        raise ValueError("PDF batch queue is unavailable")
    documents = bundle.get("documents", [])
    if len(documents) != 1:
        raise ValueError("PDF intake requires exactly one document")

    batch_id = _bundle_digest(bundle)
    batch = queue / batch_id
    batch.mkdir(exist_ok=True)
    bundle_path, status_path = batch / "bundle.json", batch / "status.json"
    if not bundle_path.exists():
        atomic_write_json(bundle_path, bundle)
    if status_path.exists():
        return read_pdf_batch(queue, batch_id)

    now = _now()
    status = {
        "batch_id": batch_id,
        "stage": "queued",
        "imported": False,
        "indexed": False,
        "chat_ready": False,
        "error": None,
        "attempts": 0,
        "document_sha256": documents[0]["source"]["sha256"],
        "created_at": now,
        "updated_at": now,
    }
    atomic_write_json(status_path, status)
    return status


def read_pdf_batch(queue_dir: Path, batch_id: str) -> dict[str, Any]:
    try:
        value = json.loads(
            (_batch_dir(queue_dir, batch_id) / "status.json").read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as exc:
        raise ValueError("invalid PDF batch status") from exc
    except OSError as exc:
        raise FileNotFoundError("PDF batch was not found") from exc
    if not isinstance(value, dict) or value.get("batch_id") != batch_id:
        raise ValueError("invalid PDF batch status")
    return value


def retry_pdf_batch(queue_dir: Path, batch_id: str) -> dict[str, Any]:
    status = read_pdf_batch(queue_dir, batch_id)
    if status.get("chat_ready") or status.get("stage") != "failed":
        return status
    status.update(stage="queued", error=None, updated_at=_now())
    atomic_write_json(_batch_dir(queue_dir, batch_id) / "status.json", status)
    return status


def load_active_projection(
    runtime_dir: Path | None, pointer_path: Path | None = None
) -> tuple[Path | None, dict[str, Any] | None]:
    """Resolve the atomically selected read-only PDF observation projection."""
    if runtime_dir is None:
        return None, None
    root = runtime_dir.resolve()
    pointer = pointer_path or root / "active.json"
    if not pointer.is_file():
        return None, None
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
        generation_id = value["generation_id"]
        generation = Path(value["path"]).resolve()
        generations = (root / "generations").resolve()
        registry_sha256 = value["registry_sha256"]
        if (
            not isinstance(generation_id, str)
            or generation.name != generation_id
            or generation.parent != generations
            or not generation.is_dir()
            or not isinstance(registry_sha256, str)
            or not _SHA256.fullmatch(registry_sha256)
        ):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("active PDF Wiki projection is invalid") from exc
    return generation, value


def _snapshot_registry(database: Path, destination: Path) -> str:
    before = _file_sha256(database)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(database, temporary)
        if _file_sha256(database) != before or _file_sha256(temporary) != before:
            raise RuntimeError("Registry changed while the Wiki projection was prepared")
        connection = _read_only_connection(temporary)
        try:
            _validate_database(connection)
        finally:
            connection.close()
        os.replace(temporary, destination)
        return before
    finally:
        temporary.unlink(missing_ok=True)


def _projection_contains(
    generation: Path,
    document_sha256: str,
    filename: str,
    pages: set[str],
) -> bool:
    citation = re.compile(
        rf"^PDF:\s*[^;\n]*{re.escape(filename)}[^;\n]*;\s*"
        rf"SHA-256:\s*{re.escape(document_sha256)};\s*page\s+(\d+|unknown)",
        re.MULTILINE,
    )
    found: set[str] = set()
    for page in generation.glob("*.md"):
        found.update(citation.findall(page.read_text(encoding="utf-8")))
    return bool(found) and pages <= found


def _retain_pdf_observations(staging: Path, blocked_occurrence_ids: set[str]) -> None:
    sections: dict[str, str] = {}
    for page in sorted(staging.glob("*.md")):
        lines = page.read_text(encoding="utf-8").splitlines()
        start = 0
        while start < len(lines):
            if not lines[start].startswith("## PDF report observation:"):
                start += 1
                continue
            end = start + 1
            while end < len(lines) and not lines[end].startswith(
                "## PDF report observation:"
            ):
                end += 1
            heading = lines[start]
            occurrence_id = heading.removeprefix("## PDF report observation:").strip()
            block = lines[start:end]
            citation = next(
                (
                    index
                    for index, line in enumerate(block)
                    if line.startswith("PDF: ") and "; SHA-256: " in line
                ),
                None,
            )
            if citation is not None and occurrence_id not in blocked_occurrence_ids:
                sections.setdefault(
                    heading, "\n".join(block[: citation + 1]).strip()
                )
            start = end
    if not sections:
        raise RuntimeError("the imported PDF produced no searchable Wiki observation")

    output = staging / "registry-pdf-intake-observations.md"
    output.write_text(
        "# Registry PDF intake observations\n\n"
        "Registry PDF report observations.\n\n"
        + "\n\n".join(sections.values())
        + "\n",
        encoding="utf-8",
    )
    for page in staging.glob("*.md"):
        if page != output:
            page.unlink()


class PdfIntakePipeline:
    """Single-writer processor for durable management PDF batches."""

    def __init__(
        self,
        queue_dir: Path,
        database: Path,
        backup_dir: Path,
        runtime_wiki_dir: Path,
        reload_chat: Callable[[str], None],
        *,
        repository_root: Path | None = None,
    ) -> None:
        repository = repository_root or Path(__file__).resolve().parents[1]
        self.queue_dir = _external(queue_dir, repository)
        self.database = _external(database, repository)
        self.backup_dir = _external(backup_dir, repository)
        self.runtime_wiki_dir = _external(runtime_wiki_dir, repository)
        self.reload_chat = reload_chat

    @property
    def active_pointer(self) -> Path:
        return self.queue_dir / "active.json"

    @property
    def pending_pointer(self) -> Path:
        return self.queue_dir / "pending.json"

    def _active_projection(self) -> tuple[Path | None, dict[str, Any] | None]:
        return load_active_projection(self.runtime_wiki_dir, self.active_pointer)

    def _activate(self, metadata: dict[str, Any]) -> None:
        atomic_write_json(self.pending_pointer, metadata)
        try:
            self.reload_chat(metadata["generation_id"])
        except Exception:
            _, active = self._active_projection()
            if active is None or active.get("generation_id") != metadata["generation_id"]:
                raise
        finally:
            try:
                pending = json.loads(self.pending_pointer.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pending = None
            if isinstance(pending, dict) and pending.get("generation_id") == metadata["generation_id"]:
                self.pending_pointer.unlink(missing_ok=True)
        _, active = self._active_projection()
        if active is None or active.get("generation_id") != metadata["generation_id"]:
            raise RuntimeError("Chat reload did not activate the requested PDF Wiki projection")

    def _save(self, batch_id: str, status: dict[str, Any], **changes: Any) -> dict[str, Any]:
        status.update(changes, updated_at=_now())
        atomic_write_json(_batch_dir(self.queue_dir, batch_id) / "status.json", status)
        return status

    def _blocked_occurrence_ids(self, current_batch_id: str) -> set[str]:
        queued: set[str] = set()
        publishable: set[str] = set()
        for status_path in self.queue_dir.glob("*/status.json"):
            status = json.loads(status_path.read_text(encoding="utf-8"))
            batch_id = str(status.get("batch_id", ""))
            bundle = json.loads(
                (status_path.parent / "bundle.json").read_text(encoding="utf-8")
            )
            occurrence_ids = {
                str(occurrence["occurrence_id"])
                for article in bundle.get("articles", [])
                for occurrence in article.get("occurrences", [])
            }
            queued.update(occurrence_ids)
            if batch_id == current_batch_id or status.get("chat_ready"):
                publishable.update(occurrence_ids)
        return queued - publishable

    def _build_generation(
        self,
        batch_id: str,
        attempt: int,
        document_sha256: str,
        filename: str,
        pages: set[str],
    ) -> tuple[Path, str, str]:
        generation_id = f"{batch_id[:16]}-{attempt:04d}"
        generations = self.runtime_wiki_dir / "generations"
        generation = generations / generation_id
        snapshot = self.runtime_wiki_dir / "registry-snapshots" / f"{generation_id}.sqlite3"
        generations.mkdir(parents=True, exist_ok=True)
        registry_sha256 = _snapshot_registry(self.database, snapshot)
        staging = Path(tempfile.mkdtemp(prefix=f".{generation_id}.", dir=generations))
        try:
            sync_registry_wiki(snapshot, staging)
            _retain_pdf_observations(staging, self._blocked_occurrence_ids(batch_id))
            if not _projection_contains(staging, document_sha256, filename, pages):
                raise RuntimeError("the imported PDF produced no searchable Wiki projection")
            if generation.exists():
                raise RuntimeError("Wiki generation already exists")
            os.replace(staging, generation)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return generation, generation_id, registry_sha256

    def process(self, batch_id: str) -> dict[str, Any]:
        status = read_pdf_batch(self.queue_dir, batch_id)
        if status.get("chat_ready"):
            return status
        attempt = int(status.get("attempts", 0)) + 1
        status = self._save(
            batch_id,
            status,
            stage="processing",
            error=None,
            chat_ready=False,
            attempts=attempt,
        )
        try:
            bundle = json.loads(
                (_batch_dir(self.queue_dir, batch_id) / "bundle.json").read_text(
                    encoding="utf-8"
                )
            )
            documents = bundle.get("documents", [])
            if len(documents) != 1:
                raise RuntimeError("PDF intake requires exactly one document")
            source = documents[0]["source"]
            document_sha256 = source["sha256"]
            filename = source["filename"]
            pages = {
                str(occurrence.get("page") or "unknown")
                for article in bundle.get("articles", [])
                for occurrence in article.get("occurrences", [])
            }
            for article in bundle.get("articles", []):
                for occurrence in article.get("occurrences", []):
                    occurrence["management_batch_id"] = batch_id
            if not status.get("imported"):
                persist_pdf_intake(self.database, self.backup_dir, bundle)
            status = self._save(batch_id, status, stage="imported", imported=True)

            generation = None
            registry_sha256 = _file_sha256(self.database)
            if status.get("indexed") and status.get("registry_sha256") == registry_sha256:
                candidate = self.runtime_wiki_dir / "generations" / str(
                    status.get("generation_id", "")
                )
                if candidate.is_dir() and _projection_contains(
                    candidate, document_sha256, filename, pages
                ):
                    generation = candidate
            if generation is None:
                generation, generation_id, registry_sha256 = self._build_generation(
                    batch_id, attempt, document_sha256, filename, pages
                )
                status = self._save(
                    batch_id,
                    status,
                    stage="indexed",
                    indexed=True,
                    generation_id=generation_id,
                    registry_sha256=registry_sha256,
                )

            status = self._save(batch_id, status, stage="activating")
            active_generation, active = self._active_projection()
            if (
                active_generation is not None
                and active is not None
                and active["generation_id"] != generation.name
                and str(active.get("batch_created_at", "")) > str(status["created_at"])
            ):
                active_status = read_pdf_batch(
                    self.queue_dir, str(active.get("batch_id", ""))
                )
                if _projection_contains(
                    active_generation, document_sha256, filename, pages
                ):
                    if not active_status.get("chat_ready"):
                        self._activate(active)
                        self._save(
                            active_status["batch_id"],
                            active_status,
                            stage="chat_ready",
                            chat_ready=True,
                            error=None,
                        )
                    return self._save(
                        batch_id,
                        status,
                        stage="chat_ready",
                        indexed=True,
                        chat_ready=True,
                        generation_id=active["generation_id"],
                        registry_sha256=active["registry_sha256"],
                    )
                active_filename = active.get("filename")
                active_pages = {
                    str(page) for page in active.get("pages", []) if page is not None
                }
                if (
                    isinstance(active_filename, str)
                    and active_pages
                    and _projection_contains(
                        generation,
                        active["document_sha256"],
                        active_filename,
                        active_pages,
                    )
                ):
                    replacement = {
                        "batch_id": active["batch_id"],
                        "batch_created_at": active["batch_created_at"],
                        "generation_id": generation.name,
                        "path": str(generation.resolve()),
                        "registry_sha256": registry_sha256,
                        "document_sha256": active["document_sha256"],
                        "filename": active_filename,
                        "pages": sorted(active_pages),
                        "activated_at": _now(),
                    }
                    self._activate(replacement)
                    self._save(
                        active_status["batch_id"],
                        active_status,
                        stage="chat_ready",
                        chat_ready=True,
                        error=None,
                        generation_id=generation.name,
                        registry_sha256=registry_sha256,
                    )
                    return self._save(
                        batch_id,
                        status,
                        stage="chat_ready",
                        indexed=True,
                        chat_ready=True,
                        generation_id=generation.name,
                        registry_sha256=registry_sha256,
                    )
                raise RuntimeError("a newer Wiki generation is already active")
            self._activate(
                {
                    "batch_id": batch_id,
                    "batch_created_at": status["created_at"],
                    "generation_id": generation.name,
                    "path": str(generation.resolve()),
                    "registry_sha256": registry_sha256,
                    "document_sha256": document_sha256,
                    "filename": filename,
                    "pages": sorted(pages),
                    "activated_at": _now(),
                }
            )
            return self._save(
                batch_id, status, stage="chat_ready", chat_ready=True, error=None
            )
        except Exception as exc:
            return self._save(
                batch_id,
                status,
                stage="failed",
                chat_ready=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    def process_next(self) -> dict[str, Any] | None:
        resumable = {"queued", "processing", "imported", "indexed", "activating"}
        candidates: list[dict[str, Any]] = []
        for status_path in sorted(self.queue_dir.glob("*/status.json")):
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if status.get("stage") in resumable:
                candidates.append(status)
        if not candidates:
            return None
        status = min(
            candidates,
            key=lambda item: (str(item.get("created_at", "")), str(item.get("batch_id", ""))),
        )
        return self.process(str(status.get("batch_id", "")))
