import pytest

from crypto_bot.core.models import SignalType
from crypto_bot.strategies.supertrend import Supertrend

# Short ATR period and a tight multiplier keep the fixtures compact (warmup = period + 2 = 5).
PARAMS = {"period": 3, "multiplier": 1.0}


def test_buy_when_trend_flips_up(make_candles):
    # A steady decline that reverses sharply: Supertrend flips from down to up on the last bar.
    candles = make_candles([100, 98, 96, 94, 92, 90, 88, 86, 100])
    signal = Supertrend(PARAMS).generate(candles)
    assert signal.type == SignalType.BUY
    assert "flipped up" in signal.reason


def test_sell_when_trend_flips_down(make_candles):
    # A steady climb that reverses sharply: Supertrend flips from up to down on the last bar.
    candles = make_candles([50, 52, 54, 56, 58, 60, 62, 64, 50])
    signal = Supertrend(PARAMS).generate(candles)
    assert signal.type == SignalType.SELL
    assert "flipped down" in signal.reason


def test_hold_when_flat(make_candles):
    assert Supertrend(PARAMS).generate(make_candles([10] * 12)).type == SignalType.HOLD


def test_hold_before_warmup(make_candles):
    # warmup is period + 2 = 5
    assert Supertrend(PARAMS).generate(make_candles([10, 9, 8, 7])).type == SignalType.HOLD


def test_rejects_nonpositive_period():
    with pytest.raises(ValueError):
        Supertrend({"period": 0})


def test_rejects_nonpositive_multiplier():
    with pytest.raises(ValueError):
        Supertrend({"period": 3, "multiplier": 0})
