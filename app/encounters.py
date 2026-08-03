"""Combat lifecycle: bridges the pure engine (app/combat.py) to the DB.

- `start_encounter` snapshots a monster into an `encounters` row (the tool calls it).
- `resolve_combat_turn` runs ONE round through the engine, writes back HP, and on
  a finished fight deletes the encounter + awards rewards — the delete is the
  replay gate (per schema-guardian: a replayed call finds no row and no-ops).
The combat NODE stays thin: classify -> resolve -> narrate.
"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.combat import (
    Combatant,
    RoundResult,
    resolve_round,
    rng_for_round,
    roll_initiative,
    summarize_round,
)
from app.db.models import Encounter, InventoryRow, Player
from app.dungeon import (
    current_level,
    current_room,
    load_monster_catalog,
    match_slug,
)

_MONSTER_SNAPSHOT_KEYS = ("slug", "name", "ac", "attack_bonus", "damage_dice", "xp", "gold")


def ability_mod(score: int) -> int:
    return (int(score) - 10) // 2


def player_combatant(player: Player) -> Combatant:
    stats = player.stats or {}
    str_mod = ability_mod(stats.get("STR", 10))
    dex_mod = ability_mod(stats.get("DEX", 10))
    dmg = f"1d6+{str_mod}" if str_mod > 0 else ("1d6" if str_mod == 0 else f"1d6{str_mod}")
    return Combatant(
        name=player.name or "Contestant",
        hp=player.hp,
        max_hp=player.max_hp,
        ac=10 + dex_mod,
        attack_bonus=2 + str_mod,
        damage_dice=dmg,
        initiative_bonus=dex_mod,
    )


def monster_combatant(enc: Encounter) -> Combatant:
    m = enc.monster
    return Combatant(
        name=m["name"],
        hp=enc.monster_hp,
        max_hp=m["max_hp"],
        ac=m["ac"],
        attack_bonus=m["attack_bonus"],
        damage_dice=m["damage_dice"],
    )


def active_encounter(session: Session, player_id: int) -> Encounter | None:
    return session.scalars(
        select(Encounter).where(Encounter.player_id == player_id)
    ).first()


def _encounter_seed(session_id: str, floor: int, x: int, y: int, slug: str) -> int:
    digest = hashlib.sha256(f"{session_id}:{floor}:{x}:{y}:{slug}".encode()).digest()
    return int.from_bytes(digest[:6], "big")  # 48-bit, fits BigInteger


def start_encounter(
    session: Session, player: Player, monster_slug: str, catalog: dict | None = None
) -> Encounter:
    """Create the active encounter (snapshot + rolled initiative). Caller must
    have already validated the monster is present and cleared it from the room."""
    if catalog is None:
        catalog = load_monster_catalog(
            player.floor, current_level(session, player).monster_catalog
        )
    mdef = catalog[monster_slug]  # KeyError => caller bug
    snapshot = {k: mdef[k] for k in _MONSTER_SNAPSHOT_KEYS if k in mdef}
    snapshot["max_hp"] = mdef["hp"]

    seed = _encounter_seed(player.session_id, player.floor, player.pos_x, player.pos_y, monster_slug)
    order = roll_initiative(
        player_combatant(player),
        Combatant(snapshot["name"], mdef["hp"], mdef["hp"], mdef["ac"], mdef["attack_bonus"], mdef["damage_dice"]),
        rng_for_round(seed, 0),
    )
    enc = Encounter(
        player_id=player.id,
        monster=snapshot,
        monster_hp=mdef["hp"],
        round=1,
        seed=seed,
        turn_order=order,
    )
    session.add(enc)
    session.flush()
    return enc


def classify_combat_action(text: str) -> tuple[str, str | None]:
    """Deterministic (no LLM): freeform combat input -> (action, item?). Default attack."""
    t = " ".join((text or "").lower().split())
    if re.search(r"\b(flee|run away|run|escape|retreat|disengage)\b", t):
        return "flee", None
    m = re.search(r"\b(?:use|drink|quaff|eat|apply)\b\s+(.+)", t)
    if m:
        return "use", m.group(1).strip()
    return "attack", None


def resolve_combat_turn(session: Session, player: Player, enc: Encounter, text: str) -> RoundResult:
    """Run one combat round for the player's input and persist the result."""
    action, item_query = classify_combat_action(text)
    pc = player_combatant(player)
    mc = monster_combatant(enc)
    rng = rng_for_round(enc.seed, enc.round)

    if action == "use":
        used = _apply_combat_item(session, player, item_query)
        if used is None:
            # Nothing usable — the turn is NOT spent; let the player try again.
            return RoundResult(
                enc.round, "use",
                [{"type": "use", "ok": False, "query": item_query}],
                player.hp, enc.monster_hp, "ongoing",
            )
        if used.get("ok") is False:
            # Already at full HP: item kept, turn not spent (no free monster hit).
            return RoundResult(enc.round, "use", [used], player.hp, enc.monster_hp, "ongoing")
        pc.hp = player.hp  # reflect the heal before the monster swings
        result = resolve_round(pc, mc, enc.turn_order, "pass", rng, enc.round)
        result.events.insert(0, used)
    else:
        result = resolve_round(pc, mc, enc.turn_order, action, rng, enc.round)

    # Persist HP.
    player.hp = result.player_hp
    enc.monster_hp = result.monster_hp

    if result.outcome == "ongoing":
        enc.round += 1
    else:
        _end_encounter(session, player, enc, result.outcome)
    session.flush()
    return result


