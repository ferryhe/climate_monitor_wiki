"""Durable handoff from authenticated PDF intake to the Registry and Chat."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from climate_delivery.io import atomic_write_json, exclusive_lock
from .pdf_intake import _existing_occurrence_ids, persist_pdf_intake
from .persistent import _file_sha256, _read_only_connection, _validate_database
from .wiki import render_runtime_registry, snapshot_registry
from .publication import public_revision, canonical_entity


_SHA256 = re.compile(r"[0-9a-f]{64}")
_RFC3339_TIMESTAMP = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)\Z"
)
PROJECTION_MANIFEST_SCHEMA = "climate-intake-projection.v1"


def _pending_pdf_entities(connection, document_sha256):
    identities = [("pdf_article", row[0]) for row in connection.execute(
        "SELECT article_id FROM pdf_intake_article_occurrences WHERE source_document_sha256=?", (document_sha256,))]
    identities.extend(("pdf_meeting", row[0]) for row in connection.execute(
        "SELECT event_id FROM pdf_intake_calendar_items WHERE source_document_sha256=?", (document_sha256,)))
    pending = set()
    for kind, identity in identities:
        kind, identity = canonical_entity(connection, kind, identity)
        state = connection.execute("SELECT published_candidate_sha256,latest_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", (kind, identity)).fetchone()
        if state is None or state[0] != state[1]:
            pending.add((kind, identity))
    return pending


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_collected_at(value: Any) -> bool:
    if not isinstance(value, str) or _RFC3339_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


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
        "failure_history": [],
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


def list_pdf_batches(queue_dir: Path) -> dict[str, Any]:
    """Return the PDF queue's current stages and independent milestones."""
    batches: list[dict[str, Any]] = []
    for status_path in queue_dir.glob("*/status.json"):
        status = read_pdf_batch(queue_dir, status_path.parent.name)
        bundle = json.loads((status_path.parent / "bundle.json").read_text(encoding="utf-8"))
        documents = bundle.get("documents", [])
        filename = documents[0].get("source", {}).get("filename") if len(documents) == 1 else None
        batches.append({**status, "filename": filename})
    batches.sort(key=lambda item: (str(item.get("created_at", "")), item["batch_id"]), reverse=True)
    stage_counts: dict[str, int] = {}
    for status in batches:
        stage = str(status.get("stage", "unknown"))
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
    return {
        "total_batches": len(batches),
        "stage_counts": dict(sorted(stage_counts.items())),
        "milestone_counts": {
            name: sum(bool(status.get(name)) for status in batches)
            for name in ("imported", "indexed", "chat_ready")
        },
        "batches": batches,
    }


def _validate_pdf_binding(status, database=None):
    binding = status.get("registry_database")
    if not binding:
        if not status.get("imported") and status.get("stage") == "queued" and status.get("attempts", 0) == 0:
            return
        raise ValueError("historical started PDF batch has no frozen Registry binding; preserve its audit and create a new task")
    from .publication import resolve_database
    resolve_database(database or binding, frozen=binding)
    if os.getenv("CLIMATE_REGISTRY_DB", "").strip():
        resolve_database(frozen=binding)


def retry_pdf_batch(queue_dir: Path, batch_id: str) -> dict[str, Any]:
    with exclusive_lock(queue_dir, "intake-writer"):
        status = read_pdf_batch(queue_dir, batch_id)
        if status.get("chat_ready") or status.get("stage") != "failed":
            return status
        _validate_pdf_binding(status)
        if status.get("error"):
            failure = {
                "at": status.get("updated_at"),
                "attempt": status.get("attempts"),
                "reason": status["error"],
            }
            history = list(status.get("failure_history", []))
            if not any(
                item.get("attempt") == failure["attempt"]
                and item.get("reason") == failure["reason"]
                for item in history
                if isinstance(item, dict)
            ):
                history.append(failure)
            status["failure_history"] = history
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


