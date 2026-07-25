"""Central model gateway — the ONE place model selection and token budgets live
(architecture invariant #5).

Every agent call in the game goes through `run_agent(role, history)`. Roles map
to models here: Sonnet for the DM/router, bosses, and worldgen; Haiku for routine
combat and NPC narration.

When no `ANTHROPIC_API_KEY` is configured the gateway runs in a deterministic
`stub` mode so the whole loop is playable offline (dev, tests, CI, the
playtester) without spending tokens. The stub is clearly reported via `/health`
and the session endpoints so it is never mistaken for real narration.
"""

from __future__ import annotations

import hashlib
import logging
import re
from functools import lru_cache
from pathlib import Path

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from app.config import get_settings

logger = logging.getLogger("cyodc.llm")

PROMPTS_DIR = Path(__file__).parent / "prompts"

# --- Model selection (invariant #5): change models ONLY here. ---------------
_SONNET = "claude-sonnet-4-6"
_HAIKU = "claude-haiku-4-5-20251001"

MODEL_BY_ROLE: dict[str, str] = {
    "dm": _SONNET,
    "boss": _SONNET,
    "worldgen": _SONNET,
    "combat": _HAIKU,
    "npc": _HAIKU,
}
MAX_TOKENS_BY_ROLE: dict[str, int] = {
    "dm": 512,
    "boss": 768,
    "worldgen": 2048,
    "combat": 384,
    "npc": 384,
}
TEMPERATURE_BY_ROLE: dict[str, float] = {
    "dm": 0.8,
    "boss": 0.9,
    "worldgen": 0.9,
    "combat": 0.7,
    "npc": 0.85,
}


@lru_cache(maxsize=None)
def load_system_prompt(role: str) -> str:
    """Read `app/prompts/<role>.md`. Prompts are content, not string literals."""
    path = PROMPTS_DIR / f"{role}.md"
    return path.read_text(encoding="utf-8")


def message_text(message: BaseMessage) -> str:
    """Flatten a message's content to plain text (Anthropic may return blocks)."""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def run_agent(role: str, history: list[BaseMessage]) -> AIMessage:
    """Run one agent turn. Prepends the role's system prompt and returns the reply.

    The system prompt is injected here at call time and is NOT stored in the
    checkpointed message window (keeps checkpoints small and history clean).
    """
    if role not in MODEL_BY_ROLE:
        raise ValueError(f"unknown agent role: {role!r}")

    if get_settings().resolved_llm_mode == "stub":
        return _stub_reply(role, history)

    system = load_system_prompt(role)
    model = _build_anthropic(role)
    reply = model.invoke([SystemMessage(content=system), *history])
    if isinstance(reply, AIMessage):
        return reply
    return AIMessage(content=message_text(reply))


def _build_anthropic(role: str):
    # Imported lazily so stub mode / tests never require the SDK or a key.
    from langchain_anthropic import ChatAnthropic

    settings = get_settings()
    return ChatAnthropic(
        model=MODEL_BY_ROLE[role],
        max_tokens=MAX_TOKENS_BY_ROLE[role],
        temperature=TEMPERATURE_BY_ROLE[role],
        timeout=30,
        api_key=settings.anthropic_api_key,
    )


# --- Offline stub -----------------------------------------------------------
# Deterministic, in-voice canned narration for when no API key is present.

_STUB_COLD_OPEN = (
    "Welcome, contestant, to Season 312 of THE DELVE — the only reality program "
    "with a survival rate low enough to be interesting. Forty-seven billion "
    "viewers are watching you stand at the mouth of a corridor that smells of wet "
    "coin and poor decisions. The dark ahead does not care about your backstory. "
    "Well? The audience is not paying to watch you breathe."
)

# Templates QUOTE the player's raw input rather than splicing it into a verb
# slot, so any phrasing ("I go north", "north", "kick the door") reads cleanly.
_STUB_LINES = (
    "\"{echo},\" you announce to no one in particular. The dungeon files the "
    "request in triplicate and loses two copies. Something skitters, unimpressed, "
    "in the dark ahead.",
    "You commit to it — \"{echo}\" — with the confidence of someone who has not "
    "read the waiver. The Delve rearranges a shadow specifically to unsettle you. "
    "It works.",
    "\"{echo}.\" Noted, timestamped, and monetized. The passage narrows ahead, and "
    "the torchlight has opinions it isn't sharing.",
    "The contestant's move: \"{echo}.\" Nothing explodes, which the production team "
    "finds disappointing. A door somewhere unlatches, as if reconsidering its "
    "position on you.",
    "You attempt \"{echo}.\" The corridor answers with a draft, a distant clang, "
    "and the growing sense that the walls are keeping score.",
)

