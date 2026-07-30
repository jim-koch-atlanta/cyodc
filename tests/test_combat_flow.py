"""Combat routing, persistence, and rewards — through the graph and the API.

Runs in stub mode (conftest); the combat engine is deterministic, so outcomes
against the weak floor-1 monsters are reproducible.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select

from app.db.base import get_db_session
from app.db.models import InventoryRow, Item, Player, Room
from app.dungeon import current_level, current_room, provision_new_player
from app.encounters import _end_encounter, active_encounter, classify_combat_action
from app.graph import build_graph
from app.main import app


def _seed_with_monster(session_id: str, slug: str = "form-filler") -> None:
    with get_db_session() as db:
        player = provision_new_player(db, session_id)
        level = current_level(db, player)
        room = current_room(db, level, player.pos_x, player.pos_y)
        room.contents["monsters"] = [slug]
        db.flush()


def _turn(graph, session_id: str, text: str) -> str:
    cfg = {"configurable": {"thread_id": session_id}}
    return graph.invoke({"messages": [HumanMessage(content=text)]}, cfg)["messages"][-1].content


def _snapshot(session_id: str) -> dict:
    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == session_id)).first()
        enc = active_encounter(db, player.id)
        return {
            "hp": player.hp,
            "gold": player.gold,
            "active": enc is not None,
            "monster_hp": enc.monster_hp if enc else None,
            "round": enc.round if enc else None,
        }


# --- classification --------------------------------------------------------
def test_classify_combat_action():
    assert classify_combat_action("attack") == ("attack", None)
    assert classify_combat_action("i swing wildly") == ("attack", None)  # default
    assert classify_combat_action("flee!") == ("flee", None)
    assert classify_combat_action("run away") == ("flee", None)
    assert classify_combat_action("drink the pudding") == ("use", "the pudding")


# --- routing + persistence -------------------------------------------------
def test_attack_starts_and_persists_an_encounter():
    _seed_with_monster("start-fight", "shift-supervisor")  # tough: survives round 1
    graph = build_graph(MemorySaver())
    _turn(graph, "start-fight", "attack the supervisor")
    s = _snapshot("start-fight")
    assert s["active"] is True
    assert s["monster_hp"] < 14  # round 1 landed


def test_encounter_survives_across_turns_and_any_input_is_a_round():
    # Mid-fight the DM short-circuits to combat, so even "check inventory" is a round.
    _seed_with_monster("persist", "shift-supervisor")
    graph = build_graph(MemorySaver())
    _turn(graph, "persist", "attack the supervisor")
    before = _snapshot("persist")
    _turn(graph, "persist", "check my inventory")
    after = _snapshot("persist")
    assert after["active"] is True
    assert after["round"] > before["round"]  # a round advanced despite non-combat text


def test_fight_to_victory_awards_gold_and_ends():
    _seed_with_monster("win", "form-filler")  # weak: dies fast, gold 3
    graph = build_graph(MemorySaver())
    _turn(graph, "win", "attack the form-filler")
    for _ in range(8):
        if not _snapshot("win")["active"]:
            break
        _turn(graph, "win", "attack")
    s = _snapshot("win")
    assert s["active"] is False  # fight ended
    assert s["gold"] == 3  # looted exactly once
    assert s["hp"] > 0  # survived


def test_after_victory_dm_resumes_exploration():
    _seed_with_monster("resume", "form-filler")
    graph = build_graph(MemorySaver())
    _turn(graph, "resume", "attack the form-filler")
    for _ in range(8):
        if not _snapshot("resume")["active"]:
            break
        _turn(graph, "resume", "attack")
    # No active fight -> the DM (exploration) handles input again.
    out = _turn(graph, "resume", "look around")
    assert "Exits:" in out or "exit" in out.lower()


# --- reward replay-safety --------------------------------------------------
def test_victory_reward_is_replay_safe():
    with get_db_session() as db:
        player = provision_new_player(db, "replay")
        level = current_level(db, player)
        room = current_room(db, level, player.pos_x, player.pos_y)
        room.contents["monsters"] = ["form-filler"]
        db.flush()
        from app.encounters import start_encounter

        enc = start_encounter(db, player, "form-filler")
        gold0 = player.gold

        _end_encounter(db, player, enc, "victory")  # awards + deletes (the gate)
        gold1 = player.gold
        _end_encounter(db, player, enc, "victory")  # replay: row gone -> no-op
        gold2 = player.gold

    assert gold1 == gold0 + 3
    assert gold2 == gold1  # not double-awarded


# --- playtester fixes ------------------------------------------------------
def _give_item(session_id: str, slug: str) -> int:
    with get_db_session() as db:
        player = provision_new_player(db, session_id)
        level = current_level(db, player)
        room = current_room(db, level, player.pos_x, player.pos_y)
        room.contents["monsters"] = ["shift-supervisor"]
        item = db.scalars(select(Item).where(Item.slug == slug)).one()
        db.add(InventoryRow(player_id=player.id, item_id=item.id, qty=1))
        db.flush()
        return player.id


def test_use_heal_at_full_hp_in_combat_keeps_item_and_turn():
    # SEV-1: a heal used at full HP must not be consumed nor spend the turn.
    _give_item("heal-full", "suspicious-pudding")
    graph = build_graph(MemorySaver())
    _turn(graph, "heal-full", "attack the supervisor")
    with get_db_session() as db:
        p = db.scalars(select(Player).where(Player.session_id == "heal-full")).first()
        p.hp = p.max_hp
        db.flush()
    before = _snapshot("heal-full")

    _turn(graph, "heal-full", "use the suspicious pudding")

    after = _snapshot("heal-full")
    assert after["round"] == before["round"]  # turn not spent
    assert after["monster_hp"] == before["monster_hp"]  # no free monster hit
    with get_db_session() as db:
        p = db.scalars(select(Player).where(Player.session_id == "heal-full")).first()
        pudding = db.scalars(select(Item).where(Item.slug == "suspicious-pudding")).one()
        kept = db.scalars(
            select(InventoryRow).where(
                InventoryRow.player_id == p.id, InventoryRow.item_id == pudding.id
            )
        ).first()
    assert kept is not None and kept.qty == 1  # not wasted


def test_use_the_pudding_matches_in_combat_with_article():
    # SEV-2 (matching): "use the pudding" must work mid-fight (token match).
    _give_item("heal-match", "suspicious-pudding")
    graph = build_graph(MemorySaver())
    _turn(graph, "heal-match", "attack the supervisor")
    with get_db_session() as db:
        p = db.scalars(select(Player).where(Player.session_id == "heal-match")).first()
        p.hp = 5
        db.flush()

    _turn(graph, "heal-match", "use the pudding")  # article + partial name

    with get_db_session() as db:
        p = db.scalars(select(Player).where(Player.session_id == "heal-match")).first()
        pudding = db.scalars(select(Item).where(Item.slug == "suspicious-pudding")).one()
        gone = db.scalars(
            select(InventoryRow).where(
                InventoryRow.player_id == p.id, InventoryRow.item_id == pudding.id
            )
        ).first()
    assert gone is None  # matched + consumed -> healing applied


def test_flee_restores_the_monster_to_the_room():
    # SEV-2 (fled monster deleted): fleeing must not delete the monster forever.
    with get_db_session() as db:
        player = provision_new_player(db, "flee-restore")
        level = current_level(db, player)
        room = current_room(db, level, player.pos_x, player.pos_y)
        room.contents["monsters"] = ["form-filler"]
        db.flush()
        room_id = room.id

    graph = build_graph(MemorySaver())
    _turn(graph, "flee-restore", "attack the form-filler")
    with get_db_session() as db:  # cleared from the room while fighting
        assert db.get(Room, room_id).contents.get("monsters") == []

    for _ in range(25):
        if not _snapshot("flee-restore")["active"]:
            break
        _turn(graph, "flee-restore", "flee")

    assert _snapshot("flee-restore")["active"] is False
    with get_db_session() as db:
        assert "form-filler" in db.get(Room, room_id).contents.get("monsters", [])


# --- API: /state exposes combat -------------------------------------------
def test_state_endpoint_reports_active_combat():
    client = TestClient(app)
    sid = client.post("/api/session").json()["session_id"]
    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == sid)).first()
        level = current_level(db, player)
        room = current_room(db, level, player.pos_x, player.pos_y)
        room.contents["monsters"] = ["shift-supervisor"]  # tough: survives round 1
        db.flush()

    client.post(f"/api/session/{sid}/turn", json={"message": "attack the supervisor"})
    state = client.get(f"/api/session/{sid}/state").json()
    assert state["combat"] is not None
    assert state["combat"]["monster"] == "Shift Supervisor"
    assert 0 < state["combat"]["monster_hp"] <= 14
    assert state["combat"]["round"] >= 1
