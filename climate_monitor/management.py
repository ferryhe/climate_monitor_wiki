from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from climate_monitor.request_budget import (
    DEFAULT_FETCH_ATTEMPTS,
    DEFAULT_SEARCH_ATTEMPTS,
    DEFAULT_SEARCH_RESULTS,
)

from climate_registry.acquisition import (
    AcquisitionIncompleteError,
    PublicationDatePolicy,
    freeze_acquisition_for_report,
    load_acquisition_batch,
)
from climate_registry.errors import RegistryInputError
from climate_registry.acquisition import ACQUISITION_WRITER_SCHEMA_VERSION
from climate_registry.contract import validate_registry_contract

JOB_ROOT = Path(__file__).resolve().parents[1] / "monitoring" / "jobs" / "weekly-climate-monitor-08h"
PROMPT_ROOT = JOB_ROOT / "prompts"
DEFAULT_TASK_PATH = JOB_ROOT / "task-definition.json"
DEFAULT_VERSION_ROOT = JOB_ROOT / ".task-versions"
TASK_SCHEMA = "climate-acquisition-task.v1"
STATE_SCHEMA = "climate-acquisition-task-state.v1"
BINDING_SCHEMA = "climate-acquisition-run-binding.v1"
PROMPT_NAMES = (
    "acquisition_task",
    "search_guidance",
    "relevance",
    "article_summary",
    "executive_summary",
)
_PROMPT_FILES = {
    "acquisition_task": ("v1", "acquisition-task-v1.prompt.md"),
    "search_guidance": ("v2", "pillar-b-search-v2.prompt.md"),
    "relevance": ("v1", "article-relevance-v1.prompt.md"),
    "article_summary": ("v1", "article-summary-v1.prompt.md"),
    "executive_summary": ("v1", "executive-summary-v1.prompt.md"),
}
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SOURCE_INVENTORY_PATH = Path(__file__).resolve().parents[1] / "monitoring" / "supranational_sources.yaml"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_COMMIT_ENV = "CLIMATE_REPOSITORY_COMMIT_SHA"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _text_sha(value: str) -> str:
    return hashlib.sha256(_normalize_text(value).encode("utf-8")).hexdigest()


def resolve_repository_commit_sha() -> str:
    """Resolve one real repository revision before a managed run starts."""
    from climate_monitor.weekly_monitor.driver import validate_repository_commit_sha

    configured = os.environ.get(REPOSITORY_COMMIT_ENV)
    if configured is not None:
        try:
            return validate_repository_commit_sha(configured)
        except ValueError as exc:
            raise ValueError(f"{REPOSITORY_COMMIT_ENV}: {exc}") from exc
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=REPOSITORY_ROOT, check=True, capture_output=True, text=True,
        )
        return validate_repository_commit_sha(completed.stdout)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ValueError(
            f"{REPOSITORY_COMMIT_ENV} is required outside a real Git checkout"
        ) from exc


def _canonical_existing_path(value: object, label: str) -> Path:
    raw = Path(str(value))
    if not raw.is_absolute():
        raise ValueError(f"{label} must be an absolute canonical regular file or directory")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} must be an absolute canonical regular file or directory") from exc
    if resolved != raw or raw.is_symlink():
        raise ValueError(f"{label} must be an absolute canonical regular file or directory")
    return resolved


def _canonical_regular_file(value: object, label: str) -> Path:
    path = _canonical_existing_path(value, label)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"{label} must be an absolute canonical regular file")
    return path


def _canonical_directory(value: object, label: str) -> Path:
    path = _canonical_existing_path(value, label)
    if not path.is_dir():
        raise ValueError(f"{label} must be an absolute canonical directory")
    return path