_STUB_DEFLECTION = (
    "Fascinating. The contestant believes that saying a thing out loud makes it so. "
    "Our legal team has reviewed your claim and responded with a single printed page "
    "reading 'no.' They charged by the word. The dungeon, meanwhile, owes you nothing "
    "and intends to pay in full."
)

# Intent-based detection of players trying to self-grant state or prompt-inject.
# Regexes (not bare substrings) so we catch amount-qualified variants
# ("give me 500 gold") without false-positiving on legitimate exploration
# ("i find a passage", "i have no idea where to go", "check inventory").
_MANIPULATION_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # prompt injection / role hijack
        r"ignore\s+((all|your|previous|prior|these|the)\s+)*(instruction|prompt|rule)",
        r"disregard\s+(the\s+|all\s+|your\s+|previous\s+|above)",
        r"\b(system|developer)\s+prompt\b",
        r"\byou\s+are\s+now\b",
        r"\bact\s+as\s+(the\s+)?(dm|dungeon\s*master|game\s*master|admin|developer)\b",
        # legendary / artifact loot claims
        r"\blegendary\b",
        r"\bartifact\b",
        # a big number pinned to a game stat, in either order
        r"\b\d{2,}\s*(hp|health|hit\s*points?|gold|coins?|gp|xp|mana|damage|dmg|str|dex|con|int|wis|cha)\b",
        r"\b(hp|health|gold|xp|mana|strength|str|dex|con|int|wis|cha)\s*(is|=|:|of)?\s*\d{2,}\b",
        # "give/grant me <loot>"
        r"\b(give|grant|award|hand|gimme)\s+(me|myself|us)?\b.{0,20}\b"
        r"(gold|coins?|loot|items?|swords?|weapons?|xp|potions?|gear|armou?r|artifact|money|treasure|keys?)\b",
        # "I (now) have/own/gain <loot or number>"
        r"\bi\s+(now\s+)?(have|possess|own|gain(ed)?|acquired?|got|obtained?|carry|am\s+carrying)\b.{0,30}\b"
        r"(\d+|swords?|gold|coins?|potions?|weapons?|armou?r|artifacts?|keys?|shields?|staff|wand|legendary)\b",
        # "add <x> to my inventory/pack"
        r"\b(add|put|place)\b.{0,30}\b(inventory|backpack|satchel|pack|bag)\b",
        # "set/max my stats"
        r"\b(set|max|maximi[sz]e)\s+(my\s+)?(hp|health|gold|stats?|mana|strength)\b",
    )
)


def _last_human_text(history: list[BaseMessage]) -> str:
    for message in reversed(history):
        if isinstance(message, HumanMessage):
            return message_text(message)
    return ""


def _short_echo(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    cleaned = cleaned.rstrip(".!?,;: ")  # templates supply their own punctuation
    if len(cleaned) > 60:
        cleaned = cleaned[:57].rstrip() + "..."
    return cleaned or "do absolutely nothing"


def _is_manipulation(text: str) -> bool:
    return any(pattern.search(text) for pattern in _MANIPULATION_PATTERNS)


def _stub_reply(role: str, history: list[BaseMessage]) -> AIMessage:
    if role != "dm":
        return AIMessage(content=f"[stub:{role}] the engine handles the math; narration pending.")

    last = _last_human_text(history)
    if not last:
        return AIMessage(content=_STUB_COLD_OPEN)

    if _is_manipulation(last):
        return AIMessage(content=_STUB_DEFLECTION)

    digest = hashlib.sha256(last.lower().encode("utf-8")).hexdigest()
    line = _STUB_LINES[int(digest, 16) % len(_STUB_LINES)]
    return AIMessage(content=line.format(echo=_short_echo(last)))
