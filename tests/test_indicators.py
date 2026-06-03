import pytest

from crypto_bot.indicators.ta import (
    bollinger_bands,
    ema,
    highest,
    lowest,
    moving_average,
    rsi,
    sma,
    stddev,
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
