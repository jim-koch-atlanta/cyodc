"""Graph wiring tested with a FAKE LLM (convention: no network in unit tests).

We monkeypatch the model call the DM node imported, so these tests assert graph
structure, checkpointer persistence, and the DM node's return contract — not
model behavior.
"""

from __future__ import annotations

import app.nodes.dm as dm_module
from app.graph import build_graph
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver


def _fake_run_agent(role: str, history: list) -> AIMessage:
    """Deterministic stand-in for app.llm.run_agent."""
    assert role == "dm"
    last_human = next(
        (m for m in reversed(history) if isinstance(m, HumanMessage)), None
    )
    if last_human is None:
        return AIMessage(content="COLD_OPEN")
    return AIMessage(content=f"echo::{last_human.content}")


def _graph(monkeypatch):
    monkeypatch.setattr(dm_module, "run_agent", _fake_run_agent)
    return build_graph(MemorySaver())


def test_cold_open_appends_one_ai_message(monkeypatch):
    graph = _graph(monkeypatch)
    cfg = {"configurable": {"thread_id": "a"}}
    result = graph.invoke({"messages": []}, cfg)

    assert len(result["messages"]) == 1
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "COLD_OPEN"


def test_dm_node_sets_next_node_none_in_m1(monkeypatch):
    graph = _graph(monkeypatch)
    cfg = {"configurable": {"thread_id": "b"}}
    result = graph.invoke({"messages": [HumanMessage(content="look")]}, cfg)

    # M1 always falls through to END.
    assert result["next_node"] is None


def test_turn_echoes_through_fake_llm(monkeypatch):
    graph = _graph(monkeypatch)
    cfg = {"configurable": {"thread_id": "c"}}
    result = graph.invoke({"messages": [HumanMessage(content="go north")]}, cfg)

    assert result["messages"][-1].content == "echo::go north"


def test_checkpointer_persists_across_turns(monkeypatch):
    graph = _graph(monkeypatch)
    cfg = {"configurable": {"thread_id": "d"}}

    graph.invoke({"messages": []}, cfg)  # cold open -> 1 msg
    graph.invoke({"messages": [HumanMessage(content="first")]}, cfg)  # +2 -> 3
    graph.invoke({"messages": [HumanMessage(content="second")]}, cfg)  # +2 -> 5

    restored = graph.get_state(cfg).values["messages"]
    assert len(restored) == 5
    # Human turns preserved in order.
    humans = [m.content for m in restored if isinstance(m, HumanMessage)]
    assert humans == ["first", "second"]


def test_threads_are_isolated(monkeypatch):
    graph = _graph(monkeypatch)
    graph.invoke({"messages": [HumanMessage(content="x")]}, {"configurable": {"thread_id": "one"}})
    two = graph.get_state({"configurable": {"thread_id": "two"}})

    assert not two.values  # a different thread_id sees nothing
