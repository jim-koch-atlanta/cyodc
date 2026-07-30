"""Unit tests for the BSP dungeon generator (deterministic — no LLM, no DB)."""

from __future__ import annotations

from app.mapgen import FLOOR, DungeonMap, Rect, generate


def _reachable_floor(dm: DungeonMap, start: tuple[int, int]) -> set[tuple[int, int]]:
    """Flood-fill floor cells reachable from `start` (4-connected)."""
    seen: set[tuple[int, int]] = set()
    stack = [start]
    while stack:
        x, y = stack.pop()
        if (x, y) in seen or not dm.is_floor(x, y):
            continue
        seen.add((x, y))
        stack.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])
    return seen


def test_is_deterministic_for_a_seed():
    a = generate(seed=42)
    b = generate(seed=42)
    assert a.grid == b.grid
    assert a.rooms == b.rooms


def test_different_seeds_differ():
    assert generate(seed=1).grid != generate(seed=2).grid


def test_grid_dimensions_and_border_walls():
    dm = generate(width=48, height=32, seed=3)
    assert len(dm.grid) == 32
    assert all(len(row) == 48 for row in dm.grid)
    # 1-cell wall border all around.
    assert all(dm.grid[0][x] == "#" for x in range(dm.width))
    assert all(dm.grid[dm.height - 1][x] == "#" for x in range(dm.width))
    assert all(row[0] == "#" and row[-1] == "#" for row in dm.grid)


def test_produces_multiple_rooms_in_bounds():
    dm = generate(seed=5)
    assert len(dm.rooms) >= 2
    for r in dm.rooms:
        assert r.x >= 1 and r.y >= 1
        assert r.x + r.w <= dm.width - 1
        assert r.y + r.h <= dm.height - 1


def test_room_interiors_are_floor():
    dm = generate(seed=9)
    for r in dm.rooms:
        assert dm.is_floor(r.cx, r.cy)
        assert dm.is_floor(r.x, r.y)


def test_all_rooms_are_connected():
    # A player starting in room 0 must be able to reach every other room.
    for seed in range(6):
        dm = generate(seed=seed)
        reach = _reachable_floor(dm, dm.rooms[0].center())
        for r in dm.rooms:
            assert r.center() in reach, f"room {r} unreachable at seed {seed}"


def test_serialization_round_trips():
    dm = generate(seed=11)
    restored = DungeonMap.from_dict(dm.to_dict())
    assert restored.grid == dm.grid
    assert restored.rooms == dm.rooms
    assert restored.width == dm.width and restored.height == dm.height


def test_rect_contains_and_room_at():
    dm = generate(seed=13)
    r = dm.rooms[0]
    assert r.contains(r.cx, r.cy)
    assert not r.contains(r.x + r.w, r.y)  # right edge is exclusive
    assert dm.room_at(r.cx, r.cy) is r
