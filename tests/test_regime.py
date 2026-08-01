import pytest

from crypto_bot.core.models import Signal, SignalType
from crypto_bot.strategies.regime import RegimeSwitch

# Small ADX period keeps fixtures compact: ADX defined from bar 2*3 = 6.
PARAMS = {"adx_period": 3, "adx_threshold": 25}


class _Stub:
    """Minimal strategy double with a fixed answer, to make routing observable."""

    warmup = 1

    def __init__(self, tag: str, sig: SignalType = SignalType.BUY) -> None:
        self.tag = tag
        self.sig = sig

    def generate(self, candles, symbol=None):
        return Signal(self.sig, reason=self.tag)


def _regime_with_stubs() -> RegimeSwitch:
    strategy = RegimeSwitch(dict(PARAMS))
    strategy.trend_strategy = _Stub("trend-leg")
    strategy.range_strategy = _Stub("range-leg", SignalType.SELL)
    return strategy


def test_routes_to_trend_leg_when_adx_high(make_candles):
    # Monotonic ramp -> ADX 100 -> the trend leg answers.
    candles = make_candles([float(i) for i in range(1, 15)])
    signal = _regime_with_stubs().generate(candles, "BTC/USDT")
    assert signal.type == SignalType.BUY
    assert "trend-leg" in signal.reason and "trend regime" in signal.reason


def test_routes_to_range_leg_when_adx_low(make_candles):
    # Zigzag -> ADX ~20 (< 25) -> the range leg answers.
    candles = make_candles([10.0, 12.0] * 8)
    signal = _regime_with_stubs().generate(candles, "BTC/USDT")
    assert signal.type == SignalType.SELL
    assert "range-leg" in signal.reason and "range regime" in signal.reason


def test_hold_passes_through_untagged(make_candles):
    strategy = _regime_with_stubs()
    strategy.trend_strategy = _Stub("quiet", SignalType.HOLD)
    candles = make_candles([float(i) for i in range(1, 15)])
    signal = strategy.generate(candles, "BTC/USDT")
    assert signal.type == SignalType.HOLD


def test_hold_before_warmup(make_candles):
    strategy = _regime_with_stubs()
    assert strategy.generate(make_candles([1.0, 2.0, 3.0])).type == SignalType.HOLD


def test_default_legs_are_supertrend_and_rsi():
    strategy = RegimeSwitch(dict(PARAMS))
    assert type(strategy.trend_strategy).__name__ == "Supertrend"
    assert type(strategy.range_strategy).__name__ == "RSIReversion"
    # Warmup must cover the ADX and both legs.
    assert strategy.warmup >= 2 * strategy.adx_period
    assert strategy.warmup >= strategy.trend_strategy.warmup
    assert strategy.warmup >= strategy.range_strategy.warmup


def test_legs_are_configurable_by_name():
    strategy = RegimeSwitch(
        {**PARAMS, "trend": {"name": "breakout", "params": {"lookback": 5}}}
    )
    assert type(strategy.trend_strategy).__name__ == "Breakout"
    assert strategy.trend_strategy.lookback == 5


def test_rejects_nesting_itself():
    with pytest.raises(ValueError):
        RegimeSwitch({**PARAMS, "trend": {"name": "regime"}})


def test_rejects_bad_leg_spec():
    with pytest.raises(ValueError):
        RegimeSwitch({**PARAMS, "range": {"params": {}}})  # missing 'name'


def test_rejects_bad_threshold():
    with pytest.raises(ValueError):
        RegimeSwitch({"adx_threshold": 0})


def test_rejects_nonpositive_adx_period():
    with pytest.raises(ValueError, match="adx_period must be positive"):
        RegimeSwitch({"adx_period": 0})


def test_hold_when_adx_not_yet_defined(make_candles, monkeypatch):
    # adx() is provably defined at the last bar for every input at exactly `warmup`
    # candles (same reasoning as the other indicator-strategies' None-guards), so this
    # branch is unreachable with well-formed data. Force it via the adx() call to prove
    # the guard still holds if that invariant were ever violated.
    import crypto_bot.strategies.regime as mod

    monkeypatch.setattr(mod, "adx", lambda highs, lows, closes, period: [None] * len(closes))
    strategy = _regime_with_stubs()
    candles = make_candles([float(i) for i in range(1, strategy.warmup + 1)])
    assert strategy.generate(candles, "BTC/USDT").type == SignalType.HOLD
