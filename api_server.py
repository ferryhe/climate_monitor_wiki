from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile, WebSocket
from limits import parse as parse_rate_limit
from slowapi import Limiter
from slowapi.util import get_remote_address
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


from agentic_wiki import AgenticWikiResponder, WikiKnowledgeBase
from climate_delivery.artifacts import load_report_artifact
from climate_delivery.io import atomic_write_json
from climate_delivery.errors import GenerationError, LockStateError
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
from climate_registry.pdf_pipeline import (
    enqueue_pdf_batch,
    load_active_projection,
    read_pdf_batch,
    retry_pdf_batch,
)
from climate_registry.range_reports import (
    RENDERER_VERSION,
    RangeReportError,
    ensure_range_report_pdf,
    freeze_range_report,
    is_report_clarification,
    load_range_report,
    render_range_report_html,
    resolve_report_followup,
    resolve_report_route,
)
from climate_monitor.pdf_intake import MAX_PDF_BYTES, import_pdf_reports


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
SHOWCASE_DIR = ROOT / "showcase"
MANAGE_DIR = ROOT / "management_ui"
WIKI_DIR = ROOT / os.getenv("WIKI_DIR", "wiki")
SOURCE_DIR = ROOT / os.getenv("SOURCE_DIR", "sources")
PDF_RUNTIME_WIKI_DIR = (
    Path(value).resolve()
    if (value := os.getenv("CLIMATE_PDF_RUNTIME_WIKI_DIR", "").strip())
    else None
)
ARTICLE_METADATA_DIR = ROOT / os.getenv("ARTICLE_METADATA_DIR", "article_metadata")
RANGE_REPORT_DIR = Path(
    os.getenv("CLIMATE_RANGE_REPORT_DIR", str(ROOT / "output" / "range-reports"))
)

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
_RELOAD_LOCK = threading.Lock()


def _configured_pdf_queue() -> Path | None:
    value = os.getenv("CLIMATE_PDF_INTAKE_QUEUE_DIR", "").strip()
    return Path(value).resolve() if value else None

def _selected_pdf_projection() -> tuple[Path | None, dict[str, Any] | None]:
    if PDF_RUNTIME_WIKI_DIR is None:
        return None, None
    queue = _configured_pdf_queue()
    if queue is None:
        raise RuntimeError("PDF runtime Wiki requires the durable batch queue")
    return load_active_projection(PDF_RUNTIME_WIKI_DIR, queue / "active.json")


_startup_projection, _ = _selected_pdf_projection()
responder = AgenticWikiResponder(WIKI_DIR, SOURCE_DIR, _startup_projection)
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
MAX_PDF_UPLOAD_BYTES = MAX_PDF_BYTES * 5


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
    max_bytes = (
        MAX_PDF_UPLOAD_BYTES if request.url.path.startswith("/api/manage/pdf-intake/")
        else MAX_REQUEST_BYTES
    )
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length."})
        if declared > max_bytes:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body too large. Maximum is {max_bytes} bytes."},
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


@app.get("/api/registry/pdf-intake/articles")
def registry_pdf_articles(
    page: str = "1", page_size: str = "20", query: str = "",
    source: str = "", report_date: str = "",
) -> dict:
    parsed_page, parsed_size = _parse_registry_decimal(page), _parse_registry_decimal(page_size)
    return _registry_query(
        lambda: _registry_reader().pdf_articles(
            page=parsed_page, page_size=parsed_size, query=query, source=source,
            report_date=report_date,
        )
    )


@app.get("/api/registry/pdf-intake/articles/{article_id}")
def registry_pdf_article(article_id: str) -> dict:
    return _registry_query(lambda: _registry_reader().pdf_article(article_id))


@app.get("/api/registry/pdf-intake/calendar")
def registry_pdf_calendar(
    page: str = "1", page_size: str = "20", query: str = "", kind: str = "",
) -> dict:
    parsed_page, parsed_size = _parse_registry_decimal(page), _parse_registry_decimal(page_size)
    return _registry_query(
        lambda: _registry_reader().pdf_calendar_items(
            page=parsed_page, page_size=parsed_size, query=query, kind=kind,
        )
    )


@app.get("/api/registry/articles/{article_id}")
def registry_article(article_id: str) -> dict:
    return _registry_query(lambda: _registry_reader().article(article_id))


