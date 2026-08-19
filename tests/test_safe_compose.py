from __future__ import annotations

import os
import subprocess

import pytest

from scripts import safe_compose


@pytest.mark.parametrize(
    ("override", "variable"),
    [
        ("docker-compose.delivery.yml", "CLIMATE_DELIVERY_ARTIFACTS_HOST_DIR"),
        ("docker-compose.job-status.yml", "CLIMATE_JOB_STATUS_HOST_DIR"),
        ("docker-compose.registry.yml", "CLIMATE_REGISTRY_HOST_DIR"),
        ("docker-compose.update-status.yml", "CLIMATE_UPDATE_STATUS_HOST_DIR"),
    ],
)
def test_missing_bind_source_is_rejected_before_compose(
    tmp_path, monkeypatch, override, variable
):
    missing = tmp_path / "missing"
    environment = os.environ | {variable: str(missing)}

    def unexpected_run(*args, **kwargs):
        pytest.fail("Docker Compose must not run for a missing bind source")

    monkeypatch.setattr(safe_compose.subprocess, "run", unexpected_run)
    with pytest.raises(
        safe_compose.ComposeBindSourceError,
        match=rf"{variable} must be an absolute existing directory$",
    ):
        safe_compose.run_compose(
            ["-f", "docker-compose.yml", "-f", override, "config", "--quiet"],
            environment=environment,
        )
    assert not missing.exists()


def test_existing_bind_sources_are_forwarded_without_rewriting_paths(
    tmp_path, monkeypatch
):
    directories = {
        variable: tmp_path / override.removeprefix("docker-compose.").removesuffix(".yml")
        for override, variable in safe_compose._BIND_SOURCE_VARIABLES.items()
    }
    for directory in directories.values():
        directory.mkdir()
    environment = os.environ | {
        variable: str(directory.resolve())
        for variable, directory in directories.items()
    }
    arguments = ["-f", "docker-compose.yml"]
    for override in safe_compose._BIND_SOURCE_VARIABLES:
        arguments.extend(("-f", override))
    arguments.extend(("config", "--quiet"))
    observed: dict[str, object] = {}

    monkeypatch.setattr(safe_compose.shutil, "which", lambda *args, **kwargs: "docker")

    def recording_run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(safe_compose.subprocess, "run", recording_run)
    assert safe_compose.run_compose(arguments, environment=environment) == 0
    assert observed["command"] == ["docker", "compose", *arguments]
    for variable, directory in directories.items():
        assert observed["environment"][variable] == str(directory.resolve())
