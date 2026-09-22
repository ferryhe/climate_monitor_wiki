"""Compose installed browser tools with fresh parent-owned Request bindings."""

# pylint: disable=too-many-arguments,too-many-positional-arguments
# pylint: disable=too-few-public-methods,too-many-instance-attributes

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Callable

from web_listening.request.model import ContentType
from web_listening.tool_registry.acquisition.builtins.web_http import (
    WEB_HTTP_MANIFEST,
    WebHttpAcquisitionTool,
)
from web_listening.tool_registry.acquisition.quality import (
    quality_failure_code,
    response_failure_code,
)
from web_listening.tool_registry.lifecycle import ToolLifecycle
from web_listening.tool_registry.manifest import (
    HealthStatus,
    QualificationStatus,
    ToolCategory,
    ToolDistribution,
    ToolLimits,
    ToolManifest,
    ToolRegistryError,
)
from web_listening.tool_registry.protocols.acquisition import (
    AcquisitionFailure,
    AcquisitionInput,
    AcquisitionOutput,
)
from web_listening.tool_registry.runners.browser_network import (
    BrowserNetworkBridge,
    _HeaderTransport,
)
from web_listening.tool_registry.runners.in_process import PinnedHttpTransport
from web_listening.tool_registry.runners.isolated_runtime import (
    IsolatedRuntime,
    NetworkBoundary,
)

_CONTEXT: ContextVar[tuple[Callable[[], bool], float | None]] = ContextVar(
    "browser_acquisition_execution", default=(lambda: False, None)
)


@contextmanager
def acquisition_execution(should_cancel, deadline):
    """Propagate the workflow's existing cancellation and absolute deadline."""
    token = _CONTEXT.set((should_cancel, deadline))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class GovernedHttpAcquisitionTool:
    """Bind the existing HTTP reader to the same deadline as browser attempts."""

    manifest = WEB_HTTP_MANIFEST

    def __init__(self, transport_factory, *, resolver=None):
        self.transport_factory = transport_factory
        self.resolver = resolver
        self._closed = False

    def acquire(self, tool_input):
        """Use the original HTTP Gateway with the current shared deadline."""
        cancelled, deadline = _CONTEXT.get()
        if self._closed or cancelled():
            return AcquisitionFailure(
                self.manifest.tool_id,
                self.manifest.version,
                "gateway.closed" if self._closed else "runtime.cancelled",
            )
        transport = _HeaderTransport(self.transport_factory())
        reader = WebHttpAcquisitionTool(
            lambda: transport, resolver=self.resolver, runtime_deadline=deadline
        )
        try:
            result = reader.acquire(tool_input)
            if (
                isinstance(result, AcquisitionFailure)
                and result.code == "gateway.http_status"
                and transport.status in {401, 403}
            ):
                return replace(
                    result,
                    code=response_failure_code(
                        transport.status, transport.last_headers
                    ),
                )
            return result
        finally:
            reader.close()

    def close(self):
        """Reject future work; each HTTP attempt already closed its transport."""
        self._closed = True


