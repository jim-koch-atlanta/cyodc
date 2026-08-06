"""story_log RAG (M5): record every narrated beat, recall the relevant older ones.

Writes are idempotent on (player_id, turn, role) so a replayed LangGraph node
never double-logs. Recall embeds the query, compares it (cosine) against this
player's rows sharing the ACTIVE embedding backend (never across backends), and
returns the best older matches for prompt injection. Retrieval is Python-side
and DB-agnostic (SQLite now, pgvector at deploy).

Memory is best-effort: any embedding failure is swallowed (a write is skipped, a
recall returns nothing) — it must never break a turn.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Player, StoryLog
from app.embeddings import active_tag, cosine, embed

logger = logging.getLogger("cyodc.memory")

RECALL_K = 4               # how many older beats to surface
EXCLUDE_RECENT_ROWS = 24   # skip rows already in the DM's verbatim window
_MIN_SIMILARITY = 0.12     # below this, a "match" is just noise
_SNIPPET = 220


def record_turn(
    db: Session,
    player: Player,
    turn: int,
    role: str,
    content: str,
    tag: str | None = None,
) -> None:
    """Append one beat. Idempotent: a replayed node re-attempting the same
    (player, turn, role) hits the UNIQUE constraint and is a no-op."""
    text = (content or "").strip()
    if not text:
        return
    try:
        vector = embed(text)
        model = active_tag()
    except Exception:  # embedding is best-effort; never break the turn
        logger.exception("embedding failed; skipping story_log write")
        return

    row = StoryLog(
        player_id=player.id,
        turn=turn,
        role=role,
        tag=tag,
        content=text,
        embedding=vector,
        embed_model=model,
    )
    try:
        with db.begin_nested():  # SAVEPOINT: a dup insert rolls back without
            db.add(row)          # poisoning the outer transaction
            db.flush()
    except IntegrityError:
        pass  # this beat is already logged (node replay) — no-op


def recall(
    db: Session,
    player: Player,
    query: str,
    *,
    k: int = RECALL_K,
    tag: str | None = None,
    exclude_recent: int = EXCLUDE_RECENT_ROWS,
) -> list[StoryLog]:
    """Older beats to surface, in chronological order.

    Two modes:
    - `tag` set (NPC memory): recency-first — the last k interactions with THAT
      entity ("does this merchant remember me?"). Not similarity-gated, since
      continuity matters more than word overlap (and the hash stub rarely
      produces overlap). The recent-window exclusion does not apply.
    - `tag` None (story recall): the k older beats most SIMILAR to `query`, above
      a small noise floor, with the verbatim recent window held out."""
    model = active_tag()

    if tag is not None:
        stmt = (
            select(StoryLog)
            .where(
                StoryLog.player_id == player.id,
                StoryLog.embed_model == model,
                StoryLog.tag == tag,
            )
            .order_by(StoryLog.id.desc())
        )
        rows = list(db.scalars(stmt).all())[:k]
        rows.sort(key=lambda r: r.id)
        return rows

    q = (query or "").strip()
    if not q:
        return []
    try:
        qvec = embed(q)
    except Exception:
        logger.exception("recall embedding failed")
        return []

    stmt = (
        select(StoryLog)
        .where(StoryLog.player_id == player.id, StoryLog.embed_model == model)
        .order_by(StoryLog.id)
    )
    rows = list(db.scalars(stmt).all())
    if exclude_recent:
        rows = rows[:-exclude_recent] if len(rows) > exclude_recent else []
    if not rows:
        return []

    scored = [(cosine(qvec, r.embedding), r) for r in rows]
    scored = [pair for pair in scored if pair[0] >= _MIN_SIMILARITY]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = [r for _, r in scored[:k]]
    top.sort(key=lambda r: r.id)  # present in the order things happened
    return top


def snippet(text: str) -> str:
    """A single-line, length-capped fragment for a recall block."""
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= _SNIPPET else one_line[: _SNIPPET - 1].rstrip() + "…"


def recall_block(rows: list[StoryLog], header: str) -> str | None:
    """Format recalled beats as an injectable context block, or None if empty."""
    if not rows:
        return None
    lines = [f"- {snippet(r.content)}" for r in rows]
    return header + "\n" + "\n".join(lines)
