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


# -- exit_lookback ------------------------------------------------------------------
def test_shorter_exit_channel_closes_a_long_before_the_full_reversal(make_candles):
    strategy = Breakout({"lookback": 6, "exit_lookback": 2})
    # Above the 6-bar low (5) but below the 2-bar low (9): an exit, not a reversal.
    signal = strategy.generate(make_candles([5, 8, 10, 12, 10, 9, 8.5]))
    assert signal.type == SignalType.SELL and signal.reduce_only
    assert "2-bar exit low" in signal.reason
    # The same candles with no exit channel are simply held.
    assert Breakout({"lookback": 6}).generate(make_candles([5, 8, 10, 12, 10, 9, 8.5])).type == (
        SignalType.HOLD
    )


def test_shorter_exit_channel_covers_a_short(make_candles):
    strategy = Breakout({"lookback": 6, "exit_lookback": 2})
    signal = strategy.generate(make_candles([12, 9, 7, 5, 7, 8, 8.5]))
    assert signal.type == SignalType.BUY and signal.reduce_only
    assert "2-bar exit high" in signal.reason


def test_full_breakouts_still_take_priority_and_quiet_bars_hold(make_candles):
    strategy = Breakout({"lookback": 6, "exit_lookback": 2})
    entry = strategy.generate(make_candles([5, 8, 10, 12, 10, 9, 13]))
    assert entry.type == SignalType.BUY and not entry.reduce_only
    assert strategy.generate(make_candles([5, 8, 10, 12, 10, 9, 9.5])).type == SignalType.HOLD


def test_exit_lookback_must_not_exceed_lookback():
    for bad in (0, 7):
        with pytest.raises(ValueError, match="exit_lookback"):
            Breakout({"lookback": 6, "exit_lookback": bad})
