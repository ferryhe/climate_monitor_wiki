from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient

from api_server import app
from climate_registry.errors import RegistryInputError
from climate_registry.persistent import _validate_database
from climate_registry.read_api import RegistryLocationError, RegistryReader
from climate_registry.schema import apply_migrations
from scripts.preflight_registry import (
    PreflightError,
    database_is_symlink,
    main as preflight_main,
    validate_registry_host_directory,
)
import scripts.test_registry_container as container_smoke
from scripts.test_registry_container import tracked_source_state, user_args_for_fixture_builder


ROOT = Path(__file__).resolve().parents[1]


def _database(directory: Path, *, version: int = 3) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "article-registry.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection, target_version=version)
    connection.close()
    return database


def _rewrite_table_sql(database: Path, table: str, old: str, new: str) -> None:
    connection = sqlite3.connect(database)
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()[0]
    assert old in sql
    if table == "reports" and ("PRIMARY KEY" in old or "UNIQUE" in old):
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE reports")
        connection.execute(sql.replace(old, new, 1))
        connection.commit()
        connection.close()
        return
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
        (sql.replace(old, new, 1), table),
    )
    schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
    connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
    connection.execute("PRAGMA writable_schema = OFF")
    connection.commit()
    connection.close()


def test_dockerfile_packages_registry_without_changing_entrypoint():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY climate_registry ./climate_registry" in dockerfile
    assert 'CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8501"]' in dockerfile


def test_registry_compose_override_uses_external_fixed_path_and_strict_read_only_bind():
    override = (ROOT / "docker-compose.registry.yml").read_text(encoding="utf-8")
    assert "CLIMATE_REGISTRY_DB: /registry/article-registry.sqlite3" in override
    assert "${CLIMATE_REGISTRY_HOST_DIR:?" in override
    assert "target: /registry" in override
    assert "read_only: true" in override
    assert "create_host_path: false" in override
    assert "/app/data/registry" not in override
    assert "./" not in override

    container_database = PurePosixPath("/registry/article-registry.sqlite3")
    with pytest.raises(ValueError):
        container_database.relative_to(PurePosixPath("/app"))
    external = ROOT.parent / "registry" / "article-registry.sqlite3"
    reader = RegistryReader(external, repository_root=ROOT)
    assert reader.database == external.resolve(strict=False)
    with pytest.raises(RegistryLocationError):
        RegistryReader(ROOT / "data" / "registry" / "article-registry.sqlite3", repository_root=ROOT)


def test_compose_renders_registry_bind_without_creating_a_host_path(tmp_path):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI is not installed")
    environment = os.environ | {
        "CLIMATE_REGISTRY_HOST_DIR": str(tmp_path.resolve()),
        "OPENAI_API_KEY": "",
        "RELOAD_TOKEN": "",
    }
    completed = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.registry.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(completed.stdout)
    mounts = rendered["services"]["wiki"]["volumes"]
    registry = next(item for item in mounts if item["target"] == "/registry")
    assert registry["source"] == str(tmp_path.resolve())
    assert registry["read_only"] is True
    assert registry["bind"]["create_host_path"] is False


def test_unconfigured_status_is_503_with_safe_reason_and_core_app_stays_healthy(monkeypatch):
    monkeypatch.delenv("CLIMATE_REGISTRY_DB", raising=False)
    client = TestClient(app)
    response = client.get("/api/registry/status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "not_configured"}
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200


def test_valid_empty_registry_is_available(tmp_path, monkeypatch):
    database = _database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    response = TestClient(app).get("/api/registry/status")
    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "schema_version": 3,
        "reports": 0,
        "articles": 0,
        "discoveries": 0,
        "latest_report_date": None,
    }


def test_corrupt_and_wrong_schema_registry_have_stable_safe_reasons(tmp_path, monkeypatch):
    client = TestClient(app)
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(corrupt))
    response = client.get("/api/registry/status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "database_unavailable"}
    assert str(tmp_path) not in response.text

    old = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(old)
    apply_migrations(connection, target_version=2)
    connection.close()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(old))
    response = client.get("/api/registry/status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_schema"}


