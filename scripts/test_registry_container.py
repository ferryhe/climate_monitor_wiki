from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "climate-monitor-wiki:registry-smoke"
DATABASE_NAME = "article-registry.sqlite3"


@dataclass(frozen=True)
class SourceState:
    head: str
    clean: bool


def tracked_source_state(root: Path = ROOT) -> SourceState:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    difference = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--"],
        cwd=root,
        check=False,
    )
    if difference.returncode not in (0, 1):
        raise RuntimeError("could not compare tracked source with HEAD")
    return SourceState(head=head, clean=difference.returncode == 0)


def user_args_for_fixture_builder(
    *, platform: str = os.name, uid: int | None = None, gid: int | None = None
) -> list[str]:
    if platform != "posix":
        return []
    actual_uid = os.getuid() if uid is None else uid
    actual_gid = os.getgid() if gid is None else gid
    return ["--user", f"{actual_uid}:{actual_gid}"]


def docker(*args: str, cwd: Path = ROOT) -> str:
    completed = subprocess.run(
        ["docker", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def snapshot(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def assert_no_sidecars(directory: Path) -> None:
    assert not any(
        path.name.endswith(("-wal", "-shm", "-journal"))
        for path in directory.rglob("*")
    )


def request(
    url: str,
    *,
    data: dict | None = None,
    parse_json: bool = True,
) -> tuple[int, dict | None]:
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=body, headers=headers), timeout=20
        ) as response:
            raw = response.read()
            payload = json.loads(raw) if parse_json and raw else None
            return response.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        payload = json.loads(raw) if parse_json and raw else None
        return exc.code, payload


def wait_for_health(port: int) -> None:
    for _ in range(80):
        try:
            if request(f"http://127.0.0.1:{port}/api/health")[0] == 200:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError("container did not become healthy")


def start_app(registry_dir: Path | None = None) -> tuple[str, int]:
    args = ["run", "--detach", "--read-only", "--publish", "127.0.0.1::8501"]
    if registry_dir is not None:
        args += ["--env", "CLIMATE_REGISTRY_DB=/registry/article-registry.sqlite3"]
        args += [
            "--mount",
            f"type=bind,src={registry_dir.resolve()},dst=/registry,readonly",
        ]
    container = docker(*args, IMAGE)
    try:
        port = int(docker("port", container, "8501/tcp").rsplit(":", 1)[1])
        wait_for_health(port)
        return container, port
    except Exception:
        docker("rm", "--force", container)
        raise


def assert_core_app(port: int) -> None:
    assert request(f"http://127.0.0.1:{port}/api/health")[0] == 200
    assert request(f"http://127.0.0.1:{port}/", parse_json=False)[0] == 200
    chat_code, chat = request(
        f"http://127.0.0.1:{port}/api/chat",
        data={"message": "Summarize the latest report", "answerMode": "brief"},
    )
    assert chat_code == 200 and chat and chat.get("text"), chat


def run_case(
    name: str,
    *,
    expected_code: int,
    expected_reason: str | None,
    registry_dir: Path | None = None,
) -> None:
    before = snapshot(registry_dir) if registry_dir is not None else None
    container, port = start_app(registry_dir)
    try:
        code, payload = request(f"http://127.0.0.1:{port}/api/registry/status")
        assert code == expected_code, (name, code, payload)
        if expected_reason:
            assert payload == {"available": False, "reason": expected_reason}, (name, payload)
        else:
            assert payload and payload.get("available") is True, (name, payload)
        assert_core_app(port)
    finally:
        docker("rm", "--force", container)
    if registry_dir is not None:
        assert snapshot(registry_dir) == before, f"application modified the {name} fixture"
        assert_no_sidecars(registry_dir)


def run_atomic_replacement_case(seeded: Path, replacement: Path) -> None:
    container, port = start_app(seeded)
    try:
        before = snapshot(seeded)
        code, payload = request(f"http://127.0.0.1:{port}/api/registry/status")
        assert code == 200 and payload and payload["reports"] > 0, payload
        assert_core_app(port)
        assert snapshot(seeded) == before

        os.replace(replacement / DATABASE_NAME, seeded / DATABASE_NAME)
        after_operator_replace = snapshot(seeded)
        code, payload = request(f"http://127.0.0.1:{port}/api/registry/status")
        assert code == 200 and payload and payload["reports"] == 0, payload
        assert_core_app(port)
        assert snapshot(seeded) == after_operator_replace
        assert_no_sidecars(seeded)
    finally:
        docker("rm", "--force", container)


def build_fixture(directory: Path, code: str) -> None:
    docker(
        "run",
        "--rm",
        *user_args_for_fixture_builder(),
        "--mount",
        f"type=bind,src={directory.resolve()},dst=/registry",
        IMAGE,
        "python",
        "-c",
        code,
    )


def main() -> int:
    source = tracked_source_state()
    if not source.clean:
        print(
            f"registry container smoke blocked: tracked source differs from HEAD {source.head}",
            file=sys.stderr,
        )
        return 2
    print(f"registry container smoke source HEAD: {source.head}")
    try:
        docker("info", "--format", "{{.ServerVersion}}")
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("registry container smoke blocked: Docker daemon is unavailable", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="climate-registry-container-") as temporary:
        scratch = Path(temporary)
        source_dir = scratch / "source"
        source_dir.mkdir()
        archive = subprocess.run(
            ["git", "archive", "--format=tar", source.head],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            tar.extractall(source_dir, filter="data")
        docker("build", "--tag", IMAGE, str(source_dir))
        docker("run", "--rm", IMAGE, "python", "-c", "import climate_registry, api_server")

        paths = {
            name: scratch / name
            for name in ("empty", "replacement", "seeded", "wrong", "corrupt", "missing")
        }
        for directory in paths.values():
            directory.mkdir()
        v3_builder = (
            "import sqlite3; from climate_registry.schema import apply_migrations; "
            "c=sqlite3.connect('/registry/article-registry.sqlite3'); apply_migrations(c); c.close()"
        )
        build_fixture(paths["empty"], v3_builder)
        build_fixture(paths["replacement"], v3_builder)
        docker(
            "run",
            "--rm",
            *user_args_for_fixture_builder(),
            "--mount",
            f"type=bind,src={paths['seeded'].resolve()},dst=/registry",
            IMAGE,
            "python",
            "-m",
            "climate_registry",
            "audit-history",
            "--source-dir",
            "/app/sources",
            "--database",
            "/registry/article-registry.sqlite3",
            "--output-dir",
            "/registry/audit",
        )
        build_fixture(
            paths["wrong"],
            "import sqlite3; from climate_registry.schema import apply_migrations; "
            "c=sqlite3.connect('/registry/article-registry.sqlite3'); "
            "apply_migrations(c,target_version=2); c.close()",
        )
        (paths["corrupt"] / DATABASE_NAME).write_bytes(b"not a sqlite database")

        run_case("unconfigured", expected_code=503, expected_reason="not_configured")
        run_case(
            "missing",
            expected_code=503,
            expected_reason="database_unavailable",
            registry_dir=paths["missing"],
        )
        run_case(
            "corrupt",
            expected_code=503,
            expected_reason="database_unavailable",
            registry_dir=paths["corrupt"],
        )
        run_case(
            "wrong",
            expected_code=503,
            expected_reason="invalid_schema",
            registry_dir=paths["wrong"],
        )
        run_case("empty", expected_code=200, expected_reason=None, registry_dir=paths["empty"])
        run_atomic_replacement_case(paths["seeded"], paths["replacement"])
    print("registry container smoke: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
