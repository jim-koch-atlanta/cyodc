"""DM / router node.

M1: interpret freeform player input and narrate in character. The node reads the
message window from state, delegates the model call to `app.llm` (which owns
model selection and prepends the system prompt), and appends the reply.

It returns `next_node=None` on every turn — M1 always falls through to END. In
M3 this node starts populating `next_node` to drive the conditional edge; the
return shape does not change.
"""

from __future__ import annotations

from app.llm import run_agent
from app.state import DMState


def dm_node(state: DMState) -> dict:
    reply = run_agent("dm", state["messages"])
    return {"messages": [reply], "next_node": None}
