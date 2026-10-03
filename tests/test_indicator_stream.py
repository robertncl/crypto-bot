"""Streaming indicators must agree bit-for-bit with the batch functions.

The batch indicators in ``ta.py`` are causal: element ``i`` depends only on inputs
``0..i``. So feeding a stream from the first element must reproduce the batch output at
*every* index — not approximately, exactly. Strategies compare these values to detect
crossovers, where a single ULP of drift is enough to move a signal by a bar.
"""

from __future__ import annotations

import random

import pytest

from crypto_bot.indicators import stream, ta

_RNG = random.Random(7)


def _random_ohlc(n: int) -> tuple[list[float], list[float], list[float]]:
    highs, lows, closes = [], [], []
    price = 100.0
    for _ in range(n):
        price = max(0.5, price * (1 + _RNG.gauss(0, 0.02)))
        highs.append(price * (1 + abs(_RNG.gauss(0, 0.01))))
        lows.append(price * (1 - abs(_RNG.gauss(0, 0.01))))
        closes.append(price)
    return highs, lows, closes


SERIES = {
    "random": _random_ohlc(400),
    "flat": ([5.0] * 80, [5.0] * 80, [5.0] * 80),
    "ramp": (
        [float(i) + 1 for i in range(80)],
        [float(i) - 1 for i in range(80)],
        [float(i) for i in range(80)],
    ),
    "ties": (
        [3.0, 3.0, 1.0, 1.0, 2.0, 2.0, 2.0, 5.0, 5.0, 1.0] * 8,
        [1.0, 1.0, 0.5, 0.5, 1.5, 1.5, 1.5, 2.0, 2.0, 0.5] * 8,
        [2.0, 2.0, 0.8, 0.8, 1.8, 1.8, 1.8, 3.0, 3.0, 0.8] * 8,
    ),
    "short": ([2.0, 3.0, 4.0], [1.0, 2.0, 3.0], [1.5, 2.5, 3.5]),
}
PERIODS = [1, 2, 3, 14, 30]


def _feed(indicator, values):
    return [indicator.update(v) for v in values]


@pytest.mark.parametrize("name", sorted(SERIES))
@pytest.mark.parametrize("period", PERIODS)
def test_ema_sma_rsi_match_batch(name, period):
    closes = SERIES[name][2]
    assert _feed(stream.EMA(period), closes) == ta.ema(closes, period)
    assert _feed(stream.SMA(period), closes) == ta.sma(closes, period)
    assert _feed(stream.RSI(period), closes) == ta.rsi(closes, period)
    assert _feed(stream.highest(period), closes) == ta.highest(closes, period)
    assert _feed(stream.lowest(period), closes) == ta.lowest(closes, period)


@pytest.mark.parametrize("name", sorted(SERIES))
@pytest.mark.parametrize("period", PERIODS)
def test_bar_indicators_match_batch(name, period):
    highs, lows, closes = SERIES[name]
    bars = list(zip(highs, lows, closes, strict=True))

    atr = stream.ATR(period)
    assert [atr.update(*b) for b in bars] == ta.atr(highs, lows, closes, period)

    adx = stream.ADX(period)
    assert [adx.update(*b) for b in bars] == ta.adx(highs, lows, closes, period)

    for multiplier in (1.0, 3.0):
        st = stream.Supertrend(period, multiplier)
        lines, dirs = [], []
        for b in bars:
            dirs.append(st.update(*b))
            lines.append(st.line if st.direction is not None else None)
        assert (lines, dirs) == ta.supertrend(highs, lows, closes, period, multiplier)


@pytest.mark.parametrize("name", sorted(SERIES))
@pytest.mark.parametrize("fast,slow,signal", [(2, 3, 2), (12, 26, 9), (5, 30, 1)])
def test_macd_matches_batch(name, fast, slow, signal):
    closes = SERIES[name][2]
    m = stream.MACD(fast, slow, signal)
    lines, sigs, hists = [], [], []
    for c in closes:
        m.update(c)
        lines.append(m.macd)
        sigs.append(m.signal)
        hists.append(m.histogram)
    assert (lines, sigs, hists) == ta.macd(closes, fast, slow, signal)


def test_moving_average_dispatch():
    assert isinstance(stream.moving_average(3, "EMA"), stream.EMA)
    assert isinstance(stream.moving_average(3, "sma"), stream.SMA)
    with pytest.raises(ValueError):
        stream.moving_average(3, "wma")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: stream.EMA(0),
        lambda: stream.SMA(0),
        lambda: stream.RSI(0),
        lambda: stream.ATR(0),
        lambda: stream.ADX(0),
        lambda: stream.Supertrend(0),
        lambda: stream.Supertrend(10, 0),
        lambda: stream.RollingExtreme(0, want_max=True),
        lambda: stream.MACD(0, 26, 9),
        lambda: stream.MACD(26, 12, 9),
    ],
)
def test_invalid_parameters_are_rejected(factory):
    with pytest.raises(ValueError):
        factory()
