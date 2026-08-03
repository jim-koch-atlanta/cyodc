"""Deterministic combat engine (invariant #3: the engine decides, the LLM narrates).

Pure Python, no DB, no LLM. Everything is driven by a seeded `random.Random`, so a
round is perfectly reproducible from `(encounter seed, round number)` — which is
how combat survives a disconnect without persisting RNG internal state.

The engine resolves ONE round and returns structured `events`; the combat node
turns those into prose. It never reads or writes game state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from random import Random

_DICE_RE = re.compile(r"^\s*(\d+)\s*d\s*(\d+)\s*([+-]\s*\d+)?\s*$", re.IGNORECASE)


@dataclass
class Combatant:
    name: str
    hp: int
    max_hp: int
    ac: int
    attack_bonus: int
    damage_dice: str
    initiative_bonus: int = 0


Outcome = str  # "ongoing" | "victory" | "defeat" | "fled"


@dataclass
class RoundResult:
    round: int
    action: str  # "attack" | "flee" | "pass"
    events: list[dict]
    player_hp: int
    monster_hp: int
    outcome: Outcome = "ongoing"


def parse_dice(notation: str) -> tuple[int, int, int]:
    """"NdM+K" -> (N, M, K). Raises ValueError on bad notation."""
    m = _DICE_RE.match(notation or "")
    if not m:
        raise ValueError(f"bad dice notation: {notation!r}")
    n, sides = int(m.group(1)), int(m.group(2))
    mod = int(m.group(3).replace(" ", "")) if m.group(3) else 0
    if n < 1 or sides < 1:
        raise ValueError(f"bad dice notation: {notation!r}")
    return n, sides, mod


def roll_dice(notation: str, rng: Random, *, crit: bool = False) -> int:
    """Roll NdM+K. On a crit the dice (not the modifier) are rolled twice.

    Result is floored at 1 — a connecting hit always stings at least a little.
    """
    n, sides, mod = parse_dice(notation)
    dice = 2 * n if crit else n
    total = sum(rng.randint(1, sides) for _ in range(dice)) + mod
    return max(1, total)


def d20(rng: Random) -> int:
    return rng.randint(1, 20)


def rng_for_round(seed: int, round_num: int) -> Random:
    """A fresh, independent RNG per round — deterministic in (seed, round)."""
    return Random(seed ^ (round_num * 0x9E3779B9))


def roll_initiative(player: Combatant, monster: Combatant, rng: Random) -> list[str]:
    """Return the turn order, e.g. ["player", "monster"]. Player wins ties."""
    p = d20(rng) + player.initiative_bonus
    m = d20(rng) + monster.initiative_bonus
    return ["player", "monster"] if p >= m else ["monster", "player"]


def make_attack(attacker: Combatant, defender: Combatant, rng: Random) -> dict:
    """One attack. Nat 20 auto-hits and crits; nat 1 auto-misses.

    Mutates the defender's hp. Returns a structured event (the narrator's input).
    """
    roll = d20(rng)
    crit = roll == 20
    auto_miss = roll == 1
    hit = crit or (not auto_miss and roll + attacker.attack_bonus >= defender.ac)

    damage = 0
    if hit:
        damage = roll_dice(attacker.damage_dice, rng, crit=crit)
        defender.hp = max(0, defender.hp - damage)

    return {
        "type": "attack",
        "attacker": attacker.name,
        "defender": defender.name,
        "hit": hit,
        "crit": crit,
        "roll": roll,
        "total": roll + attacker.attack_bonus,
        "target_ac": defender.ac,
        "damage": damage,
        "defender_hp": defender.hp,
        "defender_max_hp": defender.max_hp,
    }


def flee_check(player: Combatant, monster: Combatant, rng: Random) -> bool:
    """Escape if a d20 + the player's initiative bonus beats DC 11."""
    return d20(rng) + player.initiative_bonus >= 11


def resolve_round(
    player: Combatant,
    monster: Combatant,
    order: list[str],
    action: str,
    rng: Random,
    round_num: int,
) -> RoundResult:
    """Resolve one round. `action` is "attack", "flee", or "pass".

    "pass" = the player used their turn on something else (e.g. drinking a potion,
    applied by the caller before this call); the monster still gets to act.
    Mutates `player.hp` / `monster.hp`.
    """
    events: list[dict] = []
    outcome: Outcome = "ongoing"

    if action == "flee":
        if flee_check(player, monster, rng):
            events.append({"type": "flee", "success": True})
            outcome = "fled"
        else:
            events.append({"type": "flee", "success": False})
            events.append(make_attack(monster, player, rng))
            if player.hp <= 0:
                outcome = "defeat"
        return RoundResult(round_num, action, events, player.hp, monster.hp, outcome)

    for who in order:
        if who == "player":
            if action == "pass":
                continue
            events.append(make_attack(player, monster, rng))
            if monster.hp <= 0:
                outcome = "victory"
                break
        else:
            events.append(make_attack(monster, player, rng))
            if player.hp <= 0:
                outcome = "defeat"
                break

    return RoundResult(round_num, action, events, player.hp, monster.hp, outcome)


def summarize_round(result: RoundResult) -> str:
    """Factual, engine-authored summary of the round — the narrator's raw input.

    Deliberately plain (no flavor): the model adds the color, never the outcomes.
    """
    lines: list[str] = []
    for ev in result.events:
        if ev["type"] == "flee":
            lines.append(
                "The contestant flees successfully." if ev["success"]
                else "The contestant tries to flee and fails."
            )
        elif ev["type"] == "attack":
            if not ev["hit"]:
                lines.append(f"{ev['attacker']} attacks {ev['defender']} and misses.")
            else:
                crit = " (CRITICAL)" if ev["crit"] else ""
                lines.append(
                    f"{ev['attacker']} hits {ev['defender']} for {ev['damage']}{crit}. "
                    f"{ev['defender']} is at {ev['defender_hp']}/{ev['defender_max_hp']} HP."
                )
    tail = {
        "victory": "The monster is defeated.",
        "defeat": "The contestant has fallen.",
        "fled": "The contestant escaped the fight.",
        "ongoing": "The fight continues.",
    }[result.outcome]
    lines.append(tail)
    return " ".join(lines)
