"""story_log RAG: recording, idempotent writes, and filtered recall."""

from __future__ import annotations

from sqlalchemy import func, select

from app import memory
from app.db.base import get_db_session
from app.db.models import Player, StoryLog
from app.dungeon import provision_new_player


def _rows(db, player_id: int) -> int:
    return db.scalar(select(func.count()).select_from(StoryLog).where(StoryLog.player_id == player_id))


def test_record_then_recall_finds_the_relevant_beat():
    with get_db_session() as db:
        player = provision_new_player(db, "mem-recall")
        memory.record_turn(db, player, 1, "dm", "You slay the Form-Filler in the sulfur chamber.")
        memory.record_turn(db, player, 2, "dm", "Brindle Mox sells you a healing poultice.")
        memory.record_turn(db, player, 3, "dm", "A draft rises from the staircase down.")

        hits = memory.recall(db, player, "where did I buy the poultice", exclude_recent=0, k=1)
        assert hits and "poultice" in hits[0].content.lower()


def test_record_is_idempotent_on_player_turn_role():
    with get_db_session() as db:
        player = provision_new_player(db, "mem-idem")
        memory.record_turn(db, player, 7, "dm", "The same beat, logged twice.")
        memory.record_turn(db, player, 7, "dm", "The same beat, logged twice.")  # replay
        assert _rows(db, player.id) == 1
        # a different role at the same turn is a distinct beat (player input + dm)
        memory.record_turn(db, player, 7, "player", "look around")
        assert _rows(db, player.id) == 2


def test_empty_content_is_not_recorded():
    with get_db_session() as db:
        player = provision_new_player(db, "mem-empty")
        memory.record_turn(db, player, 1, "dm", "   ")
        assert _rows(db, player.id) == 0


def test_tagged_recall_isolates_one_entity():
    with get_db_session() as db:
        player = provision_new_player(db, "mem-tags")
        memory.record_turn(db, player, 1, "npc", "Mox: back again, are you.", tag="brindle_mox")
        memory.record_turn(db, player, 2, "npc", "A different clerk grumbles about forms.", tag="other_clerk")
        memory.record_turn(db, player, 3, "dm", "You wander a corridor.")

        hits = memory.recall(db, player, "the merchant remembers me", tag="brindle_mox", k=5)
        assert hits and all(r.tag == "brindle_mox" for r in hits)


def test_recall_never_compares_across_embedding_backends():
    with get_db_session() as db:
        player = provision_new_player(db, "mem-backend")
        # A row from some *other* embedding backend must never be retrieved by the
        # active (hash-256) backend, even if its text is a perfect match.
        db.add(StoryLog(
            player_id=player.id, turn=1, role="dm",
            content="the poultice you seek is right here",
            embedding=[0.0] * 256, embed_model="some-other-model",
        ))
        db.flush()
        memory.record_turn(db, player, 2, "dm", "an unrelated wall of stone")

        hits = memory.recall(db, player, "the poultice you seek", exclude_recent=0, k=5)
        assert all(r.embed_model == "hash-256" for r in hits)


def test_recent_window_is_excluded_from_similarity():
    with get_db_session() as db:
        player = provision_new_player(db, "mem-recent")
        memory.record_turn(db, player, 1, "dm", "a very memorable poultice moment")
        # With a wide exclusion, the only (recent) row is held out -> nothing older.
        assert memory.recall(db, player, "poultice", exclude_recent=24) == []
