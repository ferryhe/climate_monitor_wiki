"""Versioned external browser adapter; all target I/O belongs to the parent.

Protocol envelopes, describe/health/probe separation, relative output, SHA and
cleanup are migrated from Issue 18's CloakBrowser 0.5.9 adapter. Browser-specific
code consists only of SDK launch and page operations. This file is intentionally
self-contained so installed versions never import the source tree or fixtures.
"""

# The installed versions must remain self-contained. SDK failures are contained
# here; the parent retains policy and measured-failure authority.
# pylint: disable=duplicate-code,broad-exception-caught
# pylint: disable=too-many-boolean-expressions,too-many-locals,too-many-statements

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.metadata
import json
import os
import signal
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

TOOL_ID = "acquisition.playwright"
VERSION = "1.0.0"
SDK = "playwright"
SDK_VERSION = "1.62.0"
EXTERNAL_PROTOCOL = "web-listening-external-tool.v1"
QUALIFICATION_PROTOCOL = "web-listening-tool-qualification.v1"
CHECKS = [
    "health",
    "protocol",
    "scope",
    "redirect",
    "output_bound",
    "controlled_proxy_or_network_isolation",
]
FLAGS = [
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-domain-reliability",
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "--proxy-bypass-list=<-loopback>",
]


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _configuration():
    config = json.loads(
        Path(__file__).with_name("runtime.json").read_bytes(),
        object_pairs_hook=_unique_object,
    )
    if config["sdk"] != SDK or config["sdk_version"] != SDK_VERSION:
        raise ValueError("runtime identity")
    executable = Path(config["browser"])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("runtime binary")
    with executable.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != config["browser_sha256"]:
        raise ValueError("runtime binary digest")
    if importlib.metadata.version(SDK) != SDK_VERSION:
        raise ValueError("runtime SDK")
    return config


def _bootstrap():
    config_path = Path(__file__).with_name("runtime.json")
    if not config_path.is_file():
        return
    config = json.loads(config_path.read_bytes(), object_pairs_hook=_unique_object)
    image = config.get("container_image")
    if image and os.environ.get("WEB_LISTENING_RUNTIME_IMAGE") != image:
        # The Cloak runtime is the already-pinned Issue 18 image. Container
        # startup never pulls an image; absent images fail instead of downloading.
        directory = str(Path(__file__).resolve().parent)
        attempt = str(Path.cwd().resolve())
        command = [
            config["docker"],
            "run",
            "--rm",
            "-i",
            "--pull=never",
            "--network=host",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--tmpfs=/tmp:rw,nosuid,size=512m",
            "--mount",
            f"type=bind,src={directory},dst={directory},readonly",
        ]
        if attempt != directory:
            command += ["--mount", f"type=bind,src={attempt},dst={attempt}"]
        command += [
            "--workdir",
            attempt,
            "--env",
            "WEB_LISTENING_RUNTIME_IMAGE=" + image,
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint",
            config["python"],
            image,
            str(Path(__file__).resolve()),
            *sys.argv[1:],
        ]
        os.execv(config["docker"], command)
    interpreter = config["python"]
    if not Path(interpreter).is_absolute() or not Path(interpreter).is_file():
        raise ValueError("runtime interpreter")
    # Compare lexical executable paths: venv executables may be symlinks.
    if os.path.abspath(sys.executable) != interpreter:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        os.execve(
            interpreter,
            (interpreter, str(Path(__file__).resolve()), *sys.argv[1:]),
            environment,
        )


def _boundary():
    if len(sys.argv) != 3 or sys.argv[1] != "--web-listening-boundary":
        raise ValueError("boundary missing")
    value = json.loads(
        base64.urlsafe_b64decode(sys.argv[2]), object_pairs_hook=_unique_object
    )
    bridge = value["bridge"]
    parsed = urlsplit(value["proxy_server"])
    if (
        value["schema_version"] != "web-listening-network-boundary.v1"
        or value["kind"] != "controlled_proxy"
        or bridge["protocol"] != "web-listening-browser-bridge.v1"
        or parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or len(bridge["token"]) != 64
        or len(value["attempt_nonce"]) != 64
    ):
        raise ValueError("boundary invalid")
    return value