def _apply_combat_item(session: Session, player: Player, query: str | None) -> dict | None:
    """Consume a HEALING item mid-fight (only heals matter). Returns an event dict,
    or None if nothing matched. Consumption is the replay gate; a full-HP heal is
    NOT consumed and does not spend the turn (consistent with the out-of-combat use)."""
    if not query:
        return None
    heal_rows = [
        r
        for r in session.scalars(
            select(InventoryRow).where(InventoryRow.player_id == player.id)
        ).all()
        if "heal" in (r.item.effects or {})
    ]
    if not heal_rows:
        return None
    slug = match_slug(query, [(r.item.slug, r.item.name) for r in heal_rows])
    if slug is None:
        return None
    row = next(r for r in heal_rows if r.item.slug == slug)

    if player.hp >= player.max_hp:
        return {"type": "use", "ok": False, "full": True, "item": row.item.name}

    before = player.hp
    player.hp = min(player.max_hp, player.hp + int(row.item.effects["heal"]))
    if row.qty > 1:
        row.qty -= 1
    else:
        session.delete(row)
    return {"type": "use", "ok": True, "item": row.item.name, "healed": player.hp - before}


def _end_encounter(session: Session, player: Player, enc: Encounter, outcome: str) -> None:
    """Delete the encounter and apply rewards — the delete rowcount is the gate,
    so a replayed call (row already gone) cannot double-award."""
    result = session.execute(delete(Encounter).where(Encounter.id == enc.id))
    if result.rowcount != 1:
        return  # already ended elsewhere; do not re-apply
    if outcome == "victory":
        player.gold = player.gold + int(enc.monster.get("gold", 0))
    elif outcome == "defeat":
        player.hp = 1  # sponsor auto-reviver: humiliating, but alive (no death system yet)
    elif outcome == "fled":
        _return_monster_to_room(session, player, enc.monster.get("slug"))


def _return_monster_to_room(session: Session, player: Player, slug: str | None) -> None:
    """Put a fled-from monster back in the room, so the world isn't depleted by fleeing."""
    if not slug:
        return
    level = current_level(session, player)
    room = current_room(session, level, player.pos_x, player.pos_y)
    if room is not None:
        room.contents["monsters"] = list(room.contents.get("monsters", [])) + [slug]


def narration_summary(result: RoundResult, player: Player, enc: Encounter) -> str:
    """Factual, engine-authored brief for the combat narrator (it adds the color)."""
    m = enc.monster
    lines: list[str] = [f"[player action: {result.action}]"]

    used = next((e for e in result.events if e.get("type") == "use"), None)
    if used and used.get("ok") is False:
        if used.get("full"):
            lines.append(f"The contestant is already at full health and keeps the {used['item']}.")
        else:
            lines.append(f'The contestant has no usable "{used.get("query")}" to drink.')
    elif used:
        lines.append(f"The contestant uses {used['item']} and recovers {used['healed']} HP.")

    lines.append(summarize_round(result))
    if result.outcome == "victory" and m.get("gold"):
        lines.append(f"The contestant loots {m['gold']} gold from the remains.")
    if result.outcome == "defeat":
        lines.append("A sponsor's emergency auto-reviver drags the contestant back to 1 HP.")
    lines.append(f"[HP — Contestant {player.hp}/{player.max_hp}; {m['name']} {result.monster_hp}/{m['max_hp']}]")
    return " ".join(lines)