class BrowserAcquisitionTool:
    """Run one installed command; qualify and reuse its single Acquisition."""

    def __init__(
        self,
        manifest,
        command,
        lifecycle,
        *,
        transport_factory=PinnedHttpTransport,
        resolver=None,
    ):
        self.manifest = manifest
        self.command = command
        self.lifecycle = lifecycle
        self.transport_factory = transport_factory
        self.resolver = resolver
        self._closed = threading.Event()

    def acquire(
        self, tool_input: AcquisitionInput
    ):  # pylint: disable=too-many-return-statements
        """Return one current-input result, never reusing another target's binding."""
        if self._closed.is_set():
            return self._failure("gateway.closed")
        state = self.lifecycle.inspect(
            self.manifest.category, self.manifest.tool_id, self.manifest.version
        )
        if not state.active or state.disabled or state.broken:
            return self._failure("browser.inactive")
        if ContentType.HTML not in tool_input.request.scope.content_types:
            return self._failure("browser.html_required")
        cancelled, deadline = _CONTEXT.get()
        if cancelled():
            return self._failure("runtime.cancelled")
        try:
            return self.qualify(
                tool_input,
                "request-authorized",
                should_cancel=cancelled,
                deadline=deadline,
            )[1].result
        except ToolRegistryError as exc:
            return self._failure(exc.code)
        except (OSError, ValueError, KeyError):
            return self._failure("browser.runtime_missing")

    def qualify(
        self, tool_input, authorization, *, should_cancel=lambda: False, deadline=None
    ):
        """Expose parent qualification for the explicit installer workflow.

        The caller receives the original issuing runtime plus its live report
        for Lifecycle. No serialized report can activate an installation.
        """
        directory = Path(self.command[-1]).parent
        if not (directory / "runtime.json").is_file():
            raise ToolRegistryError("browser.runtime_missing")
        # Read only the installed configuration; the runtime never reads fixtures.
        configuration = json.loads((directory / "runtime.json").read_bytes())
        if configuration.get("sdk") not in {"playwright", "cloakbrowser"}:
            raise ToolRegistryError("browser.runtime_mismatch")
        with tempfile.TemporaryDirectory(prefix="web-listening-browser-") as profile:
            bridge = BrowserNetworkBridge(
                tool_input.request,
                tool_input.target_url,
                self.transport_factory(),
                resolver=self.resolver,
                deadline=deadline,
                should_cancel=lambda: self._closed.is_set() or should_cancel(),
            )
            try:
                endpoint = bridge.start()
                boundary = NetworkBoundary(
                    kind="controlled_proxy",
                    allowed_origins=tool_input.request.scope.allowed_origins,
                    proxy_server=endpoint,
                    browser_profile_home=profile,
                    observation_reader=bridge.observation,
                    bridge=bridge.credentials,
                    result_normalizer=bridge.normalize,
                    remaining_seconds=lambda: max(
                        0, bridge.deadline - time.monotonic()
                    ),
                )
                runtime = IsolatedRuntime(
                    self.manifest,
                    self.command,
                    authorization,
                    boundary,
                    tool_directory=directory,
                    state_reader=lambda: self.lifecycle.inspect(
                        self.manifest.category,
                        self.manifest.tool_id,
                        self.manifest.version,
                    ),
                )
                report = runtime.qualify(tool_input)
                if isinstance(report.result, AcquisitionOutput):
                    code = quality_failure_code(report.result)
                    if code is not None:
                        # Failed content is never an install qualification. The
                        # execution path still returns measured failure usage.
                        result = AcquisitionFailure(
                            self.manifest.tool_id,
                            self.manifest.version,
                            code,
                            report.result.requests,
                            report.result.bytes_received,
                            report.result.runtime_ms,
                            report.result.robots_decisions,
                        )
                        report = replace(
                            report, qualified=False, result=result, failure_code=code
                        )
                return runtime, report
            finally:
                bridge.close()

    def _failure(self, code):
        return AcquisitionFailure(self.manifest.tool_id, self.manifest.version, code)

    def close(self):
        """Cancel in-flight bridges and refuse later work."""
        self._closed.set()


FROZEN_RUNTIME_LOCK_SHA256 = (
    "4acc58a027525eb901de3407e0a3a33ffb4e66b8413dc0fff2b2696cd0697657"
)


