"""SQLAlchemy models — the world-state schema (SPEC.md).

Portability + integrity notes (per schema-guardian review):
- Generic `JSON` (maps to TEXT on SQLite, JSON on Postgres). Mutable dict/list
  columns are wrapped so in-place edits are tracked; tools still prefer
  read-modify-write-absolute for replay-safety.
- `kind` is a `String` + `CheckConstraint`, not `Enum`, to avoid Postgres
  `CREATE TYPE` migration pain.
- FKs cascade from the owning row; `inventory.item_id` is RESTRICT (items are a
  shared catalog). SQLite FK enforcement is turned on in `db/base.py`.
- Levels are per-player (v1 is single-player / no shared world), keyed
  UNIQUE(player_id, floor); fog-of-war is per (player_id, level_id).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# JSON columns whose contents get mutated in place get change-tracking wrappers.
JSONDict = MutableDict.as_mutable(JSON)
JSONList = MutableList.as_mutable(JSON)

ROOM_KINDS = ("entrance", "chamber", "treasure", "exit")
_ROOM_KIND_SQL = "kind IN (" + ", ".join(f"'{k}'" for k in ROOM_KINDS) + ")"


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    # session_id == LangGraph thread_id: the one bridge between a session and
    # world state. Nothing else links them.
    session_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    char_class: Mapped[str] = mapped_column("char_class", String(32), nullable=False)
    stats: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    hp: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    max_hp: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    gold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    floor: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    pos_x: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pos_y: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    inventory: Mapped[list["InventoryRow"]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )
    # At most one active fight per player (UNIQUE on encounters.player_id).
    encounter: Mapped["Encounter | None"] = relationship(
        back_populates="player", cascade="all, delete-orphan", uselist=False
    )


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (Index("ix_items_player_id", "player_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    flavor_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    effects: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # NULL = shared hand-seeded catalog (RESTRICT-protected). Non-NULL = a
    # worldgen-generated item owned by that player; cascades on player delete
    # (so generated items don't accumulate forever).
    player_id: Mapped[int | None] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=True, default=None
    )


class InventoryRow(Base):
    __tablename__ = "inventory"
    __table_args__ = (
        UniqueConstraint("player_id", "item_id", name="uq_inventory_player_item"),
        CheckConstraint("qty > 0", name="ck_inventory_qty_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT: items are a shared catalog; don't delete one that's held.
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    player: Mapped["Player"] = relationship(back_populates="inventory")
    item: Mapped["Item"] = relationship()


class Level(Base):
    __tablename__ = "levels"
    __table_args__ = (
        UniqueConstraint("player_id", "floor", name="uq_levels_player_floor"),
        Index("ix_levels_player_floor", "player_id", "floor"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    floor: Mapped[int] = mapped_column(Integer, nullable=False)
    # BigInteger: _level_seed() takes 4 bytes of a SHA-256 digest (up to
    # 0xFFFFFFFF), which overflows Postgres INTEGER (signed 32-bit).
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    theme: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    grid: Mapped[dict] = mapped_column(JSON, nullable=False)  # DungeonMap.to_dict()
    # NULL for hand-seeded floor 1; {slug: monster_def} for worldgen floors (2+),
    # so combat can resolve monster stats on any floor without a static file.
    monster_catalog: Mapped[dict | None] = mapped_column(
        JSONDict, nullable=True, default=None
    )

    rooms: Mapped[list["Room"]] = relationship(
        back_populates="level", cascade="all, delete-orphan"
    )


class Room(Base):
    __tablename__ = "rooms"
    __table_args__ = (
        CheckConstraint(_ROOM_KIND_SQL, name="ck_rooms_kind"),
        Index("ix_rooms_level_id", "level_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    level_id: Mapped[int] = mapped_column(
        ForeignKey("levels.id", ondelete="CASCADE"), nullable=False
    )
    x: Mapped[int] = mapped_column(Integer, nullable=False)
    y: Mapped[int] = mapped_column(Integer, nullable=False)
    w: Mapped[int] = mapped_column(Integer, nullable=False)
    h: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # {"items": [slug, ...], "monsters": [...], "npc": null}
    contents: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    level: Mapped["Level"] = relationship(back_populates="rooms")

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h


class Visibility(Base):
    __tablename__ = "visibility"
    __table_args__ = (
        UniqueConstraint("player_id", "level_id", name="uq_visibility_player_level"),
        Index("ix_visibility_player_level", "player_id", "level_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    level_id: Mapped[int] = mapped_column(
        ForeignKey("levels.id", ondelete="CASCADE"), nullable=False
    )
    explored: Mapped[list] = mapped_column(JSONList, nullable=False, default=list)


class Encounter(Base):
    """Active combat state. The row exists iff a fight is in progress; it is
    deleted (atomically with awarding rewards) when the fight ends — which is
    also the replay gate that keeps victory rewards from double-applying."""

    __tablename__ = "encounters"
    __table_args__ = (
        UniqueConstraint("player_id", name="uq_encounters_player"),
        Index("ix_encounters_player_id", "player_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    # Snapshot of the catalog entry at combat start:
    # {slug, name, max_hp, ac, attack_bonus, damage_dice, xp, gold}
    monster: Mapped[dict] = mapped_column(JSON, nullable=False)
    monster_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Per-round dice derive from (seed, round) — reproducible across a disconnect.
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn_order: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    player: Mapped["Player"] = relationship(back_populates="encounter")
