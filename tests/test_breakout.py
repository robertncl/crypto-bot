import pytest

from crypto_bot.core.models import SignalType
from crypto_bot.strategies.breakout import Breakout

PARAMS = {"lookback": 3}


def test_buy_on_upside_breakout(make_candles):
    # Flat range, then the last close jumps above the prior 3-bar high.
    candles = make_candles([5, 5, 5, 5, 10])
    signal = Breakout(PARAMS).generate(candles)
    assert signal.type == SignalType.BUY
    assert "high" in signal.reason


def test_sell_on_downside_breakdown(make_candles):
    # Flat range, then the last close drops below the prior 3-bar low.
    candles = make_candles([5, 5, 5, 5, 1])
    signal = Breakout(PARAMS).generate(candles)
    assert signal.type == SignalType.SELL
    assert "low" in signal.reason


def test_hold_inside_channel(make_candles):
    # Touching the prior high without exceeding it must not trigger (strict break).
    assert Breakout(PARAMS).generate(make_candles([5, 5, 5, 5, 5])).type == SignalType.HOLD


def test_hold_before_warmup(make_candles):
    # warmup is lookback + 1 = 4
    assert Breakout(PARAMS).generate(make_candles([5, 6, 7])).type == SignalType.HOLD


def test_rejects_nonpositive_lookback():
    with pytest.raises(ValueError):
        Breakout({"lookback": 0})


def test_signal_ignores_history_beyond_the_channel(make_candles):
    # The strategy feeds the indicators only the `lookback + 1` bars the Donchian
    # channel actually depends on, rather than the engine's whole ~200-bar buffer.
    # That is only safe because older bars cannot change the signal — pin it.
    import random

    rng = random.Random(4)
    strategy = Breakout(PARAMS)
    for _ in range(200):
        recent = [rng.uniform(1, 20) for _ in range(strategy.warmup)]
        older = [rng.uniform(1, 20) for _ in range(150)]
        minimal = strategy.generate(make_candles(recent))
        padded = strategy.generate(make_candles(older + recent))
        assert (minimal.type, minimal.reason) == (padded.type, padded.reason)


def test_hold_when_channel_not_yet_defined(make_candles, monkeypatch):
    # highest()/lowest() are provably defined at the compared bar for every input at
    # exactly `warmup` candles, so this guard is unreachable with well-formed data.
    # Force it via the indicator calls to prove it still holds if that ever changed.
    import crypto_bot.strategies.breakout as mod

    monkeypatch.setattr(mod, "highest", lambda values, period: [None] * len(values))
    monkeypatch.setattr(mod, "lowest", lambda values, period: [None] * len(values))
    strategy = Breakout(PARAMS)
    candles = make_candles([10] * strategy.warmup)
    assert strategy.generate(candles).type == SignalType.HOLD