def load_projection_manifest(
    generation: Path | None, metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Load the exact activated intake allowlist, when the generation has one."""
    if generation is None or metadata is None or "manifest_sha256" not in metadata:
        return None
    path = generation / "intake-manifest.json"
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("active intake projection manifest is invalid") from exc
    if (
        hashlib.sha256(payload).hexdigest() != metadata["manifest_sha256"]
        or value.get("schema_version") != PROJECTION_MANIFEST_SCHEMA
        or value.get("generation_id") != metadata.get("generation_id")
        or not isinstance(value.get("web_items"), list)
        or not isinstance(value.get("pdf_occurrence_ids"), list)
        or any(
            not isinstance(item, dict)
            or set(item) - {"review"} not in ({
                "acquisition_item_id", "batch_id", "article_id", "content_version_id",
                "publication_date", "publication_date_evidence",
            }, {
                "acquisition_item_id", "batch_id", "article_id", "content_version_id",
                "collected_at", "publication_date", "publication_date_evidence",
            })
            or any(not isinstance(item.get(key), str) or not item[key] for key in (
                "acquisition_item_id", "batch_id", "article_id", "content_version_id",
                "publication_date",
            ))
            or "collected_at" in item and not _valid_collected_at(item["collected_at"])
            or not isinstance(item.get("publication_date_evidence"), dict)
            or not item["publication_date_evidence"]
            for item in value.get("web_items", [])
        )
        or any(not isinstance(item, str) or not item for item in value.get("pdf_occurrence_ids", []))
        or not isinstance(value.get("pdf_calendar_occurrence_ids", []), list)
        or any(not isinstance(item, str) or not item for item in value.get("pdf_calendar_occurrence_ids", []))
    ):
        raise RuntimeError("active intake projection manifest is invalid")
    identities = [item["acquisition_item_id"] for item in value["web_items"]]
    try:
        valid_dates = all(
            datetime.strptime(item["publication_date"], "%Y-%m-%d").strftime("%Y-%m-%d")
            == item["publication_date"]
            for item in value["web_items"]
        )
    except ValueError:
        valid_dates = False
    if len(identities) != len(set(identities)) or not valid_dates:
        raise RuntimeError("active intake projection manifest is invalid")
    return value


def _write_projection_manifest(
    generation: Path,
    generation_id: str,
    *,
    web_items: list[dict[str, Any]],
    pdf_occurrence_ids: set[str],
    pdf_calendar_occurrence_ids: set[str] | None = None,
) -> str:
    value = {
        "schema_version": PROJECTION_MANIFEST_SCHEMA,
        "generation_id": generation_id,
        "web_items": sorted(web_items, key=lambda item: item["acquisition_item_id"]),
        "pdf_occurrence_ids": sorted(pdf_occurrence_ids),
        "pdf_calendar_occurrence_ids": sorted(pdf_calendar_occurrence_ids or []),
    }
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    (generation / "intake-manifest.json").write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


_snapshot_registry = snapshot_registry


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


def _active_pdf_occurrence_ids(
    generation: Path | None, manifest: dict[str, Any] | None
) -> set[str]:
    if manifest is not None:
        return {str(value) for value in manifest["pdf_occurrence_ids"]}
    if generation is None:
        return set()
    return {
        match.group(1)
        for page in generation.glob("*.md")
        for match in re.finditer(
            r"^## PDF report observation:\s*(\S+)\s*$",
            page.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    }


def _active_calendar_ids(database: Path, manifest: dict[str, Any] | None, pdf_ids: set[str]) -> set[str]:
    if manifest is not None and "pdf_calendar_occurrence_ids" in manifest:
        return set(manifest["pdf_calendar_occurrence_ids"])
    with sqlite3.connect(database) as connection:
        documents = {row[1] for row in connection.execute(
            "SELECT occurrence_id,source_document_sha256 FROM pdf_intake_article_occurrences") if row[0] in pdf_ids}
        return {row[0] for row in connection.execute(
            "SELECT occurrence_id,source_document_sha256 FROM pdf_intake_calendar_items") if row[1] in documents}


def _active_registry_snapshot(
    runtime_wiki_dir: Path,
    active: dict[str, Any] | None,
    kind: str,
    *,
    required: bool,
) -> Path | None:
    if not required:
        return None
    if active is None:
        raise RuntimeError(f"active {kind} Registry snapshot is missing")
    raw_path = active.get(f"{kind}_registry_snapshot")
    raw_sha256 = active.get(f"{kind}_registry_sha256")
    snapshot = Path(str(raw_path or "")).resolve()
    if (
        snapshot.parent != (runtime_wiki_dir / "registry-snapshots").resolve()
        or not snapshot.is_file()
        or not isinstance(raw_sha256, str)
        or _file_sha256(snapshot) != raw_sha256
    ):
        raise RuntimeError(f"active {kind} Registry snapshot is invalid")
    connection = _read_only_connection(snapshot)
    try:
        _validate_database(connection)
    finally:
        connection.close()
    return snapshot


def _stored_bundle_occurrence_ids(database: Path, bundle: dict[str, Any]) -> set[str]:
    """Resolve this bundle's natural identities to their persisted occurrence IDs."""
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        matches, _ = _existing_occurrence_ids(
            connection,
            bundle.get("documents", []),
            bundle.get("articles", []),
            bundle.get("calendar_items", []),
        )
    finally:
        connection.close()
    incoming = {
        str(occurrence["occurrence_id"])
        for article in bundle.get("articles", [])
        for occurrence in article.get("occurrences", [])
    }
    if set(matches) != incoming:
        raise RuntimeError("persisted PDF occurrence identity is missing")
    return {str(value) for value in matches.values()}


def _stored_calendar_ids(database: Path, bundle: dict[str, Any]) -> set[str]:
    with sqlite3.connect(database) as connection:
        _, matches = _existing_occurrence_ids(connection, bundle.get("documents", []),
            bundle.get("articles", []), bundle.get("calendar_items", []))
    if set(matches) != {item["occurrence_id"] for item in bundle.get("calendar_items", [])}:
        raise RuntimeError("persisted PDF calendar identity is missing")
    return set(matches.values())


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
        self.repository_root = repository.resolve()
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

    def _build_generation(
        self,
        batch_id: str,
        attempt: int,
        document_sha256: str,
        filename: str,
        pages: set[str],
        occurrence_ids: set[str],
        calendar_ids: set[str] | None = None,
    ) -> tuple[Path, str, str, str]:
        generation_id = f"{batch_id[:16]}-{attempt:04d}"
        generations = self.runtime_wiki_dir / "generations"
        generation = generations / generation_id
        snapshot = self.runtime_wiki_dir / "registry-snapshots" / f"{generation_id}.sqlite3"
        generations.mkdir(parents=True, exist_ok=True)
        registry_sha256 = _snapshot_registry(self.database, snapshot)
        active_generation, active_metadata = self._active_projection()
        active_manifest = load_projection_manifest(active_generation, active_metadata)
        web_items = list((active_manifest or {}).get("web_items", []))
        pdf_occurrence_ids = _active_pdf_occurrence_ids(
            active_generation, active_manifest
        ) | occurrence_ids
        pdf_calendar_ids = _active_calendar_ids(self.database, active_manifest,
            _active_pdf_occurrence_ids(active_generation, active_manifest)) | (calendar_ids or set())
        web_snapshot = _active_registry_snapshot(
            self.runtime_wiki_dir,
            active_metadata,
            "web",
            required=bool(web_items),
        )
        staging = Path(tempfile.mkdtemp(prefix=f".{generation_id}.", dir=generations))
        try:
            render_runtime_registry(
                staging,
                web_database=snapshot if public_revision(snapshot) is not None else web_snapshot,
                pdf_database=snapshot,
                manifest={
                    "web_items": web_items,
                    "pdf_occurrence_ids": sorted(pdf_occurrence_ids),
                    "pdf_calendar_occurrence_ids": sorted(pdf_calendar_ids),
                },
            )
            if not _projection_contains(staging, document_sha256, filename, pages):
                raise RuntimeError("the imported PDF produced no searchable Wiki projection")
            if generation.exists():
                raise RuntimeError("Wiki generation already exists")
            os.replace(staging, generation)
            manifest_sha256 = _write_projection_manifest(
                generation,
                generation_id,
                web_items=web_items,
                pdf_occurrence_ids=pdf_occurrence_ids,
                pdf_calendar_occurrence_ids=pdf_calendar_ids,
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return generation, generation_id, registry_sha256, manifest_sha256

    def process(self, batch_id: str) -> dict[str, Any]:
        with exclusive_lock(self.queue_dir, "intake-writer"):
            return self._process_unlocked(batch_id)

    def _process_unlocked(self, batch_id: str) -> dict[str, Any]:
        status = read_pdf_batch(self.queue_dir, batch_id)
        if status.get("chat_ready"):
            return status
        _validate_pdf_binding(status, self.database)
        attempt = int(status.get("attempts", 0)) + 1
        status = self._save(
            batch_id,
            status,
            stage="processing",
            error=None,
            chat_ready=False,
            attempts=attempt,
            registry_database=str(self.database.resolve()),
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
            pages.update(str(item.get("page") or "unknown") for item in bundle.get("calendar_items", []))
            for article in bundle.get("articles", []):
                for occurrence in article.get("occurrences", []):
                    occurrence["management_batch_id"] = batch_id
            if not status.get("imported"):
                persist_pdf_intake(self.database, self.backup_dir, bundle)
            status = self._save(batch_id, status, stage="imported", imported=True,
                registry_database=str(self.database.resolve()))
            occurrence_ids = _stored_bundle_occurrence_ids(self.database, bundle)
            calendar_ids = _stored_calendar_ids(self.database, bundle)

            from .publication import public_revision, prepare_review
            if public_revision(self.database) is not None:
                with sqlite3.connect(f"{self.database.resolve().as_uri()}?mode=ro", uri=True) as connection:
                    pending_ids = _pending_pdf_entities(connection, document_sha256)
                if pending_ids:
                    review_dir = os.getenv("CLIMATE_ACQUISITION_RUN_DIR", "").strip()
                    review_root = Path(review_dir) / "registry-review" if review_dir else None
                    if review_root:
                        prepare_review(self.database, review_root)
                    return self._save(batch_id, status, stage="pending_review", indexed=False, chat_ready=False,
                        pending_review_count=len(pending_ids), review_root=str(review_root) if review_root else None)
            generation = None
            source_registry_sha256 = _file_sha256(self.database)
            if status.get("indexed") and status.get(
                "source_registry_sha256", status.get("registry_sha256")
            ) == source_registry_sha256:
                candidate = self.runtime_wiki_dir / "generations" / str(
                    status.get("generation_id", "")
                )
                active_generation, active_metadata = self._active_projection()
                active_manifest = load_projection_manifest(active_generation, active_metadata)
                candidate_manifest = load_projection_manifest(candidate, {
                    "generation_id": candidate.name,
                    "manifest_sha256": status.get("manifest_sha256"),
                }) if candidate.is_dir() and status.get("manifest_sha256") else None
                expected_pdf_ids = _active_pdf_occurrence_ids(
                    active_generation, active_manifest
                ) | occurrence_ids
                if (
                    candidate.is_dir()
                    and _projection_contains(candidate, document_sha256, filename, pages)
                    and list((candidate_manifest or {}).get("web_items", []))
                    == list((active_manifest or {}).get("web_items", []))
                    and set((candidate_manifest or {}).get("pdf_occurrence_ids", []))
                    == expected_pdf_ids
                ):
                    generation = candidate
                    generation_id = candidate.name
                    registry_sha256 = str(status["registry_sha256"])
                    manifest_sha256 = str(status["manifest_sha256"])
            if generation is None:
                generation, generation_id, registry_sha256, manifest_sha256 = self._build_generation(
                    batch_id,
                    attempt,
                    document_sha256,
                    filename,
                    pages,
                    occurrence_ids,
                    calendar_ids,
                )
                _, prior_active = self._active_projection()
                status = self._save(
                    batch_id,
                    status,
                    stage="indexed",
                    indexed=True,
                    generation_id=generation_id,
                    registry_sha256=registry_sha256,
                    source_registry_sha256=source_registry_sha256,
                    manifest_sha256=manifest_sha256,
                    registry_snapshot=str((self.runtime_wiki_dir / "registry-snapshots" / f"{generation_id}.sqlite3").resolve()),
                    pdf_registry_snapshot=str((self.runtime_wiki_dir / "registry-snapshots" / f"{generation_id}.sqlite3").resolve()),
                    pdf_registry_sha256=registry_sha256,
                    web_registry_snapshot=(prior_active or {}).get("web_registry_snapshot"),
                    web_registry_sha256=(prior_active or {}).get("web_registry_sha256"),
                )

            status = self._save(batch_id, status, stage="activating")
            active_generation, active = self._active_projection()
            active_kind = active.get("projection_kind") if active is not None else None
            if (
                active is not None
                and active_kind is None
                and _SHA256.fullmatch(str(active.get("batch_id", ""))) is not None
                and isinstance(active.get("document_sha256"), str)
            ):
                active_kind = "pdf"
            if active is not None and active_kind not in {"pdf", "web"}:
                raise RuntimeError("active intake projection kind is invalid")
            if (
                active_generation is not None
                and active is not None
                and active["generation_id"] != generation.name
                and str(active.get("batch_created_at", "")) > str(status["created_at"])
                and active_kind == "pdf"
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
                        "projection_kind": "pdf",
                        "generation_id": generation.name,
                        "path": str(generation.resolve()),
                        "registry_sha256": registry_sha256,
                        "registry_snapshot": status.get("registry_snapshot"),
                        "pdf_registry_snapshot": status.get("pdf_registry_snapshot"),
                        "pdf_registry_sha256": status.get("pdf_registry_sha256"),
                        "web_registry_snapshot": status.get("web_registry_snapshot"),
                        "web_registry_sha256": status.get("web_registry_sha256"),
                        "document_sha256": active["document_sha256"],
                        "filename": active_filename,
                        "pages": sorted(active_pages),
                        "activated_at": _now(),
                        "manifest_sha256": status.get("manifest_sha256"),
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
                    "projection_kind": "pdf",
                    "generation_id": generation.name,
                    "path": str(generation.resolve()),
                    "registry_sha256": registry_sha256,
                    "registry_snapshot": status.get("registry_snapshot"),
                    "pdf_registry_snapshot": status.get("pdf_registry_snapshot"),
                    "pdf_registry_sha256": status.get("pdf_registry_sha256"),
                    "web_registry_snapshot": status.get("web_registry_snapshot"),
                    "web_registry_sha256": status.get("web_registry_sha256"),
                    "document_sha256": document_sha256,
                    "filename": filename,
                    "pages": sorted(pages),
                    "activated_at": _now(),
                    "manifest_sha256": status.get("manifest_sha256"),
                }
            )
            return self._save(
                batch_id, status, stage="chat_ready", chat_ready=True, error=None
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            history = list(status.get("failure_history", []))
            history.append({"at": _now(), "attempt": attempt, "reason": reason})
            return self._save(
                batch_id,
                status,
                stage="failed",
                chat_ready=False,
                error=reason,
                failure_history=history,
            )

    def refresh_checks(self, run_id: str) -> dict[str, Any]:
        """Refresh the existing writer after exact Registry approval."""
        with exclusive_lock(self.queue_dir, "intake-writer"):
            prior_generation, metadata = self._active_projection()
            if prior_generation is None or metadata is None:
                return {"status": "not_active", "run_id": run_id}
            manifest = load_projection_manifest(prior_generation, metadata)
            pdf_ids = _active_pdf_occurrence_ids(prior_generation, manifest)
            calendar_ids = _active_calendar_ids(self.database, manifest, pdf_ids)
            generation_id = "checks-" + uuid.uuid4().hex
            generation = self.runtime_wiki_dir / "generations" / generation_id
            snapshot = self.runtime_wiki_dir / "registry-snapshots" / f"{generation_id}.sqlite3"
            digest = _snapshot_registry(self.database, snapshot)
            web_items = list((manifest or {}).get("web_items", []))
            web_snapshot = _active_registry_snapshot(self.runtime_wiki_dir, metadata, "web", required=bool(web_items))
            generation.mkdir()
            render_runtime_registry(generation, web_database=snapshot if public_revision(snapshot) is not None else web_snapshot, pdf_database=snapshot,
                manifest={"web_items": web_items, "pdf_occurrence_ids": sorted(pdf_ids),
                    "pdf_calendar_occurrence_ids": sorted(calendar_ids)})
            manifest_sha = _write_projection_manifest(generation, generation_id, web_items=web_items,
                pdf_occurrence_ids=pdf_ids, pdf_calendar_occurrence_ids=calendar_ids)
            updated = {**metadata, "generation_id": generation_id, "path": str(generation.resolve()),
                "pdf_registry_snapshot": str(snapshot.resolve()), "pdf_registry_sha256": digest,
                "manifest_sha256": manifest_sha, "activated_at": _now(), "check_run_id": run_id}
            if metadata.get("projection_kind") != "web":
                updated.update(registry_snapshot=str(snapshot.resolve()), registry_sha256=digest)
            self._activate(updated)
            return {"status": "chat_ready", "generation_id": generation_id, "run_id": run_id}

    def process_next(self) -> dict[str, Any] | None:
        resumable = {"queued", "processing", "imported", "indexed", "activating"}
        candidates: list[dict[str, Any]] = []
        for status_path in sorted(self.queue_dir.glob("*/status.json")):
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if status.get("stage") in resumable or status.get("stage") == "pending_review":
                _validate_pdf_binding(status, self.database)
            ready_review = False
            if status.get("stage") == "pending_review":
                with sqlite3.connect(f"{self.database.resolve().as_uri()}?mode=ro", uri=True) as connection:
                    ready_review = not _pending_pdf_entities(connection, status["document_sha256"])
            if status.get("stage") in resumable or ready_review:
                candidates.append({**status, "projection_kind": "pdf"})
        for status_path in sorted((self.queue_dir / "web").glob("*/status.json")):
            ready_review = False
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            retry_requested = (status_path.parent / "retry.json").is_file()
            request = None
            if status.get("stage") in resumable or status.get("stage") == "pending_review" or (status.get("stage") == "failed" and retry_requested):
                from .web_ingest_pipeline import read_web_activation_request, preflight_web_activation
                preflight_web_activation(self.queue_dir, status["batch_id"], self.database)
                try:
                    request = read_web_activation_request(self.queue_dir, status["batch_id"])
                except (ValueError, RuntimeError, OSError, sqlite3.Error):
                    pass  # Preserve ordinary invalid-request handling at dispatch.
            if status.get("stage") == "pending_review":
                try:
                    if request is None:
                        request=read_web_activation_request(self.queue_dir,status["batch_id"])
                    with sqlite3.connect(f"{self.database.as_uri()}?mode=ro",uri=True) as connection:
                        ready_review=all(connection.execute("SELECT 1 FROM registry_publication WHERE entity_kind='article' AND entity_id=? AND published_candidate_sha256 IS NOT NULL AND published_candidate_sha256=latest_candidate_sha256", (item["article_id"],)).fetchone() for item in request["web_items"])
                except (ValueError,RuntimeError,OSError,sqlite3.Error):
                    ready_review=False
            if status.get("stage") in resumable or ready_review or (
                status.get("stage") == "failed" and retry_requested
            ):
                candidates.append({
                    **status, "projection_kind": "web", "status_path": status_path,
                })
        if not candidates:
            return None
        status = min(
            candidates,
            key=lambda item: (str(item.get("created_at", "")), str(item.get("batch_id", ""))),
        )
        batch_id = str(status.get("batch_id", ""))
        if status["projection_kind"] == "web":
            from .web_ingest_pipeline import WebIngestPipeline, read_web_activation_request, preflight_web_activation

            preflight_web_activation(self.queue_dir, batch_id, self.database)
            try:
                request = read_web_activation_request(self.queue_dir, batch_id)
            except Exception as exc:
                status_path = Path(status["status_path"])
                persisted = json.loads(status_path.read_text(encoding="utf-8"))
                persisted.update(
                    stage="failed", chat_ready=False,
                    error=f"{type(exc).__name__}: {exc}", updated_at=_now(),
                )
                atomic_write_json(status_path, persisted)
                (status_path.parent / "retry.json").unlink(missing_ok=True)
                return persisted
            return WebIngestPipeline(
                self.queue_dir,
                self.database,
                self.runtime_wiki_dir,
                self.reload_chat,
                repository_root=self.repository_root,
            ).process(batch_id)
        return self.process(batch_id)
