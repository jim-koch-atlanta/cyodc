"""Test setup: isolate all state and force offline stub mode.

Env is set BEFORE any `app.*` import so the settings singleton, the SqliteSaver
checkpoint DB, and the SQLAlchemy world-state engine all pick up throwaway files
and never touch a real key. The world DB is reset between tests for isolation.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

_tmp = tempfile.mkdtemp(prefix="cyodc-test-")
os.environ["CYODC_CHECKPOINT_DB"] = str(pathlib.Path(_tmp) / "checkpoints.sqlite")
os.environ["CYODC_DATABASE_URL"] = f"sqlite:///{pathlib.Path(_tmp) / 'game.sqlite'}"
os.environ["CYODC_LLM_MODE"] = "stub"
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402

from app.db.base import Base, engine, init_db  # noqa: E402

init_db()


@pytest.fixture(autouse=True)
def _reset_world_db():
    """Give each test a clean world-state DB."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
