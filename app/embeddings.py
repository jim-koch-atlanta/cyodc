"""Embedding gateway — local semantic vectors for the story_log RAG (M5).

The real backend is **fastembed** (BAAI/bge-small-en-v1.5, 384-dim, ONNX/CPU —
no torch). It loads lazily and is cached process-wide; the model downloads once
on first use. This is the analog of `app/llm.py` but for vectors: one place the
embedding backend is chosen.

Offline / tests / stub mode use a deterministic **hashing** embedding (no
network, no heavy deps, reproducible) so the whole loop stays playable without
the model — the same stub philosophy as the LLM gateway. The two backends
produce incomparable vectors, so every stored row carries the active backend's
tag (`embed_model`) and recall only ever compares vectors sharing that tag.

The backend is resolved ONCE per process (the fastembed model is cached, or we
degrade to hashing permanently if it can't load), so a row's vector and its
stored tag are always consistent.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from functools import lru_cache

from app.config import get_settings

logger = logging.getLogger("cyodc.embeddings")

_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
_HASH_DIM = 256
_HASH_TAG = f"hash-{_HASH_DIM}"

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Function words carry little topical signal but are frequent enough to dominate
# a lexical (hashing) vector; dropping them lets content words drive similarity.
# (A real transformer backend handles this itself — this is a stub-only concern.)
_STOPWORDS = frozenset(
    "a an the of to in on at is are was were be been being do does did doing have "
    "has had you your yours i me my mine we us our ours it its this that these those "
    "and or but if then so as for with from by about into over under out up down off "
    "he she they them his her their what which who whom where when why how all any "
    "some no not can will would should could may might must here there".split()
)


# --- deterministic hashing backend (offline / tests) ------------------------
def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _hash_embed(text: str) -> list[float]:
    """Signed feature-hashing embedding -> L2-normalized vector. Deterministic:
    shared words yield similar vectors (lexical similarity), which is enough for
    offline play and reproducible tests."""
    vec = [0.0] * _HASH_DIM
    for tok in _tokens(text):
        h = int.from_bytes(hashlib.sha256(tok.encode("utf-8")).digest()[:8], "big")
        vec[h % _HASH_DIM] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


# --- real fastembed backend -------------------------------------------------
@lru_cache(maxsize=1)
def _fastembed_model():
    """Load the fastembed model once per process (downloads on first use)."""
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=_LOCAL_MODEL)


def _resolved() -> tuple:
    """(embed_fn, tag) for the active backend. Resolved to a stable pair: if the
    real model is requested but unavailable, degrade to hashing for the process
    so vectors and their stored tags never diverge."""
    if get_settings().resolved_embed_mode != "local":
        return _hash_embed, _HASH_TAG
    try:
        model = _fastembed_model()
    except Exception:  # missing dep / no network on first download
        logger.exception("fastembed unavailable; falling back to hash embeddings")
        return _hash_embed, _HASH_TAG

    def _local_embed(text: str) -> list[float]:
        return [float(x) for x in next(iter(model.embed([text])))]

    return _local_embed, _LOCAL_MODEL


def active_tag() -> str:
    """The active backend's tag; store it on every row and filter recall by it."""
    return _resolved()[1]


def embed(text: str) -> list[float]:
    """Embed one string with the active backend. May raise if the real model
    fails mid-call; callers (memory.record_turn / recall) treat memory as
    best-effort and skip on failure."""
    fn, _ = _resolved()
    return fn(text)


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, guarding zero vectors and length mismatch (e.g. a stray
    cross-backend comparison, which recall's tag filter should already prevent)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
