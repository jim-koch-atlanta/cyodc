# CYODC — Choose Your Own Dungeon Crawler

A text-based, AI-narrated dungeon crawler in the spirit of Dungeon Crawler Carl.
Python/LangGraph backend, React/Vite frontend. See `docs/SPEC.md` and
`docs/ROADMAP.md` for design and build phases; `docs/BUILDLOG.md` for status.

## Milestone status

**M4 — worldgen + level transitions (done).** A Sonnet worldgen agent generates
each new floor's content (theme, room descriptions, item/monster placement) and
writes it to the DB when you descend — but only *decorates* the deterministic BSP
skeleton and never sets combat/loot numbers (code owns balance, scaled by depth).
Stand on the exit and "descend" to a freshly generated floor. Floor 1 stays
hand-seeded; generation falls back to deterministic content offline.

**M3 — combat (done).** A deterministic combat engine (initiative, to-hit,
damage, crits — plain Python, unit-tested) with an `encounters` table so a fight
survives a disconnect. The DM routes into/out of combat via conditional edges;
combat rounds narrate on Haiku while the engine owns every outcome.

**M2 — world state + tools (done).** SQLAlchemy world schema (players, items,
inventory, levels, rooms, fog-of-war), a deterministic BSP map generator, and
five typed DM tools (look, move, take, use, inventory). The DB owns all world
state; the DM changes it only through tools. Rooms + items are hand-seeded.

**M1 — walking skeleton (done).** FastAPI + a single-node LangGraph DM with a
SQLite checkpointer, and a bare React page. Type anything; the DM answers in
character; sessions persist and resume.

## Requirements

- Python 3.12 (the stack pins it; `uv` will fetch it for you)
- Node 18+
- [`uv`](https://docs.astral.sh/uv/) for the backend

## Backend

```bash
uv sync                      # create .venv (Python 3.12) + install deps
cp .env.example .env         # then add your ANTHROPIC_API_KEY (optional)
uv run uvicorn app.main:app --reload --port 8000
```

Two SQLite files live under `data/` (git-ignored): the LangGraph checkpointer
(`CYODC_CHECKPOINT_DB`) and the world-state DB (`CYODC_DATABASE_URL`), kept
separate on purpose. Tables are created on startup.

**No API key?** The backend runs in **stub mode** automatically: deterministic,
in-character narration, and simple commands ("look", "go north", "take the
torch", "attack the goblin", "flee", "inventory") are routed to the real tools
and combat engine — so the game (fights and descending included) is fully
playable offline (worldgen falls back to deterministic content). `GET /health`
reports `{"llm_mode": "stub"}`. Set `ANTHROPIC_API_KEY` in `.env` for the real
Sonnet/Haiku-narrated game and LLM-generated floors (`llm_mode: "anthropic"`).

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + current `llm_mode` |
| POST | `/api/session` | start a run; provisions a player + floor 1; returns `session_id` (the resume code) + opening narration |
| GET | `/api/session/{id}` | rehydrate a session's history |
| POST | `/api/session/{id}/turn` | body `{"message": "..."}` → DM reply (may call tools) |
| GET | `/api/session/{id}/state` | world state: HP/gold, floor, position, room, exits, inventory, monsters here, active combat, `can_descend`, and a fog-of-war ASCII map |

## Frontend

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173  (proxies /api to :8000)
```

The page stores your `session_id` in `localStorage`, so a refresh resumes the
same run. "new run" starts a fresh one.

## Tests

```bash
uv run pytest
```

Combat math and map generation (M2+) get real unit tests; graph routing is
tested with a fake LLM, so the suite needs no network and no API key.
