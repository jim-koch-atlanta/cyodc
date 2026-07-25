"""The DM's tool-calling loop (shared; future combat/npc nodes can reuse it).

One player turn = one graph node = one checkpoint. The loop runs the model, and
while it emits tool_calls, executes them deterministically and feeds the results
back — capped at MAX_TOOL_ROUNDS to honor the play-time model-call budget
(invariant #4). The final round is forced with `tools=None` so the turn never
ends on a dangling `tool_use` (which Anthropic would reject on resume).

All messages produced this turn are returned together so the DM node commits the
whole exchange in ONE checkpoint write (never a partial tool_use/tool_result).
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from sqlalchemy.orm import Session

from app.llm import run_agent_with_tools
from app.tools import TOOLS_BY_NAME

MAX_TOOL_ROUNDS = 3


def run_tool_loop(
    role: str,
    history: list[BaseMessage],
    tools: list,
    session: Session,
    player_id: int | None,
    *,
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> list[BaseMessage]:
    """Return every new message this turn produced (AIMessages + ToolMessages)."""
    produced: list[BaseMessage] = []
    working = list(history)

    for round_num in range(max_rounds + 1):
        final = round_num == max_rounds
        reply = run_agent_with_tools(role, working, tools=None if final else tools)
        produced.append(reply)
        working.append(reply)

        tool_calls = getattr(reply, "tool_calls", None) or []
        if not tool_calls or final:
            break

        for call in tool_calls:
            result = _execute(call, session, player_id)
            produced.append(result)
            working.append(result)

    return produced


def _execute(call: dict, session: Session, player_id: int | None) -> ToolMessage:
    name = call.get("name", "")
    args = call.get("args", {}) or {}
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        content = json.dumps({"ok": False, "message": f"No such tool: {name}."})
    else:
        try:
            content = tool.invoke({**args, "session": session, "player_id": player_id})
        except Exception as exc:  # a tool bug must not kill the turn
            content = json.dumps({"ok": False, "message": f"The tool jammed: {exc}"})
    return ToolMessage(content=content, tool_call_id=call.get("id", ""), name=name)
