"""DM / router node (M3): interpret input, call tools, route to combat.

Thin by design. Two paths:
  1. A fight is already active -> short-circuit to the combat node with ZERO
     model calls (combat narration is Haiku; we don't burn Sonnet per round).
  2. Otherwise run the Sonnet tool loop. If a `start_combat` tool just created an
     encounter this turn, route to combat so round 1 resolves in the same turn.

World state changes only through tools / the combat engine (invariant #1). Graph
state stays {messages, next_node}.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from sqlalchemy import select

from app.db.base import get_db_session
from app.db.models import Player
from app.encounters import active_encounter
from app.nodes.tool_loop import run_tool_loop
from app.state import DMState
from app.tools import DM_TOOLS


def dm_node(state: DMState, config: RunnableConfig) -> dict:
    thread_id = config["configurable"]["thread_id"]
    with get_db_session() as db:
        player = db.scalars(
            select(Player).where(Player.session_id == thread_id)
        ).first()

        # Mid-fight: hand straight to combat, no model call.
        if player is not None and active_encounter(db, player.id) is not None:
            return {"messages": [], "next_node": "combat"}

        tools = DM_TOOLS if player is not None else []
        player_id = player.id if player is not None else None
        new_messages = run_tool_loop("dm", list(state["messages"]), tools, db, player_id)

        # Did a start_combat tool open a fight this turn? Route to round 1.
        started = player is not None and active_encounter(db, player.id) is not None

    return {"messages": new_messages, "next_node": "combat" if started else None}
