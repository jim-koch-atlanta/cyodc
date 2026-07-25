"""Deterministic level materialization + shared world helpers (M2).

Turns BSP geometry (app/mapgen.py) plus a hand-authored content pack
(app/content/floorNN.json) into DB rows: a Level, its Rooms (decorated with
descriptions + placed items), a starting Player position, and initial fog-of-war.

This is NOT the M4 worldgen *agent* — content here is hand-seeded and picked
deterministically. M4 swaps the content source for an LLM; the DB shape stays.

Also hosts the small world helpers the tools share (current room, exits, reveal).
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from random import Random

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Item, Level, Player, Room, Visibility
from app.mapgen import DungeonMap, generate

CONTENT_DIR = Path(__file__).parent / "content"

DEFAULT_STATS = {"STR": 10, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10}

DIRECTIONS: dict[str, tuple[int, int]] = {
    "north": (0, -1),
    "south": (0, 1),
    "east": (1, 0),
    "west": (-1, 0),
}
_DIR_ALIASES = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "up": "north", "down": "south", "left": "west", "right": "east",
}


def normalize_direction(raw: str) -> str:
    d = " ".join(raw.lower().split())
    for prefix in ("go ", "move ", "head ", "walk ", "to the ", "to "):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.strip()
    return _DIR_ALIASES.get(d, d)


@functools.lru_cache(maxsize=None)
def load_floor_content(floor: int) -> dict:
    """Load a floor's content pack; fall back to floor 1 (only pack that exists in M2)."""
    path = CONTENT_DIR / f"floor{floor:02d}.json"
    if not path.exists():
        path = CONTENT_DIR / "floor01.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _level_seed(session_id: str, floor: int) -> int:
    # Stable across processes (unlike hash()), unique per (player, floor).
    digest = hashlib.sha256(f"{session_id}:{floor}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


# --- world queries shared with the tools -----------------------------------
def current_level(session: Session, player: Player) -> Level:
    return session.scalars(
        select(Level).where(Level.player_id == player.id, Level.floor == player.floor)
    ).one()


def load_map(level: Level) -> DungeonMap:
    return DungeonMap.from_dict(level.grid)


def current_room(session: Session, level: Level, x: int, y: int) -> Room | None:
    for room in session.scalars(select(Room).where(Room.level_id == level.id)).all():
        if room.contains(x, y):
            return room
    return None


def exits_from(dm: DungeonMap, x: int, y: int) -> list[str]:
    return [name for name, (dx, dy) in DIRECTIONS.items() if dm.is_floor(x + dx, y + dy)]


def _visible_cells(dm: DungeonMap, x: int, y: int) -> set[tuple[int, int]]:
    """The room you stand in (if any), plus a torch-radius patch around you."""
    cells: set[tuple[int, int]] = set()
    room = dm.room_at(x, y)
    if room is not None:
        for yy in range(room.y, room.y + room.h):
            for xx in range(room.x, room.x + room.w):
                cells.add((xx, yy))
    for dx in range(-1, 2):
        for dy in range(-1, 2):
            if dm.is_floor(x + dx, y + dy):
                cells.add((x + dx, y + dy))
    return cells


def reveal(session: Session, vis: Visibility, dm: DungeonMap, x: int, y: int) -> None:
    seen = {tuple(cell) for cell in vis.explored}
    seen |= _visible_cells(dm, x, y)
    vis.explored = sorted([list(cell) for cell in seen])
    session.flush()


# --- seeding ----------------------------------------------------------------
def ensure_item_catalog(session: Session) -> None:
    """Idempotently upsert the item catalog (keyed by slug)."""
    content = load_floor_content(1)
    existing = {slug for (slug,) in session.execute(select(Item.slug)).all()}
    for entry in content["items"]:
        if entry["slug"] not in existing:
            session.add(
                Item(
                    slug=entry["slug"],
                    name=entry["name"],
                    flavor_text=entry.get("flavor_text", ""),
                    effects=entry.get("effects", {}),
                )
            )
    session.flush()


def _assign_kinds(dm: DungeonMap) -> dict[int, str]:
    """entrance = first room; exit = farthest; one treasure; rest chambers."""
    n = len(dm.rooms)
    if n == 1:
        return {0: "entrance"}
    kinds = {i: "chamber" for i in range(n)}
    kinds[0] = "entrance"
    ex, ey = dm.rooms[0].center()
    farthest = max(
        range(1, n),
        key=lambda i: (dm.rooms[i].cx - ex) ** 2 + (dm.rooms[i].cy - ey) ** 2,
    )
    kinds[farthest] = "exit"
    candidates = [i for i in range(1, n) if i != farthest]
    if candidates:
        treasure = max(candidates, key=lambda i: dm.rooms[i].w * dm.rooms[i].h)
        kinds[treasure] = "treasure"
    return kinds


def build_level(session: Session, player: Player, floor: int) -> Level:
    content = load_floor_content(floor)
    seed = _level_seed(player.session_id, floor)
    dm = generate(seed=seed)
    rng = Random(seed ^ 0x9E3779B9)

    level = Level(
        player_id=player.id,
        floor=floor,
        seed=seed,
        theme=content.get("theme", ""),
        grid=dm.to_dict(),
    )
    session.add(level)
    session.flush()

    kinds = _assign_kinds(dm)
    for idx, rect in enumerate(dm.rooms):
        kind = kinds[idx]
        description = rng.choice(content["rooms"][kind])
        session.add(
            Room(
                level_id=level.id,
                x=rect.x, y=rect.y, w=rect.w, h=rect.h,
                kind=kind,
                description=description,
                contents={"items": [], "monsters": [], "npc": None},
            )
        )
    session.flush()
    _place_items(session, level, rng)
    return level


def _place_items(session: Session, level: Level, rng: Random) -> None:
    rooms = session.scalars(select(Room).where(Room.level_id == level.id)).all()
    by_kind: dict[str, list[Room]] = defaultdict(list)
    for room in rooms:
        by_kind[room.kind].append(room)

    def put(room: Room, slug: str) -> None:
        room.contents["items"] = list(room.contents.get("items", [])) + [slug]

    if by_kind["entrance"]:
        put(by_kind["entrance"][0], "tattered-torch")
    hoard = by_kind["treasure"] or by_kind["chamber"]
    if hoard:
        put(rng.choice(hoard), "dungeon-coins")
        put(rng.choice(hoard), "suspicious-pudding")
    if by_kind["chamber"]:
        put(rng.choice(by_kind["chamber"]), "intake-ration")
    memo_rooms = by_kind["exit"] or by_kind["chamber"] or by_kind["entrance"]
    if memo_rooms:
        put(rng.choice(memo_rooms), "supervisors-memo")
    session.flush()


def provision_new_player(
    session: Session,
    session_id: str,
    name: str = "Contestant",
    char_class: str = "Crawler",
) -> Player:
    """Create a player, seed the catalog, build floor 1, and place them at the entrance."""
    player = Player(
        session_id=session_id,
        name=name,
        char_class=char_class,
        stats=dict(DEFAULT_STATS),
        hp=20, max_hp=20, gold=0, floor=1, pos_x=0, pos_y=0,
    )
    session.add(player)
    session.flush()

    ensure_item_catalog(session)
    level = build_level(session, player, 1)

    start = session.scalars(
        select(Room).where(Room.level_id == level.id, Room.kind == "entrance")
    ).first() or session.scalars(select(Room).where(Room.level_id == level.id)).first()
    player.pos_x, player.pos_y = start.x + start.w // 2, start.y + start.h // 2

    vis = Visibility(player_id=player.id, level_id=level.id, explored=[])
    session.add(vis)
    session.flush()
    reveal(session, vis, load_map(level), player.pos_x, player.pos_y)
    return player


def render_fog_map(
    dm: DungeonMap, explored: set[tuple[int, int]], px: int, py: int
) -> list[str]:
    """ASCII map with fog of war: unexplored cells blank, player marked '@'."""
    rows: list[str] = []
    for y in range(dm.height):
        chars: list[str] = []
        for x in range(dm.width):
            if x == px and y == py:
                chars.append("@")
            elif (x, y) in explored:
                chars.append(dm.grid[y][x])
            else:
                chars.append(" ")
        rows.append("".join(chars))
    return rows
