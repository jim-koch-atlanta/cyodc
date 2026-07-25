# CYODC — Choose Your Own Dungeon Crawler

A text-based, AI-narrated dungeon crawler in the spirit of Dungeon Crawler Carl.
Python/LangGraph backend, React/Vite frontend. See `docs/SPEC.md` and
`docs/ROADMAP.md` for design and build phases; `docs/BUILDLOG.md` for status.

## Milestone status

**M1 — walking skeleton (done).** FastAPI + a single-node LangGraph DM with a
SQLite checkpointer, and a bare React page (scrolling narration + input). Type
anything; the DM answers in character. Sessions persist and resume. No DB world
state, map, or combat yet — those arrive in M2+.

## Requirements

- Python 3.12 (the stack pins it; `uv` will fetch it for you)
- Node 18+
- [`uv`](https://docs.astral.sh/uv/) for the backend

## Backend

```bash
uv sync                      # create .venv (Python 3.12) + install deps
cp .env.example .env         # then add your ANTHROPIC_API_KEY (optional in M1)
uv run uvicorn app.main:app --reload --port 8000
```

**No API key?** The backend runs in **stub mode** automatically: deterministic,
in-character canned narration so the whole loop is playable offline. `GET /health`
reports `{"llm_mode": "stub"}`. Set `ANTHROPIC_API_KEY` in `.env` to use the real
Sonnet-narrated DM (`llm_mode: "anthropic"`).

### API (M1)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + current `llm_mode` |
| POST | `/api/session` | start a run; returns `session_id` (the resume code) + opening narration |
| GET | `/api/session/{id}` | rehydrate a session's history |
| POST | `/api/session/{id}/turn` | body `{"message": "..."}` → DM reply |

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
