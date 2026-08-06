"""The DM's five typed tools: look, move, take, use, inventory.

Design constraints (from the architecture reviews):
- `session` and `player_id` are `InjectedToolArg`s — the model never sees or sets
  them; the DM node injects them at execution time. The model only chooses the
  semantic args (direction, item).
- Replay-safe (LangGraph may re-run a node): mutations are *gated on a state
  transition that happens at most once* — `take` gates on the item still being in
  the room; `use` gates on still carrying it. Re-execution is a no-op.
- Tools never adjudicate flavor — they return structured facts; the DM narrates.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import InjectedToolArg, tool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.balance import buy_price_for, item_effect_for
from app.db.models import InventoryRow, Item, Player
from app.dungeon import (
    DIRECTIONS,
    current_level,
    current_room,
    exits_from,
    load_map,
    load_monster_catalog,
    load_npc_catalog,
    normalize_direction,
    reveal,
)
from app.encounters import active_encounter, start_encounter
from app.tools.schemas import (
    BuyResult,
    CombatStartResult,
    DescendResult,
    InventoryResult,
    InvItem,
    LookResult,
    MoveResult,
    TakeResult,
    TalkResult,
    UseResult,
    WareView,
    WaresResult,
)

_CORRIDOR_DESC = (
    "A cramped, unremarkable stretch of corridor. The walls press close and the "
    "air is stale enough to file a complaint about."
)
_STOPWORDS = {"the", "a", "an", "of", "my", "some", "that", "this"}


def _item_names(session: Session, slugs: list[str]) -> list[str]:
    if not slugs:
        return []
    rows = session.scalars(select(Item).where(Item.slug.in_(slugs))).all()
    by_slug = {r.slug: r.name for r in rows}
    return [by_slug.get(s, s) for s in slugs]


def _match_slug(query: str, pairs: list[tuple[str, str]]) -> str | None:
    """Fuzzily map a player's phrase to one of (slug, name) candidates."""
    q = " ".join(query.lower().split())
    for slug, name in pairs:
        n = name.lower()
        if q == slug or slug in q or q in n or n in q or slug.replace("-", " ") in q:
            return slug
    q_tokens = set(q.split()) - _STOPWORDS
    for slug, name in pairs:
        if q_tokens & (set(name.lower().split()) | set(slug.split("-"))):
            return slug
    return None


def _describe(session, level, dm, x, y):
    room = current_room(session, level, x, y)
    slugs = list(room.contents.get("items", [])) if room else []
    monster_slugs = list(room.contents.get("monsters", [])) if room else []
    npc_slugs = list(room.contents.get("npcs", [])) if room else []
    catalog = load_monster_catalog(level.floor, level.monster_catalog)
    monsters = [catalog[s]["name"] if s in catalog else s for s in monster_slugs]
    npc_catalog = load_npc_catalog(level.floor, level.npc_catalog)
    npcs = [npc_catalog[s]["name"] if s in npc_catalog else s for s in npc_slugs]
    return (
        room.kind if room else "corridor",
        room.description if room else _CORRIDOR_DESC,
        exits_from(dm, x, y),
        _item_names(session, slugs),
        monsters,
        npcs,
    )


def _npc_here(session, level, x, y) -> dict | None:
    """The (first, wares-bearing) NPC in the room at (x, y), or None. Shared by
    talk / buy / list_wares so they all resolve the same merchant."""
    room = current_room(session, level, x, y)
    npc_slugs = list(room.contents.get("npcs", [])) if room else []
    catalog = load_npc_catalog(level.floor, level.npc_catalog)
    for slug in npc_slugs:
        if slug in catalog:
            return catalog[slug]
    return None


def _ware_slug(npc_slug: str, name: str) -> str:
    trail = "-".join(ch for ch in name.lower().replace("'", "").split() if ch)
    return f"ware-{npc_slug}-{trail}"


def _match_monster(target: str, slugs: list[str], catalog: dict) -> str | None:
    if not (target or "").strip():
        return slugs[0]  # only one to fight, usually
    q = " ".join(target.lower().split())
    for slug in slugs:
        name = catalog.get(slug, {}).get("name", slug).lower()
        if q == slug or slug in q or q in name or name in q or slug.replace("-", " ") in q:
            return slug
    q_tokens = set(q.split()) - _STOPWORDS
    for slug in slugs:
        name = catalog.get(slug, {}).get("name", slug).lower()
        if q_tokens & (set(name.split()) | set(slug.split("-"))):
            return slug
    return None


