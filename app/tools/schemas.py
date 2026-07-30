"""Pydantic result models for the game tools (invariant: typed tool I/O).

Tools return these serialized to JSON; the DM narrates them. Inputs are simple
typed args on the tool functions, from which LangChain derives the input schema.
"""

from __future__ import annotations

from pydantic import BaseModel


class LookResult(BaseModel):
    ok: bool = True
    room: str
    description: str
    exits: list[str]
    items_here: list[str]
    monsters_here: list[str] = []


class MoveResult(BaseModel):
    ok: bool
    message: str
    room: str | None = None
    description: str | None = None
    exits: list[str] = []
    items_here: list[str] = []
    monsters_here: list[str] = []


class CombatStartResult(BaseModel):
    ok: bool
    message: str
    combat_started: bool = False
    monster: str | None = None


class TakeResult(BaseModel):
    ok: bool
    message: str
    item: str | None = None


class UseResult(BaseModel):
    ok: bool
    message: str
    item: str | None = None
    hp: int | None = None
    max_hp: int | None = None
    gold: int | None = None


class InvItem(BaseModel):
    name: str
    qty: int


class InventoryResult(BaseModel):
    items: list[InvItem]
    gold: int
    hp: int
    max_hp: int
