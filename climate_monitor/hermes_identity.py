"""Sanitized effective Hermes route evidence for managed runs."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

IDENTITY_SCHEMA = "climate-hermes-effective-identity.v1"
IDENTITY_FILE = "hermes-effective-identity.json"


def identity_path(binding: Mapping[str, Any]) -> Path:
    return Path(binding["checkpoint_dir"]).parent / IDENTITY_FILE


def _validate(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "session_id", "source", "provider", "model", "observed_at",
    }:
        raise ValueError("Hermes effective identity evidence is malformed")
    if value.get("schema_version") != IDENTITY_SCHEMA:
        raise ValueError("Hermes effective identity evidence has an unsupported schema")
    result = dict(value)
    for field in ("session_id", "source", "provider", "model", "observed_at"):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise ValueError(f"Hermes effective identity evidence is missing {field}")
    return result


def load_effective_identity(binding: Mapping[str, Any]) -> dict[str, Any] | None:
    path = identity_path(binding)
    if not path.is_file():
        return None
    try:
        result = _validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Hermes effective identity evidence is unreadable") from exc
    expected_source = f"climate-acquisition-{binding['run_id']}"
    if "provider" in binding and "model" in binding:
        expected_source += f"-{binding['attempt']}"
    if result["source"] != expected_source:
        raise ValueError("Hermes effective identity evidence does not match the managed run")
    return result


def observe_effective_identity(home: Path, source: str) -> dict[str, Any]:
    """Read the actual successful per-call route recorded by pinned Hermes."""
    database = home / "state.db"
    if not database.is_file():
        raise ValueError("Hermes did not record a session database")
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        try:
            session = connection.execute(
                """
                SELECT id
                FROM sessions
                WHERE source = ?
                ORDER BY started_at DESC, rowid DESC
                LIMIT 1
                """,
                (source,),
            ).fetchone()
            if session is None:
                raise ValueError("Hermes did not record a bound session")
            row = connection.execute(
                """
                SELECT session_id, billing_provider, model
                FROM session_model_usage
                WHERE session_id = ?
                  AND COALESCE(task, '') = ''
                  AND api_call_count > 0
                  AND TRIM(COALESCE(billing_provider, '')) <> ''
                  AND TRIM(COALESCE(model, '')) NOT IN ('', 'unknown')
                ORDER BY last_seen DESC, rowid DESC
                LIMIT 1
                """,
                (session[0],),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError("Hermes session evidence is unavailable or incompatible") from exc
    if row is None:
        raise ValueError("Hermes did not record an effective provider/model route")
    return {
        "schema_version": IDENTITY_SCHEMA,
        "session_id": str(row[0]),
        "source": source,
        "provider": str(row[1]),
        "model": str(row[2]),
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def observe_session_route(home: Path, session_id: str) -> tuple[str, str]:
    """Return the latest successful main-loop provider/model for one session."""
    database = home / "state.db"
    if not database.is_file():
        raise ValueError("Hermes did not record a session database")
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                """
                SELECT billing_provider, model
                FROM session_model_usage
                WHERE session_id = ?
                  AND COALESCE(task, '') = ''
                  AND api_call_count > 0
                  AND TRIM(COALESCE(billing_provider, '')) <> ''
                  AND TRIM(COALESCE(model, '')) NOT IN ('', 'unknown')
                ORDER BY last_seen DESC, rowid DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError("Hermes session evidence is unavailable or incompatible") from exc
    if row is None:
        raise ValueError("Hermes did not record an effective provider/model route")
    return str(row[0]), str(row[1])


def bind_effective_identity(binding: Mapping[str, Any], home: Path, source: str) -> dict[str, Any]:
    """Create once, then verify, the run's observed Hermes session route."""
    observed = observe_effective_identity(home, source)
    existing = load_effective_identity(binding)
    if existing is not None:
        expected = tuple(existing[key] for key in ("session_id", "source", "provider", "model"))
        actual = tuple(observed[key] for key in ("session_id", "source", "provider", "model"))
        if actual != expected:
            raise ValueError(
                "Hermes effective identity changed; start a fresh managed run"
            )
        return existing
    path = identity_path(binding)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(observed, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return observed
