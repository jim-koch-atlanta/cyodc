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

---

## 2026-07-25 — M3: Combat (done)

**Goal met:** a deterministic combat engine owns the math; a fight survives a
disconnect; the DM routes into and out of combat.

### What exists
- **Combat engine (`app/combat.py`)** — pure, seeded Python: dice parsing,
  initiative, to-hit (d20+bonus vs AC), damage, nat-20 crit, nat-1 miss, flee.
  Per-round dice derive from `(seed, round)`, so a round is reproducible across a
  reconnect without persisting RNG state. Heavily unit-tested.
- **`encounters` table** — active combat state (monster JSON snapshot, monster_hp,
  round, seed, turn_order). `UNIQUE(player_id)` = one fight at a time; the row
  exists iff a fight is active and is deleted (with rewards) when it ends. Also
  fixed `Level.seed`/`Encounter.seed` to `BigInteger` (the SHA-256-derived seed
  overflows Postgres `INTEGER`).
- **Combat lifecycle (`app/encounters.py`)** — `start_encounter` (snapshot +
  initiative), `resolve_combat_turn` (one round -> persist), `_end_encounter`
  (delete-rowcount gate for replay-safe rewards). Combatants derive from stats
  (DEX->AC, STR->to-hit/damage).
- **Combat node (`app/nodes/combat.py`)** — classifies the action
  deterministically (attack/flee/use), runs the engine, then narrates the
  engine's factual summary on **Haiku** — OUTSIDE the DB transaction, with a
  factual fallback if narration fails.
- **Routing** — the DM node short-circuits to combat with ZERO model calls while
  a fight is active (Haiku per round, Sonnet only at the seams); the conditional
  edge `dm -> {combat, END}` finally activates `route_from_dm` + `next_node`.
  `combat -> END` (one round per turn).
- **Monsters** — 4 hand-authored floor-1 monsters (`floor01.json` "monsters"),
  seeded into non-entrance rooms (exit always guarded), shown in `look`/`move`
  and `/state.monsters_here`. `/state` now reports active `combat`.
- **Offline** — the stub routes "attack <x>" -> start_combat and narrates rounds,
  so combat is fully playable without a key.

### What's stubbed / deferred (by design)
- **No worldgen agent** (M4) — monsters/rooms still hand-seeded; floor 1 only.
- **No leveling** — monsters carry `xp` in their snapshot, but there's no
  `players.xp` column yet, so XP isn't applied (M5). Victory awards gold only.
- **Defeat = revive to 1 HP** (sponsor "auto-reviver") — no real death/respawn
  system yet. **Flee** leaves the player in the room and restores the monster to
  it (no movement-on-flee).
- Frontend unchanged (map/combat UI is M6; `/state.combat` is the data source).

### Reviews
- **langgraph-architect** (CHANGES-REQUIRED, folded in): NO entry router — the DM
  stays the hub and short-circuits on an active-encounter DB check; don't resolve
  round 1 in a separate creation turn; combat builds its own minimal Haiku context
  (no raw tool-history adjacency); combat -> END.
- **schema-guardian**: **APPROVE-FOR-COMMIT** — verified BigInteger seeds, encounter
  constraints/cascade, the atomic start_combat gate, and the delete-rowcount
  reward gate. Documented the exact M7 Postgres migration (ALTER `levels.seed` ->
  BIGINT + create `encounters`).
- **game-writer**: `app/prompts/combat.md` (Haiku, narrate engine events only) +
  4 balanced monsters.
- **playtester**: M3 meets its goal; invariant #1/#3 held against every attempt to
  dictate damage/HP/gold in prose; UNIQUE + delete-gate held under concurrency.
  Fixed the real findings and re-verified:
  - SEV-1: heal consumed at full HP *in combat* -> now kept, turn not spent.
  - SEV-2: a fled monster was deleted from the world -> restored to the room.
  - SEV-2: combat item-matching lacked token matching -> unified via
    `dungeon.match_slug` (so "use the pudding" works in and out of combat).

### Known issues / notes for later
- Alembic still deferred to M7 (`init_db` uses `create_all`); the `Level.seed`
  Integer->BigInteger change is a no-op on SQLite (dynamic typing) but needs an
  `ALTER` on Postgres — migration SQL is recorded in the schema-guardian review.
