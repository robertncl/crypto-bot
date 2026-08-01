import pytest

from crypto_bot.core.models import SignalType
from crypto_bot.strategies.rsi_reversion import RSIReversion

PARAMS = {"period": 5, "oversold": 30, "overbought": 70}


def _ramp(start: float, step: float, n: int) -> list[float]:
    return [start + step * i for i in range(n)]


def test_buy_on_recovery_through_oversold(make_candles):
    # A long, steady decline pins RSI at ~0; a sharp up-bar pops it back above 30.
    closes = _ramp(100, -1, 21)
    closes.append(closes[-1] + 2)
    signal = RSIReversion(PARAMS).generate(make_candles(closes))
    assert signal.type == SignalType.BUY
    assert "oversold" in signal.reason


def test_sell_on_drop_through_overbought(make_candles):
    # A long, steady rise pins RSI at ~100; a sharp down-bar drops it back below 70.
    closes = _ramp(80, 1, 21)
    closes.append(closes[-1] - 2)
    signal = RSIReversion(PARAMS).generate(make_candles(closes))
    assert signal.type == SignalType.SELL
    assert "overbought" in signal.reason


def test_hold_when_flat(make_candles):
    assert RSIReversion(PARAMS).generate(make_candles([10] * 20)).type == SignalType.HOLD


def test_hold_before_warmup(make_candles):
    # warmup is period + 2 = 7
    assert RSIReversion(PARAMS).generate(make_candles([1, 2, 3])).type == SignalType.HOLD


def test_rejects_inverted_thresholds():
    with pytest.raises(ValueError):
        RSIReversion({"oversold": 70, "overbought": 30})


def test_rejects_nonpositive_period():
    with pytest.raises(ValueError):
        RSIReversion({"period": 0})


def test_hold_when_indicator_not_yet_defined(make_candles, monkeypatch):
    # rsi() is provably defined at both compared bars for every input at exactly
    # `warmup` candles, so this guard is unreachable with well-formed data. Force it
    # via the indicator call to prove it still holds if that ever changed.
    import crypto_bot.strategies.rsi_reversion as mod

    monkeypatch.setattr(mod, "rsi", lambda closes, period: [None] * len(closes))
    strategy = RSIReversion(PARAMS)
    candles = make_candles([10] * strategy.warmup)
    assert strategy.generate(candles).type == SignalType.HOLD
