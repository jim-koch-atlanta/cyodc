"""Level transitions through the graph + generated-floor combat (stub mode)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select

from app.db.base import get_db_session
from app.db.models import Level, Player, Room
from app.dungeon import current_level, current_room, provision_new_player
from app.encounters import active_encounter
from app.graph import build_graph
from app.main import app


def _turn(graph, session_id: str, text: str) -> str:
    cfg = {"configurable": {"thread_id": session_id}}
    return graph.invoke({"messages": [HumanMessage(content=text)]}, cfg)["messages"][-1].content


def _provision_on_exit(session_id: str, guard: str | None = None) -> None:
    # Set up the exit room's guard in the SAME session as provisioning — a
    # separate session's write isn't reliably visible to the graph's next
    # session under the SQLite test pool (a harness quirk, not a game path).
    with get_db_session() as db:
        player = provision_new_player(db, session_id)
        level = current_level(db, player)
        exit_room = db.scalars(
            select(Room).where(Room.level_id == level.id, Room.kind == "exit")
        ).first()
        exit_room.contents["monsters"] = [guard] if guard else []
        player.pos_x = exit_room.x + exit_room.w // 2
        player.pos_y = exit_room.y + exit_room.h // 2
        db.flush()


def _player_floor(session_id: str) -> int:
    with get_db_session() as db:
        return db.scalars(select(Player).where(Player.session_id == session_id)).first().floor


def test_descend_generates_the_next_floor_and_places_the_player():
    _provision_on_exit("descend1")
    graph = build_graph(MemorySaver())
    reply = _turn(graph, "descend1", "take the stairs down")
    assert "floor 2" in reply.lower()

    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == "descend1")).first()
        assert player.floor == 2
        level2 = db.scalars(
            select(Level).where(Level.player_id == player.id, Level.floor == 2)
        ).first()
        assert level2 is not None and level2.monster_catalog is not None
        entrance = db.scalars(
            select(Room).where(Room.level_id == level2.id, Room.kind == "entrance")
        ).first()
        assert entrance.contains(player.pos_x, player.pos_y)  # placed at the new entrance


def test_descend_is_refused_away_from_the_exit():
    with get_db_session() as db:
        provision_new_player(db, "noexit")  # starts at the entrance, not the exit
    graph = build_graph(MemorySaver())
    _turn(graph, "noexit", "take the stairs down")
    assert _player_floor("noexit") == 1  # unchanged


def test_cannot_skip_floors_by_descending_twice():
    _provision_on_exit("skip")
    graph = build_graph(MemorySaver())
    _turn(graph, "skip", "descend")  # -> floor 2 (now at the floor-2 entrance)
    _turn(graph, "skip", "descend")  # from an entrance -> refused, no floor 3

    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == "skip")).first()
        floors = sorted(
            row for (row,) in db.execute(
                select(Level.floor).where(Level.player_id == player.id)
            ).all()
        )
    assert player.floor == 2 and floors == [1, 2]


def test_combat_works_on_a_generated_floor():
    # Proves the per-level monster_catalog resolves in start_combat (the M4 defect fix).
    _provision_on_exit("gencombat")
    graph = build_graph(MemorySaver())
    _turn(graph, "gencombat", "descend")  # -> floor 2

    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == "gencombat")).first()
        level2 = current_level(db, player)
        slug = next(iter(level2.monster_catalog.keys()))
        room = current_room(db, level2, player.pos_x, player.pos_y)
        room.contents["monsters"] = [slug]  # QA: drop a floor-2 monster where we stand
        db.flush()

    _turn(graph, "gencombat", "attack")  # start_combat must resolve slug via the level catalog
    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == "gencombat")).first()
        assert active_encounter(db, player.id) is not None


def test_guarded_exit_blocks_descent_until_the_guard_is_defeated():
    # BUG-1: a monster on the stairs blocks descent; defeating it (which clears it
    # from the room) opens the way. Uses the real combat flow, not a manual clear.
    _provision_on_exit("guarded", guard="form-filler")

    graph = build_graph(MemorySaver())
    _turn(graph, "guarded", "take the stairs down")
    assert _player_floor("guarded") == 1  # blocked by the guard

    _turn(graph, "guarded", "attack the form-filler")  # engage the guard
    for _ in range(8):  # fight it to the death (combat clears it from the room)
        with get_db_session() as db:
            player = db.scalars(select(Player).where(Player.session_id == "guarded")).first()
            if active_encounter(db, player.id) is None:
                break
        _turn(graph, "guarded", "attack")

    _turn(graph, "guarded", "take the stairs down")
    assert _player_floor("guarded") == 2  # guard defeated -> descent allowed


def test_state_reports_can_descend_on_the_exit():
    client = TestClient(app)
    sid = client.post("/api/session").json()["session_id"]
    with get_db_session() as db:
        player = db.scalars(select(Player).where(Player.session_id == sid)).first()
        level = current_level(db, player)
        exit_room = db.scalars(
            select(Room).where(Room.level_id == level.id, Room.kind == "exit")
        ).first()
        exit_room.contents["monsters"] = []  # unguarded stairs
        player.pos_x = exit_room.x + exit_room.w // 2
        player.pos_y = exit_room.y + exit_room.h // 2
        db.flush()

    state = client.get(f"/api/session/{sid}/state").json()
    assert state["room"] == "exit" and state["can_descend"] is True
