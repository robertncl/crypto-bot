"""Pure-Python technical indicators.

Each function takes a list of floats (typically closing prices) and returns a list
of the same length, left-padded with ``None`` for the warm-up period where the
indicator is not yet defined. Keeping the output aligned with the input makes it
trivial to zip indicator values back onto candles.

These are intentionally dependency-free (no pandas/numpy) so the strategy and test
layers stay lightweight and fast.
"""

from __future__ import annotations

from collections import deque

Number = float


def sma(values: list[Number], period: int) -> list[Number | None]:
    """Simple moving average."""
    if period <= 0:
        raise ValueError("period must be a positive integer")
    out: list[Number | None] = [None] * len(values)
    window: deque[Number] = deque()
    running = 0.0
    for i, v in enumerate(values):
        window.append(v)
        running += v
        if len(window) > period:
            running -= window.popleft()
        if len(window) == period:
            out[i] = running / period
    return out


def ema(values: list[Number], period: int) -> list[Number | None]:
    """Exponential moving average, seeded with the SMA of the first ``period`` values."""
    if period <= 0:
        raise ValueError("period must be a positive integer")
    out: list[Number | None] = [None] * len(values)
    if len(values) < period:
        return out
    multiplier = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * multiplier + prev
        out[i] = prev
    return out


def rsi(values: list[Number], period: int = 14) -> list[Number | None]:
    """Relative Strength Index using Wilder's smoothing."""
    if period <= 0:
        raise ValueError("period must be a positive integer")
    out: list[Number | None] = [None] * len(values)
    if len(values) <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)
    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def moving_average(values: list[Number], period: int, kind: str = "ema") -> list[Number | None]:
    """Dispatch helper: ``kind`` is 'ema' or 'sma'."""
    kind = kind.lower()
    if kind == "ema":
        return ema(values, period)
    if kind == "sma":
        return sma(values, period)
    raise ValueError(f"unknown moving-average kind: {kind!r} (expected 'ema' or 'sma')")
