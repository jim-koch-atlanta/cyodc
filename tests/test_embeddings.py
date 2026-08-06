"""Embedding gateway: deterministic offline backend + cosine behavior.

conftest forces stub mode, so these exercise the hashing backend (no fastembed,
no network) — the same path tests and CI always take."""

from __future__ import annotations

from app.embeddings import active_tag, cosine, embed


def test_stub_backend_is_active_and_dimensioned():
    assert active_tag() == "hash-256"
    v = embed("a shambling heap of clipboards")
    assert len(v) == 256


def test_embedding_is_deterministic():
    assert embed("the filing specter drifts past") == embed("the filing specter drifts past")


def test_cosine_self_is_one_and_bounded():
    v = embed("Brindle Mox sells a lantern")
    assert abs(cosine(v, v) - 1.0) < 1e-9
    assert -1.0 - 1e-9 <= cosine(v, embed("a wall blocks the way")) <= 1.0 + 1e-9


def test_shared_vocabulary_scores_higher_than_unrelated():
    query = embed("I want to buy a healing poultice from the merchant")
    related = embed("the merchant offers a healing poultice for sale")
    unrelated = embed("a wall of iron blocks the northern staircase")
    assert cosine(query, related) > cosine(query, unrelated)


def test_cosine_guards_length_mismatch_and_empty():
    assert cosine([], [1.0]) == 0.0
    assert cosine([1.0, 2.0], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