@tool
def look(
    *,
    session: Annotated[Session, InjectedToolArg],
    player_id: Annotated[int, InjectedToolArg],
) -> str:
    """Look at your surroundings: the room you're in, its exits, and anything here to take."""
    player = session.get(Player, player_id)
    level = current_level(session, player)
    dm = load_map(level)
    vis = _visibility(session, player_id, level.id)
    reveal(session, vis, dm, player.pos_x, player.pos_y)
    kind, desc, exits, items, monsters, npcs = _describe(
        session, level, dm, player.pos_x, player.pos_y
    )
    return LookResult(
        room=kind, description=desc, exits=exits, items_here=items,
        monsters_here=monsters, npcs_here=npcs,
    ).model_dump_json()


@tool
def move(
    direction: str,
    *,
    session: Annotated[Session, InjectedToolArg],
    player_id: Annotated[int, InjectedToolArg],
) -> str:
    """Move one step in a compass direction: "north", "south", "east", or "west"."""
    player = session.get(Player, player_id)
    level = current_level(session, player)
    dm = load_map(level)

    d = normalize_direction(direction)
    if d not in DIRECTIONS:
        return MoveResult(ok=False, message=f'"{direction}" is not a direction you can walk.').model_dump_json()

    dx, dy = DIRECTIONS[d]
    nx, ny = player.pos_x + dx, player.pos_y + dy
    if not dm.is_floor(nx, ny):
        return MoveResult(ok=False, message=f"A wall blocks the way {d}.").model_dump_json()

    player.pos_x, player.pos_y = nx, ny
    vis = _visibility(session, player_id, level.id)
    reveal(session, vis, dm, nx, ny)
    kind, desc, exits, items, monsters, npcs = _describe(session, level, dm, nx, ny)
    return MoveResult(
        ok=True, message=f"You move {d}.", room=kind, description=desc,
        exits=exits, items_here=items, monsters_here=monsters, npcs_here=npcs,
    ).model_dump_json()


@tool
def take(
    item: str,
    *,
    session: Annotated[Session, InjectedToolArg],
    player_id: Annotated[int, InjectedToolArg],
) -> str:
    """Pick up an item lying in your current room. Give the item's name."""
    player = session.get(Player, player_id)
    level = current_level(session, player)
    room = current_room(session, level, player.pos_x, player.pos_y)
    slugs = list(room.contents.get("items", [])) if room else []
    if not slugs:
        return TakeResult(ok=False, message="There is nothing here to take.").model_dump_json()

    pairs = list(zip(slugs, _item_names(session, slugs)))
    slug = _match_slug(item, pairs)
    if slug is None:
        return TakeResult(ok=False, message=f'You don\'t see any "{item}" here.').model_dump_json()

    # Gate: remove one instance from the room. Replay finds it already gone -> no-op.
    remaining = list(slugs)
    remaining.remove(slug)
    room.contents["items"] = remaining

    item_row = session.scalars(select(Item).where(Item.slug == slug)).one()
    inv = session.scalars(
        select(InventoryRow).where(
            InventoryRow.player_id == player_id, InventoryRow.item_id == item_row.id
        )
    ).first()
    if inv is not None:
        inv.qty = inv.qty + 1
    else:
        session.add(InventoryRow(player_id=player_id, item_id=item_row.id, qty=1))
    return TakeResult(ok=True, item=item_row.name, message=f"You take the {item_row.name}.").model_dump_json()


@tool
def use(
    item: str,
    *,
    session: Annotated[Session, InjectedToolArg],
    player_id: Annotated[int, InjectedToolArg],
) -> str:
    """Use or consume an item you're carrying (drink a potion, read a note, light a torch). Give its name."""
    player = session.get(Player, player_id)
    rows = session.scalars(
        select(InventoryRow).where(InventoryRow.player_id == player_id)
    ).all()
    pairs = [(r.item.slug, r.item.name) for r in rows]
    slug = _match_slug(item, pairs)
    if slug is None:
        return UseResult(ok=False, message=f'You aren\'t carrying any "{item}".').model_dump_json()

    row = next(r for r in rows if r.item.slug == slug)
    effects = row.item.effects or {}
    parts: list[str] = []

    healed = 0
    gained_gold = 0
    if "heal" in effects:
        before = player.hp
        player.hp = min(player.max_hp, player.hp + int(effects["heal"]))
        healed = player.hp - before
        parts.append(
            f"restores {healed} HP" if healed else "would heal you, but you're already at full health"
        )
    if "gold" in effects:
        gained_gold = int(effects["gold"])
        player.gold = player.gold + gained_gold
        parts.append(f"yields {gained_gold} gold")
    if effects.get("light"):
        parts.append("casts a wavering light against the dark")
    if not effects:
        parts.append("does nothing of any mechanical use whatsoever")

    # Consume an effect-bearing consumable ONLY if it actually did something —
    # don't burn a heal at full HP. Consumption is the replay gate for heal/gold.
    if healed > 0 or gained_gold > 0:
        if row.qty > 1:
            row.qty = row.qty - 1
        else:
            session.delete(row)

    return UseResult(
        ok=True,
        item=row.item.name,
        hp=player.hp,
        max_hp=player.max_hp,
        gold=player.gold,
        message=f"You use the {row.item.name}; it " + ", ".join(parts) + ".",
    ).model_dump_json()


