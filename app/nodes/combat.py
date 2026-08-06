"""Combat node (M3): resolve ONE round, then narrate it on Haiku.

Reached only when a fight is active (the DM node routes here). It classifies the
player's action deterministically, runs the engine + persists the result via
app/encounters.py, and hands the engine's factual summary to the narrator. The
DB transaction is closed BEFORE the model call, so we don't hold it across the
network. If narration fails, we fall back to the factual summary — combat state
is already committed, so the turn always completes cleanly.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy import select

from app import memory
from app.db.base import get_db_session
from app.db.models import Player
from app.encounters import active_encounter, narration_summary, resolve_combat_turn
from app.llm import message_text, run_agent
from app.state import DMState


def _last_human_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message_text(message)
    return ""


def combat_node(state: DMState, config: RunnableConfig) -> dict:
    thread_id = config["configurable"]["thread_id"]
    turn = len(state["messages"])  # replay-stable coordinate for story_log
    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == thread_id)).first()
        encounter = active_encounter(db, player.id) if player is not None else None
        if player is None or encounter is None:
            # Routed here without a live fight (shouldn't happen) — no-op back to DM.
            return {"messages": [], "next_node": None}

        result = resolve_combat_turn(db, player, encounter, _last_human_text(state["messages"]))
        summary = narration_summary(result, player, encounter)

    try:
        reply = run_agent("combat", [HumanMessage(content=summary)])
    except Exception:  # narration is flavor; state is already committed
        reply = AIMessage(content=summary)

    text = message_text(reply)
    if text.strip():
        with get_db_session() as db2:
            player2 = db2.scalars(
                select(Player).where(Player.session_id == thread_id)
            ).first()
            if player2 is not None:
                memory.record_turn(db2, player2, turn, "combat", text)

    return {"messages": [reply], "next_node": None}
