from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agentic_wiki import AgenticWikiResponder


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


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    message: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    context_path: str | None = Field(default=None, alias="contextPath")
    language: Literal["en"] = "en"
    answer_mode: Literal["brief", "detailed"] = Field(default="detailed", alias="answerMode")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def config() -> dict:
    return responder.config()


@app.post("/api/reload")
def reload_wiki() -> dict:
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
