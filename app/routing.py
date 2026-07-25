"""Conditional-edge routing functions — one explicit, testable place.

No regex on free text, no inline lambdas in the graph builder. The DM node
writes its decision into `state["next_node"]`; these functions only read it.
M1 wires `dm -> END` directly; `route_from_dm` is exercised by unit tests now
and becomes the live conditional edge in M3.
"""

from __future__ import annotations

from langgraph.graph import END

from app.state import DMState


def route_from_dm(state: DMState) -> str:
    """Where to go after the DM node. `None` means end the turn."""
    return state.get("next_node") or END
