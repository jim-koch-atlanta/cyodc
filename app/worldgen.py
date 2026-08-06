"""Worldgen: generate a floor's CONTENT (theme, room descriptions, item/monster
placement) to decorate a deterministic BSP skeleton.

Invariants honored:
- The LLM decorates the given rooms; it never invents geometry (we iterate the
  BSP skeleton and pull content by index — the model's room list is advisory).
- The LLM never emits combat/loot numbers: it picks a monster TIER and an item
  EFFECT from fixed menus, and `app/balance.py` turns those into bounded numbers.
- The model returns structured output — via LangChain's with_structured_output,
  which forces a schema-shaped tool call — validated through Pydantic before it
  touches the DB (no "please return JSON" parsing).
- Any failure (stub mode, network, bad JSON, bad values) falls back to
  deterministic content recycled from floor01.json, so descending never breaks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from random import Random

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, field_validator

from app.balance import ITEM_EFFECT_MENU, MONSTER_TIERS, item_effect_for, monster_stats_for
from app.config import get_settings
from app.dungeon import load_floor_content
from app.llm import run_structured
from app.mapgen import DungeonMap

logger = logging.getLogger("cyodc.worldgen")


@dataclass
class FloorContent:
    theme: str
    descriptions: dict[int, str]          # room_index -> description
    item_defs: dict[int, list[dict]]      # room_index -> [{slug, name, flavor_text, effects}]
    monster_slugs: dict[int, list[str]]   # room_index -> [monster slug placed]
    monster_catalog: dict[str, dict]      # slug -> monster def (stored on the level)


# --- validation of the raw LLM output (lenient: coerce, don't reject) --------
class _WGItem(BaseModel):
    name: str = "Oddment"
    flavor: str = ""
    effect: str = "trinket"

    @field_validator("effect")
    @classmethod
    def _known_effect(cls, v: str) -> str:
        return v if v in ITEM_EFFECT_MENU else "trinket"


class _WGMonster(BaseModel):
    name: str = "Thing"
    flavor: str = ""
    tier: str = "normal"

    @field_validator("tier")
    @classmethod
    def _known_tier(cls, v: str) -> str:
        return v if v in MONSTER_TIERS else "normal"


class _WGRoom(BaseModel):
    index: int
    description: str = ""
    items: list[_WGItem] = []
    monsters: list[_WGMonster] = []


class WorldgenOutput(BaseModel):
    theme: str = ""
    theme_blurb: str = ""
    rooms: list[_WGRoom] = []


_DEFAULT_DESC = {
    "entrance": "A cramped landing. The way deeper waits, indifferent to you.",
    "chamber": "A bare stone room, recently emptied of whatever once mattered.",
    "treasure": "A shallow vault, its carved shelves mostly picked clean.",
    "exit": "A terminal corridor ending at a staircase that goes only down.",
}


def _slug_base(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "x"


def _hash6(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:6]


def _item_slug(floor: int, name: str, room_index: int, session_id: str) -> str:
    return f"f{floor}-{_slug_base(name)[:40]}-{_hash6(session_id, floor, room_index, name)}"[:64]


def _monster_slug(floor: int, name: str, room_index: int, session_id: str) -> str:
    return f"f{floor}m-{_slug_base(name)[:30]}-{_hash6(session_id, floor, room_index, name)}"[:64]


def _skeleton_prompt(floor: int, dm: DungeonMap, kinds: dict[int, str]) -> str:
    rooms = []
    for i, rect in enumerate(dm.rooms):
        area = rect.w * rect.h
        size = "small" if area < 30 else ("large" if area > 80 else "medium")
        rooms.append({"index": i, "kind": kinds[i], "size": size})
    return json.dumps({"floor": floor, "rooms": rooms})


def generate_floor_content(
    floor: int, dm: DungeonMap, kinds: dict[int, str], seed: int, session_id: str
) -> FloorContent:
    """Content for a floor. Sonnet when a key is present; deterministic fallback
    otherwise (or on any failure). Callers materialize the result identically."""
    if get_settings().resolved_llm_mode != "anthropic":
        return _fallback_content(floor, dm, kinds, seed, session_id)
    try:
        out = run_structured(
            "worldgen",
            [HumanMessage(content=_skeleton_prompt(floor, dm, kinds))],
            WorldgenOutput,
        )
        return _to_floor_content(out, floor, dm, kinds, session_id)
    except Exception:
        logger.exception("worldgen generation failed on floor %s; using fallback", floor)
        return _fallback_content(floor, dm, kinds, seed, session_id)


def _to_floor_content(
    out: WorldgenOutput, floor: int, dm: DungeonMap, kinds: dict[int, str], session_id: str
) -> FloorContent:
    by_index = {r.index: r for r in out.rooms}
    descriptions: dict[int, str] = {}
    item_defs: dict[int, list[dict]] = {}
    monster_slugs: dict[int, list[str]] = {}
    catalog: dict[str, dict] = {}

    # Iterate the SKELETON (authoritative), not the model's room list.
    for i in range(len(dm.rooms)):
        kind = kinds[i]
        wg = by_index.get(i)
        desc = (wg.description.strip() if wg and wg.description.strip() else "")
        descriptions[i] = desc[:1200] or _DEFAULT_DESC.get(kind, _DEFAULT_DESC["chamber"])

        defs: list[dict] = []
        placed: list[str] = []
        if wg is not None:
            for it in wg.items:
                defs.append({
                    "slug": _item_slug(floor, it.name, i, session_id),
                    "name": (it.name or "Oddment")[:120],
                    "flavor_text": it.flavor[:600],
                    "effects": item_effect_for(it.effect, floor),
                })
            if kind != "entrance":  # entrance stays safe — never spawn monsters there
                for mo in wg.monsters:
                    slug = _monster_slug(floor, mo.name, i, session_id)
                    catalog[slug] = {
                        "slug": slug,
                        "name": (mo.name or "Thing")[:120],
                        "flavor": mo.flavor[:600],
                        **monster_stats_for(mo.tier, floor),
                    }
                    placed.append(slug)
        item_defs[i] = defs
        monster_slugs[i] = placed

    return FloorContent(
        theme=(out.theme.strip() or f"Floor {floor}")[:128],
        descriptions=descriptions,
        item_defs=item_defs,
        monster_slugs=monster_slugs,
        monster_catalog=catalog,
    )


def _fallback_content(
    floor: int, dm: DungeonMap, kinds: dict[int, str], seed: int, session_id: str
) -> FloorContent:
    """Deterministic content recycled from floor01.json for the new geometry.
    Keeps offline/stub descending playable (a fresh layout, familiar contents)."""
    content = load_floor_content(1)
    rng = Random(seed ^ 0xC0FFEE)
    items_by_slug = {it["slug"]: it for it in content["items"]}
    monsters_by_slug = {m["slug"]: m for m in content["monsters"]}

    by_kind: dict[str, list[int]] = defaultdict(list)
    for i in range(len(dm.rooms)):
        by_kind[kinds[i]].append(i)

    descriptions = {
        i: rng.choice(content["rooms"].get(kinds[i]) or content["rooms"]["chamber"])
        for i in range(len(dm.rooms))
    }
    item_defs: dict[int, list[dict]] = {i: [] for i in range(len(dm.rooms))}
    monster_slugs: dict[int, list[str]] = {i: [] for i in range(len(dm.rooms))}
    catalog: dict[str, dict] = {}

    def place_item(idx: int, slug: str) -> None:
        it = items_by_slug[slug]
        item_defs[idx].append({
            "slug": slug, "name": it["name"],
            "flavor_text": it.get("flavor_text", ""), "effects": it.get("effects", {}),
        })

    if by_kind["entrance"]:
        place_item(by_kind["entrance"][0], "tattered-torch")
    hoard = by_kind["treasure"] or by_kind["chamber"]
    if hoard:
        place_item(rng.choice(hoard), "dungeon-coins")
        place_item(rng.choice(hoard), "suspicious-pudding")
    if by_kind["chamber"]:
        place_item(rng.choice(by_kind["chamber"]), "intake-ration")
    memo_rooms = by_kind["exit"] or by_kind["chamber"] or by_kind["entrance"]
    if memo_rooms:
        place_item(rng.choice(memo_rooms), "supervisors-memo")

    mslugs = sorted(monsters_by_slug)
    for i in range(len(dm.rooms)):
        if kinds[i] == "entrance":
            continue
        if kinds[i] == "exit" or rng.random() < 0.5:
            slug = rng.choice(mslugs)
            mdef = monsters_by_slug[slug]
            # Scale by floor via the tier, so deep fallback floors aren't floor-1 easy.
            catalog[slug] = {
                "slug": slug, "name": mdef["name"], "flavor": mdef.get("flavor", ""),
                **monster_stats_for(mdef.get("tier", "normal"), floor),
            }
            monster_slugs[i].append(slug)

    return FloorContent(
        theme=content.get("theme", "The Delve"),
        descriptions=descriptions,
        item_defs=item_defs,
        monster_slugs=monster_slugs,
        monster_catalog=catalog,
    )
