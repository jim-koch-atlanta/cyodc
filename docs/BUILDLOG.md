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
  (`llm_mode="anthropic"`). The real-model (Sonnet) path is verified end-to-end
  — see the addendum below.
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

### Environment note
System Python here is 3.14 with no pip; per the pinned stack, Python **3.12** is
provisioned via `uv` (`uv sync` builds `.venv`). See `README.md`.

### Addendum — real Sonnet path verified + cold-open fix
Added an `ANTHROPIC_API_KEY` and exercised the live model, which surfaced a bug
the stub had masked (exactly the flagged risk of never calling the API in tests):
the cold-open invoked the graph with an empty message window, so `run_agent` sent
Anthropic a system prompt and **zero** messages → `400 messages: at least one
message is required`. The same class of error would hit every later turn, since
our stored history begins with the DM's cold-open (an *assistant* message) and
Anthropic requires a non-empty, **user-first** list.

Fix: `llm._to_anthropic_history` prepends an ephemeral (never-persisted) user
"kickoff" message when history is empty or assistant-first, keeping turns strictly
alternating. Verified over HTTP: cold-open + multiple turns return 200 with
in-character Sonnet narration; resume history stays ordered (dm/player/dm…); a
prompt-injection + self-grant attempt ("999 HP and the Legendary Sword") was
deflected in-character with no state granted (invariants #1/#3 hold with the real
model). Added 3 regression unit tests for the shaping (49 pytest total).

---

## 2026-07-25 — M2: World state + tools (done)

**Goal met:** the DB owns the world; the DM changes it only through typed tools.

### What exists
- **World schema (SQLAlchemy 2.0, `app/db/`)** — `players, items, inventory,
  levels, rooms, visibility`. Works on SQLite (dev) and Postgres (M7): generic
  `JSON`, `String`+`CheckConstraint` for room kind, `Index` on `rooms.level_id`,
  UNIQUE on inventory/levels/visibility, FK `ondelete=CASCADE` (item catalog is
  RESTRICT), portable `CURRENT_TIMESTAMP`, and a SQLite `PRAGMA foreign_keys=ON`
  connect listener. Separate `CYODC_DATABASE_URL` from the checkpoint DB.
- **BSP map generator (`app/mapgen.py`)** — deterministic, seeded; grid + room
  rects + connecting corridors. Unit-tested: determinism, border walls,
  in-bounds rooms, full connectivity (flood-fill), serialization round-trip.
- **Level seeding (`app/dungeon.py`)** — materializes a floor from BSP geometry +
  `app/content/floor01.json` (game-writer): assigns room kinds
  (entrance/chamber/treasure/exit), places items, sets the start position, and
  seeds fog-of-war. Per-player dungeons (no cross-session bleed). Also hosts the
  shared world helpers (current room, exits, reveal, fog-map render).
- **Five typed tools (`app/tools/`)** — look, move, take, use, inventory. Pydantic
  result models. `session`/`player_id` are `InjectedToolArg`s (the model never
  sees them). Replay-safe: mutations gate on a one-time state transition (take
  removes-from-room before adding to inventory; use consumes as the heal/gold
  gate) — safe under LangGraph node re-execution.
- **Tool-calling DM (`app/nodes/tool_loop.py`, `dm.py`)** — in-node loop, hard
  cap `MAX_TOOL_ROUNDS=3`, final round forced `tools=None` so a turn never ends on
  a dangling `tool_use`. The whole exchange is returned at once → one atomic
  checkpoint. `DMState` stays `{messages, next_node}`; world state never leaks in.
- **`llm.run_agent_with_tools`** binds tools for the DM; a stub command→tool
  router keeps the game playable offline (and lets the playtester exercise M2).
- **API (`app/main.py`)** — session-create provisions a player + floor 1; new
  `GET /api/session/{id}/state` returns stats, room, exits, inventory, and a
  fog-of-war ASCII map. Empty tool-call AIMessages filtered from the client view.

### What's stubbed / deferred (by design)
- **No worldgen agent yet** — room descriptions/items are hand-seeded from
  `floor01.json` (M4 swaps the content source for an LLM; the DB shape stays).
- **No combat / level transition** — floor 1 only; `next_node` still always
  `None` (M3 wires the conditional edge + a routing tool).
- **Frontend unchanged** — the map panel/stats UI is M6; `/state` exists now so
  M6 has a data source. `use` "light" has no persistent lighting mechanic yet.

### Reviews
- **langgraph-architect** (CHANGES-REQUIRED, all folded in): InjectedToolArg over
  closures, in-node loop over ToolNode, atomic message return, `run_agent_with_tools`.
- **schema-guardian**: **APPROVE-FOR-COMMIT** — every required fix verified
  (constraints, cascades, portable defaults, replay-safe tools). Follow-up for
  M4: validate `items.effects` through a Pydantic `ItemEffects` model in `use()`.
- **playtester**: M2 meets its goal; invariant #1 held against every cheat /
  prompt-injection / SQL-injection attempt; session isolation + idempotency
  confirmed. 6 findings; fixed the real ones and re-verified live:
  - BUG-1 (heal consumed at full HP for 0 benefit) → only consume if effective.
  - BUG-2 (manip regex false-positive on "HP is 20"/"INT is 10") → stat+number
    threshold raised to 3+ digits; 2-digit self-grants still caught by other rules.
  - BUG-3 (`supervisors-memo` never placed) → placed in an exit/chamber.
  - BUG-4/6 (stub move missed "head to the north") → composed the regex.
  - BUG-5 (broad `\bartifact\b`/`\blegendary\b` manip words) → left for now;
    revisit before M4 worldgen may name items that way.

### Known issues / notes for later
- The DM node holds one DB transaction open across the turn's model call(s).
  Fine at M2 scale; revisit for Postgres/App Runner (M7) if it causes lock
  contention.
- `use()` casts `int(effects[...])` without validating the catalog value — safe
  today (hand-authored), but add the `ItemEffects` Pydantic guard before M4.
- `_session_locks` still grows one lock per session for the process lifetime.

### Tests
85 pytest, all green, no network/key required (8 mapgen, 9 tools, 8 dungeon, 16
api, 7 graph, 6 routing, 31 stub). Tool execution is tested with a fake LLM that
emits tool_calls; the graph loop's cap is tested with a misbehaving fake.
