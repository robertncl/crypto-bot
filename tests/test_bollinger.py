import pytest

from crypto_bot.core.models import SignalType
from crypto_bot.strategies.bollinger import BollingerReversion

PARAMS = {"period": 3, "num_std": 1.0}


def test_buy_when_close_pierces_lower_band(make_candles):
    # Steady at 10 (band collapses to the mean), then a sharp drop punches below it.
    candles = make_candles([10, 10, 10, 10, 5])
    signal = BollingerReversion(PARAMS).generate(candles)
    assert signal.type == SignalType.BUY
    assert "lower" in signal.reason


def test_sell_when_close_pierces_upper_band(make_candles):
    candles = make_candles([10, 10, 10, 10, 15])
    signal = BollingerReversion(PARAMS).generate(candles)
    assert signal.type == SignalType.SELL
    assert "upper" in signal.reason


def test_hold_when_flat(make_candles):
    assert BollingerReversion(PARAMS).generate(make_candles([10] * 6)).type == SignalType.HOLD


def test_hold_before_warmup(make_candles):
    # warmup is period + 1 = 4
    assert BollingerReversion(PARAMS).generate(make_candles([10, 11])).type == SignalType.HOLD


def test_rejects_nonpositive_params():
    with pytest.raises(ValueError):
        BollingerReversion({"period": 0})
    with pytest.raises(ValueError):
        BollingerReversion({"num_std": 0})


def test_signal_ignores_history_beyond_the_band_window(make_candles):
    # The strategy slices to the `period + 1` closes the bands depend on instead of
    # running the SMA and rolling stddev over the engine's whole ~200-bar buffer.
    # Bands carry no state from earlier bars, so padding history must change nothing.
    import random

    rng = random.Random(11)
    strategy = BollingerReversion(PARAMS)
    for _ in range(200):
        recent = [rng.uniform(1, 20) for _ in range(strategy.warmup)]
        older = [rng.uniform(1, 20) for _ in range(150)]
        minimal = strategy.generate(make_candles(recent))
        padded = strategy.generate(make_candles(older + recent))
        assert (minimal.type, minimal.reason) == (padded.type, padded.reason)


def test_hold_when_bands_not_yet_defined(make_candles, monkeypatch):
    # bollinger_bands() is provably defined at both compared bars for every input at
    # exactly `warmup` candles, so this guard is unreachable with well-formed data.
    # Force it via the indicator call to prove it still holds if that ever changed.
    import crypto_bot.strategies.bollinger as mod

    none_series = [None] * 10
    monkeypatch.setattr(
        mod, "bollinger_bands", lambda *a, **k: (none_series, none_series, none_series)
    )
    strategy = BollingerReversion(PARAMS)
    candles = make_candles([10] * strategy.warmup)
    assert strategy.generate(candles).type == SignalType.HOLD
