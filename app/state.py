"""LangGraph session state.

Per architecture invariant #2, this holds ONLY conversation/session flow —
the message window and the router's next-hop. World state (inventory, HP, maps,
fog-of-war) never lives here; it belongs in the SQL schema (arriving in M2).
"""

from __future__ import annotations

from typing import Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import Annotated, TypedDict

# Every place the DM router can hand off to. M1 only ever uses `None`
# (fall through to END); the field exists now so M3's conditional edge and
# every existing checkpoint gain routing without a schema migration.
NextNode = Literal["dm", "combat", "boss", "npc", "level_transition"]


class DMState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    next_node: NextNode | None
