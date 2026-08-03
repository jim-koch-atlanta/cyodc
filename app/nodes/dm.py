"""DM / router node (M4): interpret input, call tools, route to combat/worldgen.

Routing, in order:
  1. A fight is active -> combat, ZERO model calls.
  2. Run the Sonnet tool loop. Then:
     - a `start_combat` tool opened a fight this turn -> combat (round 1).
     - a `descend` tool signaled a transition -> level_transition (worldgen).
     - otherwise the turn ends.

World state changes only through tools / the engines (invariant #1); graph state
stays {messages, next_node}.
"""

from __future__ import annotations

import json

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy import select

from app.db.base import get_db_session
from app.db.models import Player
from app.encounters import active_encounter
from app.nodes.tool_loop import run_tool_loop
from app.state import DMState
from app.tools import DM_TOOLS


def _descend_signaled(messages: list) -> bool:
    for message in messages:
        if isinstance(message, ToolMessage) and getattr(message, "name", "") == "descend":
            try:
                if json.loads(message.content).get("transition"):
                    return True
            except (ValueError, TypeError):
                pass
    return False


def dm_node(state: DMState, config: RunnableConfig) -> dict:
    thread_id = config["configurable"]["thread_id"]
    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == thread_id)).first()

        # Mid-fight: hand straight to combat, no model call.
        if player is not None and active_encounter(db, player.id) is not None:
            return {"messages": [], "next_node": "combat"}

        tools = DM_TOOLS if player is not None else []
        player_id = player.id if player is not None else None
        new_messages = run_tool_loop("dm", list(state["messages"]), tools, db, player_id)

        started = player is not None and active_encounter(db, player.id) is not None

    if _descend_signaled(new_messages):
        next_node = "level_transition"
    elif started:
        next_node = "combat"
    else:
        next_node = None
    return {"messages": new_messages, "next_node": next_node}