def test_unreadable_registry_has_stable_safe_reason(tmp_path, monkeypatch):
    database = tmp_path / "article-registry.sqlite3"
    database.touch()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))

    def deny_open(*args, **kwargs):
        raise sqlite3.OperationalError("permission denied at private path")

    monkeypatch.setattr("climate_registry.read_api.sqlite3.connect", deny_open)
    response = TestClient(app).get("/api/registry/status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "database_unavailable"}
    assert "private path" not in response.text


@pytest.mark.parametrize(
    "damage_sql",
    [
        "DELETE FROM schema_migrations WHERE version = 3",
        "UPDATE schema_migrations SET name = 'altered' WHERE version = 3",
        "DROP TRIGGER article_fetches_are_append_only_update",
        "DROP INDEX idx_article_fetches_article_fetched",
    ],
)
def test_status_fails_closed_for_altered_v3_contract(tmp_path, monkeypatch, damage_sql):
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute(damage_sql)
    connection.commit()
    connection.close()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))

    response = TestClient(app).get("/api/registry/status")

    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_schema"}
    assert str(tmp_path) not in response.text


def test_status_fails_closed_when_critical_foreign_key_is_removed(tmp_path, monkeypatch):
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'article_enrichments'"
    ).fetchone()[0]
    assert "REFERENCES article_content_versions(content_version_id)" in sql
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'article_enrichments'",
        (sql.replace(" REFERENCES article_content_versions(content_version_id)", ""),),
    )
    schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
    connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
    connection.execute("PRAGMA writable_schema = OFF")
    connection.commit()
    connection.close()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))

    response = TestClient(app).get("/api/registry/status")

    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_schema"}


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        (
            "articles",
            "CHECK (display_policy IN ('metadata_only', 'summary_excerpt', 'full_markdown'))",
            "",
        ),
        (
            "articles",
            "display_policy TEXT NOT NULL DEFAULT 'summary_excerpt'",
            "display_policy TEXT DEFAULT 'summary_excerpt'",
        ),
        ("reports", "report_id TEXT PRIMARY KEY", "report_id TEXT"),
        ("reports", "report_date TEXT NOT NULL UNIQUE", "report_date TEXT NOT NULL"),
    ],
)
def test_status_and_preflight_reject_altered_table_constraints(
    tmp_path, monkeypatch, table, old, new
):
    host_dir = tmp_path / "registry"
    database = _database(host_dir)
    _rewrite_table_sql(database, table, old, new)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))

    response = TestClient(app).get("/api/registry/status")

    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_schema"}
    with pytest.raises(PreflightError):
        validate_registry_host_directory(host_dir, repository_root=ROOT)


@pytest.mark.parametrize(
    "extra_sql",
    [
        "CREATE UNIQUE INDEX unexpected_articles_seen_unique ON articles(first_seen, last_seen)",
        """
        CREATE TRIGGER unexpected_articles_mutation
        AFTER UPDATE ON articles
        BEGIN
            UPDATE articles SET last_seen = NEW.last_seen WHERE article_id = NEW.article_id;
        END
        """,
        """
        CREATE TRIGGER unexpected_articles_uppercase
        AFTER UPDATE ON ARTICLES
        BEGIN
            SELECT 1;
        END
        """,
        """
        CREATE TRIGGER unexpected_articles_quoted_mixed_case
        AFTER UPDATE ON "ArTiClEs"
        BEGIN
            SELECT 1;
        END
        """,
    ],
)
def test_api_preflight_and_persistent_reject_unexpected_semantic_schema_objects(
    tmp_path, monkeypatch, extra_sql
):
    host_dir = tmp_path / "registry"
    database = _database(host_dir)
    connection = sqlite3.connect(database)
    connection.execute(extra_sql)
    connection.commit()
    with pytest.raises(RegistryInputError):
        _validate_database(connection)
    connection.close()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))

    response = TestClient(app).get("/api/registry/status")

    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_schema"}
    with pytest.raises(PreflightError):
        validate_registry_host_directory(host_dir, repository_root=ROOT)


def test_extra_non_unique_performance_index_is_allowed(tmp_path, monkeypatch):
    host_dir = tmp_path / "registry"
    database = _database(host_dir)
    connection = sqlite3.connect(database)
    connection.execute("CREATE INDEX optional_reports_date_lookup ON reports(report_date)")
    connection.commit()
    assert _validate_database(connection) == 3
    connection.close()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))

    response = TestClient(app).get("/api/registry/status")

    assert response.status_code == 200
    assert response.json()["available"] is True
    assert validate_registry_host_directory(host_dir, repository_root=ROOT)["available"] is True


