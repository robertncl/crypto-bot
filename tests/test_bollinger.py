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


# -- exit_on_mean -------------------------------------------------------------------
def test_exit_on_mean_sells_reduce_only_when_price_reverts_above_the_average(make_candles):
    strategy = BollingerReversion({"period": 5, "num_std": 2.0, "exit_on_mean": True})
    # Flat at 10, a dip to 9 (below the mean), then back above the 5-bar average.
    signal = strategy.generate(make_candles([10, 10, 10, 10, 9, 9.99]))
    assert signal.type == SignalType.SELL and signal.reduce_only
    assert "reverted above the Bollinger mean" in signal.reason


def test_exit_on_mean_covers_reduce_only_when_price_reverts_below_the_average(make_candles):
    strategy = BollingerReversion({"period": 5, "num_std": 2.0, "exit_on_mean": True})
    signal = strategy.generate(make_candles([10, 10, 10, 10, 11, 10.01]))
    assert signal.type == SignalType.BUY and signal.reduce_only


def test_exit_on_mean_is_off_by_default_and_band_signals_take_priority(make_candles):
    crossing = make_candles([10, 10, 10, 10, 9, 9.99])
    assert BollingerReversion({"period": 5}).generate(crossing).type == SignalType.HOLD

    # A close that pierces the lower band is still an (unrestricted) entry signal.
    strategy = BollingerReversion({"period": 5, "num_std": 1.0, "exit_on_mean": True})
    pierce = strategy.generate(make_candles([10, 10.2, 9.9, 10.1, 10, 9.0]))
    assert pierce.type == SignalType.BUY and not pierce.reduce_only
    assert strategy.generate(make_candles([10] * 6)).type == SignalType.HOLD
