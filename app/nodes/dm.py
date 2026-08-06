"""DM / router node (M4–M5): interpret input, call tools, route the turn.

Routing, in order:
  1. A fight is active -> combat, ZERO model calls.
  2. Run the Sonnet tool loop. Then:
     - a `descend` tool signaled a transition -> level_transition (worldgen).
     - a `start_combat` tool opened a fight this turn -> combat (round 1).
     - a `talk` tool addressed an NPC -> npc.
     - otherwise the turn ends.

M5 adds story_log memory: the player's input is recorded at entry (this node runs
first every turn), relevant older beats are recalled and injected as context, and
the DM's own narration is recorded when it handles the turn inline. Memory lives
in our schema, never in graph state (invariant #2); the checkpointer still holds
only {messages, next_node}.
"""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy import select

from app import memory
from app.db.base import get_db_session
from app.db.models import Player
from app.encounters import active_encounter
from app.llm import message_text
from app.nodes.tool_loop import run_tool_loop
from app.state import DMState
from app.tools import DM_TOOLS

# Verbatim window handed to the model: the last N whole turns (a turn = one
# HumanMessage). Older-but-relevant context comes from recall instead, which
# bounds token cost on long runs (invariant #5) without severing tool_use pairs.
VERBATIM_TURNS = 12

_RECALL_HEADER = "You recall, from earlier in this run:"


def _descend_signaled(messages: list) -> bool:
    for message in messages:
        if isinstance(message, ToolMessage) and getattr(message, "name", "") == "descend":
            try:
                if json.loads(message.content).get("transition"):
                    return True
            except (ValueError, TypeError):
                pass
    return False


def _talk_signaled(messages: list) -> bool:
    for message in messages:
        if isinstance(message, ToolMessage) and getattr(message, "name", "") == "talk":
            try:
                if json.loads(message.content).get("talk"):
                    return True
            except (ValueError, TypeError):
                pass
    return False


def _last_human_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message_text(message)
    return ""


def _recent_window(messages: list, max_turns: int) -> list:
    """Keep the last `max_turns` whole turns. Slicing at a HumanMessage boundary
    never splits an AI(tool_use)/ToolMessage pair (those follow their Human)."""
    human_idxs = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(human_idxs) <= max_turns:
        return list(messages)
    return list(messages[human_idxs[-max_turns]:])


def dm_node(state: DMState, config: RunnableConfig) -> dict:
    thread_id = config["configurable"]["thread_id"]
    messages = list(state["messages"])
    turn = len(messages)  # replay-stable coordinate for story_log
    player_text = _last_human_text(messages)

    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == thread_id)).first()

        if player is not None and player_text:
            memory.record_turn(db, player, turn, "player", player_text)

        # Mid-fight: hand straight to combat, no model call.
        if player is not None and active_encounter(db, player.id) is not None:
            return {"messages": [], "next_node": "combat"}

        tools = DM_TOOLS if player is not None else []
        player_id = player.id if player is not None else None

        context = None
        if player is not None and player_text:
            recalled = memory.recall(db, player, player_text)
            context = memory.recall_block(recalled, _RECALL_HEADER)

        history = _recent_window(messages, VERBATIM_TURNS)
        new_messages = run_tool_loop("dm", history, tools, db, player_id, context=context)

        started = player is not None and active_encounter(db, player.id) is not None
        have_player = player is not None

    if _descend_signaled(new_messages):
        next_node = "level_transition"
    elif started:
        next_node = "combat"
    elif _talk_signaled(new_messages):
        next_node = "npc"
    else:
        next_node = None

    # Record the DM's narration only when it handled the turn inline; routing
    # turns are narrated (and recorded) by the target node.
    if next_node is None and have_player:
        final = message_text(new_messages[-1]) if new_messages else ""
        if final.strip():
            with get_db_session() as db2:
                player2 = db2.scalars(
                    select(Player).where(Player.session_id == thread_id)
                ).first()
                if player2 is not None:
                    memory.record_turn(db2, player2, turn, "dm", final)

    return {"messages": new_messages, "next_node": next_node}
