"""Deterministic BSP dungeon generator.

Per architecture invariant #3, geometry is plain Python — no LLM. `generate()`
produces a wall/floor grid, the list of room rectangles, and connecting
corridors, all from a single integer seed: the same seed yields an identical
map (unit-tested). The worldgen agent (M4) later *decorates* rooms; it never
invents geometry.

Grid is row-major: `grid[y][x]`. Cells are WALL or FLOOR.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random

WALL = "#"
FLOOR = "."


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def center(self) -> tuple[int, int]:
        return (self.cx, self.cy)

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.w, self.h]


@dataclass
class DungeonMap:
    width: int
    height: int
    grid: list[str]  # len == height, each row len == width
    rooms: list[Rect]

    def is_floor(self, x: int, y: int) -> bool:
        if 0 <= x < self.width and 0 <= y < self.height:
            return self.grid[y][x] == FLOOR
        return False

    def room_at(self, x: int, y: int) -> Rect | None:
        for room in self.rooms:
            if room.contains(x, y):
                return room
        return None

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "grid": self.grid,
            "rooms": [r.as_list() for r in self.rooms],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DungeonMap":
        return cls(
            width=data["width"],
            height=data["height"],
            grid=list(data["grid"]),
            rooms=[Rect(*r) for r in data["rooms"]],
        )


def generate(
    width: int = 48,
    height: int = 32,
    seed: int = 0,
    *,
    min_leaf: int = 10,
    max_depth: int = 5,
    min_room: int = 4,
    margin: int = 1,
) -> DungeonMap:
    """Generate a connected BSP dungeon. Deterministic in `seed`."""
    rng = Random(seed)
    cells = [[WALL] * width for _ in range(height)]

    # Leave a 1-cell wall border around the whole map.
    root = Rect(1, 1, width - 2, height - 2)
    leaves = _split(root, rng, min_leaf, max_depth)

    rooms: list[Rect] = []
    for leaf in leaves:
        room = _place_room(leaf, rng, min_room, margin)
        if room is not None:
            rooms.append(room)
            _carve_room(cells, room)

    # Connect rooms in leaf order — a path visiting every room guarantees the
    # whole dungeon is reachable (verified by the connectivity unit test).
    for a, b in zip(rooms, rooms[1:]):
        _carve_corridor(cells, a.center(), b.center(), rng)

    grid = ["".join(row) for row in cells]
    return DungeonMap(width=width, height=height, grid=grid, rooms=rooms)


def _split(
    region: Rect, rng: Random, min_leaf: int, max_depth: int, depth: int = 0
) -> list[Rect]:
    can_split_x = region.w >= 2 * min_leaf
    can_split_y = region.h >= 2 * min_leaf
    if depth >= max_depth or not (can_split_x or can_split_y):
        return [region]

    if can_split_x and can_split_y:
        # Prefer splitting the noticeably longer axis; otherwise pick randomly.
        if region.w >= int(region.h * 1.25):
            split_x = True
        elif region.h >= int(region.w * 1.25):
            split_x = False
        else:
            split_x = rng.random() < 0.5
    else:
        split_x = can_split_x

    if split_x:
        cut = rng.randint(min_leaf, region.w - min_leaf)
        left = Rect(region.x, region.y, cut, region.h)
        right = Rect(region.x + cut, region.y, region.w - cut, region.h)
        children = (left, right)
    else:
        cut = rng.randint(min_leaf, region.h - min_leaf)
        top = Rect(region.x, region.y, region.w, cut)
        bottom = Rect(region.x, region.y + cut, region.w, region.h - cut)
        children = (top, bottom)

    result: list[Rect] = []
    for child in children:
        result.extend(_split(child, rng, min_leaf, max_depth, depth + 1))
    return result


def _place_room(leaf: Rect, rng: Random, min_room: int, margin: int) -> Rect | None:
    max_w = leaf.w - 2 * margin
    max_h = leaf.h - 2 * margin
    if max_w < min_room or max_h < min_room:
        return None
    w = rng.randint(min_room, max_w)
    h = rng.randint(min_room, max_h)
    x = leaf.x + margin + rng.randint(0, max_w - w)
    y = leaf.y + margin + rng.randint(0, max_h - h)
    return Rect(x, y, w, h)


def _carve_room(cells: list[list[str]], room: Rect) -> None:
    for y in range(room.y, room.y + room.h):
        for x in range(room.x, room.x + room.w):
            cells[y][x] = FLOOR


def _carve_corridor(
    cells: list[list[str]], a: tuple[int, int], b: tuple[int, int], rng: Random
) -> None:
    (x1, y1), (x2, y2) = a, b
    if rng.random() < 0.5:
        _h_line(cells, x1, x2, y1)
        _v_line(cells, y1, y2, x2)
    else:
        _v_line(cells, y1, y2, x1)
        _h_line(cells, x1, x2, y2)


def _h_line(cells: list[list[str]], x1: int, x2: int, y: int) -> None:
    for x in range(min(x1, x2), max(x1, x2) + 1):
        cells[y][x] = FLOOR


def _v_line(cells: list[list[str]], y1: int, y2: int, x: int) -> None:
    for y in range(min(y1, y2), max(y1, y2) + 1):
        cells[y][x] = FLOOR