def tree_digest(root: Path) -> str:
    """Hash the pinned SDK package independently of caches and install paths."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def host_runtime(root: Path, data_root: Path, entry: dict) -> dict:
    """Verify a separately provisioned Playwright runtime below the data root."""
    root = root.resolve(strict=True)
    if not root.is_relative_to(data_root / "browser-runtimes"):
        raise ValueError("browser.runtime_outside_data_directory")
    package = root / "lib/python3.12/site-packages/playwright"
    if tree_digest(package) != entry["sdk_tree_sha256"]:
        raise ValueError("browser.sdk_digest_mismatch")
    binary = (
        root
        / "browsers"
        / ("chromium-" + entry["browser_revision"])
        / "chrome-linux64/chrome"
    )
    with binary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != entry["browser_sha256"]:
        raise ValueError("browser.binary_digest_mismatch")
    interpreter = root / "bin/python"
    if not interpreter.is_file():
        raise ValueError("browser.runtime_missing")
    return {
        "python": str(interpreter),
        "sdk": "playwright",
        "sdk_version": entry["sdk_version"],
        "browser": str(binary),
        "browser_sha256": digest,
        "browser_version": entry["browser_version"],
        "browser_channel": entry["browser_channel"],
        "browser_revision": entry["browser_revision"],
        "sdk_tree_sha256": entry["sdk_tree_sha256"],
    }


def cloak_runtime(entry: dict, docker: str) -> dict:
    """Measure the existing pinned image with networking disabled and no pull."""
    executable = shutil.which(docker)
    if executable is None:
        raise ValueError("browser.container_runtime_missing")
    # Read files/metadata only. ensure_binary()/launch() are deliberately absent.
    probe = (
        "import hashlib,importlib.metadata,json,os,sys; from pathlib import Path; "
        "from cloakbrowser.config import get_binary_path; "
        "p=Path(os.environ.get('CLOAKBROWSER_BINARY_PATH') or "
        f"get_binary_path({entry['browser_build']!r})); "
        "f=p.open('rb'); digest=hashlib.file_digest(f,'sha256').hexdigest(); f.close(); "
        "print(json.dumps({'python':sys.executable,'sdk':'cloakbrowser',"
        "'sdk_version':importlib.metadata.version('cloakbrowser'),"
        "'browser':str(p),'browser_sha256':digest}))"
    )
    completed = subprocess.run(
        (
            executable,
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--entrypoint=python",
            entry["image"],
            "-B",
            "-c",
            probe,
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    config = json.loads(completed.stdout)
    if config["sdk_version"] != entry["sdk_version"]:
        raise ValueError("browser.sdk_version_mismatch")
    if config["browser_sha256"] != entry["browser_sha256"]:
        raise ValueError("browser.binary_digest_mismatch")
    config.update(
        container_image=entry["image"],
        docker=executable,
        browser_version=entry["browser_version"],
        browser_build=entry["browser_build"],
        browser_channel=entry["browser_channel"],
    )
    return config


BROWSER_ADAPTER_VERSIONS = {
    "acquisition.playwright": "1.0.0",
    "acquisition.cloakbrowser": "0.5.9",
}


def _runtime_identity_matches(data_dir, manifest):
    """Verify installed files against the reviewed lock before registering code."""
    try:
        data_root = Path(data_dir).resolve()
        installed = (
            data_root / "tools/acquisition" / manifest.tool_id / manifest.version
        )
        lock_bytes = (installed / "runtime-lock.json").read_bytes()
        if hashlib.sha256(lock_bytes).hexdigest() != FROZEN_RUNTIME_LOCK_SHA256:
            return False
        name = manifest.tool_id.removeprefix("acquisition.")
        entry = json.loads(lock_bytes)["tools"][name]
        if (entry["tool_id"], entry["adapter_version"]) != (
            manifest.tool_id,
            manifest.version,
        ):
            return False
        for filename, expected in entry["adapter_files_sha256"].items():
            if (
                hashlib.sha256((installed / filename).read_bytes()).hexdigest()
                != expected
            ):
                return False
        config = json.loads((installed / "runtime.json").read_bytes())
        if name == "playwright":
            expected_config = host_runtime(
                Path(config["python"]).parent.parent, data_root, entry
            )
        else:
            if config["container_image"] != entry["image"]:
                return False
            expected_config = cloak_runtime(entry, config["docker"])
        return config == expected_config
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return False


def installed_browser_tools(data_dir, *, transport_factory=PinnedHttpTransport):
    """Load only active default browser installations from this Runtime's data."""
    lifecycle = ToolLifecycle(data_dir)
    return tuple(
        BrowserAcquisitionTool(
            item.manifest, item.command, lifecycle, transport_factory=transport_factory
        )
        for item in lifecycle.active_versions(ToolCategory.ACQUISITION)
        if BROWSER_ADAPTER_VERSIONS.get(item.manifest.tool_id) == item.manifest.version
        and _runtime_identity_matches(data_dir, item.manifest)
    )


def browser_catalog_exclusions(data_dir, active_ids):
    """Describe missing/inactive defaults without loading their executable code."""
    lifecycle = ToolLifecycle(data_dir)
    exclusions = []
    for tool_id, version in BROWSER_ADAPTER_VERSIONS.items():
        if tool_id in active_ids:
            continue
        states = lifecycle.list_versions(ToolCategory.ACQUISITION, tool_id)
        if states:
            state = next(
                (item for item in states if item.active),
                next(
                    (item for item in states if item.manifest.version == version),
                    states[-1],
                ),
            )
            if state.manifest.version != version:
                exclusions.append((state.manifest, "eligibility.version_mismatch"))
                continue
            if state.active and not _runtime_identity_matches(data_dir, state.manifest):
                exclusions.append(
                    (state.manifest, "eligibility.runtime_identity_mismatch")
                )
                continue
            reason = (
                "eligibility.disabled"
                if state.disabled
                else (
                    "eligibility.unhealthy"
                    if state.broken
                    else (
                        "eligibility.unqualified"
                        if not state.qualified
                        else "eligibility.inactive"
                    )
                )
            )
            exclusions.append((state.manifest, reason))
        else:
            manifest = ToolManifest(
                tool_id,
                version,
                ToolCategory.ACQUISITION,
                ToolDistribution.INSTALLED,
                frozenset({"browser_render", "governed_network"}),
                ToolLimits(60, 4096, 8 * 1024 * 1024),
                HealthStatus.HEALTHY,
                QualificationStatus.UNQUALIFIED,
            )
            exclusions.append((manifest, "eligibility.not_installed"))
    return tuple(exclusions)
