"""Application settings.

Single source of truth for env-driven configuration. Reads a local `.env`
when present. `ANTHROPIC_API_KEY` uses its conventional (unprefixed) name;
everything else is namespaced under `CYODC_`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMMode = Literal["auto", "anthropic", "stub"]
EmbedMode = Literal["auto", "local", "stub"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str | None = Field(
        default=None, validation_alias="ANTHROPIC_API_KEY"
    )
    checkpoint_db: str = Field(
        default="./data/checkpoints.sqlite",
        validation_alias="CYODC_CHECKPOINT_DB",
    )
    # World-state DB (SQLAlchemy) — SEPARATE from the LangGraph checkpoint DB so
    # our tables never collide with the checkpointer's `checkpoints`/`writes`.
    database_url: str = Field(
        default="sqlite:///./data/game.sqlite",
        validation_alias="CYODC_DATABASE_URL",
    )
    frontend_origin: str = Field(
        default="http://localhost:5173",
        validation_alias="CYODC_FRONTEND_ORIGIN",
    )
    # "auto" resolves to "anthropic" when a key is present, else "stub".
    llm_mode: LLMMode = Field(default="auto", validation_alias="CYODC_LLM_MODE")
    # Embedding backend for story_log RAG. "auto" follows the LLM mode: a real
    # run ("anthropic") uses the local fastembed model; offline/tests ("stub")
    # use the deterministic hashing embedding. See app/embeddings.py.
    embed_mode: EmbedMode = Field(default="auto", validation_alias="CYODC_EMBED_MODE")

    @property
    def resolved_llm_mode(self) -> Literal["anthropic", "stub"]:
        """Concrete mode after resolving "auto"."""
        if self.llm_mode != "auto":
            return self.llm_mode  # type: ignore[return-value]
        return "anthropic" if self.anthropic_api_key else "stub"

    @property
    def resolved_embed_mode(self) -> Literal["local", "stub"]:
        """Concrete embedding backend after resolving "auto" (follows LLM mode)."""
        if self.embed_mode != "auto":
            return self.embed_mode  # type: ignore[return-value]
        return "local" if self.resolved_llm_mode == "anthropic" else "stub"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
