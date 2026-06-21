import pytest

from crypto_bot.core.models import SignalType
from crypto_bot.strategies.macd import MACDMomentum

# Short, fast periods keep the fixtures compact (warmup = slow + signal = 9).
PARAMS = {"fast_period": 3, "slow_period": 6, "signal_period": 3}

# A long uptrend then a dip-and-rip: the MACD line crosses *above* its signal line on the
# final bar. Built so the cross lands after the warm-up (it is edge-triggered on the last bar).
_UP = list(range(10, 28))
BUY_SERIES = (_UP + [26, 24, 22, 20, 18, 16, 14, 16, 19, 23, 28])[:26]

# Mirror image: long downtrend then a pop-and-drop -> MACD crosses *below* signal on the last bar.
_DOWN = list(range(40, 22, -1))
SELL_SERIES = (_DOWN + [24, 26, 28, 30, 32, 34, 36, 34, 31, 27, 22])[:26]


def test_buy_on_bullish_crossover(make_candles):
    signal = MACDMomentum(PARAMS).generate(make_candles(BUY_SERIES))
    assert signal.type == SignalType.BUY
    assert "above signal" in signal.reason


def test_sell_on_bearish_crossover(make_candles):
    signal = MACDMomentum(PARAMS).generate(make_candles(SELL_SERIES))
    assert signal.type == SignalType.SELL
    assert "below signal" in signal.reason


def test_hold_when_flat(make_candles):
    assert MACDMomentum(PARAMS).generate(make_candles([10] * 20)).type == SignalType.HOLD


def test_hold_before_warmup(make_candles):
    # warmup is slow + signal = 9
    assert MACDMomentum(PARAMS).generate(make_candles([1, 2, 3, 4, 5])).type == SignalType.HOLD


def test_rejects_fast_ge_slow():
    with pytest.raises(ValueError):
        MACDMomentum({"fast_period": 6, "slow_period": 6})


def test_rejects_nonpositive_signal():
    with pytest.raises(ValueError):
        MACDMomentum({"fast_period": 3, "slow_period": 6, "signal_period": 0})
