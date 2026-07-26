from __future__ import annotations

import pytest

from crypto_bot.core.models import MarketContext, SignalType
from crypto_bot.strategies.funding_bias import FundingBias


def _ctx(rate: float | None, hours: float = 8.0) -> MarketContext:
    return MarketContext("BTC/USDT", funding_rate=rate, funding_interval_hours=hours)


def _flat(n: int = 120):
    return [100.0] * n


def test_funding_apr_annualizes_the_interval_rate():
    # 0.01% every 8h = 3 payments a day = ~10.95%/yr.
    assert _ctx(0.0001).funding_apr == pytest.approx(0.1095)
    assert _ctx(None).funding_apr is None


def test_crowded_longs_produce_a_short(make_candles):
    strat = FundingBias({"trend_period": 0})
    sig = strat.generate(make_candles(_flat()), "BTC/USDT", _ctx(0.001))  # ~110%/yr
    assert sig.type == SignalType.SELL
    assert "crowded longs" in sig.reason


def test_crowded_shorts_produce_a_long(make_candles):
    strat = FundingBias({"trend_period": 0})
    sig = strat.generate(make_candles(_flat()), "BTC/USDT", _ctx(-0.001))
    assert sig.type == SignalType.BUY
    assert "crowded shorts" in sig.reason


def test_ordinary_funding_is_ignored(make_candles):
    strat = FundingBias({"trend_period": 0, "enter_apr": 0.20})
    # 0.00001/8h is ~1%/yr — nowhere near the threshold.
    assert strat.generate(make_candles(_flat()), "BTC/USDT", _ctx(0.00001)).type == SignalType.HOLD


def test_holds_without_funding_data(make_candles):
    strat = FundingBias({"trend_period": 0})
    candles = make_candles(_flat())
    assert strat.generate(candles, "BTC/USDT", _ctx(None)).type == SignalType.HOLD
    assert strat.generate(candles, "BTC/USDT", None).type == SignalType.HOLD


def test_trend_guard_blocks_shorting_an_uptrend(make_candles):
    strat = FundingBias({"trend_period": 20})
    rising = make_candles([float(i) for i in range(1, 101)])  # price well above its EMA
    assert strat.generate(rising, "BTC/USDT", _ctx(0.001)).type == SignalType.HOLD

    falling = make_candles([float(i) for i in range(100, 0, -1)])  # below its EMA
    assert strat.generate(falling, "BTC/USDT", _ctx(0.001)).type == SignalType.SELL


def test_trend_guard_blocks_buying_a_downtrend(make_candles):
    strat = FundingBias({"trend_period": 20})
    falling = make_candles([float(i) for i in range(100, 0, -1)])
    assert strat.generate(falling, "BTC/USDT", _ctx(-0.001)).type == SignalType.HOLD


def test_warmup_tracks_the_trend_period():
    assert FundingBias({"trend_period": 0}).warmup == 1
    assert FundingBias({"trend_period": 50}).warmup == 50


def test_strategy_opts_into_context():
    assert FundingBias().wants_context is True


def test_rejects_invalid_params():
    with pytest.raises(ValueError):
        FundingBias({"enter_apr": 0})
    with pytest.raises(ValueError):
        FundingBias({"trend_period": -1})
