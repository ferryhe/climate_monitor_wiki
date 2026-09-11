from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from limits import parse as parse_rate_limit
from slowapi import Limiter
from slowapi.util import get_remote_address
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


from agentic_wiki import AgenticWikiResponder
from climate_delivery.artifacts import load_report_artifact
from climate_monitor.job_status import (
    JobStatusInvalidSnapshotError,
    JobStatusLocationError,
    JobStatusSnapshotReader,
    JobStatusUnavailableError,
)
from climate_monitor.run_ledger import (
    LedgerContractError,
    LedgerLocationError,
    LedgerUnavailableError,
    RunLedgerReader,
)
from climate_monitor.management import ManagementService
from climate_monitor.console_auth import (
    ConsoleUser,
    auth_router,
    current_console_user,
    optional_console_user,
)
from climate_monitor.hermes_dashboard import (
    oauth_callback_proxy_path,
    proxy_http,
    proxy_websocket,
)
from climate_registry.read_api import (
    RegistryContractError,
    RegistryLocationError,
    RegistryNotFoundError,
    RegistryQueryError,
    RegistryReader,
    RegistryUnavailableError,
)


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
SHOWCASE_DIR = ROOT / "showcase"
MANAGE_DIR = ROOT / "management_ui"
WIKI_DIR = ROOT / os.getenv("WIKI_DIR", "wiki")
SOURCE_DIR = ROOT / os.getenv("SOURCE_DIR", "sources")
ARTICLE_METADATA_DIR = ROOT / os.getenv("ARTICLE_METADATA_DIR", "article_metadata")

# In production, disable Swagger/OpenAPI documentation to reduce attack surface.
# These are development conveniences, not required for the public site.
_ENABLE_DOCS = os.getenv("ENABLE_DOCS", "").strip().lower() in {"1", "true", "yes"}

app = FastAPI(
    title="Climate Monitor Wiki Agent",
    description="Agentic RAG API over the Climate Monitor Obsidian wiki.",
    version="0.1.0",
    docs_url="/docs" if _ENABLE_DOCS else None,
    redoc_url="/redoc" if _ENABLE_DOCS else None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,
)

# CORS: the application is same-origin served. No cross-origin clients exist.
# Wildcard origins are intentionally NOT used. If a legitimate cross-origin
# client emerges, add its explicit origin rather than opening to all.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "X-Reload-Token"],
)
app.include_router(auth_router, prefix="/api/manage/auth", include_in_schema=False)
_LOGIN_LIMITER = Limiter(key_func=get_remote_address)
_LOGIN_RATE = parse_rate_limit("5/minute")
app.state.limiter = _LOGIN_LIMITER

responder = AgenticWikiResponder(WIKI_DIR, SOURCE_DIR)
RELOAD_TOKEN = os.getenv("RELOAD_TOKEN", "").strip()
management_service: ManagementService | None = None
ConsolePrincipal = Annotated[ConsoleUser, Depends(current_console_user)]
OptionalConsolePrincipal = Annotated[ConsoleUser | None, Depends(optional_console_user)]


def _management_service() -> ManagementService:
    """Initialize console-only state only after an authenticated console call."""
    global management_service
    if management_service is None:
        management_service = ManagementService.from_environment()
    return management_service

# --- Input validation constants ---
MAX_MESSAGE_LENGTH = 8000          # Maximum characters per user message
MAX_MESSAGES = 50                  # Maximum messages in history
MAX_REQUEST_BYTES = 200 * 1024     # Maximum request body size (200 KB)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    # `content` is required, as it was before the hardening pass. Only a
    # maximum length is added here; do not give this a default, which would
    # silently accept message objects with no content at all.
    content: str = Field(max_length=MAX_MESSAGE_LENGTH)


class ChatRequest(BaseModel):
    message: str | None = Field(default=None, max_length=MAX_MESSAGE_LENGTH)
    messages: list[ChatMessage] = Field(default_factory=list)
    context_path: str | None = Field(default=None, alias="contextPath")
    language: Literal["en"] = "en"
    answer_mode: Literal["brief", "detailed", "executive"] = Field(default="detailed", alias="answerMode")