- Combat narrates *outside* the DB transaction (improvement over M2's DM node);
  a hard crash after a round commits but before the HTTP response could, on a
  future retry, resolve one extra round — a documented M7-scale edge, not a
  local risk.

### Tests
123 pytest, all green, no network/key (27 combat engine, 11 combat flow, + the
M1/M2 suite). The engine is tested with a scripted RNG for exact mechanics;
routing into/out of combat is tested through the graph with the deterministic
engine; reward replay-safety is tested by double-calling the end-of-fight gate.

---

## 2026-08-02 — M4: Worldgen agent + level transitions (done)

**Goal met:** a Sonnet worldgen agent decorates a deterministic BSP skeleton and
writes the floor to the DB; the player descends between floors; the LLM never
owns geometry or numbers.

### What exists
- **Worldgen agent (`app/worldgen.py`, `app/prompts/worldgen.md`)** — given the
  BSP room skeleton (index/kind/size), Sonnet returns strict JSON decorating each
  room: theme, per-room description, item/monster placement. It picks a monster
  **tier** and an item **effect** from fixed menus; it emits no numbers. Output is
  validated through Pydantic (`WorldgenOutput`, unknown tier/effect coerced to
  safe defaults) before it touches the DB. Any failure (stub mode, timeout, bad
  JSON) falls back to deterministic content recycled from floor01.json, so
  descending never breaks.
- **Code-owned balance (`app/balance.py`)** — tier stat-blocks + item-effect menu
  → concrete numbers scaled/clamped by floor depth (invariant #3). Verified live:
  real Sonnet produced fresh themed floors ("Records Retention", monsters like
  "Compliance Auditor") with every stat set by code.
- **Level transitions** — a `descend` tool (read-only signal; validates you're on
  the exit and unguarded) makes the DM route to a new **worldgen node**. The node
  runs in three phases (read → one Sonnet call with NO DB held → materialize +
  place at the new entrance), replay-safe via an explicit `SELECT` before insert
  and get-or-create of visibility. `worldgen -> END`; the arrival is narrated from
  the generated entrance description (one model call for the whole descend turn).
- **Storage** — generated items go in the `items` table scoped by the new nullable
  `items.player_id` (cascade on delete; NULL = shared catalog). Generated monster
  defs live in the new `levels.monster_catalog` JSON column, so combat resolves
  stats on any floor. Added composite indexes (`ix_levels_player_floor`,
  `ix_visibility_player_level`, `ix_items_player_id`).
- **Floor 1 stays hand-seeded** (fast, offline, known-good); floors 2+ are
  generated. `/state` gains `can_descend`; the stub routes "descend"/"stairs".

### Reviews
- **langgraph-architect** (CHANGES-REQUIRED, folded in): three-phase node (no DB
  held across the model call), descend signaled via a ToolMessage scan (no DB
  marker), `worldgen -> END`, explicit-SELECT replay guard.
- **schema-guardian**: **APPROVE-FOR-COMMIT** — verified item scoping, the
  `monster_catalog` column, the indexes, Pydantic-validated generated content, and
  fixed a latent defect (combat previously only read floor01.json →
  `load_monster_catalog(floor, override)` now resolves the per-level catalog).
  M7 migration SQL documented (2 nullable columns + 3 indexes).
- **game-writer**: `worldgen.md` ("Senior Floor Decorator" voice; decorates the
  skeleton, picks tier/effect, never numbers).
- **playtester**: M4 meets its goal; geometry deterministic, combat works on
  generated floors, transitions replay-safe, invariant #1 held (no self-granted
  floor jumps). Fixed the real findings and re-verified:
  - a guarded exit could be walked past → descent now blocked until the guard is
    defeated (defeating it clears it from the room).
  - stub-fallback monsters used floor-1 stats on deep floors → floor01 monsters
    tagged with a tier and scaled by depth.

### Known issues / notes for later
- Worldgen gets a 120s timeout + 4096 max_tokens (it's a big between-levels call);
  on timeout it falls back to deterministic content — proven live when the first
  real call timed out and the descend still succeeded.
- Still no leveling: monsters carry `xp` but there's no `players.xp` column (M5).
- Alembic still deferred to M7; the two new nullable columns + three indexes are
  additive. Devs must delete a stale M3 dev DB (create_all won't add columns).
- Test-harness note: mutating the world DB from a bare session and immediately
  reading it back through the graph is flaky under the SQLite test pool; tests set
  such state up inside the provisioning session. The game never does this — tools
  mutate within the graph's own session — and the real fight-guard-then-descend
  flow is verified across seeds and live over HTTP.

### Tests
139 pytest, all green, no network/key. Worldgen validation/clamping is tested
with a fake LLM (bad tiers/effects coerced; numbers always code-owned); the
fallback + geometry determinism are pinned; the transition flow (descend creates
floor 2, refuses off-exit, can't skip floors, combat on a generated floor) runs
through the real graph.
