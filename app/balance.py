"""Code-owned combat/loot balance (invariant #3: the engine owns the numbers).

The worldgen LLM only picks a monster TIER and an item EFFECT from fixed menus;
this module turns those choices into concrete, bounded values scaled by floor
depth. The model never emits a number, so it can't hand a floor-1 crawler an
elite's damage or a 9999-gold trinket.
"""

from __future__ import annotations

MONSTER_TIERS = ("weak", "normal", "tough", "elite")
ITEM_EFFECT_MENU = (
    "minor_heal",
    "major_heal",
    "small_coins",
    "large_coins",
    "light",
    "trinket",
)

# Floor-1 reference stat blocks (roughly the hand-authored M3 monsters).
_TIER_BASE = {
    "weak":   {"hp": 6,  "ac": 9,  "attack_bonus": 1, "die": 4, "mod": 0, "xp": 8,  "gold": 3},
    "normal": {"hp": 11, "ac": 11, "attack_bonus": 3, "die": 6, "mod": 0, "xp": 15, "gold": 6},
    "tough":  {"hp": 16, "ac": 12, "attack_bonus": 4, "die": 6, "mod": 2, "xp": 25, "gold": 12},
    "elite":  {"hp": 24, "ac": 14, "attack_bonus": 5, "die": 8, "mod": 2, "xp": 40, "gold": 20},
}


def monster_stats_for(tier: str, floor: int) -> dict:
    """Concrete stat block for a tier at a given floor. Bounded, deterministic."""
    base = _TIER_BASE.get(tier, _TIER_BASE["normal"])
    step = max(0, floor - 1)
    dmg_mod = base["mod"] + step // 2
    damage_dice = f"1d{base['die']}" + (f"+{dmg_mod}" if dmg_mod else "")
    return {
        "hp": min(base["hp"] + step * (2 + base["hp"] // 8), 300),
        "ac": min(base["ac"] + step // 3, 20),
        "attack_bonus": min(base["attack_bonus"] + step // 2, 12),
        "damage_dice": damage_dice,
        "xp": base["xp"] + (step * base["xp"]) // 2,
        "gold": base["gold"] + (step * base["gold"]) // 2,
    }


def item_effect_for(menu: str, floor: int) -> dict:
    """Concrete, clamped effect dict for a menu choice at a given floor."""
    step = max(0, floor - 1)
    if menu == "minor_heal":
        return {"heal": min(8 + step * 2, 40)}
    if menu == "major_heal":
        return {"heal": min(18 + step * 4, 80)}
    if menu == "small_coins":
        return {"gold": min(8 + step * 4, 120)}
    if menu == "large_coins":
        return {"gold": min(20 + step * 8, 300)}
    if menu == "light":
        return {"light": True}
    return {}  # trinket: pure flavor


# Base merchant prices per effect (floor-1 reference). The NPC card may *suggest*
# a price, but the engine owns the number — same principle as monster stats: the
# LLM never emits gold, it only picks the effect and the code prices it.
_WARE_BASE_PRICE = {
    "minor_heal": 12,
    "major_heal": 30,
    "small_coins": 40,
    "large_coins": 120,
    "light": 8,
    "trinket": 5,
}


def buy_price_for(effect: str, floor: int) -> int:
    """Bounded, floor-scaled purchase price for a merchant ware."""
    base = _WARE_BASE_PRICE.get(effect, 10)
    step = max(0, floor - 1)
    return min(base + (base * step) // 4, 999)