@app.post("/api/reload")
def reload_wiki(
    request: Request,
    x_reload_token: str | None = Header(default=None),
    generation_id: str | None = Query(default=None),
) -> dict:
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

    with _RELOAD_LOCK:
        queue = _configured_pdf_queue()
        pending = queue / "pending.json" if queue is not None else None
        if generation_id is not None:
            if PDF_RUNTIME_WIKI_DIR is None or pending is None:
                raise HTTPException(status_code=503, detail="PDF activation is not configured.")
            try:
                metadata = json.loads(pending.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=409, detail="PDF activation is not pending.") from exc
            if metadata.get("generation_id") != generation_id:
                raise HTTPException(status_code=409, detail="PDF activation generation does not match.")
            projection, metadata = load_active_projection(PDF_RUNTIME_WIKI_DIR, pending)
        else:
            projection, metadata = _selected_pdf_projection()

        if PDF_RUNTIME_WIKI_DIR is None:
            responder.kb.reload()
        else:
            replacement = WikiKnowledgeBase(WIKI_DIR, SOURCE_DIR, projection)
            if generation_id is not None:
                try:
                    current_pending = json.loads(pending.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise HTTPException(status_code=409, detail="PDF activation was withdrawn.") from exc
                if current_pending != metadata:
                    raise HTTPException(status_code=409, detail="PDF activation was replaced.")
            elif _selected_pdf_projection()[1] != metadata:
                raise HTTPException(status_code=409, detail="PDF active generation changed during reload.")
            previous_kb = responder.kb
            previous_directories = list(_wiki_static_files.all_directories)
            responder.kb = replacement
            _wiki_static_files.all_directories = (
                [str(WIKI_DIR), str(projection)] if projection is not None else [str(WIKI_DIR)]
            )
            if generation_id is not None:
                try:
                    atomic_write_json(queue / "active.json", metadata)
                except Exception:
                    try:
                        _, committed = load_active_projection(
                            PDF_RUNTIME_WIKI_DIR, queue / "active.json"
                        )
                    except RuntimeError:
                        committed = None
                    if committed != metadata:
                        responder.kb = previous_kb
                        _wiki_static_files.all_directories = previous_directories
                    raise
                pending.unlink(missing_ok=True)
        payload = _public_config()
        payload["pdf_projection"] = metadata
        return payload


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

    pending_question = None
    if (
        len(history) >= 2
        and history[-2].get("role") == "user"
        and history[-1].get("role") == "assistant"
        and is_report_clarification(history[-1].get("content", ""))
    ):
        pending_index = len(history) - 2
        pending_question = history[pending_index].get("content", "").strip()
        while (
            pending_index >= 2
            and history[pending_index - 2].get("role") == "user"
            and history[pending_index - 1].get("role") == "assistant"
            and is_report_clarification(history[pending_index - 1].get("content", ""))
        ):
            pending_index -= 2
            pending_question = history[pending_index].get("content", "").strip()
    report_route = (
        resolve_report_followup(question, pending_question)
        if pending_question
        else resolve_report_route(question)
    )
    if report_route.action == "clarify":
        return {
            "text": report_route.clarification,
            "sources": [],
            "needs_clarification": True,
            "model": "registry-snapshot",
            "agent_mode": "offline",
            "language": request.language,
            "answer_mode": request.answer_mode,
        }
    if report_route.action == "generate":
        try:
            snapshot = freeze_range_report(
                _registry_reader(),
                RANGE_REPORT_DIR,
                start_date=report_route.start_date,
                end_date=report_route.end_date,
                meeting_snapshot_id=report_route.meeting_snapshot_id,
            )
        except (RegistryUnavailableError, RegistryContractError) as exc:
            raise HTTPException(status_code=503, detail="Article registry is unavailable.") from exc
        except LockStateError as exc:
            raise HTTPException(
                status_code=503,
                detail="The report is being created. Please retry shortly.",
            ) from exc
        except (GenerationError, OSError, RangeReportError) as exc:
            raise HTTPException(
                status_code=503,
                detail="The report could not be saved. Please retry.",
            ) from exc
        snapshot_id = snapshot["snapshot_id"]
        web_url = f"/api/registry/range-reports/{snapshot_id}/{RENDERER_VERSION}"
        pdf_url = web_url + "/pdf"
        text = (
            f"Your report for {report_route.start_date} through {report_route.end_date} "
            f"is ready: [open the web report]({web_url}) or "
            f"[download the PDF]({pdf_url})."
        )
        return {
            "text": text,
            "sources": [],
            "model": "registry-snapshot",
            "agent_mode": "offline",
            "language": request.language,
            "answer_mode": request.answer_mode,
            "range_report": {
                "snapshot_id": snapshot_id,
                "snapshot_sha256": snapshot["snapshot_sha256"],
                "renderer_version": RENDERER_VERSION,
                "date_range": snapshot["date_range"],
                "article_count": len(snapshot["articles"]),
                "unknown_publication_date_count": snapshot["unknown_publication_date_count"],
                "meeting_status": snapshot["meeting"]["status"],
                "web_url": web_url,
                "pdf_url": pdf_url,
            },
        }

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


def _load_range_report_or_http(snapshot_id: str, renderer_version: str) -> dict[str, Any]:
    if renderer_version != RENDERER_VERSION:
        raise HTTPException(status_code=404, detail="Report renderer not found.")
    try:
        return load_range_report(RANGE_REPORT_DIR, snapshot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Range report not found.") from exc
    except RangeReportError as exc:
        raise HTTPException(status_code=503, detail="Stored range report is invalid.") from exc


@app.get(
    "/api/registry/range-reports/{snapshot_id}/{renderer_version}",
    response_class=HTMLResponse,
)
def registry_range_report(snapshot_id: str, renderer_version: str) -> HTMLResponse:
    snapshot = _load_range_report_or_http(snapshot_id, renderer_version)
    return HTMLResponse(render_range_report_html(snapshot))


@app.get(
    "/api/registry/range-reports/{snapshot_id}/{renderer_version}/pdf",
    response_class=Response,
)
def registry_range_report_pdf(snapshot_id: str, renderer_version: str) -> Response:
    snapshot = _load_range_report_or_http(snapshot_id, renderer_version)
    try:
        path = ensure_range_report_pdf(snapshot, RANGE_REPORT_DIR)
        pdf_bytes = path.read_bytes()
    except (GenerationError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Range report PDF is unavailable.") from exc
    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(status_code=503, detail="Range report PDF is invalid.")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{path.name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


def _manage_call(callback):
    try:
        return callback()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Management record not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _pdf_intake_queue() -> tuple[Path, None] | tuple[None, str]:
    queue = _configured_pdf_queue()
    if queue is None:
        return None, "PDF batch processing is not configured."
    if queue == ROOT or ROOT in queue.parents:
        return None, "The PDF batch queue must be outside the application repository."
    if not queue.is_dir() or not os.access(queue, os.W_OK | os.X_OK):
        return None, "The PDF batch queue is unavailable for writing."
    return queue, None


def _pdf_intake_preview(bundle: dict[str, Any], writable: bool, error: str | None = None) -> dict[str, Any]:
    filenames = {
        item["source"]["sha256"]: item["source"]["filename"]
        for item in bundle["documents"]
    }
    return {
        "writable": writable,
        "error": error,
        "preview_digest": _pdf_intake_bundle_digest(bundle),
        "documents": [
            {"sha256": item["source"]["sha256"], "filename": item["source"]["filename"],
             "page_count": item["page_count"], "summary": item.get("executive_summary"),
             "source_observations": item["source"]["source_observations"]}
            for item in bundle["documents"]
        ],
        "article_occurrences": sum(len(item["occurrences"]) for item in bundle["articles"]),
        "calendar_items": len(bundle["calendar_items"]),
        "articles": [
            {"url": occurrence["raw_url"], "title": article.get("title") or occurrence.get("anchor_text"),
             "page": occurrence["page"], "report_summary": occurrence["summary"],
             "source_document": occurrence["source_document"]}
            for article in bundle["articles"] for occurrence in article["occurrences"]
        ],
        "calendar": [
            {"name": item.get("name"), "date": item.get("start_date") or item.get("raw_date"),
             "page": item["page"], "summary": item["summary"],
             "source_document": filenames[item["source_document_sha256"]]}
            for item in bundle["calendar_items"]
        ],
        "typesafe": bundle["typesafe"],
    }


def _pdf_intake_bundle_digest(bundle: dict[str, Any]) -> str:
    """Fingerprint the parsed values that an import can persist."""
    normalized = dict(bundle)
    normalized.pop("generated_at", None)
    normalized["documents"] = [
        document | {"source": {
            key: value for key, value in document["source"].items()
            if key != "original_pdf_base64"
        }}
        for document in bundle.get("documents", [])
    ]
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_one_pdf(files: list[UploadFile]) -> None:
    if len(files) != 1:
        raise HTTPException(status_code=422, detail="Choose exactly one PDF file.")


async def _uploaded_pdf_bundle(files: list[UploadFile]) -> dict[str, Any]:
    _require_one_pdf(files)
    with tempfile.TemporaryDirectory(prefix="climate-pdf-intake-") as directory:
        temporary = Path(directory)
        uploads: dict[str, str] = {}
        for index, upload in enumerate(files):
            filename = Path((upload.filename or "").replace("\\", "/")).name or "upload.pdf"
            path = temporary / f"{index}.pdf"
            path.write_bytes(await upload.read())
            uploads[str(path.resolve())] = filename
        try:
            bundle = import_pdf_reports(temporary / f"{index}.pdf" for index in range(len(files)))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"PDF batch was not imported: {exc}") from exc

    for document in bundle["documents"]:
        source = document["source"]
        observations = [
            {"path": f"manage-upload://{source['sha256']}/{quote(uploads[item['path']], safe='')}",
             "filename": uploads[item["path"]]}
            for item in source["source_observations"]
        ]
        source["path"] = observations[0]["path"]
        source["filename"] = observations[0]["filename"]
        source["source_observations"] = observations
    filenames = {document["source"]["sha256"]: document["source"]["filename"] for document in bundle["documents"]}
    for article in bundle["articles"]:
        for occurrence in article["occurrences"]:
            occurrence["source_document"] = filenames[occurrence["source_document_sha256"]]
    return bundle


@app.get("/manage/login", response_class=HTMLResponse, include_in_schema=False)
def console_login_page() -> FileResponse:
    return FileResponse(MANAGE_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/manage", response_class=HTMLResponse, include_in_schema=False)
def console_page(user: OptionalConsolePrincipal):
    if user is None:
        return RedirectResponse("/manage/login", status_code=303)
    return FileResponse(MANAGE_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/manage/pdf-import", response_class=HTMLResponse, include_in_schema=False)
def console_pdf_import_page(user: OptionalConsolePrincipal):
    if user is None:
        return RedirectResponse("/manage/login?next=/manage/pdf-import", status_code=303)
    return FileResponse(MANAGE_DIR / "pdf_import.html", headers={"Cache-Control": "no-store"})


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
    if filename not in {"manage.css", "manage.js", "pdf_import.js"}:
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(MANAGE_DIR / filename, headers={"Cache-Control": "no-store"})


@app.post("/api/manage/pdf-intake/preview", include_in_schema=False)
async def console_pdf_intake_preview(user: ConsolePrincipal, files: list[UploadFile] = File()) -> dict[str, Any]:
    bundle = await _uploaded_pdf_bundle(files)
    queue = _pdf_intake_queue()
    if queue[0] is not None:
        return _pdf_intake_preview(bundle, True)
    return _pdf_intake_preview(bundle, False, queue[1])


@app.post("/api/manage/pdf-intake/import", include_in_schema=False)
async def console_pdf_intake_import(
    user: ConsolePrincipal, files: list[UploadFile] = File(), confirmed: bool = False,
    preview_sha: list[str] = Query(default=[]), preview_digest: str = "",
) -> dict[str, Any]:
    _require_one_pdf(files)
    if not confirmed:
        raise HTTPException(status_code=422, detail="Confirm the PDF import before writing.")
    queue = _pdf_intake_queue()
    if queue[0] is None:
        raise HTTPException(status_code=503, detail=queue[1])
    bundle = await _uploaded_pdf_bundle(files)
    if preview_sha != [document["source"]["sha256"] for document in bundle["documents"]]:
        raise HTTPException(status_code=422, detail="Uploaded PDFs do not match the preview.")
    if preview_digest != _pdf_intake_bundle_digest(bundle):
        raise HTTPException(status_code=422, detail="Parsed PDF details do not match the preview.")
    try:
        return enqueue_pdf_batch(queue[0], bundle, repository_root=ROOT)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"PDF batch was not queued: {exc}") from exc


@app.get("/api/manage/pdf-intake/batches/{batch_id}", include_in_schema=False)
def console_pdf_intake_batch(batch_id: str, user: ConsolePrincipal) -> dict[str, Any]:
    queue = _pdf_intake_queue()
    if queue[0] is None:
        raise HTTPException(status_code=503, detail=queue[1])
    try:
        return read_pdf_batch(queue[0], batch_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PDF batch was not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid PDF batch status.") from exc


@app.post("/api/manage/pdf-intake/batches/{batch_id}/retry", include_in_schema=False)
def console_retry_pdf_intake_batch(batch_id: str, user: ConsolePrincipal) -> dict[str, Any]:
    queue = _pdf_intake_queue()
    if queue[0] is None:
        raise HTTPException(status_code=503, detail=queue[1])
    try:
        return retry_pdf_batch(queue[0], batch_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PDF batch was not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid PDF batch status.") from exc


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


@app.post("/api/manage/runs/{run_id}/meetings", include_in_schema=False)
def console_start_meetings(run_id: str, payload: dict[str, Any], user: ConsolePrincipal) -> dict[str, Any]:
    if set(payload) - {"retry_failed"} or type(payload.get("retry_failed", False)) is not bool:
        raise HTTPException(status_code=422, detail="meeting start accepts only retry_failed boolean")
    return _manage_call(lambda: _management_service().start_meetings(
        run_id, retry_failed=payload.get("retry_failed", False),
    ))


@app.get("/api/manage/runs/{run_id}/meetings", include_in_schema=False)
def console_meeting_progress(run_id: str, user: ConsolePrincipal) -> dict[str, Any]:
    return _manage_call(lambda: _management_service().meeting_progress(run_id))


def _meeting_filters(
    *, organizer: str | None, event_types: str | None, start_date: str | None,
    end_date: str | None, include_unknown: bool, include_deadlines: bool,
    include_cancelled: bool, include_retrospective: bool, base_date: str | None,
    timezone_name: str,
) -> dict[str, Any]:
    return {
        "organizer": organizer,
        "event_types": [value.strip() for value in event_types.split(",") if value.strip()]
        if event_types else None,
        "start_date": start_date, "end_date": end_date,
        "include_unknown": include_unknown, "include_deadlines": include_deadlines,
        "include_cancelled": include_cancelled,
        "include_retrospective": include_retrospective,
        "base_date": base_date, "timezone_name": timezone_name,
    }


@app.get("/api/manage/meetings", include_in_schema=False)
def console_meetings(
    user: ConsolePrincipal, organizer: str | None = None, event_types: str | None = None,
    start_date: str | None = None, end_date: str | None = None,
    include_unknown: bool = False, include_deadlines: bool = False,
    include_cancelled: bool = False, include_retrospective: bool = False,
    base_date: str | None = None, timezone_name: str = "America/New_York",
) -> dict[str, Any]:
    filters = _meeting_filters(
        organizer=organizer, event_types=event_types, start_date=start_date, end_date=end_date,
        include_unknown=include_unknown, include_deadlines=include_deadlines,
        include_cancelled=include_cancelled, include_retrospective=include_retrospective,
        base_date=base_date, timezone_name=timezone_name,
    )
    return _manage_call(lambda: _management_service().meeting_events(**filters))


@app.post("/api/manage/meeting-snapshots", include_in_schema=False)
def console_freeze_meeting_snapshot(payload: dict[str, Any], user: ConsolePrincipal) -> dict[str, Any]:
    allowed = {
        "organizer", "event_types", "start_date", "end_date", "include_unknown",
        "include_deadlines", "include_cancelled", "include_retrospective", "base_date",
        "timezone_name",
    }
    if set(payload) - allowed:
        raise HTTPException(status_code=422, detail="unexpected meeting snapshot filter")
    return _manage_call(lambda: _management_service().freeze_meeting_snapshot(**payload))


@app.get("/api/manage/meeting-snapshots/{snapshot_id}", include_in_schema=False)
def console_meeting_snapshot(snapshot_id: str, user: ConsolePrincipal) -> dict[str, Any]:
    from climate_monitor.meetings import load_snapshot

    database = _management_service().store.load()["definition"]["runtime"]["registry_database"]
    return _manage_call(lambda: load_snapshot(database, snapshot_id))


_wiki_static_files = StaticFiles(directory=WIKI_DIR)
if _startup_projection is not None:
    _wiki_static_files.all_directories = [str(WIKI_DIR), str(_startup_projection)]
app.mount("/wiki", _wiki_static_files, name="wiki")
app.mount("/sources", StaticFiles(directory=SOURCE_DIR), name="sources")
app.mount("/showcase", StaticFiles(directory=SHOWCASE_DIR), name="showcase")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(SHOWCASE_DIR / "index.html")
