"""DM / router node (M2): interpret input, call tools, narrate results.

Thin by design (per langgraph-architect): resolve the player from the session's
thread_id, then delegate to the shared tool loop. World state changes ONLY
through the tools, which mutate the DB (invariant #1). Graph state stays
{messages, next_node} — no world state leaks in.

The node opens one DB transaction for the whole turn (`get_db_session` commits on
success, rolls back on error). It returns the entire tool exchange at once so the
checkpoint is written atomically.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from sqlalchemy import select

from app.db.base import get_db_session
from app.db.models import Player
from app.nodes.tool_loop import run_tool_loop
from app.state import DMState
from app.tools import DM_TOOLS


def dm_node(state: DMState, config: RunnableConfig) -> dict:
    thread_id = config["configurable"]["thread_id"]
    with get_db_session() as db:
        player = db.scalars(
            select(Player).where(Player.session_id == thread_id)
        ).first()
        tools = DM_TOOLS if player is not None else []
        player_id = player.id if player is not None else None
        new_messages = run_tool_loop("dm", list(state["messages"]), tools, db, player_id)

    # M2 always ends the turn at the DM. M3 populates next_node from a routing
    # signal (a dedicated tool), and the graph's conditional edge reads it.
    return {"messages": new_messages, "next_node": None}
