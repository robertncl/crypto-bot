import pytest

from crypto_bot.indicators.ta import (
    atr,
    bollinger_bands,
    ema,
    highest,
    lowest,
    macd,
    moving_average,
    rsi,
    sma,
    stddev,
    supertrend,
    true_range,
)


def test_sma_basic():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


def test_sma_full_window_only():
    out = sma([5, 5, 5], 3)
    assert out == [None, None, 5.0]


def test_ema_seeds_with_sma():
    # period 3 -> multiplier 0.5, seed = mean(first 3) = 2
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)


def test_ema_insufficient_data():
    assert ema([1, 2], 5) == [None, None]


def test_rsi_all_gains_is_100():
    closes = list(range(1, 20))  # strictly increasing
    out = rsi(closes, 14)
    assert out[13] is None
    assert out[14] == pytest.approx(100.0)


def test_rsi_needs_more_than_period():
    assert rsi([1, 2, 3], 14) == [None, None, None]


def test_moving_average_dispatch():
    closes = [1, 2, 3, 4, 5]
    assert moving_average(closes, 2, "sma") == sma(closes, 2)
    assert moving_average(closes, 3, "ema") == ema(closes, 3)


def test_moving_average_rejects_unknown_kind():
    with pytest.raises(ValueError):
        moving_average([1, 2, 3], 2, "wma")


def test_period_validation():
    for fn in (sma, ema, rsi, stddev, highest, lowest):
        with pytest.raises(ValueError):
            fn([1, 2, 3], 0)


def test_stddev_known_population_value():
    # Classic example: these eight values have a population std of exactly 2.0.
    values = [2, 4, 4, 4, 5, 5, 7, 9]
    out = stddev(values, 8)
    assert out[:7] == [None] * 7
    assert out[7] == pytest.approx(2.0)


def test_stddev_constant_is_zero():
    assert stddev([5, 5, 5, 5], 3) == [None, None, 0.0, 0.0]


def test_bollinger_bands_compose_sma_and_stddev():
    values = [1, 2, 3, 5, 8, 13, 21]
    lower, middle, upper = bollinger_bands(values, 3, num_std=2.0)
    assert middle == sma(values, 3)
    sd = stddev(values, 3)
    for lo, mid, up, s in zip(lower, middle, upper, sd, strict=True):
        if mid is None:
            assert lo is None and up is None
            continue
        assert lo == pytest.approx(mid - 2.0 * s)
        assert up == pytest.approx(mid + 2.0 * s)


def test_highest_and_lowest_rolling_window():
    values = [1, 3, 2, 5, 4]
    assert highest(values, 3) == [None, None, 3, 5, 5]
    assert lowest(values, 3) == [None, None, 1, 2, 2]


def test_macd_line_is_fast_minus_slow_ema():
    values = list(range(1, 40))
    macd_line, _signal, _hist = macd(values, fast=12, slow=26, signal=9)
    fast_ema, slow_ema = ema(values, 12), ema(values, 26)
    # MACD line is undefined until the slow EMA exists (index slow - 1 = 25).
    assert macd_line[:25] == [None] * 25
    for i in range(25, len(values)):
        assert macd_line[i] == pytest.approx(fast_ema[i] - slow_ema[i])


def test_macd_histogram_is_line_minus_signal():
    values = [float(v) for v in range(1, 50)]
    macd_line, signal_line, hist = macd(values, fast=3, slow=6, signal=3)
    for m, s, h in zip(macd_line, signal_line, hist, strict=True):
        if s is None:
            assert h is None
        else:
            assert h == pytest.approx(m - s)


def test_macd_validates_periods():
    with pytest.raises(ValueError):
        macd([1, 2, 3], fast=6, slow=3)  # fast must be < slow


def test_true_range_uses_prev_close():
    highs = [10, 12, 11, 13]
    lows = [8, 9, 9, 10]
    closes = [9, 11, 10, 12]
    # bar 0 has no previous close -> falls back to high - low = 2.
    # bar 1: max(12-9, |12-9|, |9-9|) = 3; bar 2: max(2, 0, 2) = 2; bar 3: max(3, 3, 0) = 3.
    assert true_range(highs, lows, closes) == [2, 3, 2, 3]


def test_atr_wilder_smoothing():
    highs = [10, 12, 11, 13]
    lows = [8, 9, 9, 10]
    closes = [9, 11, 10, 12]
    # TR = [2, 3, 2, 3]; seed = mean(2, 3) = 2.5 at index 1, then Wilder-smoothed.
    out = atr(highs, lows, closes, period=2)
    assert out[0] is None
    assert out[1] == pytest.approx(2.5)
    assert out[2] == pytest.approx((2.5 * 1 + 2) / 2)
    assert out[3] == pytest.approx((2.25 * 1 + 3) / 2)


def test_supertrend_direction_flips_on_reversal():
    closes = [100, 98, 96, 94, 92, 90, 88, 86, 100, 102]
    line, direction = supertrend(closes, closes, closes, period=3, multiplier=1.0)
    assert direction[:2] == [None, None]  # ATR warm-up
    assert all(d in (1, -1) for d in direction[2:])
    assert direction[7] == -1 and direction[8] == 1  # downtrend flips up
    # In an uptrend the line trails *below* price; in a downtrend, above it.
    assert line[8] is not None and line[8] <= closes[8]


def test_supertrend_validates_inputs():
    with pytest.raises(ValueError):
        supertrend([1, 2], [1, 2], [1, 2], period=0)
    with pytest.raises(ValueError):
        supertrend([1, 2], [1, 2], [1, 2], multiplier=0)
