"""Unit tests for the deterministic combat engine (no DB, no LLM)."""

from __future__ import annotations

from random import Random

import pytest

from app.combat import (
    Combatant,
    d20,
    flee_check,
    make_attack,
    parse_dice,
    resolve_round,
    rng_for_round,
    roll_dice,
    roll_initiative,
    summarize_round,
)


class FakeRNG:
    """Scripted rng: `randint` returns the next queued value (ignores bounds)."""

    def __init__(self, values):
        self.values = list(values)
        self.i = 0

    def randint(self, a, b):
        v = self.values[self.i]
        self.i += 1
        return v


def _player(**kw):
    base = dict(name="Contestant", hp=20, max_hp=20, ac=12, attack_bonus=3,
               damage_dice="1d6+1", initiative_bonus=2)
    base.update(kw)
    return Combatant(**base)


def _monster(**kw):
    base = dict(name="Goblin", hp=8, max_hp=8, ac=11, attack_bonus=2,
               damage_dice="1d6", initiative_bonus=0)
    base.update(kw)
    return Combatant(**base)


# --- dice ------------------------------------------------------------------
@pytest.mark.parametrize(
    "notation,expected",
    [("1d6", (1, 6, 0)), ("2d4+2", (2, 4, 2)), ("1d8-1", (1, 8, -1)), (" 3 d 10 + 5 ", (3, 10, 5))],
)
def test_parse_dice_valid(notation, expected):
    assert parse_dice(notation) == expected


@pytest.mark.parametrize("bad", ["", "d6", "1d", "six", "0d6", "1d0", "1x6"])
def test_parse_dice_invalid(bad):
    with pytest.raises(ValueError):
        parse_dice(bad)


def test_roll_dice_is_within_bounds_and_deterministic():
    for seed in range(50):
        a = roll_dice("2d6+3", Random(seed))
        b = roll_dice("2d6+3", Random(seed))
        assert a == b  # same seed -> same roll
        assert 5 <= a <= 15  # 2..12 + 3


def test_roll_dice_crit_rolls_dice_twice_not_the_modifier():
    # "2d6+3": normal = 2 dice + 3; crit = 4 dice + 3 (modifier added once).
    assert roll_dice("2d6+3", FakeRNG([1, 1]), crit=False) == 1 + 1 + 3
    assert roll_dice("2d6+3", FakeRNG([1, 1, 1, 1]), crit=True) == 1 + 1 + 1 + 1 + 3


def test_roll_dice_floored_at_one():
    assert roll_dice("1d4-10", FakeRNG([1])) == 1


# --- attacks ---------------------------------------------------------------
def test_nat_20_auto_hits_and_crits():
    monster = _monster(hp=8)
    ev = make_attack(_player(damage_dice="1d6"), monster, FakeRNG([20, 5, 6]))
    assert ev["hit"] and ev["crit"]
    assert ev["damage"] == 11  # two d6 rolled (5,6), no modifier
    assert monster.hp == 0  # 8 - 11 floored at 0


def test_nat_1_auto_misses_even_against_low_ac():
    monster = _monster(ac=1, hp=8)
    ev = make_attack(_player(attack_bonus=10), monster, FakeRNG([1]))
    assert ev["hit"] is False and ev["damage"] == 0
    assert monster.hp == 8


def test_normal_hit_and_miss_respect_ac():
    # attack_bonus +3 vs AC 12: roll 9 -> 12 hits; roll 8 -> 11 misses.
    hit = make_attack(_player(attack_bonus=3, damage_dice="1d6"), _monster(ac=12), FakeRNG([9, 4]))
    assert hit["hit"] is True and hit["damage"] == 4
    miss = make_attack(_player(attack_bonus=3), _monster(ac=12), FakeRNG([8]))
    assert miss["hit"] is False


def test_hp_never_goes_negative():
    monster = _monster(hp=2)
    make_attack(_player(damage_dice="1d6"), monster, FakeRNG([15, 6]))
    assert monster.hp == 0


# --- rounds ----------------------------------------------------------------
def test_attack_round_trades_blows_in_order():
    player, monster = _player(), _monster()
    # player rolls 10(+3=13 hit) dmg 3(+1=4); monster rolls 12(+2=14 hit) dmg 3
    result = resolve_round(player, monster, ["player", "monster"], "attack",
                           FakeRNG([10, 3, 12, 3]), round_num=1)
    assert result.outcome == "ongoing"
    assert monster.hp == 4  # 8 - 4
    assert player.hp == 17  # 20 - 3
    assert [e["attacker"] for e in result.events] == ["Contestant", "Goblin"]


def test_player_kill_ends_round_before_monster_acts():
    player, monster = _player(), _monster(hp=4)
    result = resolve_round(player, monster, ["player", "monster"], "attack",
                           FakeRNG([20, 6, 6]), round_num=1)  # crit kills
    assert result.outcome == "victory"
    assert monster.hp == 0
    assert player.hp == 20  # monster never got to swing
    assert len(result.events) == 1


def test_monster_first_can_defeat_player():
    player, monster = _player(hp=3), _monster()
    result = resolve_round(player, monster, ["monster", "player"], "attack",
                           FakeRNG([20, 6, 6]), round_num=1)
    assert result.outcome == "defeat"
    assert player.hp == 0


def test_flee_success_ends_fight_without_a_swing():
    player, monster = _player(), _monster()
    result = resolve_round(player, monster, ["player", "monster"], "flee",
                           FakeRNG([20]), round_num=2)  # high roll -> escapes
    assert result.outcome == "fled"
    assert player.hp == 20 and monster.hp == 8


def test_flee_failure_gives_the_monster_a_free_attack():
    player, monster = _player(), _monster()
    result = resolve_round(player, monster, ["player", "monster"], "flee",
                           FakeRNG([1, 15, 4]), round_num=2)  # fail, then monster hits
    assert result.outcome == "ongoing"
    assert player.hp == 16  # took the free hit


def test_pass_lets_only_the_monster_act():
    player, monster = _player(), _monster()
    result = resolve_round(player, monster, ["player", "monster"], "pass",
                           FakeRNG([15, 3]), round_num=1)
    assert monster.hp == 8  # player did not attack
    assert player.hp == 17


def test_initiative_and_flee_are_deterministic():
    for seed in range(20):
        assert roll_initiative(_player(), _monster(), Random(seed)) == \
               roll_initiative(_player(), _monster(), Random(seed))
        assert flee_check(_player(), _monster(), Random(seed)) == \
               flee_check(_player(), _monster(), Random(seed))


def test_full_fight_is_reproducible_from_seed():
    def run():
        p, m = _player(), _monster(hp=12)
        order = roll_initiative(p, m, rng_for_round(999, 0))
        rounds = []
        n = 1
        while True:
            r = resolve_round(p, m, order, "attack", rng_for_round(999, n), n)
            rounds.append((r.outcome, r.player_hp, r.monster_hp))
            if r.outcome != "ongoing":
                break
            n += 1
        return rounds

    assert run() == run()  # identical seed -> identical fight


def test_summarize_round_is_plain_and_covers_outcome():
    player, monster = _player(), _monster(hp=2)
    result = resolve_round(player, monster, ["player", "monster"], "attack",
                           FakeRNG([20, 6, 6]), round_num=1)
    text = summarize_round(result)
    assert "defeated" in text
    assert "Goblin" in text
