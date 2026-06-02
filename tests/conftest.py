"""Shared test fixtures."""

from __future__ import annotations

import pytest

from crypto_bot.core.models import Candle


@pytest.fixture
def make_candles():
    """Factory: turn a list of close prices into Candle objects (OHLC == close)."""

    def _make(closes: list[float], start_ts: int = 1_000_000) -> list[Candle]:
        return [
            Candle(
                timestamp=start_ts + i * 60_000,
                open=c,
                high=c,
                low=c,
                close=c,
                volume=1.0,
            )
            for i, c in enumerate(closes)
        ]

    return _make
