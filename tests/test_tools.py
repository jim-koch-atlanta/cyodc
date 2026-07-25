"""The five typed tools, exercised directly against a seeded world."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import InventoryRow, Item, Player
from app.dungeon import provision_new_player
from app.tools.game_tools import inventory, look, move, take, use


@pytest.fixture
def world():
    s = SessionLocal()
    player = provision_new_player(s, "tools-test")
    yield s, player.id
    s.rollback()
    s.close()


def _call(tool, s, pid, **kwargs) -> dict:
    return json.loads(tool.invoke({**kwargs, "session": s, "player_id": pid}))


def test_look_reports_entrance_with_torch(world):
    s, pid = world
    r = _call(look, s, pid)
    assert r["ok"] and r["room"] == "entrance"
    assert r["exits"]
    assert "Tattered Torch" in r["items_here"]


def test_take_moves_item_and_is_idempotent(world):
    s, pid = world
    r = _call(take, s, pid, item="torch")
    assert r["ok"] and r["item"] == "Tattered Torch"

    inv = _call(inventory, s, pid)
    assert any(i["name"] == "Tattered Torch" for i in inv["items"])

    # Gate holds: taking the (now absent) torch again is a clean failure, not a dup.
    assert _call(take, s, pid, item="torch")["ok"] is False


def test_take_unknown_item_fails(world):
    s, pid = world
    assert _call(take, s, pid, item="the holy grail")["ok"] is False


def test_use_heal_restores_hp_capped(world):
    s, pid = world
    s.get(Player, pid).hp = 5
    ration = s.scalars(select(Item).where(Item.slug == "intake-ration")).one()
    s.add(InventoryRow(player_id=pid, item_id=ration.id, qty=1))
    s.flush()

    r = _call(use, s, pid, item="ration")
    assert r["ok"]
    assert s.get(Player, pid).hp == 15  # 5 + 10, under max_hp 20


def test_use_gold_adds_and_consumes(world):
    s, pid = world
    coins = s.scalars(select(Item).where(Item.slug == "dungeon-coins")).one()
    s.add(InventoryRow(player_id=pid, item_id=coins.id, qty=1))
    s.flush()
    before = s.get(Player, pid).gold

    r = _call(use, s, pid, item="coins")
    assert r["ok"] and s.get(Player, pid).gold == before + 12
    # consumed
    gone = s.scalars(
        select(InventoryRow).where(
            InventoryRow.player_id == pid, InventoryRow.item_id == coins.id
        )
    ).first()
    assert gone is None


def test_use_torch_lights_but_is_not_consumed(world):
    s, pid = world
    _call(take, s, pid, item="torch")
    assert _call(use, s, pid, item="torch")["ok"]
    inv = _call(inventory, s, pid)
    assert any(i["name"] == "Tattered Torch" for i in inv["items"])


def test_use_unknown_item_fails(world):
    s, pid = world
    assert _call(use, s, pid, item="nonexistent")["ok"] is False


def test_use_heal_at_full_hp_is_not_consumed(world):
    # BUG-1: a heal used at full HP must not be silently wasted.
    s, pid = world
    ration = s.scalars(select(Item).where(Item.slug == "intake-ration")).one()
    s.add(InventoryRow(player_id=pid, item_id=ration.id, qty=1))
    s.flush()
    assert s.get(Player, pid).hp == 20  # already full

    assert _call(use, s, pid, item="ration")["ok"] is True
    assert s.get(Player, pid).hp == 20  # unchanged
    kept = s.scalars(
        select(InventoryRow).where(
            InventoryRow.player_id == pid, InventoryRow.item_id == ration.id
        )
    ).first()
    assert kept is not None and kept.qty == 1  # retained, not burned


def test_move_rejects_nonsense_and_walls_but_takes_a_real_exit(world):
    s, pid = world
    assert _call(move, s, pid, direction="widdershins")["ok"] is False
    exits = _call(look, s, pid)["exits"]
    r = _call(move, s, pid, direction=exits[0])
    assert r["ok"] is True


def test_move_updates_position(world):
    s, pid = world
    before = (s.get(Player, pid).pos_x, s.get(Player, pid).pos_y)
    exits = _call(look, s, pid)["exits"]
    _call(move, s, pid, direction=exits[0])
    after = (s.get(Player, pid).pos_x, s.get(Player, pid).pos_y)
    assert after != before
