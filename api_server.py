from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agentic_wiki import AgenticWikiResponder
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
WIKI_DIR = ROOT / os.getenv("WIKI_DIR", "wiki")
SOURCE_DIR = ROOT / os.getenv("SOURCE_DIR", "sources")

app = FastAPI(
    title="Climate Monitor Wiki Agent",
    description="Agentic RAG API over the Climate Monitor Obsidian wiki.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

responder = AgenticWikiResponder(WIKI_DIR, SOURCE_DIR)
RELOAD_TOKEN = os.getenv("RELOAD_TOKEN", "").strip()


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    message: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    context_path: str | None = Field(default=None, alias="contextPath")
    language: Literal["en"] = "en"
    answer_mode: Literal["brief", "detailed", "executive"] = Field(default="detailed", alias="answerMode")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots() -> str:
    return "User-agent: *\nDisallow: /\n"


@app.get("/api/config")
def config() -> dict:
    return responder.config()


def _registry_reader() -> RegistryReader:
    configured = os.getenv("CLIMATE_REGISTRY_DB", "").strip()
    if not configured:
        raise RegistryUnavailableError("registry is not configured")
    return RegistryReader(configured, repository_root=ROOT)


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


@app.get("/api/registry/status")
def registry_status() -> dict:
    configured = os.getenv("CLIMATE_REGISTRY_DB", "").strip()
    if not configured:
        return {"available": False, "reason": "not_configured"}
    try:
        return RegistryReader(configured, repository_root=ROOT).status()
    except RegistryLocationError:
        return {"available": False, "reason": "invalid_location"}
    except RegistryContractError:
        return {"available": False, "reason": "invalid_schema"}
    except RegistryUnavailableError:
        return {"available": False, "reason": "database_unavailable"}


@app.get("/api/registry/reports")
def registry_reports(page: str = "1", page_size: str = "20") -> dict:
    parsed_page, parsed_size = _parse_registry_decimal(page), _parse_registry_decimal(page_size)
    return _registry_query(lambda: _registry_reader().reports(page=parsed_page, page_size=parsed_size))


@app.get("/api/registry/reports/{report_date}")
def registry_report(report_date: str) -> dict:
    return _registry_query(lambda: _registry_reader().report(report_date))


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
    return responder.config()


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
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


app.mount("/wiki", StaticFiles(directory=WIKI_DIR), name="wiki")
app.mount("/sources", StaticFiles(directory=SOURCE_DIR), name="sources")
app.mount("/showcase", StaticFiles(directory=SHOWCASE_DIR), name="showcase")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(SHOWCASE_DIR / "index.html")
