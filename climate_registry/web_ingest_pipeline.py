"""Activate one frozen web-acquisition batch for Wiki, Chat, and range reports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from climate_delivery.io import atomic_write_json, exclusive_lock

from .pdf_pipeline import (
    _external,
    _snapshot_registry,
    _write_projection_manifest,
    _active_calendar_ids,
    load_active_projection,
    load_projection_manifest,
)
from .persistent import _file_sha256, _read_only_connection, _validate_database
from .wiki import render_runtime_registry


WEB_ACTIVATION_REQUEST_SCHEMA = "climate-web-activation-request.v1"
_WEB_ITEM_KEYS = frozenset({
    "acquisition_item_id", "batch_id", "article_id", "content_version_id",
    "publication_date", "publication_date_evidence",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _web_item_matches(expected: Any, actual: dict[str, Any]) -> bool:
    if not isinstance(expected, dict) or set(expected) not in (
        _WEB_ITEM_KEYS, _WEB_ITEM_KEYS | {"collected_at"},
    ):
        return False
    if "collected_at" not in expected:
        actual = {key: value for key, value in actual.items() if key != "collected_at"}
    return expected == actual


def _web_items_match(expected: Any, actual: list[dict[str, Any]]) -> bool:
    return (
        isinstance(expected, list)
        and len(expected) == len(actual)
        and all(_web_item_matches(old, new) for old, new in zip(expected, actual))
    )


def _status_path(queue_dir: Path, batch_id: str) -> Path:
    key = hashlib.sha256(batch_id.encode()).hexdigest()
    return queue_dir / "web" / key / "status.json"


def _job_dir(queue_dir: Path, batch_id: str) -> Path:
    return _status_path(queue_dir, batch_id).parent


def _read_status(queue_dir: Path, batch_id: str) -> dict[str, Any] | None:
    path = _status_path(queue_dir, batch_id)
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("batch_id") != batch_id:
        raise RuntimeError("invalid web ingest status")
    return value


def read_web_activation_request(queue_dir: Path, batch_id: str) -> dict[str, Any]:
    job = _job_dir(queue_dir, batch_id)
    try:
        request = json.loads((job / "request.json").read_text(encoding="utf-8"))
        snapshot = (job / request["registry_snapshot"]).resolve()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("web activation request is invalid") from exc
    if (
        request.get("schema_version") != WEB_ACTIVATION_REQUEST_SCHEMA
        or request.get("batch_id") != batch_id
        or request.get("registry_snapshot") != "registry.sqlite3"
        or snapshot.parent != job.resolve()
        or not snapshot.is_file()
        or not isinstance(request.get("registry_sha256"), str)
        or _file_sha256(snapshot) != request["registry_sha256"]
        or not isinstance(request.get("frozen_payload_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", request["frozen_payload_sha256"])
        or not isinstance(request.get("web_items"), list)
    ):
        raise RuntimeError("web activation request is invalid")
    validation = _read_only_connection(snapshot)
    try:
        _validate_database(validation)
    finally:
        validation.close()
    expected = request["web_items"]
    if not _web_items_match(
        expected, [_manifest_item(item) for item in _batch_items(snapshot, batch_id)]
    ):
        raise RuntimeError("web activation request differs from its Registry snapshot")
    request["registry_snapshot_path"] = str(snapshot)
    return request


def enqueue_web_activation(
    queue_dir: Path,
    database: Path,
    batch_id: str,
    *,
    frozen_payload_sha256: str,
    repository_root: Path,
) -> dict[str, Any]:
    """Create or re-observe one immutable Runtime-to-writer activation job."""
    queue = _external(queue_dir, repository_root)
    database = _external(database, repository_root)
    if not queue.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", frozen_payload_sha256):
        raise ValueError("web activation queue or frozen payload digest is invalid")
    items = [_manifest_item(item) for item in _batch_items(database, batch_id)]
    job = _job_dir(queue, batch_id)
    job.mkdir(parents=True, exist_ok=True)
    request_path = job / "request.json"
    if not request_path.exists():
        snapshot = job / "registry.sqlite3"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".registry.", suffix=".sqlite3", dir=job,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            source = _read_only_connection(database)
            destination = sqlite3.connect(temporary)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            validation = _read_only_connection(temporary)
            try:
                _validate_database(validation)
            finally:
                validation.close()
            if [_manifest_item(item) for item in _batch_items(temporary, batch_id)] != items:
                raise RuntimeError("web activation snapshot differs from the frozen batch")
            if snapshot.exists():
                temporary.unlink()
            else:
                os.replace(temporary, snapshot)
        finally:
            temporary.unlink(missing_ok=True)
        validation = _read_only_connection(snapshot)
        try:
            _validate_database(validation)
        finally:
            validation.close()
        if [_manifest_item(item) for item in _batch_items(snapshot, batch_id)] != items:
            raise RuntimeError("existing web activation snapshot differs from the frozen batch")
        request = {
            "schema_version": WEB_ACTIVATION_REQUEST_SCHEMA,
            "batch_id": batch_id,
            "frozen_payload_sha256": frozen_payload_sha256,
            "registry_snapshot": "registry.sqlite3",
            "registry_sha256": _file_sha256(snapshot),
            "web_items": items,
            "created_at": _now(),
        }
        atomic_write_json(request_path, request)
    request = read_web_activation_request(queue, batch_id)
    if (
        request["frozen_payload_sha256"] != frozen_payload_sha256
        or not _web_items_match(request["web_items"], items)
    ):
        raise RuntimeError("web activation request is immutable")
    status = _read_status(queue, batch_id)
    if status is None:
        status = {
            "batch_id": batch_id, "stage": "queued", "acquisition_complete": True,
            "indexed": False, "chat_ready": False, "attempts": 0, "error": None,
            "created_at": request["created_at"], "updated_at": _now(),
        }
        atomic_write_json(_status_path(queue, batch_id), status)
    elif status.get("stage") == "failed":
        retry = _job_dir(queue, batch_id) / "retry.json"
        if not retry.exists():
            atomic_write_json(retry, {"batch_id": batch_id, "requested_at": _now()})
    return status


def wait_web_activation(
    queue_dir: Path, batch_id: str, *, timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while True:
        status = _read_status(queue_dir, batch_id)
        if status is None:
            raise RuntimeError("web activation status is missing")
        retry_pending = (_job_dir(queue_dir, batch_id) / "retry.json").is_file()
        if status.get("chat_ready") or (
            status.get("stage") == "failed" and not retry_pending
        ):
            return status
        if time.monotonic() >= deadline:
            return {**status, "error": "web activation is still queued for the intake writer"}
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def _active_pdf_ids(generation: Path | None, manifest: dict[str, Any] | None) -> set[str]:
    if manifest is not None:
        return {str(value) for value in manifest["pdf_occurrence_ids"]}
    if generation is None:
        return set()
    ids: set[str] = set()
    for page in generation.glob("*.md"):
        ids.update(re.findall(r"^## PDF report observation:\s*(\S+)\s*$", page.read_text(encoding="utf-8"), re.MULTILINE))
    return ids


def _active_pdf_snapshot(
    runtime_wiki_dir: Path,
    active: dict[str, Any] | None,
    pdf_ids: set[str],
) -> tuple[str | None, str | None]:
    if not pdf_ids:
        return None, None
    if active is None:
        raise RuntimeError("active PDF projection metadata is missing")
    raw_path = active.get("pdf_registry_snapshot") or active.get("registry_snapshot")
    snapshot = (
        Path(str(raw_path)).resolve()
        if raw_path
        else (runtime_wiki_dir / "registry-snapshots" / f"{active.get('generation_id')}.sqlite3").resolve()
    )
    expected_parent = (runtime_wiki_dir / "registry-snapshots").resolve()
    expected_sha256 = active.get("pdf_registry_sha256") or active.get("registry_sha256")
    if (
        snapshot.parent != expected_parent
        or not snapshot.is_file()
        or not isinstance(expected_sha256, str)
        or _file_sha256(snapshot) != expected_sha256
    ):
        raise RuntimeError("legacy active PDF Registry snapshot is invalid")
    return str(snapshot), expected_sha256


def _active_web_snapshot(
    runtime_wiki_dir: Path,
    active: dict[str, Any] | None,
    web_items: list[dict[str, Any]],
) -> Path | None:
    if not web_items:
        return None
    if active is None:
        raise RuntimeError("active web projection metadata is missing")
    snapshot = Path(str(active.get("web_registry_snapshot") or "")).resolve()
    expected_sha256 = active.get("web_registry_sha256")
    if (
        snapshot.parent != (runtime_wiki_dir / "registry-snapshots").resolve()
        or not snapshot.is_file()
        or not isinstance(expected_sha256, str)
        or _file_sha256(snapshot) != expected_sha256
    ):
        raise RuntimeError("active web Registry snapshot is invalid")
    validation = _read_only_connection(snapshot)
    try:
        _validate_database(validation)
    finally:
        validation.close()
    return snapshot


def _batch_items(database: Path, batch_id: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        batch = connection.execute(
            "SELECT frozen_at FROM acquisition_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None or not batch["frozen_at"]:
            raise RuntimeError("web acquisition batch is not frozen")
        rows = connection.execute(
            """
            SELECT i.acquisition_item_id, i.batch_id, i.article_id, i.content_version_id,
                   i.publication_date, i.publication_date_evidence_json, i.raw_url,
                   i.title, i.summary, i.discovered_at, i.source_name,
                   a.canonical_url, a.display_policy, c.content_sha256,
                   f.fetched_at AS collected_at,
                   c.markdown_content
            FROM acquisition_items i
            JOIN articles a ON a.article_id=i.article_id
            JOIN article_fetches f ON f.fetch_id=i.fetch_id AND f.fetch_status='success'
            JOIN article_content_versions c
              ON c.article_id=i.article_id AND c.content_version_id=i.content_version_id
            WHERE i.batch_id=? AND i.selection_status='selected'
              AND i.processing_status='complete' AND i.material_status='full_content'
              AND i.date_status='eligible' AND i.publication_date IS NOT NULL
              AND i.publication_date_evidence_json IS NOT NULL
            ORDER BY i.ordinal
            """,
            (batch_id,),
        ).fetchall()
    finally:
        connection.close()
    items = [dict(row) for row in rows]
    if not items:
        raise RuntimeError("frozen web acquisition batch has no indexable items")
    for item in items:
        evidence = json.loads(item.pop("publication_date_evidence_json"))
        if not isinstance(evidence, dict) or not evidence:
            raise RuntimeError("web acquisition item lacks publication-date evidence")
        item["publication_date_evidence"] = evidence
    return items


def _manifest_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in (
            "acquisition_item_id", "batch_id", "article_id", "content_version_id",
            "collected_at", "publication_date", "publication_date_evidence",
        )
    }


def _resolve_items(database: Path, allowlist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    by_batch: dict[str, list[dict[str, Any]]] = {}
    for item in allowlist:
        by_batch.setdefault(item["batch_id"], []).append(item)
    for batch_id, expected in by_batch.items():
        actual = {item["acquisition_item_id"]: item for item in _batch_items(database, batch_id)}
        for identity in expected:
            item = actual.get(identity["acquisition_item_id"])
            if item is None or not _web_item_matches(identity, _manifest_item(item)):
                raise RuntimeError("activated web item differs from its pinned identity")
            resolved.append(item)
    return resolved


class WebIngestPipeline:
    """Retryable post-processing for an already persisted and frozen web batch."""

    def __init__(
        self,
        queue_dir: Path,
        database: Path,
        runtime_wiki_dir: Path,
        reload_chat: Callable[[str], None],
        *,
        repository_root: Path,
    ) -> None:
        self.queue_dir = _external(queue_dir, repository_root)
        self.database = _external(database, repository_root)
        self.runtime_wiki_dir = _external(runtime_wiki_dir, repository_root)
        self.reload_chat = reload_chat

    def _save(self, status: dict[str, Any], **changes: Any) -> dict[str, Any]:
        status.update(changes, updated_at=_now())
        path = _status_path(self.queue_dir, status["batch_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, status)
        return status

    def process(self, batch_id: str) -> dict[str, Any]:
        with exclusive_lock(self.queue_dir, "intake-writer"):
            return self._process_unlocked(batch_id)

    def _process_unlocked(self, batch_id: str) -> dict[str, Any]:
        status = _read_status(self.queue_dir, batch_id) or {
            "batch_id": batch_id, "stage": "acquisition_complete",
            "acquisition_complete": True, "indexed": False, "chat_ready": False,
            "attempts": 0, "error": None, "created_at": _now(), "updated_at": _now(),
        }
        if status["chat_ready"]:
            return status
        status = self._save(status, stage="processing", attempts=int(status["attempts"]) + 1, error=None)
        (_job_dir(self.queue_dir, batch_id) / "retry.json").unlink(missing_ok=True)
        try:
            new_items = _batch_items(self.database, batch_id)
            if (_job_dir(self.queue_dir, batch_id) / "request.json").is_file():
                request = read_web_activation_request(self.queue_dir, batch_id)
                if (
                    Path(request["registry_snapshot_path"]) != self.database
                    or not _web_items_match(
                        request["web_items"], [_manifest_item(item) for item in new_items]
                    )
                ):
                    raise RuntimeError("web activation writer input differs from its request")
            active_generation, active = load_active_projection(
                self.runtime_wiki_dir, self.queue_dir / "active.json"
            )
            active_manifest = load_projection_manifest(active_generation, active)
            web = {
                item["acquisition_item_id"]: item
                for item in (active_manifest or {}).get("web_items", [])
            }
            web.update({item["acquisition_item_id"]: _manifest_item(item) for item in new_items})
            allowlist = list(web.values())
            active_web_snapshot = _active_web_snapshot(
                self.runtime_wiki_dir,
                active,
                list((active_manifest or {}).get("web_items", [])),
            )
            selected_database = None
            resolved_items = None
            for candidate in (active_web_snapshot, self.database):
                if candidate is None or candidate == selected_database:
                    continue
                try:
                    resolved_items = _resolve_items(candidate, allowlist)
                except RuntimeError:
                    continue
                selected_database = candidate
                break
            if resolved_items is None or selected_database is None:
                raise RuntimeError("no immutable Registry snapshot contains the active web union")
            pdf_ids = _active_pdf_ids(active_generation, active_manifest)
            calendar_ids = set((active_manifest or {}).get("pdf_calendar_occurrence_ids", []))
            pdf_registry_snapshot, pdf_registry_sha256 = _active_pdf_snapshot(
                self.runtime_wiki_dir, active, pdf_ids | calendar_ids,
            )
            if pdf_registry_snapshot:
                calendar_ids = _active_calendar_ids(Path(pdf_registry_snapshot), active_manifest, pdf_ids)
            generation_id = f"web-{hashlib.sha256(batch_id.encode()).hexdigest()[:16]}-{status['attempts']:04d}"
            generations = self.runtime_wiki_dir / "generations"
            generation = generations / generation_id
            snapshot = self.runtime_wiki_dir / "registry-snapshots" / f"{generation_id}.sqlite3"
            generations.mkdir(parents=True, exist_ok=True)
            registry_sha256 = _snapshot_registry(selected_database, snapshot)
            staging = Path(tempfile.mkdtemp(prefix=f".{generation_id}.", dir=generations))
            try:
                render_runtime_registry(
                    staging,
                    web_database=snapshot,
                    pdf_database=Path(pdf_registry_snapshot) if pdf_registry_snapshot else None,
                    manifest={
                        "web_items": list(web.values()),
                        "pdf_occurrence_ids": sorted(pdf_ids),
                        "pdf_calendar_occurrence_ids": sorted(calendar_ids),
                    },
                )
                if generation.exists():
                    raise RuntimeError("Wiki generation already exists")
                os.replace(staging, generation)
            finally:
                if staging.exists():
                    import shutil

                    shutil.rmtree(staging)
            manifest_sha256 = _write_projection_manifest(
                generation, generation_id, web_items=list(web.values()), pdf_occurrence_ids=pdf_ids,
                pdf_calendar_occurrence_ids=calendar_ids,
            )
            status = self._save(
                status, stage="indexed", indexed=True, generation_id=generation_id,
                registry_sha256=registry_sha256, manifest_sha256=manifest_sha256,
            )
            metadata = {
                "batch_id": batch_id, "batch_created_at": status["created_at"],
                "projection_kind": "web",
                "generation_id": generation_id, "path": str(generation.resolve()),
                "registry_snapshot": str(snapshot.resolve()),
                "registry_sha256": registry_sha256, "manifest_sha256": manifest_sha256,
                "web_registry_snapshot": str(snapshot.resolve()),
                "web_registry_sha256": registry_sha256,
                "pdf_registry_snapshot": pdf_registry_snapshot,
                "pdf_registry_sha256": pdf_registry_sha256,
                "activated_at": _now(),
            }
            self._save(status, stage="activating")
            atomic_write_json(self.queue_dir / "pending.json", metadata)
            try:
                self.reload_chat(generation_id)
            except Exception:
                _, activated = load_active_projection(
                    self.runtime_wiki_dir, self.queue_dir / "active.json"
                )
                if activated is None or activated.get("generation_id") != generation_id:
                    raise
            finally:
                try:
                    pending = json.loads((self.queue_dir / "pending.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pending = None
                if isinstance(pending, dict) and pending.get("generation_id") == generation_id:
                    (self.queue_dir / "pending.json").unlink(missing_ok=True)
            _, activated = load_active_projection(self.runtime_wiki_dir, self.queue_dir / "active.json")
            if activated is None or activated.get("generation_id") != generation_id:
                raise RuntimeError("Chat reload did not activate the requested intake projection")
            return self._save(status, stage="chat_ready", chat_ready=True, error=None)
        except Exception as exc:
            return self._save(status, stage="failed", chat_ready=False, error=f"{type(exc).__name__}: {exc}")
