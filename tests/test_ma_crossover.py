import pytest

from crypto_bot.core.models import SignalType
from crypto_bot.strategies.ma_crossover import MACrossover

PARAMS = {"fast_period": 2, "slow_period": 4, "ma_type": "sma"}


def test_buy_on_cross_up(make_candles):
    # Falling then sharply rising: fast SMA crosses above slow SMA on the last bar.
    candles = make_candles([10, 9, 8, 7, 6, 5, 7, 10])
    signal = MACrossover(PARAMS).generate(candles)
    assert signal.type == SignalType.BUY
    assert "crossed above" in signal.reason


def test_sell_on_cross_down(make_candles):
    # Rising then sharply falling: fast SMA crosses below slow SMA on the last bar.
    candles = make_candles([5, 6, 7, 8, 9, 10, 8, 5])
    signal = MACrossover(PARAMS).generate(candles)
    assert signal.type == SignalType.SELL
    assert "crossed below" in signal.reason


def test_hold_when_flat(make_candles):
    candles = make_candles([10] * 10)
    assert MACrossover(PARAMS).generate(candles).type == SignalType.HOLD


def test_hold_before_warmup(make_candles):
    candles = make_candles([1, 2, 3])  # fewer than warmup (slow + 1 = 5)
    assert MACrossover(PARAMS).generate(candles).type == SignalType.HOLD


def test_rejects_fast_ge_slow():
    with pytest.raises(ValueError):
        MACrossover({"fast_period": 5, "slow_period": 5})


def test_hold_when_indicator_not_yet_defined(make_candles, monkeypatch):
    # Both moving averages are provably defined for every input at exactly `warmup`
    # candles (checked by brute force across params), so this guard can't be reached
    # with well-formed data. Force it via the indicator call to prove it still holds
    # if that invariant were ever violated.
    import crypto_bot.strategies.ma_crossover as mod

    monkeypatch.setattr(mod, "moving_average", lambda closes, period, kind: [None] * len(closes))
    strategy = MACrossover(PARAMS)
    candles = make_candles([10] * strategy.warmup)
    assert strategy.generate(candles).type == SignalType.HOLD


def test_rejects_nonpositive_periods():
    with pytest.raises(ValueError):
        MACrossover({"fast_period": 0, "slow_period": 4})
    with pytest.raises(ValueError):
        MACrossover({"fast_period": 4, "slow_period": 0})
