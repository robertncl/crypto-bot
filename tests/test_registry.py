"""Strategy registry: name -> class lookup, duplicate/unknown-name errors."""

from __future__ import annotations

import pytest

from crypto_bot.strategies.base import Strategy
from crypto_bot.strategies.registry import (
    _REGISTRY,
    available_strategies,
    build_strategy,
    register_strategy,
)


def test_available_strategies_lists_the_builtins():
    names = available_strategies()
    assert names == sorted(names)
    for expected in ("ma_crossover", "rsi_reversion", "breakout", "dca", "regime"):
        assert expected in names


def test_build_strategy_constructs_the_registered_class():
    strategy = build_strategy("ma_crossover", {"fast_period": 3, "slow_period": 10})
    assert strategy.fast_period == 3


def test_build_strategy_name_lookup_is_case_insensitive():
    assert type(build_strategy("MA_Crossover")) is type(build_strategy("ma_crossover"))


def test_build_strategy_rejects_unknown_name():
    with pytest.raises(KeyError, match="unknown strategy"):
        build_strategy("not_a_real_strategy")


def test_register_strategy_rejects_a_conflicting_duplicate_name():
    class _Dummy(Strategy):
        name = "ma_crossover"  # collides with the real MACrossover class

        @property
        def warmup(self):
            return 1

        def generate(self, candles, symbol=None):
            raise NotImplementedError

    with pytest.raises(ValueError, match="already registered"):
        register_strategy(_Dummy)


def test_register_strategy_is_idempotent_for_the_same_class():
    # Re-registering the exact same class (not just the same name) must not raise.
    cls = _REGISTRY["dca"]
    assert register_strategy(cls) is cls
