"""FastAPI app — the HTTP loop around the LangGraph DM.

M1 endpoint contract (this is what the frontend and playtester depend on):

    GET  /health                          -> {status, llm_mode}
    POST /api/session                     -> mint a session, return opening narration
    GET  /api/session/{sid}               -> rehydrate a session's message history
    POST /api/session/{sid}/turn          -> {message} -> DM reply

The `session_id` is a UUID minted here and IS the resume code: the client holds
it (localStorage), re-submits it to resume. It maps 1:1 to the LangGraph
`thread_id`, so the checkpointer restores mid-conversation after a reconnect.
"""

from __future__ import annotations

import logging
import threading
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.graph import graph
from app.llm import message_text

logger = logging.getLogger("cyodc.api")

settings = get_settings()

app = FastAPI(title="CYODC", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- wire models ------------------------------------------------------------
class ChatMessage(BaseModel):
    role: str  # "player" | "dm"
    content: str


class SessionResponse(BaseModel):
    session_id: str
    messages: list[ChatMessage]
    llm_mode: str


class TurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("message must not be blank")
        return trimmed


class TurnResponse(BaseModel):
    session_id: str
    reply: str
    llm_mode: str


# --- helpers ----------------------------------------------------------------
# Per-session locks serialize turns on the SAME session. Two concurrent
# invocations on one thread_id would otherwise read the same parent checkpoint
# and write diverging children — the checkpointer keeps only one branch, so a
# turn is acknowledged to the client but silently dropped from history. The
# lock makes read-modify-write atomic per session; different sessions still run
# fully in parallel. (Endpoints are sync `def`, so Starlette runs them in a
# threadpool — a threading.Lock is the correct primitive.)
_session_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
        return lock


def _thread_config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


def _serialize(messages: list) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            role = "player"
        elif isinstance(message, AIMessage):
            role = "dm"
        else:
            continue  # never surface system/tool plumbing to the client
        out.append(ChatMessage(role=role, content=message_text(message)))
    return out


def _load_messages(session_id: str) -> list:
    snapshot = graph.get_state(_thread_config(session_id))
    return snapshot.values.get("messages", []) if snapshot.values else []


# --- endpoints --------------------------------------------------------------
@app.get("/")
def root() -> dict:
    return {"app": "CYODC", "version": "0.1.0", "docs": "/docs"}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "llm_mode": settings.resolved_llm_mode}


@app.post("/api/session", response_model=SessionResponse)
def create_session() -> SessionResponse:
    """Start a new run. Invoking with an empty window makes the DM cold-open."""
    session_id = uuid4().hex
    config = _thread_config(session_id)
    try:
        result = graph.invoke({"messages": []}, config)
    except Exception:  # pragma: no cover - surfaced as a clean 502
        logger.exception("graph invoke failed on session create")
        raise HTTPException(status_code=502, detail="The Delve is buffering. Try again.")
    return SessionResponse(
        session_id=session_id,
        messages=_serialize(result["messages"]),
        llm_mode=settings.resolved_llm_mode,
    )


@app.get("/api/session/{session_id}", response_model=SessionResponse)
def get_session(session_id: str) -> SessionResponse:
    """Rehydrate an existing session (resume after closing the tab)."""
    messages = _load_messages(session_id)
    if not messages:
        raise HTTPException(status_code=404, detail="No such session.")
    return SessionResponse(
        session_id=session_id,
        messages=_serialize(messages),
        llm_mode=settings.resolved_llm_mode,
    )


@app.post("/api/session/{session_id}/turn", response_model=TurnResponse)
def take_turn(session_id: str, body: TurnRequest) -> TurnResponse:
    """Advance one turn. Requires an existing session.

    The existence check and graph invocation run under the session lock so a
    turn's read-modify-write of the checkpoint can't interleave with another
    turn on the same session (see BUG-1 / _session_lock).
    """
    config = _thread_config(session_id)
    with _session_lock(session_id):
        if not _load_messages(session_id):
            raise HTTPException(status_code=404, detail="No such session. Start a new one.")
        try:
            result = graph.invoke({"messages": [HumanMessage(content=body.message)]}, config)
        except Exception:  # pragma: no cover - surfaced as a clean 502
            logger.exception("graph invoke failed on turn")
            raise HTTPException(status_code=502, detail="The Delve is buffering. Try again.")

    return TurnResponse(
        session_id=session_id,
        reply=message_text(result["messages"][-1]),
        llm_mode=settings.resolved_llm_mode,
    )
