"""Domain model conversions and edge cases not exercised elsewhere."""

from __future__ import annotations

from crypto_bot.core.models import Candle, Position, PositionSide


def test_candle_from_ccxt_converts_a_raw_ohlcv_row():
    row = [1_700_000_000_000, "10.5", "11.0", "9.5", "10.8", "123.4", "ignored-extra-field"]
    candle = Candle.from_ccxt(row)
    assert candle.timestamp == 1_700_000_000_000
    assert candle.open == 10.5
    assert candle.high == 11.0
    assert candle.low == 9.5
    assert candle.close == 10.8
    assert candle.volume == 123.4


def test_position_unrealized_pnl_pct_is_zero_at_zero_entry_price():
    # Guards a division by zero; a zero entry price shouldn't happen in practice but
    # the method must degrade gracefully rather than raise.
    long = Position(symbol="BTC/USDT", amount=1.0, entry_price=0.0)
    assert long.unrealized_pnl_pct(100.0) == 0.0

    short = Position(symbol="BTC/USDT", amount=1.0, entry_price=0.0, side=PositionSide.SHORT)
    assert short.unrealized_pnl_pct(100.0) == 0.0
