"""Worldgen node (M4): generate the next floor between turns, place the player.

Three phases so the DB transaction is NOT held across the model call (per the
langgraph-architect review):
  1. Read the player; build the deterministic BSP skeleton (DB closed).
  2. ONE worldgen model call to decorate it (no DB connection held).
  3. Materialize + place the player atomically (DB re-opened).

Replay-safe: phase 3 checks for the already-materialized Level with an explicit
SELECT (the UNIQUE(player_id, floor) makes a re-materialize impossible anyway),
so a re-run just re-reads and re-narrates. Worldgen failure never breaks the
descent — `generate_floor_content` falls back to deterministic content, and the
arrival is narrated from the (always-populated) entrance description.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy import select

from app.db.base import get_db_session
from app.db.models import Level, Player, Room, Visibility
from app.dungeon import (
    _assign_kinds,
    _level_seed,
    load_map,
    materialize_generated_level,
    reveal,
)
from app.mapgen import generate
from app.state import DMState
from app.worldgen import _fallback_content, generate_floor_content


def worldgen_node(state: DMState, config: RunnableConfig) -> dict:
    thread_id = config["configurable"]["thread_id"]

    # Phase 1: read the player, build the deterministic skeleton.
    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == thread_id)).first()
        if player is None:
            return {"messages": [], "next_node": None}
        floor = player.floor + 1
        session_id = player.session_id
        already = (
            db.scalars(
                select(Level).where(Level.player_id == player.id, Level.floor == floor)
            ).first()
            is not None
        )

    seed = _level_seed(session_id, floor)
    dm = generate(seed=seed)
    kinds = _assign_kinds(dm)

    # Phase 2: one model call to decorate (skipped on replay). No DB held.
    content = None if already else generate_floor_content(floor, dm, kinds, seed, session_id)

    # Phase 3: materialize + place the player, atomically.
    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == thread_id)).first()
        level = db.scalars(
            select(Level).where(Level.player_id == player.id, Level.floor == floor)
        ).first()
        if level is None:
            if content is None:
                # Phase 1 saw a level that's since gone (out-of-band delete, not a
                # replay). Regenerate deterministic content — no model call held.
                content = _fallback_content(floor, dm, kinds, seed, session_id)
            level = materialize_generated_level(db, player, floor, dm, kinds, content)

        entrance = db.scalars(
            select(Room).where(Room.level_id == level.id, Room.kind == "entrance")
        ).first() or db.scalars(select(Room).where(Room.level_id == level.id)).first()

        player.floor = floor
        player.pos_x = entrance.x + entrance.w // 2
        player.pos_y = entrance.y + entrance.h // 2

        vis = db.scalars(
            select(Visibility).where(
                Visibility.player_id == player.id, Visibility.level_id == level.id
            )
        ).first()
        if vis is None:
            vis = Visibility(player_id=player.id, level_id=level.id, explored=[])
            db.add(vis)
            db.flush()
        reveal(db, vis, load_map(level), player.pos_x, player.pos_y)

        arrival = f"You descend to floor {floor}. {entrance.description}"

    return {"messages": [AIMessage(content=arrival)], "next_node": None}
