# Roadmap — build ONE milestone at a time

Each milestone ends with: tests passing, the playtester subagent run, and a short
note in `docs/BUILDLOG.md` about what exists and what's stubbed.

## M1 — Walking skeleton
FastAPI app + minimal LangGraph graph (single DM node, SqliteSaver checkpointer)
+ bare React page with text pane and input. Player types anything, DM responds in
character. No DB, no map, no combat. **Goal: prove the loop end-to-end.**

## M2 — World state + tools
SQLAlchemy schema from SPEC.md. DM gets typed tools: look, move, take, use,
inventory. BSP `mapgen.py` with unit tests. Rooms exist and are hand-seeded (no
worldgen agent yet). Fog-of-war tracked in `visibility`.

## M3 — Combat
Deterministic `combat.py` engine (initiative, to-hit, damage; unit tested).
Combat node narrates engine output. DM routes into/out of combat via conditional
edges. `encounters` table so combat survives a disconnect.

## M4 — Worldgen agent
Between-level generation: theme, room descriptions, monster/loot placement
written to DB. Level transitions. Loot with flavor text.

## M5 — Memory + NPCs + bosses
pgvector-backed `story_log` RAG (start with SQLite + a simple embedding table if
pgvector is deferred). NPC node with personality cards + interaction memory.
Boss node with one fully realized floor boss.

## M6 — Frontend polish + map panel
ASCII map rendering, stats bar, achievement popups, resume codes.

## M7 — AWS (only when M1–M6 are solid)
Postgres + pgvector (RDS), PostgresSaver checkpointer, App Runner deploy, per-
player rate limits and daily token budgets, spending alarm + kill switch.
