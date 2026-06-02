import pytest

from crypto_bot.indicators.ta import ema, moving_average, rsi, sma


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
    for fn in (sma, ema, rsi):
        with pytest.raises(ValueError):
            fn([1, 2, 3], 0)