def _ipc(boundary, path, **values):
    payload = {"nonce": boundary["attempt_nonce"], **values}
    request = urllib.request.Request(
        boundary["proxy_server"] + path,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={
            "Authorization": "Bearer " + boundary["bridge"]["token"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(
        request, timeout=max(0.1, min(60, boundary["limits"]["max_runtime_seconds"]))
    ) as response:
        # Responses contain only a bounded, parent-authorized body and metadata.
        limit = boundary["limits"]["max_output_bytes"] * 2 + 65536
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("bridge response limit")
        return json.loads(raw)


def _failure(code):
    return {
        "protocol_version": EXTERNAL_PROTOCOL,
        "category": "acquisition",
        "status": "failed",
        "tool_id": TOOL_ID,
        "tool_version": VERSION,
        "result": {"code": code},
    }


def _control(request):
    operation = request.get("operation")
    expected = {
        "protocol_version": QUALIFICATION_PROTOCOL,
        "operation": operation,
        "tool_id": TOOL_ID,
        "version": VERSION,
        "category": "acquisition",
    }
    if operation == "probe":
        expected["checks"] = CHECKS
    if request != expected:
        return {
            "protocol_version": QUALIFICATION_PROTOCOL,
            "operation": operation,
            "status": "error",
        }
    if operation == "describe":
        return {**expected, "status": "ok"}
    try:
        _configuration()
        _boundary()
    except (OSError, ValueError, KeyError, importlib.metadata.PackageNotFoundError):
        return {
            "protocol_version": QUALIFICATION_PROTOCOL,
            "operation": operation,
            "status": "error",
        }
    if operation == "health":
        return {
            "protocol_version": QUALIFICATION_PROTOCOL,
            "operation": operation,
            "status": "ok",
            "health": "healthy",
        }
    return {
        "protocol_version": QUALIFICATION_PROTOCOL,
        "operation": operation,
        "status": "ok",
        "result": "qualified",
        "category": "acquisition",
        "checks": CHECKS,
    }


def _launch(config, boundary, timeout):
    options = {
        "headless": True,
        "args": FLAGS,
        "timeout": timeout,
        "proxy": {"server": boundary["proxy_server"], "bypass": "<-loopback>"},
    }
    if SDK == "cloakbrowser":
        os.environ["CLOAKBROWSER_BINARY_PATH"] = config["browser"]
        os.environ["CLOAKBROWSER_AUTO_UPDATE"] = "false"
        return importlib.import_module("cloakbrowser").launch(**options), None
    playwright = (
        importlib.import_module("playwright.sync_api").sync_playwright().start()
    )
    try:
        return (
            playwright.chromium.launch(executable_path=config["browser"], **options),
            playwright,
        )
    except BaseException:
        playwright.stop()
        raise


def _wait_for_redirect_render(page, redirects, timeout, started):
    """Settle HAR replay's load-only navigation within the original deadline."""
    if redirects:
        remaining = timeout - (time.monotonic() - started) * 1000
        if remaining <= 0:
            raise TimeoutError("Redirect render deadline exhausted")
        page.wait_for_load_state("networkidle", timeout=remaining)


def _acquire(request):
    try:
        boundary = _boundary()
        value = request["input"]
        if (
            set(request)
            != {
                "protocol_version",
                "category",
                "tool_id",
                "tool_version",
                "attempt_directory",
                "input",
            }
            or request["protocol_version"] != EXTERNAL_PROTOCOL
            or request["category"] != "acquisition"
            or request["tool_id"] != TOOL_ID
            or request["tool_version"] != VERSION
            or request["attempt_directory"] != "."
            or value["target_url"] != boundary["target_url"]
            or value["allowed_origins"] != boundary["allowed_origins"]
            or "html" not in value["content_types"]
            or value["limits"]["max_requests"] != boundary["limits"]["max_requests"]
            or value["limits"]["max_bytes"] > boundary["limits"]["max_output_bytes"]
            or value["limits"]["max_runtime_seconds"]
            > boundary["limits"]["max_runtime_seconds"]
        ):
            return _failure("browser.binding_mismatch")
        config = _configuration()
    except (OSError, ValueError, KeyError, importlib.metadata.PackageNotFoundError):
        return _failure("browser.runtime_missing")
    browser = context = page = playwright = None
    stopped = threading.Event()
    interrupted = {"code": None}

    def interrupt(_number, _frame):
        raise InterruptedError("browser stopped")

    previous_signal = signal.signal(signal.SIGTERM, interrupt)

    def watch():
        while not stopped.wait(0.1):
            try:
                status = _ipc(boundary, "/status")
                if status["ok"]:
                    continue
                interrupted["code"] = status["code"]
            except Exception:
                interrupted["code"] = "browser.bridge_unavailable"
            if not stopped.is_set():
                os.kill(os.getpid(), signal.SIGTERM)
            return

    watcher = threading.Thread(target=watch, daemon=True)
    started = time.monotonic()
    failure = None
    output = None
    redirects = []
    document = {}
    try:
        watcher.start()
        timeout = max(1, int(value["limits"]["max_runtime_seconds"] * 1000))
        browser, playwright = _launch(config, boundary, timeout)
        if browser.version != config["browser_version"]:
            raise ValueError("browser runtime mismatch")
        context = browser.new_context(accept_downloads=False, service_workers="block")

        def route_request(route):
            request_value = route.request
            navigation = request_value.is_navigation_request()
            main_document = navigation and getattr(
                request_value, "frame", None
            ) is getattr(page, "main_frame", None)
            response = _ipc(
                boundary,
                "/read",
                url=request_value.url,
                method=request_value.method,
                navigation=navigation,
                main_document=main_document,
            )
            while response["ok"] and 300 <= response["status"] < 400:
                target = response["headers"]["location"]
                if main_document:
                    redirects.append(
                        {
                            "from_url": response["url"],
                            "to_url": target,
                            "status_code": response["status"],
                        }
                    )
                if navigation:
                    # Like the pinned SDK's HAR router: a fulfilled 302 would
                    # follow natively without routing its next hop. Replay a
                    # navigation instead, so the parent cache serves that hop.
                    # pylint: disable=protected-access
                    route._sync(route._impl_obj._redirected_navigation_request(target))
                    # pylint: enable=protected-access
                    return
                # Resources consume already-governed redirect responses here;
                # never send a redirect to Chromium's native network path.
                response = _ipc(
                    boundary,
                    "/read",
                    url=target,
                    method=request_value.method,
                    navigation=False,
                    main_document=False,
                )
            if not response["ok"]:
                interrupted["code"] = response["code"]
                route.abort("blockedbyclient")
                return
            if main_document and 200 <= response["status"] < 300:
                document.update(response)
            route.fulfill(
                status=response["status"],
                headers=response["headers"],
                body=base64.b64decode(response["body"]),
            )

        context.route("**/*", route_request)
        context.route_web_socket("**/*", lambda socket: socket.close())
        page = context.new_page()
        response = page.goto(
            value["target_url"], wait_until="networkidle", timeout=timeout
        )
        _wait_for_redirect_render(page, redirects, timeout, started)
        status = _ipc(boundary, "/status")
        if not status["ok"] or interrupted["code"]:
            interrupted["code"] = interrupted["code"] or status["code"]
            raise InterruptedError("browser stopped")
        if response is None or not 200 <= response.status < 300:
            raise ValueError("browser navigation failed")
        body = page.content().encode("utf-8")
        if len(body) > value["limits"]["max_bytes"]:
            interrupted["code"] = "browser.output_limit"
            raise InterruptedError("browser output limit")
        mime_type = document.get("mime_type", "text/html")
        path = Path(
            "content.xhtml" if mime_type == "application/xhtml+xml" else "content.html"
        )
        path.write_bytes(body)
        output = {
            "protocol_version": EXTERNAL_PROTOCOL,
            "category": "acquisition",
            "status": "success",
            "tool_id": TOOL_ID,
            "tool_version": VERSION,
            "result": {
                "requested_url": value["target_url"],
                "final_url": page.url,
                "status_code": response.status,
                "mime_type": mime_type,
                "output_path": str(path),
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "redirects": redirects,
                "runtime_ms": round((time.monotonic() - started) * 1000),
            },
        }
    except InterruptedError:
        failure = interrupted["code"] or "runtime.cancelled"
    except Exception as exc:
        failure = interrupted["code"] or (
            "browser.navigation_timeout"
            if type(exc).__name__ == "TimeoutError"
            else "browser.navigation_failed"
        )
    finally:
        stopped.set()
        watcher.join(1)
        for resource, method in (
            (page, "close"),
            (context, "close"),
            (browser, "close"),
            (playwright, "stop"),
        ):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    failure = failure or "browser.cleanup_failed"
        signal.signal(signal.SIGTERM, previous_signal)
    return _failure(failure) if failure else output


def main():
    """Process one existing protocol envelope and exit."""
    try:
        _bootstrap()
        request = json.load(sys.stdin, object_pairs_hook=_unique_object)
        result = (
            _control(request)
            if request.get("protocol_version") == QUALIFICATION_PROTOCOL
            else _acquire(request)
        )
    except Exception:
        result = _failure("browser.runtime_missing")
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
