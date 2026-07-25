# Build Log

Append a dated entry after each milestone: what exists, what is stubbed, known issues.

---

## 2026-07-24 — M1: Walking skeleton (done)

**Goal met:** prove the loop end-to-end. A player types freeform text in the
browser; a single-node LangGraph DM answers in character; sessions persist to
SQLite and resume after a reconnect.

### What exists
- **Backend (FastAPI, `app/`)**
  - `main.py` — endpoints `GET /health`, `POST /api/session`,
    `GET /api/session/{id}`, `POST /api/session/{id}/turn`. The `session_id`
    (uuid) *is* the LangGraph `thread_id` and the resume code. Per-session
    threading locks serialize turns on the same session.
  - `graph.py` — `START → dm → END`. `SqliteSaver` checkpointer as a
    process-wide singleton (one shared sqlite connection, `check_same_thread=
    False`); the graph is compiled once at import.
  - `state.py` — `DMState = {messages (add_messages), next_node}`. `next_node`
    is always `None` in M1; it exists now so M3's conditional edge lands without
    a checkpoint migration.
  - `nodes/dm.py` — reads the message window, calls `llm.run_agent("dm", …)`,
    returns `{messages, next_node: None}`.
  - `routing.py` — `route_from_dm` (reads `next_node`; unit-tested now, wired as
    the live conditional edge in M3).
  - `llm.py` — the single model gateway (invariant #5): Sonnet for
    dm/boss/worldgen, Haiku for combat/npc; per-role max_tokens/temperature. The
    system prompt is injected at call time and is **not** stored in the
    checkpoint. Includes the offline stub narrator.
  - `config.py` — pydantic-settings; `ANTHROPIC_API_KEY` + `CYODC_*`; `.env`.
  - `prompts/dm.md` — the Announcer system prompt (game-writer). Original
    setting ("The Delve"); deflects manipulation; never adjudicates mechanics or
    grants durable state.
- **Frontend (`frontend/`, React + Vite)** — single page: scrolling narration
  pane + input, stub-mode badge, "new run", resume via `localStorage`
  session_id. Vite proxies `/api` → `:8000`. Production build verified.
- **Tests** — 46 pytest, no network and no API key required. Graph routing via a
  fake LLM; HTTP contract; stub calibration; concurrency regression.

### What's stubbed / deferred (by design for M1)
- **No world state.** No DB schema, inventory, HP, gold, map, fog-of-war, or
  combat — those are M2/M3. The DM narrates atmosphere only and is prompted to
  stay vague on hard numbers.
- **LLM stub mode.** With no `ANTHROPIC_API_KEY`, the backend serves
  deterministic in-character canned narration (`llm_mode="stub"`, surfaced by
  `/health` and the UI badge). Set the key for real Sonnet narration
  (`llm_mode="anthropic"`). The real-model path is wired but was **not**
  exercised end-to-end in this environment (no key available here).
- No auth/resume UI beyond the localStorage session_id; no map panel, stats bar,
  or achievements (M6).

### Playtester run (adversarial, live API, stub mode)
Verdict: **PASS** on the M1 goal. Six issues found; the real ones fixed and
re-verified against the live server:
- **BUG-1 (critical, real):** concurrent turns on one session forked the
  checkpoint and silently dropped messages. Fixed with per-session locks in
  `main.py`; regression test + live 5-concurrent-turn check now yields 11/11
  messages.
- **BUG-3 (real):** whitespace-only message returned 200 → now 422 (validator
  strips, then enforces non-empty).
- **BUG-2 / BUG-4 / BUG-5 (stub-only):** missed "give me N gold"; broken
  "You I go north" echo grammar; false-positive deflection on legitimate
  exploration. Stub rewritten (regex intent detection + quoted echo). All
  verified.
- **BUG-6 (low, deferred):** no HTTP body-size limit. Deferred to M7, where
  per-player rate limits and daily token budgets live.

### Known issues / notes for later
- `_session_locks` grows one lock per session id for the process lifetime — fine
  for local dev; revisit with a TTL/LRU or a real lock manager before deploy.
- `SqliteSaver` uses a single shared connection (adequate for single-process
  dev). M7 swaps to `PostgresSaver` — one line in `graph._make_checkpointer`.
- Starlette's TestClient emits an httpx deprecation warning; cosmetic.
- The real Anthropic narration path is unverified pending an API key.

### Environment note
System Python here is 3.14 with no pip; per the pinned stack, Python **3.12** is
provisioned via `uv` (`uv sync` builds `.venv`). See `README.md`.
