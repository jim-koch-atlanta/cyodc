"""FastAPI app — the HTTP loop around the LangGraph DM.

M2 endpoint contract:

    GET  /health                          -> {status, llm_mode}
    POST /api/session                     -> provision a player+level, cold-open
    GET  /api/session/{sid}               -> rehydrate a session's message history
    POST /api/session/{sid}/turn          -> {message} -> DM reply (may call tools)
    GET  /api/session/{sid}/state         -> world state: stats, room, fog-map, inventory

`session_id` (a UUID) is the resume code and equals both the LangGraph thread_id
and `players.session_id` — the single link between a session and its world state.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.config import get_settings
from app.db.base import get_db_session, init_db
from app.db.models import InventoryRow, Item, Player, Visibility
from app.dungeon import (
    current_level,
    current_room,
    exits_from,
    load_map,
    load_monster_catalog,
    load_npc_catalog,
    provision_new_player,
    render_fog_map,
)
from app.encounters import active_encounter
from app.graph import graph
from app.llm import message_text

logger = logging.getLogger("cyodc.api")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # create world-state tables if missing (M7 switches to Alembic)
    yield


app = FastAPI(title="CYODC", version="0.2.0", lifespan=lifespan)

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


class InventoryEntry(BaseModel):
    name: str
    qty: int


class CombatState(BaseModel):
    monster: str
    monster_hp: int
    monster_max_hp: int
    round: int


class StateResponse(BaseModel):
    hp: int
    max_hp: int
    gold: int
    floor: int
    pos: list[int]
    room: str | None
    exits: list[str]
    items_here: list[str]
    monsters_here: list[str]
    npcs_here: list[str]
    inventory: list[InventoryEntry]
    theme: str
    map: list[str]
    combat: CombatState | None = None
    can_descend: bool = False


# --- helpers ----------------------------------------------------------------
# Per-session locks serialize turns on one session (BUG-1): concurrent invokes
# would otherwise fork the checkpoint and silently drop a turn.
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
            if not message_text(message).strip():
                continue  # tool-call-only turns carry no narration to show
            role = "dm"
        else:
            continue  # ToolMessage / SystemMessage are internal plumbing
        out.append(ChatMessage(role=role, content=message_text(message)))
    return out


def _load_messages(session_id: str) -> list:
    snapshot = graph.get_state(_thread_config(session_id))
    return snapshot.values.get("messages", []) if snapshot.values else []


def _item_names(db, slugs: list[str]) -> list[str]:
    if not slugs:
        return []
    rows = db.scalars(select(Item).where(Item.slug.in_(slugs))).all()
    by_slug = {r.slug: r.name for r in rows}
    return [by_slug.get(s, s) for s in slugs]


# --- endpoints --------------------------------------------------------------
@app.get("/")
def root() -> dict:
    return {"app": "CYODC", "version": "0.2.0", "docs": "/docs"}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "llm_mode": settings.resolved_llm_mode}


@app.post("/api/session", response_model=SessionResponse)
def create_session() -> SessionResponse:
    """Start a run: provision the player + floor 1, then cold-open the DM."""
    session_id = uuid4().hex
    try:
        with get_db_session() as db:
            provision_new_player(db, session_id)
    except Exception:  # pragma: no cover
        logger.exception("world provisioning failed on session create")
        raise HTTPException(status_code=502, detail="The Delve failed to load. Try again.")

    config = _thread_config(session_id)
    try:
        result = graph.invoke({"messages": []}, config)
    except Exception:  # pragma: no cover
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
    """Advance one turn. Serialized per session so concurrent turns can't fork history."""
    config = _thread_config(session_id)
    with _session_lock(session_id):
        if not _load_messages(session_id):
            raise HTTPException(status_code=404, detail="No such session. Start a new one.")
        try:
            result = graph.invoke({"messages": [HumanMessage(content=body.message)]}, config)
        except Exception:  # pragma: no cover
            logger.exception("graph invoke failed on turn")
            raise HTTPException(status_code=502, detail="The Delve is buffering. Try again.")

    return TurnResponse(
        session_id=session_id,
        reply=message_text(result["messages"][-1]),
        llm_mode=settings.resolved_llm_mode,
    )


@app.get("/api/session/{session_id}/state", response_model=StateResponse)
def get_world_state(session_id: str) -> StateResponse:
    """Current world state for the session: stats, room, fog-of-war map, inventory."""
    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == session_id)).first()
        if player is None:
            raise HTTPException(status_code=404, detail="No such session.")

        level = current_level(db, player)
        dm = load_map(level)
        room = current_room(db, level, player.pos_x, player.pos_y)
        vis = db.scalars(
            select(Visibility).where(
                Visibility.player_id == player.id, Visibility.level_id == level.id
            )
        ).first()
        explored = {tuple(cell) for cell in (vis.explored if vis else [])}
        here_slugs = list(room.contents.get("items", [])) if room else []
        monster_slugs = list(room.contents.get("monsters", [])) if room else []
        npc_slugs = list(room.contents.get("npcs", [])) if room else []
        catalog = load_monster_catalog(player.floor, level.monster_catalog)
        npc_catalog = load_npc_catalog(player.floor, level.npc_catalog)
        inv_rows = db.scalars(
            select(InventoryRow).where(InventoryRow.player_id == player.id)
        ).all()

        enc = active_encounter(db, player.id)
        combat = (
            CombatState(
                monster=enc.monster["name"],
                monster_hp=enc.monster_hp,
                monster_max_hp=enc.monster["max_hp"],
                round=enc.round,
            )
            if enc is not None
            else None
        )

        return StateResponse(
            hp=player.hp,
            max_hp=player.max_hp,
            gold=player.gold,
            floor=player.floor,
            pos=[player.pos_x, player.pos_y],
            room=room.kind if room else None,
            exits=exits_from(dm, player.pos_x, player.pos_y),
            items_here=_item_names(db, here_slugs),
            monsters_here=[catalog[s]["name"] if s in catalog else s for s in monster_slugs],
            npcs_here=[npc_catalog[s]["name"] if s in npc_catalog else s for s in npc_slugs],
            inventory=[InventoryEntry(name=r.item.name, qty=r.qty) for r in inv_rows],
            theme=level.theme,
            map=render_fog_map(dm, explored, player.pos_x, player.pos_y),
            combat=combat,
            can_descend=(
                room is not None
                and room.kind == "exit"
                and enc is None
                and not room.contents.get("monsters")  # a guard blocks the stairs
            ),
        )
