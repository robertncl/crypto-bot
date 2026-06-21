import pytest

from crypto_bot.core.models import SignalType
from crypto_bot.strategies.dca import DCA


def test_buys_once_per_new_candle(make_candles):
    dca = DCA({"every": 1})
    candles = make_candles([10, 10, 10])
    first = dca.generate(candles)
    second = dca.generate(candles)  # same latest candle, polled again
    assert first.type == SignalType.BUY
    assert "DCA scheduled buy" in first.reason
    assert second.type == SignalType.HOLD  # deduped within the candle


def test_fires_again_on_the_next_candle(make_candles):
    dca = DCA({"every": 1})
    assert dca.generate(make_candles([10, 10])).type == SignalType.BUY
    # A new candle arrives (one more bar) -> buy again.
    assert dca.generate(make_candles([10, 10, 10])).type == SignalType.BUY


def test_every_n_cadence_is_exact(make_candles):
    dca = DCA({"every": 2})
    # Walk a growing window one new candle at a time; exactly half should be buys.
    buys = sum(
        dca.generate(make_candles([10] * n)).type == SignalType.BUY for n in range(2, 12)
    )
    assert buys == 5  # 10 candles, every 2nd -> 5 buys


def test_dedupe_is_per_symbol(make_candles):
    # The engine shares one strategy instance across symbols: each symbol must buy on a new
    # candle even though their timestamps are identical, then dedupe independently.
    dca = DCA({"every": 1})
    candles = make_candles([10, 10, 10])
    assert dca.generate(candles, "BTC/USDT").type == SignalType.BUY
    assert dca.generate(candles, "ETH/USDT").type == SignalType.BUY
    assert dca.generate(candles, "BTC/USDT").type == SignalType.HOLD
    assert dca.generate(candles, "ETH/USDT").type == SignalType.HOLD


def test_never_sells(make_candles):
    dca = DCA({"every": 1})
    signals = [dca.generate(make_candles([10] * n)) for n in range(2, 8)]
    assert all(s.type in (SignalType.BUY, SignalType.HOLD) for s in signals)


def test_hold_before_warmup(make_candles):
    assert DCA().generate(make_candles([10])).type == SignalType.HOLD


def test_rejects_nonpositive_every():
    with pytest.raises(ValueError):
        DCA({"every": 0})
