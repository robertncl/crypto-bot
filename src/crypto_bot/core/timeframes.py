"""Timeframe arithmetic shared by the live engine and the backtester."""

from __future__ import annotations

_TIMEFRAME_UNITS_MS = {
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 7 * 86_400_000,
}


def timeframe_to_ms(timeframe: str) -> int:
    """Convert a ccxt-style timeframe ('1m', '4h', '1d', '1w') to milliseconds."""
    tf = timeframe.strip()
    if len(tf) < 2 or tf[-1] not in _TIMEFRAME_UNITS_MS or not tf[:-1].isdigit():
        raise ValueError(
            f"unsupported timeframe {timeframe!r} (expected e.g. 1m, 15m, 1h, 4h, 1d, 1w)"
        )
    return int(tf[:-1]) * _TIMEFRAME_UNITS_MS[tf[-1]]
