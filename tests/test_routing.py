"""Routing is deterministic plain code — unit test it directly (no LLM)."""

from __future__ import annotations

import pytest
from langgraph.graph import END

from app.routing import route_from_dm


def test_none_next_node_ends_the_turn():
    assert route_from_dm({"messages": [], "next_node": None}) is END


def test_missing_next_node_ends_the_turn():
    # A checkpoint written before `next_node` existed deserializes without it.
    assert route_from_dm({"messages": []}) is END


@pytest.mark.parametrize("dest", ["combat", "boss", "npc", "level_transition"])
def test_named_next_node_routes_through(dest):
    assert route_from_dm({"messages": [], "next_node": dest}) == dest
