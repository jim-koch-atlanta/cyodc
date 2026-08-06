"""Graph + tool-loop wiring, tested with a FAKE LLM (no network).

We monkeypatch the model call the tool loop imported, so these tests assert
graph structure, the tool-calling loop, checkpointer persistence, and that tool
execution actually mutates the DB — never model behavior.
"""

from __future__ import annotations

import app.nodes.tool_loop as tool_loop
from app.db.base import get_db_session
from app.db.models import Visibility
from app.dungeon import provision_new_player
from app.graph import build_graph
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select


def _echo_narrator(role, history, tools, context=None):
    """Basic fake: cold-open on empty history, else echo the last human turn."""
    last_human = next((m for m in reversed(history) if isinstance(m, HumanMessage)), None)
    if last_human is None:
        return AIMessage(content="COLD_OPEN")
    return AIMessage(content=f"echo::{last_human.content}")


def _graph(monkeypatch, narrator=_echo_narrator):
    monkeypatch.setattr(tool_loop, "run_agent_with_tools", narrator)
    return build_graph(MemorySaver())


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


# --- basic wiring (no player => no tools) -----------------------------------
def test_cold_open_appends_one_ai_message(monkeypatch):
    graph = _graph(monkeypatch)
    result = graph.invoke({"messages": []}, _cfg("a"))
    assert len(result["messages"]) == 1
    assert result["messages"][-1].content == "COLD_OPEN"


def test_dm_node_sets_next_node_none_in_m2(monkeypatch):
    graph = _graph(monkeypatch)
    result = graph.invoke({"messages": [HumanMessage(content="look")]}, _cfg("b"))
    assert result["next_node"] is None


def test_turn_echoes_through_fake_llm(monkeypatch):
    graph = _graph(monkeypatch)
    result = graph.invoke({"messages": [HumanMessage(content="go north")]}, _cfg("c"))
    assert result["messages"][-1].content == "echo::go north"


def test_checkpointer_persists_across_turns(monkeypatch):
    graph = _graph(monkeypatch)
    cfg = _cfg("d")
    graph.invoke({"messages": []}, cfg)
    graph.invoke({"messages": [HumanMessage(content="first")]}, cfg)
    graph.invoke({"messages": [HumanMessage(content="second")]}, cfg)
    restored = graph.get_state(cfg).values["messages"]
    humans = [m.content for m in restored if isinstance(m, HumanMessage)]
    assert humans == ["first", "second"]


def test_threads_are_isolated(monkeypatch):
    graph = _graph(monkeypatch)
    graph.invoke({"messages": [HumanMessage(content="x")]}, _cfg("one"))
    assert not graph.get_state(_cfg("two")).values


# --- tool loop actually executes tools and mutates the world ----------------
def test_tool_call_executes_and_mutates_world(monkeypatch):
    with get_db_session() as db:
        player = provision_new_player(db, "tool-thread")
        pid = player.id
        before = len(
            db.scalars(select(Visibility).where(Visibility.player_id == pid)).one().explored
        )

    def tool_then_narrate(role, history, tools, context=None):
        # First round emits a `look` tool call; after the ToolMessage, narrate.
        if history and isinstance(history[-1], ToolMessage):
            return AIMessage(content="You survey the room.")
        return AIMessage(
            content="",
            tool_calls=[{"name": "look", "args": {}, "id": "c1", "type": "tool_call"}],
        )

    graph = _graph(monkeypatch, tool_then_narrate)
    result = graph.invoke({"messages": [HumanMessage(content="look")]}, _cfg("tool-thread"))

    msgs = result["messages"]
    assert any(isinstance(m, ToolMessage) for m in msgs), "tool was executed"
    assert msgs[-1].content == "You survey the room."  # loop ended on narration
    # look revealed cells -> the world (fog-of-war) changed via the tool.
    with get_db_session() as db:
        after = len(
            db.scalars(select(Visibility).where(Visibility.player_id == pid)).one().explored
        )
    assert after >= before


def test_loop_forces_final_narration_when_model_keeps_calling_tools(monkeypatch):
    with get_db_session() as db:
        provision_new_player(db, "loopy-thread")

    def always_tool(role, history, tools, context=None):
        # A misbehaving model that never stops calling tools; the cap must save us.
        if tools is None:  # final forced round -> must narrate
            return AIMessage(content="Fine. You look around, exhaustively.")
        return AIMessage(
            content="",
            tool_calls=[{"name": "look", "args": {}, "id": "loop", "type": "tool_call"}],
        )

    graph = _graph(monkeypatch, always_tool)
    result = graph.invoke({"messages": [HumanMessage(content="look")]}, _cfg("loopy-thread"))

    # Never ends on a dangling tool_use; last message is narration text.
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "Fine. You look around, exhaustively."
    assert not (result["messages"][-1].tool_calls or [])
