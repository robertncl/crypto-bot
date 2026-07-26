from __future__ import annotations

import pytest

from crypto_bot.core.models import Candle, SignalType
from crypto_bot.strategies.trend_ls import TrendLongShort


def _ohlc(closes: list[float]) -> list[Candle]:
    """Candles whose high/low straddle the close, so ADX/Donchian have real range."""
    return [
        Candle(1_000_000 + i * 60_000, c, c * 1.01, c * 0.99, c, 1.0)
        for i, c in enumerate(closes)
    ]


def _params(**over):
    base = {"lookback": 5, "adx_period": 5, "adx_threshold": 0.0, "trend_period": 0}
    base.update(over)
    return base


def test_breakout_above_channel_goes_long():
    # A steady climb, then a decisive new high.
    closes = [10.0 + i * 0.5 for i in range(40)] + [60.0]
    sig = TrendLongShort(_params()).generate(_ohlc(closes))
    assert sig.type == SignalType.BUY
    assert "broke" in sig.reason and "high" in sig.reason


def test_breakdown_below_channel_goes_short():
    closes = [60.0 - i * 0.5 for i in range(40)] + [10.0]
    sig = TrendLongShort(_params()).generate(_ohlc(closes))
    assert sig.type == SignalType.SELL
    assert "low" in sig.reason


def test_adx_filter_suppresses_signals_in_chop():
    # Oscillating series: a break happens, but trend strength is weak.
    closes = [100.0 + (2.0 if i % 2 else -2.0) for i in range(40)] + [104.0]
    permissive = TrendLongShort(_params(adx_threshold=0.0)).generate(_ohlc(closes))
    strict = TrendLongShort(_params(adx_threshold=90.0)).generate(_ohlc(closes))
    assert strict.type == SignalType.HOLD
    # The threshold is doing the work, not an absence of a break.
    assert permissive.type in (SignalType.BUY, SignalType.SELL)


def test_regime_filter_blocks_counter_trend_breaks():
    # Long downtrend, then a single pop that breaks the short-term high while price is
    # still far below the slow EMA: the regime filter must veto the long.
    closes = [100.0 - i for i in range(60)] + [48.0]
    blocked = TrendLongShort(_params(trend_period=50)).generate(_ohlc(closes))
    allowed = TrendLongShort(_params(trend_period=0)).generate(_ohlc(closes))
    assert blocked.type == SignalType.HOLD
    assert allowed.type == SignalType.BUY


def test_holds_below_warmup():
    strat = TrendLongShort(_params())
    assert strat.generate(_ohlc([10.0] * 3)).type == SignalType.HOLD


def test_warmup_covers_every_filter():
    strat = TrendLongShort({"lookback": 20, "adx_period": 14, "trend_period": 100})
    assert strat.warmup == 100  # the slow EMA dominates here
    strat = TrendLongShort({"lookback": 20, "adx_period": 30, "trend_period": 10})
    assert strat.warmup == 60  # 2 * adx_period dominates


def test_flat_market_produces_no_signal():
    assert TrendLongShort(_params()).generate(_ohlc([100.0] * 40)).type == SignalType.HOLD


def test_rejects_invalid_params():
    for bad in ({"lookback": 0}, {"adx_period": 0}, {"adx_threshold": 100}, {"trend_period": -1}):
        with pytest.raises(ValueError):
            TrendLongShort(bad)


def test_is_registered():
    from crypto_bot.strategies.registry import build_strategy

    assert isinstance(build_strategy("trend_ls", {}), TrendLongShort)
