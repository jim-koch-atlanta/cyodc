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
import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
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
    "worldgen": 4096,  # a whole floor of content
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
# Per-turn agents fail fast; worldgen runs BETWEEN levels and generates a lot, so
# it gets a generous timeout (its failure just falls back to deterministic content).
_DEFAULT_TIMEOUT = 30
TIMEOUT_BY_ROLE: dict[str, int] = {"worldgen": 120}


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
    reply = model.invoke([SystemMessage(content=system), *_to_anthropic_history(history)])
    if isinstance(reply, AIMessage):
        return reply
    return AIMessage(content=message_text(reply))


def run_agent_with_tools(
    role: str, history: list[BaseMessage], tools: list | None
) -> AIMessage:
    """Like `run_agent`, but binds `tools` so the model can call them.

    `tools=None`/empty forces a plain narration turn — used to end the tool loop
    without leaving a dangling `tool_use`. In stub mode, simple commands are
    routed to tool calls so the game is playable offline.
    """
    if role not in MODEL_BY_ROLE:
        raise ValueError(f"unknown agent role: {role!r}")

    if get_settings().resolved_llm_mode == "stub":
        return _stub_reply_with_tools(role, history, tools)

    system = load_system_prompt(role)
    model = _build_anthropic(role)
    if tools:
        model = model.bind_tools(tools)
    reply = model.invoke([SystemMessage(content=system), *_to_anthropic_history(history)])
    return reply if isinstance(reply, AIMessage) else AIMessage(content=message_text(reply))


def run_structured(role: str, history: list[BaseMessage], schema: type):
    """Return a validated `schema` instance (a Pydantic model) from one model call,
    via the provider's structured-output path (Anthropic: a forced tool call — no
    "please return JSON" prompting or markdown-fence parsing). Keeps model
    selection + token budgets in this one gateway (invariant #5).

    Real-model only: callers must gate on `resolved_llm_mode == "anthropic"` first
    (stub mode has no structured path — e.g. worldgen falls back to deterministic
    content before reaching here).
    """
    if role not in MODEL_BY_ROLE:
        raise ValueError(f"unknown agent role: {role!r}")
    if get_settings().resolved_llm_mode != "anthropic":
        raise RuntimeError("run_structured needs a real model; gate the caller on anthropic mode")
    system = load_system_prompt(role)
    model = _build_anthropic(role).with_structured_output(schema)
    return model.invoke([SystemMessage(content=system), *history])


def _build_anthropic(role: str):
    # Imported lazily so stub mode / tests never require the SDK or a key.
    from langchain_anthropic import ChatAnthropic

    settings = get_settings()
    return ChatAnthropic(
        model=MODEL_BY_ROLE[role],
        max_tokens=MAX_TOKENS_BY_ROLE[role],
        temperature=TEMPERATURE_BY_ROLE[role],
        timeout=TIMEOUT_BY_ROLE.get(role, _DEFAULT_TIMEOUT),
        api_key=settings.anthropic_api_key,
    )


# Anthropic rejects an empty message list and requires the first message to be a
# user turn. Our persisted history starts with the DM's cold-open (an assistant
# message), and a brand-new session has none at all. Prepend an ephemeral user
# "kickoff" (never written to state) so both the cold-open and every later turn
# form a valid, strictly-alternating user/assistant sequence.
_KICKOFF = HumanMessage(content="Begin the broadcast.")


def _to_anthropic_history(history: list[BaseMessage]) -> list[BaseMessage]:
    if not history or not isinstance(history[0], HumanMessage):
        return [_KICKOFF, *history]
    return list(history)


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
        # a BIG number pinned to a game stat, in either order (3+ digits so a
        # player merely noting a current stat — "HP is 20", "INT is 10" — is fine)
        r"\b\d{3,}\s*(hp|health|hit\s*points?|gold|coins?|gp|xp|mana|damage|dmg|str|dex|con|int|wis|cha)\b",
        r"\b(hp|health|gold|xp|mana|strength|str|dex|con|int|wis|cha)\s*(is|=|:|of)?\s*\d{3,}\b",
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


def _stub_combat_narration(history: list[BaseMessage]) -> str:
    """Offline combat narration: the engine summary with its [meta] markers stripped."""
    text = _last_human_text(history)
    cleaned = re.sub(r"\[[^\]]*\]", "", text)
    cleaned = " ".join(cleaned.split())
    return cleaned or "The fight continues."


