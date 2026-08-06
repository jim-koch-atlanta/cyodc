"""NPC node: talk routing, the merchant economy, and per-NPC memory.

Stub mode routes shopping intents to the talk/buy/list_wares tools, so the whole
merchant flow is exercised offline (no key, deterministic)."""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select

from app.balance import buy_price_for
from app.db.base import get_db_session
from app.db.models import InventoryRow, Player, Room, StoryLog
from app.dungeon import current_level, provision_new_player
from app.graph import build_graph
from app.tools.game_tools import buy, list_wares, talk

_POULTICE = "Regulation Field Poultice"


# --- setup helpers ----------------------------------------------------------
def _merchant_room(db, level) -> Room | None:
    for room in db.scalars(select(Room).where(Room.level_id == level.id)).all():
        if "brindle_mox" in room.contents.get("npcs", []):
            return room
    return None


def _stand_with_merchant(session_id: str, gold: int = 100) -> int:
    with get_db_session() as db:
        player = provision_new_player(db, session_id)
        level = current_level(db, player)
        room = _merchant_room(db, level)
        assert room is not None, "the merchant must be placed on floor 1"
        player.pos_x = room.x + room.w // 2
        player.pos_y = room.y + room.h // 2
        player.gold = gold
        db.flush()
        return player.id


def _player(db, session_id: str) -> Player:
    return db.scalars(select(Player).where(Player.session_id == session_id)).first()


def _turn(graph, session_id: str, text: str) -> str:
    cfg = {"configurable": {"thread_id": session_id}}
    return graph.invoke({"messages": [HumanMessage(content=text)]}, cfg)["messages"][-1].content


# --- placement + balance ----------------------------------------------------
def test_merchant_is_placed_on_floor_one_in_a_safe_room():
    with get_db_session() as db:
        player = provision_new_player(db, "npc-seed")
        level = current_level(db, player)
        room = _merchant_room(db, level)
        assert room is not None
        assert room.kind != "exit"  # never behind the guarded stairs
        assert not room.contents.get("monsters")  # and never sharing with a guard
        assert level.npc_catalog and "brindle_mox" in level.npc_catalog


def test_buy_price_is_bounded_and_scales_with_floor():
    assert buy_price_for("minor_heal", 1) == 12
    assert buy_price_for("minor_heal", 5) >= buy_price_for("minor_heal", 1)
    assert buy_price_for("major_heal", 100) <= 999


# --- the tools --------------------------------------------------------------
def test_talk_returns_the_routing_signal():
    _stand_with_merchant("npc-talk")
    with get_db_session() as db:
        out = json.loads(talk.invoke({"target": "", "session": db, "player_id": _player(db, "npc-talk").id}))
    assert out["ok"] and out["talk"] is True and out["npc_slug"] == "brindle_mox"


def test_talk_finds_no_one_at_the_guarded_stairs():
    with get_db_session() as db:
        player = provision_new_player(db, "npc-alone")
        level = current_level(db, player)
        exit_room = db.scalars(
            select(Room).where(Room.level_id == level.id, Room.kind == "exit")
        ).first()
        player.pos_x = exit_room.x + exit_room.w // 2
        player.pos_y = exit_room.y + exit_room.h // 2
        db.flush()
        out = json.loads(talk.invoke({"target": "", "session": db, "player_id": player.id}))
    assert out["ok"] is False and not out["talk"]


def test_list_wares_quotes_engine_prices():
    _stand_with_merchant("npc-wares")
    with get_db_session() as db:
        out = json.loads(list_wares.invoke({"session": db, "player_id": _player(db, "npc-wares").id}))
    assert out["ok"]
    poultice = next(w for w in out["wares"] if w["name"] == _POULTICE)
    assert poultice["price_gold"] == buy_price_for("minor_heal", 1)  # code owns the price


def test_buy_deducts_gold_and_adds_inventory():
    _stand_with_merchant("npc-buy", gold=100)
    price = buy_price_for("minor_heal", 1)
    with get_db_session() as db:
        out = json.loads(buy.invoke({"item": _POULTICE, "session": db, "player_id": _player(db, "npc-buy").id}))
    assert out["ok"] and out["gold"] == 100 - price
    with get_db_session() as db:
        player = _player(db, "npc-buy")
        assert player.gold == 100 - price
        rows = db.scalars(select(InventoryRow).where(InventoryRow.player_id == player.id)).all()
        assert any(r.item.name == _POULTICE for r in rows)


def test_buy_is_refused_when_you_cant_afford_it():
    _stand_with_merchant("npc-broke", gold=3)
    with get_db_session() as db:
        player = _player(db, "npc-broke")
        out = json.loads(buy.invoke({"item": _POULTICE, "session": db, "player_id": player.id}))
        assert out["ok"] is False
        assert _player(db, "npc-broke").gold == 3  # unchanged
        assert not db.scalars(select(InventoryRow).where(InventoryRow.player_id == player.id)).all()


def test_buy_rejects_an_item_not_for_sale():
    _stand_with_merchant("npc-noitem", gold=100)
    with get_db_session() as db:
        out = json.loads(buy.invoke({"item": "a legendary flaming sword", "session": db, "player_id": _player(db, "npc-noitem").id}))
    assert out["ok"] is False


# --- through the graph ------------------------------------------------------
def test_talking_routes_through_the_npc_node_and_records_tagged_memory():
    _stand_with_merchant("npc-graph")
    graph = build_graph(MemorySaver())
    reply = _turn(graph, "npc-graph", "talk to Brindle Mox")
    assert reply.strip()
    with get_db_session() as db:
        player = _player(db, "npc-graph")
        npc_rows = db.scalars(
            select(StoryLog).where(StoryLog.player_id == player.id, StoryLog.role == "npc")
        ).all()
        assert npc_rows and all(r.tag == "brindle_mox" for r in npc_rows)


def test_buying_through_the_graph_deducts_gold_and_adds_the_item():
    _stand_with_merchant("npc-buygraph", gold=100)
    graph = build_graph(MemorySaver())
    _turn(graph, "npc-buygraph", "buy the Regulation Field Poultice")
    price = buy_price_for("minor_heal", 1)
    with get_db_session() as db:
        player = _player(db, "npc-buygraph")
        assert player.gold == 100 - price
        rows = db.scalars(select(InventoryRow).where(InventoryRow.player_id == player.id)).all()
        assert any(r.item.name == _POULTICE for r in rows)
