"""Level seeding + world helpers (deterministic; DB-backed, no LLM)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Item, Level, Room, Visibility
from app.dungeon import (
    _level_seed,
    current_level,
    ensure_item_catalog,
    provision_new_player,
)

ALL_ITEM_SLUGS = {
    "tattered-torch",
    "intake-ration",
    "dungeon-coins",
    "suspicious-pudding",
    "supervisors-memo",
}


@pytest.fixture
def session():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


def test_provision_creates_full_world(session):
    p = provision_new_player(session, "sess-x")
    assert p.id and p.floor == 1 and p.hp == 20 == p.max_hp
    level = session.scalars(select(Level).where(Level.player_id == p.id)).one()
    rooms = session.scalars(select(Room).where(Room.level_id == level.id)).all()
    assert len(rooms) >= 2
    vis = session.scalars(select(Visibility).where(Visibility.player_id == p.id)).one()
    assert len(vis.explored) > 0  # spawn reveals the entrance


def test_kinds_include_entrance_and_exit(session):
    p = provision_new_player(session, "sess-k")
    level = current_level(session, p)
    kinds = {r.kind for r in session.scalars(select(Room).where(Room.level_id == level.id)).all()}
    assert "entrance" in kinds
    assert "exit" in kinds


def test_player_starts_inside_the_entrance(session):
    p = provision_new_player(session, "sess-e")
    level = current_level(session, p)
    entrance = session.scalars(
        select(Room).where(Room.level_id == level.id, Room.kind == "entrance")
    ).one()
    assert entrance.contains(p.pos_x, p.pos_y)


def test_item_catalog_is_seeded(session):
    provision_new_player(session, "sess-i")
    slugs = {slug for (slug,) in session.execute(select(Item.slug)).all()}
    assert ALL_ITEM_SLUGS <= slugs


def test_catalog_seed_is_idempotent(session):
    ensure_item_catalog(session)
    ensure_item_catalog(session)
    assert len(session.scalars(select(Item)).all()) == len(ALL_ITEM_SLUGS)


def test_level_seed_is_deterministic_and_scoped():
    assert _level_seed("abc", 1) == _level_seed("abc", 1)
    assert _level_seed("abc", 1) != _level_seed("abc", 2)
    assert _level_seed("abc", 1) != _level_seed("xyz", 1)


def test_all_seeded_items_are_placed(session):
    # BUG-3: every catalog item (incl. supervisors-memo) must be reachable.
    p = provision_new_player(session, "sess-place")
    level = current_level(session, p)
    placed: set[str] = set()
    for room in session.scalars(select(Room).where(Room.level_id == level.id)).all():
        placed |= set(room.contents.get("items", []))
    assert "supervisors-memo" in placed
    assert "tattered-torch" in placed


def test_monsters_are_seeded_and_the_exit_is_guarded(session):
    p = provision_new_player(session, "sess-monsters")
    level = current_level(session, p)
    from app.dungeon import load_monster_catalog

    catalog = load_monster_catalog(level.floor)
    placed: list[str] = []
    exit_room = None
    for room in session.scalars(select(Room).where(Room.level_id == level.id)).all():
        placed += room.contents.get("monsters", [])
        if room.kind == "exit":
            exit_room = room
    assert placed, "at least one monster placed"
    assert all(slug in catalog for slug in placed)  # validated against catalog
    assert exit_room is not None and exit_room.contents.get("monsters")  # exit guarded
    # the entrance stays safe
    entrance = session.scalars(
        select(Room).where(Room.level_id == level.id, Room.kind == "entrance")
    ).one()
    assert not entrance.contents.get("monsters")


def test_two_players_get_independent_dungeons(session):
    a = provision_new_player(session, "sess-a")
    b = provision_new_player(session, "sess-b")
    la = current_level(session, a)
    lb = current_level(session, b)
    assert la.id != lb.id
    assert la.seed != lb.seed  # different session_id -> different seed
