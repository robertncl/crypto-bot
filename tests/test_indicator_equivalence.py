"""Cross-checks pinning the optimised indicator internals to their textbook form.

The indicators in :mod:`crypto_bot.indicators.ta` are the hot path of every backtest,
so they are written for speed: fused passes, running scalars instead of intermediate
lists, and a monotonic deque for the rolling extremes. Those rewrites are only worth
anything if they stay *exactly* faithful to the naive definitions, so this module keeps
a slow, obviously-correct reference beside each optimised one and asserts they agree —
including on the awkward inputs (ties, flats, ramps, degenerate periods) where a clever
implementation is most likely to drift.

Kept separate from ``test_indicators.py``, which pins the indicators' *semantics*;
these tests pin their *equivalence* and exist to guard future optimisation work.
"""

from __future__ import annotations

import random

import pytest

from crypto_bot.indicators.ta import adx, highest, lowest, true_range

# Deterministic pseudo-random OHLC series, plus the degenerate shapes that break
# naive assumptions: flat data (zero range), monotone ramps, and heavy ties.
_RNG = random.Random(20240613)


def _random_ohlc(n: int) -> tuple[list[float], list[float], list[float]]:
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    price = 100.0
    for _ in range(n):
        price = max(0.5, price * (1 + _RNG.gauss(0, 0.02)))
        high = price * (1 + abs(_RNG.gauss(0, 0.01)))
        low = price * (1 - abs(_RNG.gauss(0, 0.01)))
        highs.append(high)
        lows.append(low)
        closes.append(price)
    return highs, lows, closes


SERIES = {
    "random": _random_ohlc(120),
    "flat": ([5.0] * 60, [5.0] * 60, [5.0] * 60),
    "ramp_up": (
        [float(i) + 1 for i in range(60)],
        [float(i) - 1 for i in range(60)],
        [float(i) for i in range(60)],
    ),
    "ramp_down": (
        [float(60 - i) + 1 for i in range(60)],
        [float(60 - i) - 1 for i in range(60)],
        [float(60 - i) for i in range(60)],
    ),
    "ties": (
        [3.0, 3.0, 1.0, 1.0, 2.0, 2.0, 2.0, 5.0, 5.0, 1.0] * 6,
        [1.0, 1.0, 0.5, 0.5, 1.5, 1.5, 1.5, 2.0, 2.0, 0.5] * 6,
        [2.0, 2.0, 0.8, 0.8, 1.8, 1.8, 1.8, 3.0, 3.0, 0.8] * 6,
    ),
}


# -- rolling extremes ------------------------------------------------------------
def _naive_extreme(values: list[float], period: int, pick) -> list[float | None]:
    """Rescan the whole window on every bar — O(n * period), obviously correct."""
    return [
        pick(values[i - period + 1 : i + 1]) if i >= period - 1 else None
        for i in range(len(values))
    ]


@pytest.mark.parametrize("name", sorted(SERIES))
@pytest.mark.parametrize("period", [1, 2, 3, 7, 20, 59])
def test_rolling_extremes_match_naive_rescan(name: str, period: int) -> None:
    # The monotonic-deque scan must agree with a plain max()/min() over each window,
    # ties and all — a deque that pops the wrong side of an equal value would drift
    # only on repeated values, which the "ties" series is built to expose.
    _highs, _lows, closes = SERIES[name]
    assert highest(closes, period) == _naive_extreme(closes, period, max)
    assert lowest(closes, period) == _naive_extreme(closes, period, min)


def test_rolling_extreme_window_shorter_than_period() -> None:
    assert highest([1.0, 2.0], 5) == [None, None]
    assert lowest([1.0, 2.0], 5) == [None, None]


# -- true range ------------------------------------------------------------------
def _naive_true_range(highs, lows, closes) -> list[float]:
    """The literal definition: greatest of the three candidate ranges."""
    out = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        prev_close = closes[i - 1]
        out.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
        )
    return out


@pytest.mark.parametrize("name", sorted(SERIES))
def test_true_range_matches_three_way_max(name: str) -> None:
    highs, lows, closes = SERIES[name]
    assert true_range(highs, lows, closes) == _naive_true_range(highs, lows, closes)


def test_true_range_covers_gaps_either_side_of_the_bar() -> None:
    # true_range() uses the identity max(h, pc) - min(l, pc), which has to hold whether
    # the previous close gapped above the bar, below it, or sat inside it.
    highs = [10.0, 12.0, 11.0, 20.0]
    lows = [8.0, 9.0, 9.0, 18.0]
    closes = [9.0, 30.0, 1.0, 19.0]  # gap up, gap down, then an inside close
    assert true_range(highs, lows, closes) == _naive_true_range(highs, lows, closes)
    assert true_range(highs, lows, closes) == [2.0, 3.0, 21.0, 19.0]


def test_true_range_empty_input() -> None:
    assert true_range([], [], []) == []


# -- ADX -------------------------------------------------------------------------
def _naive_adx(highs, lows, closes, period: int) -> list[float | None]:
    """Wilder's ADX built step by step through full intermediate lists."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < 2 * period:
        return out
    tr = _naive_true_range(highs, lows, closes)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        if up > down and up > 0:
            plus_dm[i] = up
        elif down > up and down > 0:
            minus_dm[i] = down

    def dx_at(str_, spd, smd):
        if str_ == 0:
            return 0.0
        plus_di = 100.0 * spd / str_
        minus_di = 100.0 * smd / str_
        total = plus_di + minus_di
        return 0.0 if total == 0 else 100.0 * abs(plus_di - minus_di) / total

    smooth_tr = sum(tr[1 : period + 1])
    smooth_pdm = sum(plus_dm[1 : period + 1])
    smooth_mdm = sum(minus_dm[1 : period + 1])
    dx = [0.0] * n
    dx[period] = dx_at(smooth_tr, smooth_pdm, smooth_mdm)
    for i in range(period + 1, n):
        smooth_tr += tr[i] - smooth_tr / period
        smooth_pdm += plus_dm[i] - smooth_pdm / period
        smooth_mdm += minus_dm[i] - smooth_mdm / period
        dx[i] = dx_at(smooth_tr, smooth_pdm, smooth_mdm)

    prev = sum(dx[period : 2 * period]) / period
    out[2 * period - 1] = prev
    for i in range(2 * period, n):
        prev = (prev * (period - 1) + dx[i]) / period
        out[i] = prev
    return out


@pytest.mark.parametrize("name", sorted(SERIES))
@pytest.mark.parametrize("period", [1, 2, 3, 14, 29])
def test_adx_matches_naive_reference(name: str, period: int) -> None:
    # adx() streams true range, both directional-movement legs and DX through scalars
    # in a single pass; this pins that fusion to the list-by-list construction. The
    # seed windows are summed with sum() on purpose — it compensates float error, and
    # a hand-rolled running total drifts by an ULP right at index 2 * period - 1.
    highs, lows, closes = SERIES[name]
    assert adx(highs, lows, closes, period) == _naive_adx(highs, lows, closes, period)


def test_adx_needs_two_full_wilder_windows() -> None:
    highs, lows, closes = SERIES["random"]
    period = 10
    out = adx(highs[: 2 * period - 1], lows[: 2 * period - 1], closes[: 2 * period - 1], period)
    assert out == [None] * (2 * period - 1)
