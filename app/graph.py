"""LangGraph assembly and the process-wide compiled-graph singleton.

M1 shape:  START -> dm -> END  with a SqliteSaver checkpointer.

The checkpointer connection and the compiled graph are created once at import
time and reused for the life of the process (see langgraph-architect review):
compiling per request is expensive and not safe to repeat, and the SQLite
connection must outlive individual requests. `check_same_thread=False` lets the
threaded server share the one connection; SqliteSaver guards it with a lock.

M3 replaces the single `dm -> END` edge with a conditional edge over
`route_from_dm`; nothing else in this module changes. M7 swaps SqliteSaver for
PostgresSaver in `_make_checkpointer` alone.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.nodes.combat import combat_node
from app.nodes.dm import dm_node
from app.routing import route_from_dm
from app.state import DMState


def build_graph(checkpointer):
    """Compile the M1 graph against a caller-supplied checkpointer.

    Tests pass an in-memory checkpointer; the app passes the SQLite singleton.
    """
    builder = StateGraph(DMState)
    builder.add_node("dm", dm_node)
    builder.add_node("combat", combat_node)
    builder.add_edge(START, "dm")
    # The DM either ends the turn (exploration) or routes to combat (next_node).
    builder.add_conditional_edges("dm", route_from_dm, {"combat": "combat", END: END})
    builder.add_edge("combat", END)  # one round per turn; the DM re-routes next turn
    return builder.compile(checkpointer=checkpointer)


def _make_checkpointer() -> SqliteSaver:
    settings = get_settings()
    db_path = Path(settings.checkpoint_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn)


# Process-wide singletons. Imported by app.main.
checkpointer: SqliteSaver = _make_checkpointer()
graph = build_graph(checkpointer)