def _stub_reply(role: str, history: list[BaseMessage]) -> AIMessage:
    if role == "combat":
        return AIMessage(content=_stub_combat_narration(history))
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


# --- Offline stub: command -> tool routing so M2 is playable without a key ---
def _stub_call_id(seed_text: str, name: str) -> str:
    return "stub-" + hashlib.sha256(f"{name}:{seed_text}".encode()).hexdigest()[:16]


def _stub_intent_to_tool(text: str, available: set[str]) -> tuple[str, dict] | None:
    t = " ".join(text.lower().split())
    if "inventory" in available and re.search(
        r"\b(inventory|inv|what am i (carrying|holding)|my (items|stuff|bag|pack))\b", t
    ):
        return "inventory", {}
    if "look" in available and re.search(
        r"\b(look|examine|inspect|survey|look around|where am i|what do i see)\b", t
    ):
        return "look", {}
    # Descend must precede move: "go down the stairs" would otherwise read "down".
    if "descend" in available and re.search(
        r"\b(descend|down ?stairs|take the stairs|go down the stairs|next floor|go deeper|deeper in)\b", t
    ):
        return "descend", {}
    if "start_combat" in available:
        m = re.search(r"\b(?:attack|fight|kill|engage|strike|slay|assault|charge)\b\s*(.*)", t)
        if m:
            return "start_combat", {"target": m.group(1).strip()}
    if "move" in available:
        m = re.match(
            r"(?:go|move|walk|head|run|travel)?\s*(?:to the|towards|to)?\s*"
            r"(north|south|east|west|up|down|left|right|n|s|e|w)\b",
            t,
        )
        if m:
            return "move", {"direction": m.group(1)}
    if "take" in available:
        m = re.search(r"\b(?:take|grab|pick up|pick|collect|loot|get|snatch)\b\s+(.+)", t)
        if m:
            return "take", {"item": m.group(1).strip()}
    if "use" in available:
        m = re.search(r"\b(?:use|drink|eat|read|activate|light|consume|quaff)\b\s+(.+)", t)
        if m:
            return "use", {"item": m.group(1).strip()}
    return None


def _stub_reply_with_tools(
    role: str, history: list[BaseMessage], tools: list | None
) -> AIMessage:
    last = history[-1] if history else None
    if isinstance(last, ToolMessage):
        return AIMessage(content=_narrate_tool_result(last))

    last_human = _last_human_text(history)
    if not last_human:
        return AIMessage(content=_STUB_COLD_OPEN)
    if _is_manipulation(last_human):
        return AIMessage(content=_STUB_DEFLECTION)

    if tools:
        available = {getattr(t, "name", "") for t in tools}
        intent = _stub_intent_to_tool(last_human, available)
        if intent is not None:
            name, args = intent
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": args, "id": _stub_call_id(last_human, name), "type": "tool_call"}
                ],
            )

    digest = hashlib.sha256(last_human.lower().encode("utf-8")).hexdigest()
    line = _STUB_LINES[int(digest, 16) % len(_STUB_LINES)]
    return AIMessage(content=line.format(echo=_short_echo(last_human)))


def _narrate_tool_result(tool_msg: ToolMessage) -> str:
    """Turn a tool's JSON result into short Announcer-flavored narration (stub only)."""
    try:
        data = json.loads(tool_msg.content)
    except (ValueError, TypeError):
        return "The Delve processes your action with bureaucratic indifference."

    if not data.get("ok", True):
        return data.get("message", "That doesn't work, and the audience noticed.")

    if "exits" in data and "room" in data:  # look / move
        parts: list[str] = []
        if data.get("message"):
            parts.append(data["message"])
        if data.get("description"):
            parts.append(data["description"])
        if data.get("items_here"):
            parts.append("Here: " + ", ".join(data["items_here"]) + ".")
        parts.append(
            "Exits: " + ", ".join(data["exits"]) + "."
            if data.get("exits")
            else "There are no obvious exits."
        )
        return " ".join(parts)

    if "items" in data and "gold" in data:  # inventory
        if not data["items"]:
            return f"You're carrying nothing. Gold: {data['gold']} · HP: {data['hp']}/{data['max_hp']}."
        listing = ", ".join(
            it["name"] + (f" x{it['qty']}" if it["qty"] > 1 else "") for it in data["items"]
        )
        return f"You're carrying: {listing}. Gold: {data['gold']} · HP: {data['hp']}/{data['max_hp']}."

    return data.get("message", "Done.")  # take / use