@pytest.mark.parametrize("kind", ["relative", "inside", "missing-dir", "missing-db", "v2", "corrupt", "sidecar"])
def test_registry_host_preflight_rejects_invalid_candidates(tmp_path, kind):
    outside = tmp_path / "external"
    outside.mkdir()
    host_dir: Path | str = outside
    if kind == "relative":
        host_dir = Path("relative-registry")
    elif kind == "inside":
        host_dir = ROOT / "tmp" / "registry-preflight-do-not-create"
    elif kind == "missing-dir":
        host_dir = tmp_path / "missing"
    elif kind == "missing-db":
        pass
    elif kind == "v2":
        _database(outside, version=2)
    elif kind == "corrupt":
        (outside / "article-registry.sqlite3").write_bytes(b"broken")
    elif kind == "sidecar":
        database = _database(outside)
        Path(f"{database}-wal").touch()

    with pytest.raises(PreflightError):
        validate_registry_host_directory(host_dir, repository_root=ROOT)


def test_registry_host_preflight_accepts_valid_v3_without_writing(tmp_path):
    host_dir = tmp_path / "external"
    database = _database(host_dir)
    before = database.read_bytes()

    result = validate_registry_host_directory(host_dir, repository_root=ROOT)

    assert result == {"available": True, "schema_version": 3}
    assert database.read_bytes() == before
    assert not any(Path(f"{database}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))


def test_database_symlink_detection_is_unit_testable_without_platform_support(tmp_path):
    database = tmp_path / "article-registry.sqlite3"
    assert database_is_symlink(database, predicate=lambda path: path == database) is True
    assert database_is_symlink(database, predicate=lambda path: False) is False


def test_registry_host_preflight_rejects_database_symlink_when_supported(tmp_path):
    target_dir = tmp_path / "target"
    target = _database(target_dir)
    host_dir = tmp_path / "registry"
    host_dir.mkdir()
    link = host_dir / "article-registry.sqlite3"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable for this user/platform")

    with pytest.raises(PreflightError, match="symbolic link"):
        validate_registry_host_directory(host_dir, repository_root=ROOT)


def test_registry_host_preflight_cli_exits_nonzero_with_safe_output(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing"
    monkeypatch.setattr(sys, "argv", ["preflight_registry.py", "--host-dir", str(missing)])

    assert preflight_main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "invalid",
        "reason": "preflight_failed",
    }


def test_container_source_guard_allows_untracked_but_rejects_tracked_changes(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "Dockerfile"
    tracked.write_text("FROM scratch\n", encoding="utf-8")
    subprocess.run(["git", "add", "Dockerfile"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)

    clean = tracked_source_state(tmp_path)
    assert clean.clean is True
    assert len(clean.head) == 40
    (tmp_path / "untracked.txt").write_text("preserve", encoding="utf-8")
    assert tracked_source_state(tmp_path).clean is True
    tracked.write_text("FROM busybox\n", encoding="utf-8")
    dirty = tracked_source_state(tmp_path)
    assert dirty.clean is False
    assert dirty.head == clean.head


def test_fixture_builder_uses_host_identity_on_posix():
    args = user_args_for_fixture_builder(platform="posix", uid=123, gid=456)
    assert args == ["--user", "123:456"]
    assert user_args_for_fixture_builder(platform="nt", uid=None, gid=None) == []


def test_start_app_removes_detached_container_when_startup_fails(monkeypatch):
    calls = []

    def fake_docker(*args, cwd=ROOT):
        calls.append(args)
        if args[0] == "run":
            return "container-id"
        if args[:2] == ("port", "container-id"):
            raise subprocess.CalledProcessError(1, args)
        if args[:3] == ("rm", "--force", "container-id"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(container_smoke, "docker", fake_docker)

    with pytest.raises(subprocess.CalledProcessError):
        container_smoke.start_app()

    assert calls[-1] == ("rm", "--force", "container-id")


def test_reusable_container_and_browser_smoke_scripts_are_present():
    container = (ROOT / "scripts" / "test_registry_container.py").read_text(encoding="utf-8")
    browser = (ROOT / "scripts" / "test_registry_browser.py").read_text(encoding="utf-8")
    preflight = (ROOT / "scripts" / "preflight_registry.py").read_text(encoding="utf-8")
    assert '"git", "archive"' in container
    assert 'docker("build"' in container
    assert "import climate_registry" in container
    assert '"--read-only"' in container
    assert "/api/registry/status" in container
    assert "/api/chat" in container
    assert 'chat.get("text")' in container
    assert "os.replace" in container
    assert "sync_playwright" in browser
    assert "route.abort" in browser
    assert "validate_registry_host_directory" in preflight