def _source_inventory(source_keys: list[str]) -> dict[str, Any]:
    from climate_monitor.config import load_sources

    indexed = {source.key: source for source in load_sources(SOURCE_INVENTORY_PATH)}
    records = [asdict(indexed[key]) for key in source_keys]
    raw = canonical_json_bytes(records)
    return {
        "schema_version": "climate-source-inventory-binding.v1",
        "path": str(SOURCE_INVENTORY_PATH.resolve(strict=True)),
        "records": records,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def default_task_definition() -> dict[str, Any]:
    from climate_monitor.config import load_sources
    from climate_registry.persistent import initialize_registry

    root = Path(__file__).resolve().parents[1]
    database = root / "output" / "climate_registry.sqlite3"
    run_root = root / "output" / "acquisition-runs"
    initialize_registry(database)
    run_root.mkdir(parents=True, exist_ok=True)

    prompts = {
        name: {
            "version": version,
            "text": (PROMPT_ROOT / filename).read_text(encoding="utf-8"),
        }
        for name, (version, filename) in _PROMPT_FILES.items()
    }
    return {
        "schema_version": TASK_SCHEMA,
        "task_id": "weekly-climate-monitor-acquisition",
        "parameters": {
            "report_date": "auto",
            "timezone": "Asia/Shanghai",
            "source_keys": [source.key for source in load_sources(SOURCE_INVENTORY_PATH)],
            "date_policy": {"mode": "unlimited"},
            "budgets": {
                "search_attempts": DEFAULT_SEARCH_ATTEMPTS,
                "search_results": DEFAULT_SEARCH_RESULTS,
                "fetch_attempts": DEFAULT_FETCH_ATTEMPTS,
                "retries_per_item": 2,
                "runtime_seconds": 3600,
            },
            "provider": "openai-codex",
            "model": "gpt-5.6-sol-900k",
        },
        "runtime": {
            "registry_database": str(database),
            "run_root": str(run_root),
        },
        "taxonomy": {
            "schema_version": "article-taxonomy-ref.v1",
            "path": "monitoring/taxonomies/article_categories_v1.yaml",
            "version": "v1",
        },
        "prompts": prompts,
    }


def _validate_date_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("date_policy must be an object")
    mode = value.get("mode", "unlimited")
    if mode == "unlimited":
        if set(value) != {"mode"}:
            raise ValueError("unlimited date_policy accepts only mode")
        return {"mode": "unlimited"}
    if mode in {"recent", "last_n_days"}:
        raw_days = value.get("days")
        if type(raw_days) is not int or raw_days <= 0:
            raise ValueError("recent days must be a positive integer")
        return {"mode": "recent", "days": raw_days}
    if mode == "custom":
        try:
            start = date.fromisoformat(str(value["start"]))
            end = date.fromisoformat(str(value["end"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("custom date_policy requires ISO start and end") from exc
        if start > end:
            raise ValueError("custom date_policy start must not be after end")
        return {"mode": "custom", "start": start.isoformat(), "end": end.isoformat()}
    raise ValueError("date_policy mode must be unlimited, recent, or custom")


def validate_task_definition(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("task definition must be an object")
    expected = {"schema_version", "task_id", "parameters", "runtime", "taxonomy", "prompts"}
    if set(value) != expected:
        raise ValueError(f"task definition fields must be exactly {sorted(expected)}")
    if value.get("schema_version") != TASK_SCHEMA:
        raise ValueError(f"unsupported task definition schema_version; expected {TASK_SCHEMA}")
    task_id = str(value.get("task_id", "")).strip()
    if not task_id:
        raise ValueError("task_id is required")
    params = value.get("parameters")
    if not isinstance(params, Mapping):
        raise ValueError("parameters must be an object")
    parameter_fields = {"report_date", "timezone", "source_keys", "date_policy", "budgets", "provider", "model"}
    if set(params) != parameter_fields:
        raise ValueError(f"parameters fields must be exactly {sorted(parameter_fields)}")
    report_date_value = str(params.get("report_date", ""))
    if report_date_value != "auto":
        try:
            report_date_value = date.fromisoformat(report_date_value).isoformat()
        except ValueError as exc:
            raise ValueError("report_date must be auto or an ISO calendar date") from exc
    tz_name = str(params.get("timezone", ""))
    try:
        ZoneInfo(tz_name)
    except Exception as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    source_keys = params.get("source_keys")
    if not isinstance(source_keys, list) or any(not isinstance(item, str) or not item.strip() for item in source_keys):
        raise ValueError("source_keys must be a list of non-empty strings")
    normalized_keys = [item.strip().lower() for item in source_keys]
    if len(set(normalized_keys)) != len(normalized_keys):
        raise ValueError("source_keys must not contain duplicates")
    from climate_monitor.config import load_sources
    inventory = {source.key for source in load_sources(SOURCE_INVENTORY_PATH)}
    unknown = sorted(set(normalized_keys) - inventory)
    if unknown:
        raise ValueError(f"source_keys are not in the configured source inventory: {unknown}")
    budgets = params.get("budgets")
    budget_fields = {"search_attempts", "search_results", "fetch_attempts", "retries_per_item", "runtime_seconds"}
    if not isinstance(budgets, Mapping) or set(budgets) != budget_fields:
        raise ValueError(f"budgets fields must be exactly {sorted(budget_fields)}")
    normalized_budgets: dict[str, int] = {}
    for key in sorted(budget_fields):
        number = budgets[key]
        if isinstance(number, bool) or not isinstance(number, int) or number < (0 if key == "retries_per_item" else 1):
            raise ValueError(f"budget {key} must be a valid positive integer")
        normalized_budgets[key] = number
    provider, model = str(params.get("provider", "")).strip(), str(params.get("model", "")).strip()
    if not provider or not model:
        raise ValueError("provider and model are required")
    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping) or set(runtime) != {"registry_database", "run_root"}:
        raise ValueError("runtime must contain only registry_database and run_root")
    if not all(str(runtime.get(key, "")).strip() for key in runtime):
        raise ValueError("runtime paths are required")
    database = _canonical_regular_file(runtime["registry_database"], "registry database")
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        try:
            schema_version = validate_registry_contract(connection)
        finally:
            connection.close()
    except (sqlite3.Error, RegistryInputError) as exc:
        raise ValueError(f"registry database contract is invalid: {exc}") from exc
    if schema_version != ACQUISITION_WRITER_SCHEMA_VERSION:
        raise ValueError(
            f"registry database must use schema {ACQUISITION_WRITER_SCHEMA_VERSION}; found {schema_version}"
        )
    run_root = _canonical_directory(runtime["run_root"], "run root")
    taxonomy = value.get("taxonomy")
    taxonomy_fields = {"schema_version", "path", "version"}
    if not isinstance(taxonomy, Mapping) or set(taxonomy) != taxonomy_fields:
        raise ValueError(f"taxonomy fields must be exactly {sorted(taxonomy_fields)}")
    if taxonomy.get("schema_version") != "article-taxonomy-ref.v1" or not taxonomy.get("version") or not taxonomy.get("path"):
        raise ValueError("taxonomy reference is incomplete or unsupported")
    taxonomy_path = Path(str(taxonomy["path"]))
    root = Path(__file__).resolve().parents[1]
    resolved_taxonomy = taxonomy_path.resolve() if taxonomy_path.is_absolute() else (root / taxonomy_path).resolve()
    taxonomy_root = (root / "monitoring" / "taxonomies").resolve()
    if resolved_taxonomy.parent != taxonomy_root or not resolved_taxonomy.is_file():
        raise ValueError("taxonomy path must name an existing file in monitoring/taxonomies")
    taxonomy_schema = resolved_taxonomy.read_text(encoding="utf-8").splitlines()[0].partition(":")[2].strip()
    if taxonomy_schema != "article-category-taxonomy.v1" or str(taxonomy["version"]) != "v1":
        raise ValueError("taxonomy version/contract mismatch")
    prompts = value.get("prompts")
    if not isinstance(prompts, Mapping) or set(prompts) != set(PROMPT_NAMES):
        raise ValueError(f"prompts must contain exactly {list(PROMPT_NAMES)}")
    normalized_prompts: dict[str, dict[str, str]] = {}
    for name in PROMPT_NAMES:
        component = prompts[name]
        if not isinstance(component, Mapping) or set(component) != {"version", "text"}:
            raise ValueError(f"prompt {name} must contain only version and text")
        version = str(component.get("version", "")).strip()
        text = _normalize_text(str(component.get("text", "")))
        if not version or not text.strip():
            raise ValueError(f"prompt {name} version and text are required")
        normalized_prompts[name] = {"version": version, "text": text}
    acquisition = normalized_prompts["acquisition_task"]["text"]
    for component in PROMPT_NAMES[1:]:
        if component not in acquisition:
            raise ValueError(f"acquisition_task must reference prompt component {component}")
    search_text = normalized_prompts["search_guidance"]["text"]
    missing_placeholders = [
        placeholder for placeholder in (
            "${report_date}", "${date_policy_json}",
            "${search_time_guidance}", "${output_path_json}",
        ) if placeholder not in search_text
    ]
    if missing_placeholders:
        raise ValueError(f"search_guidance is missing required placeholders: {missing_placeholders}")
    if "climate_related" not in normalized_prompts["article_summary"]["text"]:
        raise ValueError("article_summary prompt contract is missing climate_related")
    if "executive_summary" not in normalized_prompts["executive_summary"]["text"]:
        raise ValueError("executive_summary prompt contract is missing executive_summary")
    return {
        "schema_version": TASK_SCHEMA,
        "task_id": task_id,
        "parameters": {
            "report_date": report_date_value,
            "timezone": tz_name,
            "source_keys": normalized_keys,
            "date_policy": _validate_date_policy(params["date_policy"]),
            "budgets": normalized_budgets,
            "provider": provider,
            "model": model,
        },
        "runtime": {"registry_database": str(database), "run_root": str(run_root)},
        "taxonomy": {key: str(taxonomy[key]).strip() for key in ("schema_version", "path", "version")},
        "prompts": normalized_prompts,
    }


def _resolved_report_date(parameters: Mapping[str, Any], now: datetime | None = None) -> date:
    configured = parameters["report_date"]
    if configured != "auto":
        return date.fromisoformat(configured)
    instant = now or _utc_now()
    return instant.astimezone(ZoneInfo(parameters["timezone"])).date()


def _resolved_date_range(parameters: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    policy = parameters["date_policy"]
    report_date = _resolved_report_date(parameters, now)
    if policy["mode"] == "unlimited":
        return {"mode": "unlimited", "start": None, "end": None}
    if policy["mode"] == "recent":
        start = report_date - timedelta(days=policy["days"] - 1)
        return {"mode": "recent", "start": start.isoformat(), "end": report_date.isoformat()}
    return {"mode": "custom", "start": policy["start"], "end": policy["end"]}


def effective_task(value: Mapping[str, Any]) -> dict[str, Any]:
    definition = validate_task_definition(value)
    hashes = {name: _text_sha(definition["prompts"][name]["text"]) for name in PROMPT_NAMES}
    prompt_versions = {name: definition["prompts"][name]["version"] for name in PROMPT_NAMES}
    return {
        "task_id": definition["task_id"],
        "parameters": copy.deepcopy(definition["parameters"]),
        "runtime": copy.deepcopy(definition["runtime"]),
        "taxonomy": copy.deepcopy(definition["taxonomy"]),
        "taxonomy_sha256": _text_sha((Path(__file__).resolve().parents[1] / definition["taxonomy"]["path"]).read_text(encoding="utf-8")),
        "prompt_versions": prompt_versions,
        "prompt_hashes": hashes,
    }


def definition_view(definition: Mapping[str, Any], *, version: int) -> dict[str, Any]:
    normalized = validate_task_definition(definition)
    effective = effective_task(normalized)
    return {
        "version": version,
        "definition": normalized,
        "effective": effective,
        "resolved_date_range": _resolved_date_range(effective["parameters"]),
        "effective_prompts": copy.deepcopy(normalized["prompts"]),
        "hashes": {
            "definition_sha256": _sha(normalized),
            "effective_sha256": _sha(effective),
            "components": copy.deepcopy(effective["prompt_hashes"]),
        },
    }


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _exclusive_lock_nowait(path: Path):
    """Take an interprocess lock or reject instead of silently overlapping."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("managed acquisition state is already owned by another run") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class TaskDefinitionStore:
    def __init__(self, active_path: str | Path = DEFAULT_TASK_PATH, version_root: str | Path = DEFAULT_VERSION_ROOT):
        self.active_path = Path(active_path)
        self.version_root = Path(version_root)

    def _state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.active_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"task definition not configured: {self.active_path}") from exc
        if isinstance(payload, dict) and payload.get("schema_version") == "climate-acquisition-task-bootstrap.v1":
            definition = default_task_definition()
            return {
                "schema_version": STATE_SCHEMA,
                "version": 1,
                "saved_at": str(payload.get("created_at", "2026-09-10T00:00:00Z")),
                "saved_by": "repository-bootstrap",
                "definition_sha256": _sha(definition),
                "definition": definition,
            }
        if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA:
            raise ValueError(f"unsupported or malformed task state at {self.active_path}")
        if not isinstance(payload.get("version"), int) or payload["version"] < 1:
            raise ValueError("task state version must be a positive integer")
        payload["definition"] = validate_task_definition(payload.get("definition", {}))
        if payload.get("definition_sha256") != _sha(payload["definition"]):
            raise ValueError("task definition hash mismatch")
        return payload

    def load(self) -> dict[str, Any]:
        state = self._state()
        result = definition_view(state["definition"], version=state["version"])
        result.update(saved_at=state["saved_at"], saved_by=state["saved_by"])
        return result

    def preview(self, definition: Mapping[str, Any]) -> dict[str, Any]:
        try:
            version = self._state()["version"]
        except FileNotFoundError:
            version = 0
        return definition_view(definition, version=version)

    def save(self, definition: Mapping[str, Any], *, expected_version: int | None = None, actor: str) -> dict[str, Any]:
        with _exclusive_lock(self.active_path.parent / ".task-definition.lock"):
            return self._save_locked(definition, expected_version=expected_version, actor=actor)

    def _save_locked(self, definition: Mapping[str, Any], *, expected_version: int | None, actor: str) -> dict[str, Any]:
        normalized = validate_task_definition(definition)
        current: dict[str, Any] | None = None
        try:
            current = self._state()
            previous = current["version"]
        except FileNotFoundError:
            previous = 0
        if expected_version is not None and expected_version != previous:
            raise RuntimeError(f"task version conflict: expected {expected_version}, current {previous}")
        version = previous + 1
        state = {
            "schema_version": STATE_SCHEMA,
            "version": version,
            "saved_at": _rfc3339(_utc_now()),
            "saved_by": actor,
            "definition_sha256": _sha(normalized),
            "definition": normalized,
        }
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        if previous:
            assert current is not None
            previous_path = self.version_root / f"{previous:08d}.json"
            if not previous_path.exists():
                previous_encoded = json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
                _atomic_write(previous_path, previous_encoded)
        version_path = self.version_root / f"{version:08d}.json"
        if version_path.exists():
            raise RuntimeError(f"task version already exists: {version}")
        _atomic_write(version_path, encoded)
        try:
            _atomic_write(self.active_path, encoded)
        except Exception:
            version_path.unlink(missing_ok=True)
            raise
        return self.load()

    def versions(self) -> list[dict[str, Any]]:
        current = self.load()["version"]
        result = []
        if self.version_root.exists():
            for path in sorted(self.version_root.glob("[0-9]" * 8 + ".json")):
                payload = json.loads(path.read_text(encoding="utf-8"))
                result.append({
                    "version": payload["version"],
                    "saved_at": payload["saved_at"],
                    "saved_by": payload["saved_by"],
                    "definition_sha256": payload["definition_sha256"],
                    "active": payload["version"] == current,
                })
        if not result:
            state = self._state()
            result.append({key: state[key] for key in ("version", "saved_at", "saved_by", "definition_sha256")} | {"active": True})
        return result

    def _version(self, version: int) -> dict[str, Any]:
        path = self.version_root / f"{version:08d}.json"
        if not path.exists():
            state = self._state()
            if state["version"] == version:
                return state
            raise KeyError(f"task version {version} not found")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != version or payload.get("definition_sha256") != _sha(validate_task_definition(payload["definition"])):
            raise ValueError(f"task version {version} is invalid")
        return payload

    def diff(self, old_version: int, new_version: int) -> dict[str, Any]:
        old, new = self._version(old_version), self._version(new_version)
        old_view, new_view = definition_view(old["definition"], version=old_version), definition_view(new["definition"], version=new_version)
        return {
            "old_version": old_version,
            "new_version": new_version,
            "effective_changed": old_view["hashes"]["effective_sha256"] != new_view["hashes"]["effective_sha256"],
            "changed_components": [name for name in PROMPT_NAMES if old_view["hashes"]["components"][name] != new_view["hashes"]["components"][name]],
            "old_hashes": old_view["hashes"],
            "new_hashes": new_view["hashes"],
        }

    def restore(self, version: int, *, expected_version: int, actor: str) -> dict[str, Any]:
        return self.save(self._version(version)["definition"], expected_version=expected_version, actor=actor)


def managed_report_inputs(definition: Mapping[str, Any], run_id: str) -> dict[str, str]:
    """Resolve the report paths frozen into a managed acquisition binding."""
    run_root = Path(definition["runtime"]["run_root"])
    run_dir = run_root / run_id
    return {
        "acquisition_batch": str(run_dir / "report-acquisition-batch.json"),
        "web_listening_manifest": str(run_dir / "report-web-listening-manifest.json"),
        "pillar_b_artifact": str(run_dir / "report-pillar-b.json"),
        "staging_dir": str(run_dir / "report-staging"),
        "state_dir": os.environ.get(
            "CLIMATE_MANAGED_STATE_DIR", str(REPOSITORY_ROOT / "monitoring" / "state")
        ),
        "source_dir": os.environ.get(
            "CLIMATE_MANAGED_SOURCE_DIR", str(REPOSITORY_ROOT / "sources")
        ),
        "wiki_dir": os.environ.get(
            "CLIMATE_MANAGED_WIKI_DIR", str(REPOSITORY_ROOT / "wiki")
        ),
    }


def build_task_binding(
    definition: Mapping[str, Any], *, task_version: int, run_id: str,
    attempt: int, created_at: datetime | None = None,
) -> dict[str, Any]:
    normalized = validate_task_definition(definition)
    view = definition_view(normalized, version=task_version)
    parameters = normalized["parameters"]
    frozen_at = created_at or _utc_now()
    repository_commit_sha = resolve_repository_commit_sha()
    report_date = _resolved_report_date(parameters, frozen_at)
    policy_input = parameters["date_policy"]
    policy = PublicationDatePolicy.resolve(policy_input, anchor_date=report_date, frozen_at=_rfc3339(frozen_at))
    run_root = Path(normalized["runtime"]["run_root"])
    from climate_monitor.config import load_site_scopes
    from climate_monitor.models import MonitorSource
    from climate_monitor.web_listening_adapter import gateway_configuration

    source_inventory = _source_inventory(parameters["source_keys"])
    scopes = {scope.source_key: scope for scope in load_site_scopes(
        REPOSITORY_ROOT / "monitoring" / "site_scopes.yaml"
    ) if scope.source_key in parameters["source_keys"]}
    scope_records = [asdict(scopes[key]) for key in parameters["source_keys"] if key in scopes]
    scope_inventory = {
        "records": scope_records,
        "sha256": hashlib.sha256(canonical_json_bytes(scope_records)).hexdigest(),
    }
    gateway = gateway_configuration(
        [MonitorSource(**record) for record in source_inventory["records"]], scopes,
        budget_limit=parameters["budgets"]["fetch_attempts"],
    )
    return {
        "schema_version": BINDING_SCHEMA,
        "run_id": run_id,
        "attempt": attempt,
        "trigger": "manual",
        "created_at": _rfc3339(frozen_at),
        "task_id": normalized["task_id"],
        "task_version": task_version,
        "definition_sha256": view["hashes"]["definition_sha256"],
        "effective_sha256": view["hashes"]["effective_sha256"],
        "repository_commit_sha": repository_commit_sha,
        "taxonomy_sha256": view["effective"]["taxonomy_sha256"],
        "prompt_hashes": view["hashes"]["components"],
        "prompt_versions": view["effective"]["prompt_versions"],
        "report_date": report_date.isoformat(),
        "timezone": parameters["timezone"],
        "resolved_date_range": _resolved_date_range(parameters, frozen_at),
        "date_policy": policy.to_dict(),
        "budgets": copy.deepcopy(parameters["budgets"]),
        "source_keys": copy.deepcopy(parameters["source_keys"]),
        "source_inventory": source_inventory,
        "site_scope_inventory": scope_inventory,
        "governed_gateway": gateway,
        "provider": parameters["provider"],
        "model": parameters["model"],
        "acquisition_lineage_id": f"acq-{run_id}",
        "acquisition_batch_id": f"acq-{run_id}-attempt-{attempt}",
        "checkpoint_dir": str(run_root / run_id / "checkpoint"),
        "registry_database": normalized["runtime"]["registry_database"],
        "frozen_report_input": str(run_root / run_id / "frozen-report-input.json"),
        "report_inputs": managed_report_inputs(normalized, run_id),
        "definition": normalized,
    }


Launcher = Callable[[dict[str, Any]], int | Mapping[str, Any]]


class ManagementService:
    def __init__(self, *, store: TaskDefinitionStore, runtime_root: str | Path, launcher: Launcher | None = None):
        self.store = store
        configured = Path(store.load()["definition"]["runtime"]["run_root"])
        supplied = _canonical_directory(runtime_root, "run root")
        if supplied != configured:
            raise ValueError("management runtime root must equal the task definition run_root")
        self.runtime_root = configured
        self._launcher = launcher or self._launch_process

    @classmethod
    def from_environment(cls) -> "ManagementService":
        store = TaskDefinitionStore(
            os.environ.get("CLIMATE_TASK_CONFIG", str(DEFAULT_TASK_PATH)),
            os.environ.get("CLIMATE_TASK_VERSION_DIR", str(DEFAULT_VERSION_ROOT)),
        )
        configured = Path(store.load()["definition"]["runtime"]["run_root"])
        override = os.environ.get("CLIMATE_ACQUISITION_RUN_DIR")
        return cls(store=store, runtime_root=override or configured)

    def _run_dir(self, run_id: str) -> Path:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise KeyError("invalid run id")
        return self.runtime_root / run_id

    def _attempt_path(self, run_id: str, attempt: int) -> Path:
        return self._run_dir(run_id) / f"attempt-{attempt}.json"

    @staticmethod
    def _state_lock_path(binding: Mapping[str, Any]) -> Path:
        return Path(binding["report_inputs"]["state_dir"]) / ".managed-acquisition.lock"

    @staticmethod
    def _pid_is_alive(value: object) -> bool:
        if not isinstance(value, int) or value <= 0:
            return False
        try:
            os.kill(value, 0)
        except (OSError, ValueError):
            return False
        return True

    def _assert_no_startup_owner(self, binding: Mapping[str, Any], *, exclude_run_id: str | None = None) -> None:
        """Cover the short interval before a launched child acquires the state lock."""
        state_dir = str(Path(binding["report_inputs"]["state_dir"]))
        for path in self.runtime_root.iterdir():
            if not path.is_dir() or path.name == exclude_run_id:
                continue
            try:
                active = self.binding(path.name)
                if str(Path(active["report_inputs"]["state_dir"])) != state_dir:
                    continue
                result_path = path / f"attempt-{active['attempt']}-result.json"
                if result_path.exists():
                    continue
                runtime = json.loads((path / "runtime.json").read_text(encoding="utf-8"))
                launched = datetime.fromisoformat(
                    str(runtime.get("launched_at", "")).replace("Z", "+00:00")
                )
                recent_launch = (_utc_now() - launched).total_seconds() <= 300
                if recent_launch or self._pid_is_alive(runtime.get("pid")):
                    raise RuntimeError(
                        f"managed acquisition state is already owned by run {path.name}"
                    )
            except RuntimeError:
                raise
            except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue

    def _launch_process(self, binding: dict[str, Any]) -> int:
        attempt_path = self._attempt_path(binding["run_id"], binding["attempt"])
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_agent_acquisition.py"
        log_path = self._run_dir(binding["run_id"]) / f"attempt-{binding['attempt']}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("ab")
        try:
            process = subprocess.Popen(
                [sys.executable, str(script), "--binding", str(attempt_path.resolve())],
                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
            )
            threading.Thread(
                target=process.wait,
                name=f"acquisition-reaper-{process.pid}",
                daemon=True,
            ).start()
        finally:
            log.close()
        return process.pid

    def start(self, *, trigger: str = "manual", now: datetime | None = None) -> dict[str, Any]:
        if trigger not in {"manual", "scheduled"}:
            raise ValueError("trigger must be manual or scheduled")
        loaded = self.store.load()
        stamp = now or _utc_now()
        run_id = stamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)
        binding = build_task_binding(loaded["definition"], task_version=loaded["version"], run_id=run_id, attempt=1, created_at=stamp)
        binding["trigger"] = trigger
        with _exclusive_lock(self.runtime_root / ".runs.lock"):
            with _exclusive_lock_nowait(self._state_lock_path(binding)):
                self._assert_no_startup_owner(binding)
                run_dir = self._run_dir(run_id)
                run_dir.mkdir(parents=False, exist_ok=False)
                encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
                _atomic_write(run_dir / "binding.json", encoded)
                _atomic_write(self._attempt_path(run_id, 1), encoded)
                launched_at = _rfc3339(_utc_now())
                self._write_runtime(run_id, {"schema_version": "climate-acquisition-runtime.v1", "state": "launching", "attempt": 1, "pid": None, "command": None, "launched_at": launched_at, "heartbeat_at": launched_at})
                self._write_progress(run_id, binding, stage="launching")
                launched = self._launcher(copy.deepcopy(binding))
                launch_info = dict(launched) if isinstance(launched, Mapping) else {"pid": launched}
                pid = launch_info.get("pid")
                self._write_runtime(run_id, {"schema_version": "climate-acquisition-runtime.v1", "state": "running", "attempt": 1, "pid": pid, "command": launch_info.get("command"), "launched_at": launched_at, "heartbeat_at": _rfc3339(_utc_now())})
                self._write_progress(run_id, binding, stage="running")
        return {"accepted": True, "run_id": run_id, "attempt": 1, "pid": pid, "task_version": loaded["version"]}

    def _write_runtime(self, run_id: str, payload: dict[str, Any]) -> None:
        _atomic_write(self._run_dir(run_id) / "runtime.json", json.dumps(payload, sort_keys=True, indent=2).encode() + b"\n")

    def _write_progress(self, run_id: str, binding: Mapping[str, Any], *, stage: str, error: str | None = None, next_step: str | None = None) -> None:
        payload = {
            "schema_version": "climate-acquisition-progress.v1",
            "run_id": run_id, "attempt": binding["attempt"], "stage": stage,
            "updated_at": _rfc3339(_utc_now()),
            "current": {"organization": None, "url": None},
            "error": error, "next_step": next_step,
        }
        _atomic_write(self._run_dir(run_id) / "progress.json", json.dumps(payload, sort_keys=True, indent=2).encode() + b"\n")

    def binding(self, run_id: str) -> dict[str, Any]:
        base_path = self._run_dir(run_id) / "binding.json"
        if not base_path.exists():
            raise KeyError(f"run {run_id} not found")
        base = json.loads(base_path.read_text(encoding="utf-8"))
        attempts: list[tuple[int, Path]] = []
        for path in self._run_dir(run_id).glob("attempt-*.json"):
            match = re.fullmatch(r"attempt-(\d+)\.json", path.name)
            if match:
                attempts.append((int(match.group(1)), path))
        return json.loads(max(attempts)[1].read_text(encoding="utf-8")) if attempts else base

    def resume(self, run_id: str) -> dict[str, Any]:
        binding = self.binding(run_id)
        with _exclusive_lock(self.runtime_root / ".runs.lock"):
            with _exclusive_lock_nowait(self._state_lock_path(binding)):
                self._assert_no_startup_owner(binding, exclude_run_id=run_id)
                return self._resume_locked(run_id)

    def _resume_locked(self, run_id: str) -> dict[str, Any]:
        with _exclusive_lock(self._run_dir(run_id) / ".run.lock"):
            original_path = self._run_dir(run_id) / "binding.json"
            if not original_path.exists():
                raise KeyError(f"run {run_id} not found")
            original = json.loads(original_path.read_text(encoding="utf-8"))
            current = self.binding(run_id)
            result_path = self._run_dir(run_id) / f"attempt-{current['attempt']}-result.json"
            if not result_path.exists():
                runtime_path = self._run_dir(run_id) / "runtime.json"
                runtime = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.exists() else {}
                try:
                    heartbeat = datetime.fromisoformat(str(runtime.get("heartbeat_at")).replace("Z", "+00:00"))
                    age = (_utc_now() - heartbeat).total_seconds()
                except (TypeError, ValueError):
                    age = 0
                if age <= 300:
                    raise RuntimeError("acquisition attempt is already running")
            else:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if result.get("exit_code") != 0 and not result.get("retryable"):
                    raise RuntimeError("acquisition attempt is terminal and non-retryable")
            if Path(original["frozen_report_input"]).exists():
                if not result_path.exists():
                    raise RuntimeError("frozen report run has no finished report attempt")
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if result.get("exit_code") == 0:
                    raise RuntimeError("report preparation is already complete")
                if not result.get("retryable") or result.get("resume_phase") != "report":
                    raise RuntimeError("frozen report run is terminal and non-retryable")
                launched_at = _rfc3339(_utc_now())
                archived_result = self._run_dir(run_id) / f"attempt-{current['attempt']}-report-failure.json"
                _atomic_write(
                    archived_result,
                    json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n",
                )
                result_path.unlink()
                self._write_runtime(run_id, {
                    "schema_version": "climate-acquisition-runtime.v1", "state": "launching",
                    "attempt": current["attempt"], "pid": None, "command": None,
                    "launched_at": launched_at, "heartbeat_at": launched_at,
                    "resume_phase": "report",
                })
                launched = self._launcher(copy.deepcopy(current))
                launch_info = dict(launched) if isinstance(launched, Mapping) else {"pid": launched}
                pid = launch_info.get("pid")
                self._write_runtime(run_id, {
                    "schema_version": "climate-acquisition-runtime.v1", "state": "running",
                    "attempt": current["attempt"], "pid": pid,
                    "command": launch_info.get("command"), "launched_at": launched_at,
                    "heartbeat_at": _rfc3339(_utc_now()), "resume_phase": "report",
                })
                self._write_progress(run_id, current, stage="report_resuming")
                return {
                    "accepted": True, "run_id": run_id, "attempt": current["attempt"],
                    "pid": pid, "task_version": current["task_version"], "phase": "report",
                }
            attempt = int(current["attempt"]) + 1
            resumed = copy.deepcopy(original)
            resumed["attempt"] = attempt
            resumed["created_at"] = _rfc3339(_utc_now())
            _atomic_write(self._attempt_path(run_id, attempt), json.dumps(resumed, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
            launched_at = _rfc3339(_utc_now())
            self._write_runtime(run_id, {"schema_version": "climate-acquisition-runtime.v1", "state": "launching", "attempt": attempt, "pid": None, "command": None, "launched_at": launched_at, "heartbeat_at": launched_at})
            launched = self._launcher(copy.deepcopy(resumed))
            launch_info = dict(launched) if isinstance(launched, Mapping) else {"pid": launched}
            pid = launch_info.get("pid")
            self._write_runtime(run_id, {"schema_version": "climate-acquisition-runtime.v1", "state": "running", "attempt": attempt, "pid": pid, "command": launch_info.get("command"), "launched_at": launched_at, "heartbeat_at": _rfc3339(_utc_now())})
            self._write_progress(run_id, resumed, stage="running")
            return {"accepted": True, "run_id": run_id, "attempt": attempt, "pid": pid, "task_version": resumed["task_version"]}

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.runtime_root.exists():
            return []
        result = []
        for path in sorted(self.runtime_root.iterdir(), reverse=True):
            if path.is_dir() and (path / "binding.json").exists():
                result.append(self.progress(path.name))
        return result

    def progress(self, run_id: str) -> dict[str, Any]:
        binding = self.binding(run_id)
        runtime_path = self._run_dir(run_id) / "runtime.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.exists() else {}
        result_path = self._run_dir(run_id) / f"attempt-{binding['attempt']}-result.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
        database = Path(binding["registry_database"])
        if not database.is_absolute():
            database = Path(__file__).resolve().parents[1] / database
        batch = None
        if database.exists():
            try:
                batch = load_acquisition_batch(database, binding["acquisition_batch_id"])
            except (KeyError, ValueError):
                batch = None
        source_keys = {
            row.get("key") for row in binding.get("source_inventory", {}).get("records", [])
            if row.get("key")
        }
        completed_source_keys: set[str] = set()
        outcomes_path = Path(binding["report_inputs"]["acquisition_batch"])
        if outcomes_path.exists():
            try:
                for outcome in json.loads(outcomes_path.read_text(encoding="utf-8")):
                    if (outcome.get("authoritative_status") != "completed"
                            or outcome.get("full_success") is not True):
                        continue
                    for disposition in outcome.get("dispositions", []):
                        if (disposition.get("site_key") in source_keys
                                and disposition.get("disposition") in {"updated", "unchanged"}):
                            completed_source_keys.add(disposition["site_key"])
            except (AttributeError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        else:
            context_path = self._run_dir(run_id) / f"attempt-{binding['attempt']}-trusted-context.json"
            try:
                trusted_context = json.loads(context_path.read_text(encoding="utf-8"))
                source_results = trusted_context.get("web_listening", {}).get("source_results", [])
                for source_result in source_results:
                    outcome = source_result.get("outcome", {})
                    if (
                        source_result.get("source") in source_keys
                        and source_result.get("status") == "succeeded"
                        and outcome.get("authoritative_status") == "completed"
                        and outcome.get("full_success") is True
                    ):
                        completed_source_keys.add(source_result["source"])
            except (AttributeError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        source_progress = {"completed": len(completed_source_keys), "total": len(source_keys)}
        counts: dict[str, Any] = {
            "scopes": source_progress,
            "organizations": dict(source_progress),
            **{key: None for key in (
                "candidates", "body_available", "stored", "unchanged", "failed", "pending"
            )},
        }
        items: list[dict[str, Any]] = []
        progress_path = self._run_dir(run_id) / "progress.json"
        persisted = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
        persisted_stage = str(persisted.get("stage") or "")
        active_report_stage = (
            persisted_stage
            if (
                persisted_stage in {"report_preparing", "report_resuming"}
                and runtime.get("state") == "running"
                and runtime.get("run_id") == run_id
                and runtime.get("attempt") == binding["attempt"]
                and persisted.get("attempt") == binding["attempt"]
            )
            else None
        )
        stage = str(persisted_stage or runtime.get("state") or "unknown")
        if result:
            stage = (
                "report_failed"
                if result.get("resume_phase") == "report" and result.get("exit_code") != 0
                else (
                    "terminal_partial"
                    if result.get("retryable")
                    else ("completed" if result.get("exit_code") == 0 else "terminal_failure")
                )
            )
        if batch:
            articles = batch.get("items", [])
            searches = batch.get("searches", [])
            provenance_path = self._run_dir(run_id) / f"attempt-{binding['attempt']}-tool-provenance.json"
            provenance = None
            try:
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            used_budget = (
                provenance.get("cumulative_actual", provenance.get("actual"))
                if isinstance(provenance, dict) else {
                "search_attempts": len(searches),
                "search_results": sum(search.get("budget", {}).get("used_results", 0) for search in searches),
                "fetch_attempts": sum(len(item.get("attempts", [])) for item in articles),
                "retries": sum(max(0, len(item.get("attempts", [])) - 1) for item in articles),
                "runtime_seconds": None,
            })
            counts.update(
                candidates=len(articles),
                body_available=sum(bool(item.get("markdown_content")) for item in articles),
                stored=sum(bool(item.get("content_version_id")) for item in articles),
                unchanged=sum(item.get("update_status") == "unchanged" for item in articles),
                failed=sum(item.get("fetch_status") == "failed" or item.get("processing_status") == "failed" for item in articles),
                pending=sum(item.get("date_status") == "unknown_pending_review" or item.get("processing_status") == "pending" for item in articles),
            )
            items = [self._item_summary(item) for item in articles]
            stage = "acquisition_stored"
            frozen = Path(binding["frozen_report_input"])
            try:
                expected = freeze_acquisition_for_report(
                    database,
                    binding["acquisition_batch_id"],
                    report_date=binding["report_date"],
                )
                if json.loads(frozen.read_text(encoding="utf-8")) == expected:
                    stage = ("report_completed" if result and result.get("exit_code") == 0
                             else ("report_failed" if result and result.get("resume_phase") == "report"
                                   else "report_input_frozen"))
            except (AcquisitionIncompleteError, FileNotFoundError, json.JSONDecodeError, ValueError):
                pass
            if result and stage not in {"report_input_frozen", "report_completed", "report_failed"}:
                stage = ("terminal_partial" if result.get("retryable")
                         else ("completed" if result.get("exit_code") == 0 else "terminal_failure"))
        else:
            used_budget = None
        if active_report_stage and batch:
            # A matching running attempt is authoritative while authoring is
            # inside the existing report command; frozen is only the idle handoff.
            stage = active_report_stage
        source_rows = []
        acquisition_path = self._run_dir(run_id) / f"attempt-{binding['attempt']}-acquisition.json"
        try:
            from climate_registry.acquisition import readback_source_outcomes
            acquisition_payload = json.loads(acquisition_path.read_text())
            source_rows = readback_source_outcomes(database, acquisition_payload)
        except (OSError, KeyError, ValueError):
            pass
        coverage = {
            "execution_complete": (result or {}).get("execution_complete", False),
            "full_success": bool(source_rows) and all(row.get("status") == "succeeded" for row in source_rows)
                            and (result or {}).get("full_coverage") is not False,
            "successful_sources": sum(row.get("status") == "succeeded" for row in source_rows),
            "rejected_sources": sum(row.get("coverage_status") == "rejected" for row in source_rows),
            "incomplete_sources": sum(row.get("coverage_status") == "incomplete" for row in source_rows),
            "total_sources": len(source_keys),
        }
        if result and result.get("execution_complete") and result.get("full_coverage") is False:
            stage = "completed_with_gaps"
        from climate_monitor.request_budget import RequestBudget, ledger_path
        if ledger_path(binding).is_file():
            used_budget = RequestBudget(ledger_path(binding), binding).usage()
        updated_at = persisted.get("updated_at") or runtime.get("heartbeat_at") or runtime.get("launched_at") or binding["created_at"]
        try:
            age = (_utc_now() - datetime.fromisoformat(updated_at.replace("Z", "+00:00"))).total_seconds()
            freshness = "stale" if age > 300 else "fresh"
        except (TypeError, ValueError):
            freshness = "unknown"
        return {
            "run_id": run_id,
            "attempt": binding["attempt"],
            "stage": stage,
            "current": persisted.get("current") or {"organization": None, "url": None},
            "counts": counts,
            "coverage": coverage,
            "search_decision": {"status": batch.get("search_decision"),
                                "reason": batch.get("no_search_reason")} if batch else None,
            "source_outcomes": [{key: row.get(key) for key in (
                "source", "coverage_status", "artifact_id", "warnings")} for row in source_rows],
            "budget": {"limits": binding["budgets"], "used": used_budget},
            "freshness": freshness,
            "updated_at": updated_at,
            "items": items,
            "scheduler_snapshot": None,
            "report_phase": ("completed" if stage == "report_completed"
                             else ("failed" if stage == "report_failed"
                             else ("active" if stage in {"report_preparing", "report_resuming"}
                             else ("frozen" if stage == "report_input_frozen"
                                   else "not_started")))),
            "publish_phase": "not_started",
            "error": persisted.get("error") or (result or {}).get("error"),
            "next_step": persisted.get("next_step"),
        }

    @staticmethod
    def _item_summary(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "item_id": item.get("article_id") or item.get("canonical_url_hash") or item.get("url"),
            "organization": item.get("source_key") or item.get("source"),
            "url": item.get("url") or item.get("canonical_url"),
            "title": item.get("title"),
            "publication_date": item.get("publication_date"),
            "eligibility": item.get("date_status"),
            "error": item.get("processing_error") or item.get("failure_reason"),
            "status": item.get("processing_status") or ("stored" if item.get("content_version_id") else "pending"),
        }

    def item_detail(self, run_id: str, item_id: str) -> dict[str, Any]:
        binding = self.binding(run_id)
        database = Path(binding["registry_database"])
        if not database.is_absolute():
            database = Path(__file__).resolve().parents[1] / database
        try:
            batch = load_acquisition_batch(database, binding["acquisition_batch_id"])
        except (FileNotFoundError, KeyError, ValueError, RegistryInputError) as exc:
            raise KeyError(f"item {item_id} not found") from exc
        for item in batch.get("items", []):
            summary = self._item_summary(item)
            if item_id in {str(summary["item_id"]), str(summary["url"])}:
                search_ref = next((origin.get("search_ref") for origin in item.get("origins", []) if origin.get("search_ref")), None)
                search_attempt = next((search for search in batch.get("searches", []) if search.get("search_ref") == search_ref), None)
                body = item.get("markdown_content") or ""
                return {
                    **summary,
                    "date_evidence": item.get("publication_date_evidence"),
                    "source_evidence": item.get("origins") or [],
                    "stored_body": {"content_version_id": item.get("content_version_id"), "content_sha256": item.get("content_sha256"), "preview": body[:1000], "truncated": len(body) > 1000},
                    "tool_attempts": item.get("attempts") or [],
                    "search_reason": batch.get("no_search_reason") if batch.get("search_decision") == "no_search" else ((search_attempt or {}).get("reason")),
                    "search_attempt": search_attempt,
                    "retry_reason": item.get("processing_error"),
                    "error": item.get("error_message") or item.get("processing_error"),
                    "next_step": item.get("next_step"),
                }
        raise KeyError(f"item {item_id} not found")


def load_active_prompt(name: str, *, task_path: str | Path | None = None) -> dict[str, str]:
    if name not in PROMPT_NAMES:
        raise KeyError(name)
    store = TaskDefinitionStore(task_path or os.environ.get("CLIMATE_TASK_CONFIG", str(DEFAULT_TASK_PATH)), os.environ.get("CLIMATE_TASK_VERSION_DIR", str(DEFAULT_VERSION_ROOT)))
    loaded = store.load()
    component = loaded["definition"]["prompts"][name]
    return {
        "version": component["version"],
        "text": component["text"],
        "sha256": loaded["hashes"]["components"][name],
        "path": str(store.active_path),
    }
