"""Streaming strategies must decide exactly as the windowed originals did.

``tests/legacy`` holds frozen copies of the pre-streaming strategies, which recomputed
every indicator over the candle list on each call. Fed the *whole* history up to bar
``i``, those are the reference; the streaming state folding in one bar at a time must
emit the identical signal (type and reason text) on every bar.
"""

from __future__ import annotations

import random

import pytest

from crypto_bot.core.models import HOLD, Candle, MarketContext, Signal, SignalType
from crypto_bot.strategies.base import Strategy, StrategyState, WindowedState
from crypto_bot.strategies.registry import build_strategy
from legacy import LEGACY


def _walk(n: int, seed: int, vol: float = 0.012) -> list[Candle]:
    rng = random.Random(seed)
    price = 100.0
    out = []
    for i in range(n):
        open_ = price
        price = max(0.5, price * (1 + rng.gauss(0, vol)))
        high = max(open_, price) * (1 + abs(rng.gauss(0, vol / 3)))
        low = min(open_, price) * (1 - abs(rng.gauss(0, vol / 3)))
        out.append(Candle(1_700_000_000_000 + i * 3_600_000, open_, high, low, price, 1.0))
    return out


CASES = [
    ("ma_crossover", {}),
    ("ma_crossover", {"fast_period": 3, "slow_period": 7, "ma_type": "sma"}),
    ("rsi_reversion", {}),
    ("rsi_reversion", {"period": 5, "oversold": 40, "overbought": 60}),
    ("breakout", {}),
    ("breakout", {"lookback": 3}),
    ("bollinger", {}),
    ("bollinger", {"period": 5, "num_std": 1.0}),
    ("macd", {}),
    ("macd", {"fast_period": 3, "slow_period": 6, "signal_period": 2}),
    ("supertrend", {}),
    ("supertrend", {"period": 4, "multiplier": 1.5}),
    ("dca", {"every": 3}),
    ("regime", {}),
    ("regime", {"adx_period": 5, "adx_threshold": 20,
                "trend": {"name": "macd", "params": {}},
                "range": {"name": "bollinger", "params": {"period": 10}}}),
    ("trend_ls", {}),
    ("trend_ls", {"lookback": 5, "adx_period": 4, "adx_threshold": 15, "trend_period": 0}),
    ("funding_bias", {"enter_apr": 0.05}),
    ("funding_bias", {"enter_apr": 0.05, "trend_period": 0}),
]


def _context(rng: random.Random, symbol: str) -> MarketContext:
    return MarketContext(symbol=symbol, funding_rate=rng.choice([None, -3e-4, 0.0, 1e-5, 4e-4]))


@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("name,params", CASES, ids=[f"{n}-{i}" for i, (n, _) in enumerate(CASES)])
def test_streaming_matches_legacy_full_history(name, params, seed):
    candles = _walk(260, seed)
    legacy = LEGACY[name](params)
    state = build_strategy(name, params).new_state("BTC/USDT")
    ctx_rng = random.Random(seed)
    actionable = 0
    for i, candle in enumerate(candles):
        ctx = _context(ctx_rng, "BTC/USDT")
        history = candles[: i + 1]
        if legacy.wants_context:
            expected = legacy.generate(history, "BTC/USDT", ctx)
        else:
            expected = legacy.generate(history, "BTC/USDT")
        got = state.update(candle, ctx)
        assert (got.type, got.reason) == (expected.type, expected.reason), f"bar {i}"
        actionable += got.is_actionable
    assert actionable > 0  # the comparison exercised real signals, not only HOLDs


class _Legacy(Strategy):
    """A third-party-style strategy that only implements the windowed generate()."""

    name = "legacy_probe"

    def __init__(self) -> None:
        super().__init__({})
        self.seen: list[int] = []

    @property
    def warmup(self) -> int:
        return 2

    def generate(self, candles, symbol=None):
        self.seen.append(len(candles))
        if len(candles) >= 2 and candles[-1].close > candles[-2].close:
            return Signal(SignalType.BUY, "up")
        return HOLD


def test_legacy_generate_strategies_run_through_a_rolling_window():
    strategy = _Legacy()
    state = strategy.new_state("BTC/USDT")
    assert isinstance(state, WindowedState)
    candles = _walk(250, 3)
    for c in candles:
        state.update(c)
    # The buffer grows to the legacy window (200) and then rolls.
    assert strategy.seen[:3] == [1, 2, 3]
    assert max(strategy.seen) == 200 and strategy.seen[-1] == 200


def test_windowed_state_passes_context_only_to_strategies_that_want_it():
    class _Ctx(_Legacy):
        wants_context = True

        def generate(self, candles, symbol=None, context=None):
            self.seen.append(context)
            return HOLD

    strategy = _Ctx()
    ctx = MarketContext(symbol="BTC/USDT", funding_rate=1e-4)
    strategy.new_state().update(_walk(1, 4)[0], ctx)
    assert strategy.seen == [ctx]


def test_a_strategy_must_implement_new_state_or_generate():
    class _Neither(Strategy):
        name = "neither"
        warmup = 1

    with pytest.raises(TypeError, match="must implement"):
        _Neither().new_state()
    with pytest.raises(TypeError, match="must implement"):
        _Neither().generate(_walk(3, 5))


def test_generate_replays_history_through_a_fresh_state():
    strategy = build_strategy("breakout", {"lookback": 3})
    candles = _walk(60, 6)
    state = strategy.new_state()
    streamed = [state.update(c) for c in candles][-1]
    assert strategy.generate(candles) == streamed
    assert isinstance(strategy.new_state(), StrategyState)
