# Choose Your Own Dungeon Crawler (CYODC)

A text-based, AI-narrated dungeon crawler in the spirit of Dungeon Crawler Carl,
played in the browser. Python/LangGraph backend, React frontend.

## Read these before writing code
- `docs/SPEC.md` — game design and agent responsibilities
- `docs/ROADMAP.md` — build phases. **Work one milestone at a time. Do not skip ahead.**

## Stack (do not substitute without asking)
- Backend: Python 3.12, FastAPI, LangGraph (Python), `langchain-anthropic`
- DB: SQLite for local dev, Postgres + pgvector for deployed; use SQLAlchemy so both work
- Session/graph state: LangGraph checkpointer (SqliteSaver locally, PostgresSaver deployed)
- Frontend: React + Vite, single page — scrolling narration pane + ASCII map panel
- Deployment target (later): AWS App Runner. Do not build deploy infra until Milestone 6.

## Architecture invariants (never violate these)
1. **LLMs never own game state.** Postgres/SQLite owns inventory, stats, HP, level
   layouts, fog-of-war. Agents read state and propose mutations ONLY through typed
   tools. If an agent narrates "you found a sword," the sword exists only if the
   `add_inventory_item` tool was called and committed.
2. **The LangGraph checkpointer holds session state only** (conversation flow, whose
   turn, pending combat node). World state lives in our own schema. Never stuff
   inventory or maps into graph state.
3. **Deterministic code decides; LLMs narrate.** Dice rolls, hit/miss, damage,
   loot rolls, and map generation (BSP) are plain Python. Agents describe outcomes;
   they do not adjudicate them. A player must not be able to argue their way out
   of damage.
4. **Worldgen runs between levels, not during turns.** Level content is generated
   once, written to the DB, then served. Play-time turns hit at most 1–2 model calls.
5. **Cost discipline from day one.** Haiku for routine narration and NPCs; Sonnet
   for the DM router, bosses, and worldgen. Every model call goes through
   `app/llm.py` so model selection and token budgets live in one place.

## Graph shape
```
player input → DM (router node)
                 ├─ narrate/explore (DM handles inline)
                 ├─ combat node      → back to DM
                 ├─ boss node        → back to DM
                 ├─ npc node         → back to DM
                 └─ level transition → worldgen (offline path) → DM
```

## Conventions
- Type hints everywhere; Pydantic models for all tool inputs/outputs.
- Every agent's system prompt lives in `app/prompts/<agent>.md` — prompts are
  content, not string literals buried in code. The game-writer subagent owns these.
- Tests: pytest. Combat math and map generation must have unit tests (they're
  deterministic — no excuse). Graph routing gets tested with a fake LLM.
- After each milestone, run the playtester subagent before declaring it done.

## Subagents available to you
- `langgraph-architect` — consult before adding/changing graph nodes, edges, or
  checkpointing behavior
- `schema-guardian` — must review any migration or schema change
- `game-writer` — owns runtime prompt files and all DCC-flavored narrative content
- `playtester` — runs after each milestone; plays the game via the API and reports