@tool
def inventory(
    *,
    session: Annotated[Session, InjectedToolArg],
    player_id: Annotated[int, InjectedToolArg],
) -> str:
    """List what you're carrying, along with your gold and current HP."""
    player = session.get(Player, player_id)
    rows = session.scalars(
        select(InventoryRow).where(InventoryRow.player_id == player_id)
    ).all()
    items = [InvItem(name=r.item.name, qty=r.qty) for r in rows]
    return InventoryResult(
        items=items, gold=player.gold, hp=player.hp, max_hp=player.max_hp
    ).model_dump_json()


def _visibility(session: Session, player_id: int, level_id: int):
    from app.db.models import Visibility

    vis = session.scalars(
        select(Visibility).where(
            Visibility.player_id == player_id, Visibility.level_id == level_id
        )
    ).first()
    if vis is None:  # defensive: seeding always creates one
        vis = Visibility(player_id=player_id, level_id=level_id, explored=[])
        session.add(vis)
        session.flush()
    return vis


@tool
def start_combat(
    target: str = "",
    *,
    session: Annotated[Session, InjectedToolArg],
    player_id: Annotated[int, InjectedToolArg],
) -> str:
    """Begin a fight with a monster in your current room. Pass the monster's name (blank if only one is present)."""
    player = session.get(Player, player_id)
    level = current_level(session, player)
    room = current_room(session, level, player.pos_x, player.pos_y)
    monster_slugs = list(room.contents.get("monsters", [])) if room else []
    if not monster_slugs:
        return CombatStartResult(ok=False, message="There is nothing here to fight.").model_dump_json()

    catalog = load_monster_catalog(player.floor, level.monster_catalog)
    slug = _match_monster(target, monster_slugs, catalog)
    if slug is None:
        return CombatStartResult(
            ok=False, message=f'You don\'t see any "{target}" to fight here.'
        ).model_dump_json()

    # Gate (replay-safety): remove the monster from the room atomically with
    # creating the encounter, so a replay finds nothing to fight and no-ops.
    remaining = list(monster_slugs)
    remaining.remove(slug)
    room.contents["monsters"] = remaining
    encounter = start_encounter(session, player, slug, catalog=catalog)
    name = encounter.monster["name"]
    return CombatStartResult(
        ok=True,
        combat_started=True,
        monster=name,
        message=f"You square off against the {name}. Forty-seven billion viewers lean in.",
    ).model_dump_json()


@tool
def descend(
    *,
    session: Annotated[Session, InjectedToolArg],
    player_id: Annotated[int, InjectedToolArg],
) -> str:
    """Take the staircase down to the next floor. Only works while standing in the exit room."""
    player = session.get(Player, player_id)
    if active_encounter(session, player_id) is not None:
        return DescendResult(ok=False, message="Not while something is trying to kill you.").model_dump_json()
    level = current_level(session, player)
    room = current_room(session, level, player.pos_x, player.pos_y)
    if room is None or room.kind != "exit":
        return DescendResult(
            ok=False, message="There are no stairs down here — find the exit first."
        ).model_dump_json()
    if room.contents.get("monsters"):  # a guard blocks the stairs until dealt with
        return DescendResult(
            ok=False, message="Something between you and the stairs objects, loudly. Deal with it first."
        ).model_dump_json()
    # Read-only signal; the worldgen node performs the actual transition. The
    # replay gate is position: after the move, the player is on the new floor's
    # entrance, so a re-run finds no exit here and returns transition=False.
    return DescendResult(
        ok=True, transition=True, message="You step onto the staircase and start down."
    ).model_dump_json()


@tool
def talk(
    target: str = "",
    *,
    session: Annotated[Session, InjectedToolArg],
    player_id: Annotated[int, InjectedToolArg],
) -> str:
    """Speak with an NPC in your current room — a merchant, a clerk, anyone who talks back. Pass their name (blank if only one is here)."""
    player = session.get(Player, player_id)
    level = current_level(session, player)
    room = current_room(session, level, player.pos_x, player.pos_y)
    npc_slugs = list(room.contents.get("npcs", [])) if room else []
    if not npc_slugs:
        return TalkResult(ok=False, message="There's no one here to talk to.").model_dump_json()

    catalog = load_npc_catalog(level.floor, level.npc_catalog)
    pairs = [(s, catalog.get(s, {}).get("name", s)) for s in npc_slugs]
    slug = _match_slug(target, pairs) if (target or "").strip() else npc_slugs[0]
    if slug is None:
        slug = npc_slugs[0]  # addressed vaguely — talk to whoever's here
    name = catalog.get(slug, {}).get("name", slug)
    # Read-only signal; the npc node runs the actual exchange. `talk=True` is what
    # the DM node routes on (mirrors descend's `transition`).
    return TalkResult(
        ok=True, talk=True, npc_slug=slug, npc_name=name, message=f"You turn to {name}."
    ).model_dump_json()


