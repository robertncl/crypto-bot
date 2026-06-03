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
