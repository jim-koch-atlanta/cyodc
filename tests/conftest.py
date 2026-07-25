"""Test setup: isolate state and force offline stub mode.

Env is set BEFORE any `app.*` import so the settings singleton and the
module-level SqliteSaver pick up a throwaway DB and never touch a real key.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

_tmp = tempfile.mkdtemp(prefix="cyodc-test-")
os.environ["CYODC_CHECKPOINT_DB"] = str(pathlib.Path(_tmp) / "checkpoints.sqlite")
os.environ["CYODC_LLM_MODE"] = "stub"
os.environ.pop("ANTHROPIC_API_KEY", None)
