"""Worldgen: code-owned balance, LLM-output validation/clamping, and fallback.

The LLM never sets numbers — it picks a tier/effect from a menu and the engine
turns that into bounded stats. These tests pin that boundary.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

import app.worldgen as worldgen_mod
from app.balance import item_effect_for, monster_stats_for
from app.dungeon import _assign_kinds
from app.mapgen import generate
from app.worldgen import WorldgenOutput, _to_floor_content, generate_floor_content


# --- balance ---------------------------------------------------------------
def test_monster_stats_scale_and_stay_bounded():
    weak1 = monster_stats_for("weak", 1)
    assert weak1["hp"] == 6 and weak1["ac"] == 9 and weak1["damage_dice"] == "1d4"

    deep = monster_stats_for("elite", 50)
    assert deep["hp"] <= 300 and deep["ac"] <= 20 and deep["attack_bonus"] <= 12
    # deeper floors are at least as tough
    assert monster_stats_for("normal", 5)["hp"] > monster_stats_for("normal", 1)["hp"]


def test_unknown_tier_falls_back_to_normal():
    assert monster_stats_for("godlike", 1) == monster_stats_for("normal", 1)


def test_item_effects_are_bounded_by_menu_and_floor():
    assert item_effect_for("minor_heal", 1) == {"heal": 8}
    assert item_effect_for("light", 1) == {"light": True}
    assert item_effect_for("trinket", 1) == {}
    assert item_effect_for("large_coins", 100)["gold"] <= 300  # clamp holds


# --- validation of raw LLM output ------------------------------------------
def test_worldgen_output_coerces_bad_effect_and_tier():
    out = WorldgenOutput.model_validate({
        "theme": "T",
        "rooms": [{
            "index": 0, "description": "d",
            "items": [{"name": "X", "flavor": "f", "effect": "wish_for_infinite_gold"}],
            "monsters": [{"name": "Y", "flavor": "g", "tier": "demigod"}],
        }],
    })
    assert out.rooms[0].items[0].effect == "trinket"  # unknown -> safe default
    assert out.rooms[0].monsters[0].tier == "normal"


def _skeleton():
    dm = generate(seed=7)
    return dm, _assign_kinds(dm)


def test_to_floor_content_maps_numbers_and_keeps_entrance_safe():
    dm, kinds = _skeleton()
    entrance_idx = next(i for i, k in kinds.items() if k == "entrance")
    exit_idx = next(i for i, k in kinds.items() if k == "exit")

    out = WorldgenOutput.model_validate({
        "theme": "Test Floor",
        "rooms": [
            # the model tries to put a monster in the (safe) entrance
            {"index": entrance_idx, "description": "the door",
             "monsters": [{"name": "Sneaky", "flavor": "boo", "tier": "elite"}]},
            {"index": exit_idx, "description": "the stairs",
             "monsters": [{"name": "Guard", "flavor": "halt", "tier": "tough"}],
             "items": [{"name": "Potion", "flavor": "glug", "effect": "major_heal"}]},
        ],
    })
    content = _to_floor_content(out, floor=3, dm=dm, kinds=kinds, session_id="s")

    # every skeleton room is covered
    assert set(content.descriptions) == set(range(len(dm.rooms)))
    # entrance stays safe despite the model
    assert content.monster_slugs[entrance_idx] == []
    # the exit guard exists with CONCRETE, code-owned stats (not from the model)
    guard_slug = content.monster_slugs[exit_idx][0]
    guard = content.monster_catalog[guard_slug]
    assert guard["hp"] == monster_stats_for("tough", 3)["hp"]
    assert "damage_dice" in guard and isinstance(guard["hp"], int)
    # the item got a concrete, bounded effect from the menu
    potion = content.item_defs[exit_idx][0]
    assert potion["effects"] == item_effect_for("major_heal", 3)


# --- generate_floor_content: LLM path, failure, and stub fallback ----------
def test_generate_uses_llm_when_available(monkeypatch):
    dm, kinds = _skeleton()

    class _AnthropicSettings:
        resolved_llm_mode = "anthropic"

    payload = {"theme": "Neon Ossuary", "rooms": [
        {"index": i, "description": f"room {i} of bone"} for i in range(len(dm.rooms))
    ]}
    monkeypatch.setattr(worldgen_mod, "get_settings", lambda: _AnthropicSettings())
    monkeypatch.setattr(worldgen_mod, "run_agent", lambda role, msgs: AIMessage(content=json.dumps(payload)))

    content = generate_floor_content(2, dm, kinds, seed=5, session_id="s")
    assert content.theme == "Neon Ossuary"
    assert "bone" in content.descriptions[0]


def test_generate_falls_back_on_bad_llm_output(monkeypatch):
    dm, kinds = _skeleton()

    class _AnthropicSettings:
        resolved_llm_mode = "anthropic"

    monkeypatch.setattr(worldgen_mod, "get_settings", lambda: _AnthropicSettings())
    monkeypatch.setattr(worldgen_mod, "run_agent", lambda role, msgs: AIMessage(content="not json at all"))

    content = generate_floor_content(2, dm, kinds, seed=5, session_id="s")  # must not raise
    assert set(content.descriptions) == set(range(len(dm.rooms)))  # fallback covered every room


def test_generate_stub_mode_uses_fallback():
    # conftest forces stub mode -> deterministic floor01-recycled content.
    dm, kinds = _skeleton()
    content = generate_floor_content(2, dm, kinds, seed=5, session_id="s")
    assert content.theme  # populated
    assert set(content.descriptions) == set(range(len(dm.rooms)))
    # fallback places at least one monster (exit is guarded) with concrete stats
    all_slugs = [s for slugs in content.monster_slugs.values() for s in slugs]
    assert all_slugs and all("hp" in content.monster_catalog[s] for s in all_slugs)


def test_fallback_scales_monster_stats_by_floor():
    # BUG-2: stub/fallback monsters must scale by depth (via their tier), not
    # stay floor-1 easy.
    from app.balance import monster_stats_for
    from app.dungeon import load_monster_catalog

    dm, kinds = _skeleton()
    content = generate_floor_content(3, dm, kinds, seed=5, session_id="s")
    catalog1 = load_monster_catalog(1)
    for slug, mdef in content.monster_catalog.items():
        tier = catalog1[slug]["tier"]
        assert mdef["hp"] == monster_stats_for(tier, 3)["hp"]  # scaled to this floor
    # at least one is tougher than its floor-1 hand-seeded value
    assert any(m["hp"] > catalog1[s]["hp"] for s, m in content.monster_catalog.items())


def test_geometry_is_independent_of_content():
    # Worldgen never touches geometry: same seed -> identical BSP every time.
    assert generate(seed=13).grid == generate(seed=13).grid
