"""NPC node (M5): one in-character exchange on Haiku, with per-NPC memory.

Reached when the DM's `talk` tool fires. The NPC to speak as is read from that
`talk` ToolMessage (not from graph state — invariant #2); its personality card
and engine-priced wares come from the level's npc_catalog; past interactions
with THIS NPC are recalled from story_log (tagged by slug), so a merchant
"remembers you" without any held conversation state. One reply, then the DM
re-routes on the next input.

The card + recall are injected as system context; the model responds to just the
player's latest line, and `buy`/`list_wares` are the only sanctioned way state
changes here (invariant #1). Narration is recorded to story_log, tagged, so the
memory compounds visit to visit.
"""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy import select

from app import memory
from app.balance import buy_price_for
from app.db.base import get_db_session
from app.db.models import Player
from app.dungeon import current_level, load_npc_catalog
from app.llm import message_text
from app.nodes.tool_loop import run_tool_loop
from app.state import DMState
from app.tools import NPC_TOOLS

# NPCs are brief: one tool call (buy/list_wares) + one narration round.
NPC_MAX_ROUNDS = 2


def _talked_npc_slug(messages: list) -> str | None:
    for message in reversed(messages):
        if isinstance(message, ToolMessage) and getattr(message, "name", "") == "talk":
            try:
                data = json.loads(message.content)
            except (ValueError, TypeError):
                continue
            if data.get("talk"):
                return data.get("npc_slug")
    return None


def _last_human_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message_text(message)
    return ""


def _card_block(npc: dict, floor: int) -> str:
    lines = [
        "--- CHARACTER CARD ---",
        f"slug: {npc.get('slug', '')}",
        f"name: {npc.get('name', '')}",
        f"role: {npc.get('role', 'merchant')}",
        f"personality: {npc.get('personality', '')}",
        f"voice: {npc.get('voice', '')}",
    ]
    wares = npc.get("wares", [])
    if wares:
        lines.append("wares:")
        for ware in wares:
            effect = ware.get("effect", "trinket")
            lines.append(f"  - name: {ware.get('name', '')}")
            lines.append(f"    flavor: {ware.get('flavor', '')}")
            lines.append(f"    effect: {effect}")
            lines.append(f"    price_gold: {buy_price_for(effect, floor)}")
    lines.append("--- END CHARACTER CARD ---")
    return "\n".join(lines)


def _recall_block(rows: list) -> str:
    if rows:
        body = "\n".join(f"- {memory.snippet(r.content)}" for r in rows)
    else:
        body = "Nothing yet. First meeting."
    return f"--- YOU RECALL ---\n{body}\n--- END YOU RECALL ---"


def npc_node(state: DMState, config: RunnableConfig) -> dict:
    thread_id = config["configurable"]["thread_id"]
    messages = list(state["messages"])
    turn = len(messages)
    slug = _talked_npc_slug(messages)
    player_text = _last_human_text(messages)

    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == thread_id)).first()
        if player is None or slug is None:
            # Routed here without a resolvable NPC (shouldn't happen) — no-op.
            return {"messages": [], "next_node": None}

        level = current_level(db, player)
        catalog = load_npc_catalog(level.floor, level.npc_catalog)
        npc = catalog.get(slug)
        if npc is None:
            return {"messages": [], "next_node": None}

        recalled = memory.recall(db, player, player_text, tag=slug, exclude_recent=0)
        context = _card_block(npc, level.floor) + "\n\n" + _recall_block(recalled)

        # The NPC responds to the player's latest line (card + memory as context);
        # buy/list_wares resolve room state from the DB when called.
        history = [HumanMessage(content=player_text or "Hello.")]
        new_messages = run_tool_loop(
            "npc", history, NPC_TOOLS, db, player.id,
            max_rounds=NPC_MAX_ROUNDS, context=context,
        )
        final = message_text(new_messages[-1]) if new_messages else ""

    if final.strip():
        with get_db_session() as db2:
            player2 = db2.scalars(
                select(Player).where(Player.session_id == thread_id)
            ).first()
            if player2 is not None:
                memory.record_turn(db2, player2, turn, "npc", final, tag=slug)

    return {"messages": new_messages, "next_node": None}