@app.middleware("http")
async def limit_request_body(request: Request, call_next):
    """Reject oversized request bodies before they reach a route handler.

    Pydantic already bounds individual chat fields, but without this an
    unbounded body could still be buffered. Only a declared Content-Length is
    checked here; chunked/streaming uploads are not used by this API.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length."})
        if declared > MAX_REQUEST_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body too large. Maximum is {MAX_REQUEST_BYTES} bytes."},
            )
    return await call_next(request)


@app.middleware("http")
async def limit_console_login_attempts(request: Request, call_next):
    """Throttle FastAPI Users' password endpoint per client address."""
    if request.method == "POST" and request.url.path == "/api/manage/auth/login":
        client_key = get_remote_address(request)
        if not _LOGIN_LIMITER._limiter.hit(_LOGIN_RATE, client_key):
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many sign-in attempts. Try again later."},
                headers={"Retry-After": "60"},
            )
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Apply security headers to every response as defense-in-depth."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    return response


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots() -> str:
    return "User-agent: *\nDisallow: /\n"


def _public_config() -> dict:
    """Return a sanitized configuration for public consumption.

    Removes internal-only fields:
    - obsidian_plugin (contains an internal localhost server URL)
    - retrieval_corpora (internal corpus/infrastructure detail)

    `github_blob_base_url` is intentionally retained: the repository is public
    and the frontend's source links are built from it.
    """
    full = responder.config()
    # Remove fields that expose internal implementation details
    for key in ("obsidian_plugin", "retrieval_corpora"):
        full.pop(key, None)
    return full


@app.get("/api/config")
def config() -> dict:
    return _public_config()


@app.get("/api/update-status", response_model=None)
def update_status():
    configured = os.getenv("CLIMATE_UPDATE_STATUS_DIR", "").strip()
    if not configured:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "not_configured"},
        )
    try:
        return RunLedgerReader(configured, repository_root=ROOT).status()
    except LedgerLocationError:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "invalid_location"},
        )
    except LedgerContractError:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "invalid_ledger"},
        )
    except LedgerUnavailableError:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "ledger_unavailable"},
        )


@app.get("/api/job-status", response_model=None)
def job_status():
    configured = os.getenv("CLIMATE_JOB_STATUS_DIR", "").strip()
    if not configured:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "not_configured"},
        )
    try:
        return JobStatusSnapshotReader(configured, repository_root=ROOT).status()
    except JobStatusLocationError:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "invalid_location"},
        )
    except JobStatusUnavailableError:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "snapshot_unavailable"},
        )
    except JobStatusInvalidSnapshotError:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "invalid_snapshot"},
        )


def _registry_reader() -> RegistryReader:
    configured = os.getenv("CLIMATE_REGISTRY_DB", "").strip()
    if not configured:
        raise RegistryUnavailableError("registry is not configured")
    return RegistryReader(
        configured,
        repository_root=ROOT,
        source_dir=SOURCE_DIR,
        metadata_dir=ARTICLE_METADATA_DIR,
    )


def _registry_query(callable_):
    try:
        return callable_()
    except RegistryQueryError as exc:
        raise HTTPException(status_code=400, detail="Invalid registry query parameters.") from exc
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Registry record not found.") from exc
    except (RegistryUnavailableError, RegistryContractError) as exc:
        raise HTTPException(status_code=503, detail="Article registry is unavailable.") from exc


def _parse_registry_decimal(value: str) -> int:
    if not value or len(value) > 7 or not value.isascii() or not value.isdecimal():
        raise HTTPException(status_code=400, detail="Invalid registry query parameters.")
    return int(value)


@app.get("/api/registry/status", response_model=None)
def registry_status():
    configured = os.getenv("CLIMATE_REGISTRY_DB", "").strip()
    if not configured:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "not_configured"},
        )
    try:
        return RegistryReader(configured, repository_root=ROOT).status()
    except RegistryLocationError:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "invalid_location"},
        )
    except RegistryContractError:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "invalid_schema"},
        )
    except RegistryUnavailableError:
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "database_unavailable"},
        )


@app.get("/api/registry/reports")
def registry_reports(page: str = "1", page_size: str = "20") -> dict:
    parsed_page, parsed_size = _parse_registry_decimal(page), _parse_registry_decimal(page_size)
    return _registry_query(lambda: _registry_reader().reports(page=parsed_page, page_size=parsed_size))