@tool
def list_wares(
    *,
    session: Annotated[Session, InjectedToolArg],
    player_id: Annotated[int, InjectedToolArg],
) -> str:
    """List what the merchant in your current room has for sale, with prices."""
    player = session.get(Player, player_id)
    level = current_level(session, player)
    npc = _npc_here(session, level, player.pos_x, player.pos_y)
    if npc is None or not npc.get("wares"):
        return WaresResult(ok=False, message="Nothing's for sale here.").model_dump_json()
    views = [
        WareView(
            name=w.get("name", "Oddment"),
            flavor=w.get("flavor", ""),
            effect=w.get("effect", "trinket"),
            price_gold=buy_price_for(w.get("effect", "trinket"), level.floor),
        )
        for w in npc["wares"]
    ]
    return WaresResult(ok=True, npc=npc.get("name", "the merchant"), wares=views).model_dump_json()


@tool
def buy(
    item: str,
    *,
    session: Annotated[Session, InjectedToolArg],
    player_id: Annotated[int, InjectedToolArg],
) -> str:
    """Buy a ware from the merchant in your current room. Give the item's name. The price is set by the shop, not negotiable."""
    player = session.get(Player, player_id)
    level = current_level(session, player)
    npc = _npc_here(session, level, player.pos_x, player.pos_y)
    if npc is None or not npc.get("wares"):
        return BuyResult(ok=False, message="There's no one here selling anything.").model_dump_json()

    wares = npc["wares"]
    pairs = [(_ware_slug(npc["slug"], w["name"]), w["name"]) for w in wares]
    slug = _match_slug(item, pairs)
    ware = next(
        (w for w in wares if _ware_slug(npc["slug"], w["name"]) == slug), None
    ) if slug else None
    if ware is None:
        return BuyResult(ok=False, message=f'No "{item}" for sale here.').model_dump_json()

    price = buy_price_for(ware.get("effect", "trinket"), level.floor)
    if player.gold < price:
        return BuyResult(
            ok=False, gold=player.gold,
            message=f"You can't afford the {ware['name']} — {price} gold, and you have {player.gold}.",
        ).model_dump_json()

    # Code owns the price (invariant #3); gold + inventory change together in this
    # one committed turn. (Buy has no natural one-time gate like `take`; a mid-node
    # replay is the same residual risk as `use` — documented for a later token gate.)
    player.gold = player.gold - price
    item_row = _get_or_create_ware_item(session, player, npc["slug"], ware, level.floor)
    inv = session.scalars(
        select(InventoryRow).where(
            InventoryRow.player_id == player_id, InventoryRow.item_id == item_row.id
        )
    ).first()
    if inv is not None:
        inv.qty = inv.qty + 1
    else:
        session.add(InventoryRow(player_id=player_id, item_id=item_row.id, qty=1))
    return BuyResult(
        ok=True, item=ware["name"], gold=player.gold,
        message=f"You buy the {ware['name']} for {price} gold.",
    ).model_dump_json()


def _get_or_create_ware_item(session: Session, player: Player, npc_slug: str, ware: dict, floor: int) -> Item:
    """Insert-or-get the Item backing a merchant ware. Player-scoped like other
    generated items, so it cascades on player delete. Effects come from the menu."""
    slug = _ware_slug(npc_slug, ware["name"])
    existing = session.scalars(select(Item).where(Item.slug == slug)).first()
    if existing is not None:
        return existing
    item = Item(
        slug=slug,
        name=ware.get("name", "Oddment"),
        flavor_text=ware.get("flavor", ""),
        effects=item_effect_for(ware.get("effect", "trinket"), floor),
        player_id=player.id,
    )
    session.add(item)
    session.flush()
    return item


DM_TOOLS = [look, move, take, use, inventory, start_combat, descend, talk]
# Executed inside the npc node's tool loop (Haiku). `buy` mutates state through
# the same typed-tool discipline as the DM's tools (invariant #1).
NPC_TOOLS = [buy, list_wares]
TOOLS_BY_NAME = {t.name: t for t in [*DM_TOOLS, *NPC_TOOLS]}
