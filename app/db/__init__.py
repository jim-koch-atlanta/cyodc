"""World-state persistence (SQLAlchemy). Works on SQLite (dev) and Postgres (M7).

This is our own schema — the single source of truth for inventory, stats, HP,
maps, and fog-of-war (invariant #1). It is deliberately separate from the
LangGraph checkpointer, which owns only session/conversation flow (invariant #2).
"""