@app.get("/api/registry/reports/{report_date}")
def registry_report(report_date: str) -> dict:
    def read_report() -> dict:
        report, identity = _registry_reader().report_with_identity(report_date)
        artifact = load_report_artifact(
            os.getenv("CLIMATE_DELIVERY_OUTPUT_DIR", "").strip(),
            report_date=identity.report_date,
            report_filename=identity.filename,
            report_title=identity.report_title,
            report_sha256=identity.report_sha256,
            include_pdf_bytes=False,
            # Read-tolerant: persisted artifacts of an explicitly accepted
            # off-cycle report remain loadable via the API.
            allow_offcycle=True,
        )
        report["report_briefing"] = artifact.briefing if artifact else None
        report["report_pdf"] = (
            {
                "filename": artifact.pdf_filename,
                "download_url": f"/api/registry/reports/{identity.report_date}/pdf",
            }
            if artifact
            else None
        )
        return report

    return _registry_query(read_report)


@app.get("/api/registry/reports/{report_date}/pdf", response_class=Response)
def registry_report_pdf(report_date: str) -> Response:
    def read_pdf() -> Response:
        identity = _registry_reader().report_identity(report_date)
        artifact = load_report_artifact(
            os.getenv("CLIMATE_DELIVERY_OUTPUT_DIR", "").strip(),
            report_date=identity.report_date,
            report_filename=identity.filename,
            report_title=identity.report_title,
            report_sha256=identity.report_sha256,
            include_pdf_bytes=True,
            # Read-tolerant: persisted artifacts of an explicitly accepted
            # off-cycle report remain loadable via the API.
            allow_offcycle=True,
        )
        if artifact is None:
            raise HTTPException(status_code=404, detail="Report PDF not found.")
        return Response(
            content=artifact.pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.pdf_filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    return _registry_query(read_pdf)


@app.get("/api/registry/publishers")
def registry_publishers() -> dict:
    return _registry_query(lambda: _registry_reader().publishers())


@app.get("/api/registry/articles")
def registry_articles(
    page: str = "1",
    page_size: str = "20",
    query: str = "",
    source: str = "",
    pillar: str = "",
    report_date: str = "",
) -> dict:
    parsed_page, parsed_size = _parse_registry_decimal(page), _parse_registry_decimal(page_size)
    return _registry_query(
        lambda: _registry_reader().articles(
            page=parsed_page,
            page_size=parsed_size,
            query=query,
            source=source,
            pillar=pillar,
            report_date=report_date,
        )
    )


@app.get("/api/registry/articles/{article_id}")
def registry_article(article_id: str) -> dict:
    return _registry_query(lambda: _registry_reader().article(article_id))


@app.post("/api/reload")
def reload_wiki(request: Request, x_reload_token: str | None = Header(default=None)) -> dict:
    client_host = (request.client.host if request.client else "").strip()
    is_local_client = client_host in {"127.0.0.1", "::1", "localhost"}

    if RELOAD_TOKEN:
        if not x_reload_token or not secrets.compare_digest(x_reload_token, RELOAD_TOKEN):
            raise HTTPException(status_code=403, detail="Invalid reload token.")
    elif not is_local_client:
        raise HTTPException(
            status_code=403,
            detail="Reload is restricted to localhost unless RELOAD_TOKEN is configured.",
        )

    responder.kb.reload()
    return _public_config()


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
    # Enforce message count limit
    if len(request.messages) > MAX_MESSAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many messages. Maximum is {MAX_MESSAGES}.",
        )

    messages = [item.model_dump() for item in request.messages]
    question = (request.message or "").strip()
    if not question:
        for item in reversed(messages):
            if item.get("role") == "user" and item.get("content", "").strip():
                question = item["content"].strip()
                break
    if not question:
        raise HTTPException(status_code=400, detail="A user message is required.")

    history = [item for item in messages if item.get("role") in {"user", "assistant"}]
    if history and history[-1].get("role") == "user" and history[-1].get("content") == question:
        history = history[:-1]

    try:
        return responder.answer(
            question,
            history=history,
            context_path=request.context_path,
            language=request.language,
            answer_mode=request.answer_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _manage_call(callback):
    try:
        return callback()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/manage/login", response_class=HTMLResponse, include_in_schema=False)
def console_login_page() -> FileResponse:
    return FileResponse(MANAGE_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/manage", response_class=HTMLResponse, include_in_schema=False)
def console_page(user: OptionalConsolePrincipal):
    if user is None:
        return RedirectResponse("/manage/login", status_code=303)
    return FileResponse(MANAGE_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/manage/session", include_in_schema=False)
def console_session(user: OptionalConsolePrincipal) -> dict[str, bool]:
    """Expose only whether the shared operator session is active."""
    return {"authenticated": user is not None}


@app.api_route(
    "/hermes/",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
@app.api_route(
    "/hermes",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def hermes_root(request: Request, user: OptionalConsolePrincipal) -> Response:
    if user is None:
        if request.method == "GET":
            return RedirectResponse("/manage/login?next=/hermes", status_code=303)
        raise HTTPException(status_code=401, detail="Unauthorized")
    return await proxy_http(request, "")


@app.api_route(
    "/hermes/api/mcp/oauth/callback/{server_name}",
    methods=["GET"],
    include_in_schema=False,
)
async def hermes_mcp_oauth_callback(request: Request, server_name: str) -> Response:
    """Forward Hermes' state-protected provider callback without a SameSite cookie."""
    path = oauth_callback_proxy_path(server_name, request.scope.get("raw_path"))
    return await proxy_http(request, path, expected_upstream_path=f"/{path}")


@app.api_route(
    "/hermes/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def hermes_http(request: Request, path: str, user: ConsolePrincipal) -> Response:
    return await proxy_http(request, path)


@app.websocket("/hermes/{path:path}")
async def hermes_websocket(websocket: WebSocket, path: str) -> None:
    await proxy_websocket(websocket, path)


@app.get("/manage/assets/{filename}", include_in_schema=False)
def console_asset(filename: str, user: ConsolePrincipal) -> FileResponse:
    if filename not in {"manage.css", "manage.js"}:
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(MANAGE_DIR / filename, headers={"Cache-Control": "no-store"})


@app.get("/api/manage/config", include_in_schema=False)
def console_config(user: ConsolePrincipal) -> dict[str, Any]:
    return _manage_call(_management_service().store.load)


@app.get("/api/manage/versions", include_in_schema=False)
def console_versions(user: ConsolePrincipal) -> list[dict[str, Any]]:
    return _manage_call(_management_service().store.versions)


@app.get("/api/manage/diff", include_in_schema=False)
def console_diff(user: ConsolePrincipal, old_version: int, new_version: int) -> dict[str, Any]:
    return _manage_call(lambda: _management_service().store.diff(old_version, new_version))


@app.post("/api/manage/config/preview", include_in_schema=False)
def console_preview(definition: dict[str, Any], user: ConsolePrincipal) -> dict[str, Any]:
    return _manage_call(lambda: _management_service().store.preview(definition))


@app.put("/api/manage/config", include_in_schema=False)
def console_save(definition: dict[str, Any], user: ConsolePrincipal, expected_version: int) -> dict[str, Any]:
    return _manage_call(lambda: _management_service().store.save(definition, expected_version=expected_version, actor=user.email))


@app.post("/api/manage/versions/{version}/restore", include_in_schema=False)
def console_restore(version: int, user: ConsolePrincipal, expected_version: int) -> dict[str, Any]:
    return _manage_call(lambda: _management_service().store.restore(version, expected_version=expected_version, actor=user.email))


@app.get("/api/manage/progress", include_in_schema=False)
def console_all_progress(user: ConsolePrincipal) -> list[dict[str, Any]]:
    return _manage_call(_management_service().list_runs)


@app.post("/api/manage/runs", include_in_schema=False)
def console_start(payload: dict[str, Any], user: ConsolePrincipal) -> dict[str, Any]:
    if payload:
        raise HTTPException(status_code=422, detail="Manual start accepts no overrides; save a version first.")
    return _manage_call(lambda: _management_service().start(trigger="manual"))


@app.post("/api/manage/runs/{run_id}/resume", include_in_schema=False)
def console_resume(run_id: str, user: ConsolePrincipal) -> dict[str, Any]:
    return _manage_call(lambda: _management_service().resume(run_id))


@app.get("/api/manage/runs/{run_id}/progress", include_in_schema=False)
def console_run_progress(run_id: str, user: ConsolePrincipal) -> dict[str, Any]:
    return _manage_call(lambda: _management_service().progress(run_id))


@app.get("/api/manage/runs/{run_id}/items/{item_id:path}", include_in_schema=False)
def console_item_detail(run_id: str, item_id: str, user: ConsolePrincipal) -> dict[str, Any]:
    return _manage_call(lambda: _management_service().item_detail(run_id, item_id))


app.mount("/wiki", StaticFiles(directory=WIKI_DIR), name="wiki")
app.mount("/sources", StaticFiles(directory=SOURCE_DIR), name="sources")
app.mount("/showcase", StaticFiles(directory=SHOWCASE_DIR), name="showcase")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(SHOWCASE_DIR / "index.html")
