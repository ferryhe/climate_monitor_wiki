from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from climate_registry.read_api import (
    RegistryContractError,
    RegistryLocationError,
    RegistryReader,
    RegistryUnavailableError,
)


ROOT = Path(__file__).resolve().parents[1]
DATABASE_NAME = "article-registry.sqlite3"
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class PreflightError(ValueError):
    """A host Registry candidate is not safe to mount into the application."""


def database_is_symlink(database: Path, *, predicate=os.path.islink) -> bool:
    return bool(predicate(database))


def database_sidecars(database: Path) -> tuple[Path, ...]:
    candidates = {database}
    try:
        candidates.add(database.resolve(strict=False))
    except OSError:
        pass
    return tuple(
        sidecar
        for candidate in candidates
        for suffix in SIDECAR_SUFFIXES
        if (sidecar := Path(f"{candidate}{suffix}")).exists()
    )


def validate_registry_host_directory(
    host_directory: str | Path,
    *,
    repository_root: str | Path = ROOT,
) -> dict[str, int | bool]:
    configured = Path(host_directory).expanduser()
    if not configured.is_absolute():
        raise PreflightError("host directory must be an absolute external path")
    try:
        directory = configured.resolve(strict=True)
        root = Path(repository_root).resolve(strict=False)
    except OSError as exc:
        raise PreflightError("host directory is unavailable") from exc
    if not directory.is_dir():
        raise PreflightError("host path is not a directory")
    try:
        directory.relative_to(root)
    except ValueError:
        pass
    else:
        raise PreflightError("host directory must be outside the repository")

    database = directory / DATABASE_NAME
    if database_is_symlink(database):
        raise PreflightError("registry database must not be a symbolic link")
    if not database.is_file():
        raise PreflightError(f"host directory must contain {DATABASE_NAME}")
    if database_sidecars(database):
        raise PreflightError("registry database has SQLite sidecar files")

    try:
        reader = RegistryReader(database, repository_root=root)
        with reader.connect() as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchone()
    except (RegistryLocationError, RegistryContractError, RegistryUnavailableError, sqlite3.Error) as exc:
        raise PreflightError("registry database failed its read-only contract") from exc
    if quick != "ok" or integrity != "ok" or foreign_keys is not None:
        raise PreflightError("registry database failed SQLite validation")
    if database_sidecars(database):
        raise PreflightError("registry validation produced unexpected SQLite sidecars")
    return {"available": True, "schema_version": 3}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Registry directory without modifying it.")
    parser.add_argument(
        "--host-dir",
        default=os.getenv("CLIMATE_REGISTRY_HOST_DIR", ""),
        help="absolute external directory containing article-registry.sqlite3",
    )
    arguments = parser.parse_args()
    if not arguments.host_dir:
        print(json.dumps({"status": "invalid", "reason": "not_configured"}))
        return 2
    try:
        result = validate_registry_host_directory(arguments.host_dir)
    except PreflightError:
        print(json.dumps({"status": "invalid", "reason": "preflight_failed"}))
        return 2
    print(json.dumps({"status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
