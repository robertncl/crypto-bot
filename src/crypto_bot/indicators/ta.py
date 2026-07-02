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


def stddev(values: list[Number], period: int) -> list[Number | None]:
    """Rolling **population** standard deviation over a trailing window of ``period``.

    Population (divide by N, not N-1) is the convention Bollinger Bands use. Output is
    left-padded with ``None`` until the first full window, like the moving averages.
    """
    if period <= 0:
        raise ValueError("period must be a positive integer")
    out: list[Number | None] = [None] * len(values)
    window: deque[Number] = deque()
    running = 0.0
    running_sq = 0.0
    for i, v in enumerate(values):
        window.append(v)
        running += v
        running_sq += v * v
        if len(window) > period:
            old = window.popleft()
            running -= old
            running_sq -= old * old
        if len(window) == period:
            mean = running / period
            # Clamp tiny negatives from floating-point error before the square root.
            variance = max(0.0, running_sq / period - mean * mean)
            out[i] = variance**0.5
    return out


def bollinger_bands(
    values: list[Number], period: int = 20, num_std: float = 2.0
) -> tuple[list[Number | None], list[Number | None], list[Number | None]]:
    """Bollinger Bands: ``(lower, middle, upper)`` where middle is the SMA and the
    bands sit ``num_std`` population standard deviations either side of it.

    Each returned list is aligned with ``values`` and left-padded with ``None`` over
    the warm-up period.
    """
    middle = sma(values, period)
    sd = stddev(values, period)
    lower: list[Number | None] = [None] * len(values)
    upper: list[Number | None] = [None] * len(values)
    for i, (m, s) in enumerate(zip(middle, sd, strict=True)):
        if m is None or s is None:
            continue
        lower[i] = m - num_std * s
        upper[i] = m + num_std * s
    return lower, middle, upper


def macd(
    values: list[Number], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[Number | None], list[Number | None], list[Number | None]]:
    """MACD: ``(macd_line, signal_line, histogram)``, each aligned with ``values``.

    * ``macd_line`` = EMA(fast) − EMA(slow). Defined once the slow EMA is (index
      ``slow - 1`` onward).
    * ``signal_line`` = EMA(``signal``) of the MACD line.
    * ``histogram`` = ``macd_line − signal_line`` (its sign-flip is the crossover).

    All three lists are left-padded with ``None`` over their respective warm-up.
    """
    if fast <= 0 or slow <= 0 or signal <= 0:
        raise ValueError("fast, slow and signal periods must be positive")
    if fast >= slow:
        raise ValueError("fast period must be smaller than slow period")

    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    macd_line: list[Number | None] = [None] * len(values)
    for i, (f, s) in enumerate(zip(fast_ema, slow_ema, strict=True)):
        if f is not None and s is not None:
            macd_line[i] = f - s

    signal_line: list[Number | None] = [None] * len(values)
    histogram: list[Number | None] = [None] * len(values)
    # The MACD line is defined over a single contiguous tail; take the EMA of just
    # that region, then re-align the result back onto the full-length output.
    start = next((i for i, v in enumerate(macd_line) if v is not None), None)
    if start is not None:
        defined = [v for v in macd_line[start:]]
        sig = ema(defined, signal)
        for offset, s in enumerate(sig):
            if s is not None:
                idx = start + offset
                signal_line[idx] = s
                histogram[idx] = macd_line[idx] - s
    return macd_line, signal_line, histogram


def true_range(
    highs: list[Number], lows: list[Number], closes: list[Number]
) -> list[Number | None]:
    """True Range per bar: the greatest of high−low, |high−prev_close|, |low−prev_close|.

    The first bar has no previous close, so it falls back to the high−low range. Output
    is aligned with the inputs (no warm-up padding — every bar has a value).
    """
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs, lows and closes must be the same length")
    out: list[Number | None] = [None] * n
    for i in range(n):
        if i == 0:
            out[i] = highs[i] - lows[i]
        else:
            prev_close = closes[i - 1]
            out[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
    return out


def atr(
    highs: list[Number], lows: list[Number], closes: list[Number], period: int = 14
) -> list[Number | None]:
    """Average True Range using Wilder's smoothing, seeded with the simple mean of the
    first ``period`` true ranges. Defined from index ``period - 1`` onward."""
    if period <= 0:
        raise ValueError("period must be a positive integer")
    tr = true_range(highs, lows, closes)
    out: list[Number | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    seed = sum(tr[:period]) / period  # type: ignore[arg-type]  # tr has no None values
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(closes)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def supertrend(
    highs: list[Number],
    lows: list[Number],
    closes: list[Number],
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[list[Number | None], list[int | None]]:
    """Supertrend: ``(line, direction)`` aligned with the inputs.

    The line trails ``multiplier × ATR`` below price in an uptrend and above it in a
    downtrend, flipping side when price closes through it. ``direction`` is ``+1`` for
    uptrend, ``-1`` for downtrend, and ``None`` during the ATR warm-up. The flip of
    ``direction`` is the trade signal.

    Uses the standard recursive band construction: each final band ratchets in the
    trend's favour and only resets once price closes beyond it.
    """
    if period <= 0:
        raise ValueError("period must be a positive integer")
    if multiplier <= 0:
        raise ValueError("multiplier must be positive")

    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs, lows and closes must be the same length")

    atr_vals = atr(highs, lows, closes, period)
    line: list[Number | None] = [None] * n
    direction: list[int | None] = [None] * n
    final_upper: list[Number | None] = [None] * n
    final_lower: list[Number | None] = [None] * n

    for i in range(n):
        a = atr_vals[i]
        if a is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2
        basic_upper = hl2 + multiplier * a
        basic_lower = hl2 - multiplier * a

        if direction[i - 1] is None:
            # First bar with a defined ATR: seed the bands and assume an uptrend.
            # The seed is arbitrary, hence the strategy's extra warm-up bar.
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            direction[i] = 1
            line[i] = final_lower[i]
            continue

        prev_upper = final_upper[i - 1]
        prev_lower = final_lower[i - 1]
        prev_close = closes[i - 1]
        final_upper[i] = (
            basic_upper if basic_upper < prev_upper or prev_close > prev_upper else prev_upper
        )
        final_lower[i] = (
            basic_lower if basic_lower > prev_lower or prev_close < prev_lower else prev_lower
        )

        if direction[i - 1] == 1:
            direction[i] = -1 if closes[i] < final_lower[i] else 1
        else:
            direction[i] = 1 if closes[i] > final_upper[i] else -1
        line[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return line, direction


def adx(
    highs: list[Number], lows: list[Number], closes: list[Number], period: int = 14
) -> list[Number | None]:
    """Average Directional Index (Wilder): 0–100 *trend strength*, direction-agnostic.

    Built from directional movement: bars where the high pushes up more than the low
    pushes down count as +DM, the reverse as −DM. Both are Wilder-smoothed against the
    true range into +DI/−DI, their normalized gap is DX, and ADX is the Wilder average
    of DX. Readings above ~25 are conventionally "trending"; below ~20, "ranging".

    First defined at index ``2 * period - 1`` (one Wilder window to seed the DI lines,
    a second to seed the DX average); earlier entries are ``None``.
    """
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs, lows and closes must be the same length")
    if period <= 0:
        raise ValueError("period must be a positive integer")
    out: list[Number | None] = [None] * n
    if n < 2 * period:
        return out

    tr = true_range(highs, lows, closes)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        if up > down and up > 0:
            plus_dm[i] = up
        elif down > up and down > 0:
            minus_dm[i] = down

    def _dx(spd: float, smd: float, str_: float) -> float:
        if str_ == 0:
            return 0.0
        plus_di = 100.0 * spd / str_
        minus_di = 100.0 * smd / str_
        total = plus_di + minus_di
        return 0.0 if total == 0 else 100.0 * abs(plus_di - minus_di) / total

    # Wilder smoothing, seeded with plain sums over the first `period` movement bars.
    smooth_tr = sum(tr[i] for i in range(1, period + 1))
    smooth_pdm = sum(plus_dm[1 : period + 1])
    smooth_mdm = sum(minus_dm[1 : period + 1])
    dx = [0.0] * n
    dx[period] = _dx(smooth_pdm, smooth_mdm, smooth_tr)
    for i in range(period + 1, n):
        smooth_tr += tr[i] - smooth_tr / period
        smooth_pdm += plus_dm[i] - smooth_pdm / period
        smooth_mdm += minus_dm[i] - smooth_mdm / period
        dx[i] = _dx(smooth_pdm, smooth_mdm, smooth_tr)

    seed = sum(dx[period : 2 * period]) / period
    out[2 * period - 1] = seed
    prev = seed
    for i in range(2 * period, n):
        prev = (prev * (period - 1) + dx[i]) / period
        out[i] = prev
    return out


def highest(values: list[Number], period: int) -> list[Number | None]:
    """Rolling maximum over a trailing window of ``period`` values (Donchian upper)."""
    return _rolling_extreme(values, period, max)


def lowest(values: list[Number], period: int) -> list[Number | None]:
    """Rolling minimum over a trailing window of ``period`` values (Donchian lower)."""
    return _rolling_extreme(values, period, min)


def _rolling_extreme(values: list[Number], period: int, pick) -> list[Number | None]:
    if period <= 0:
        raise ValueError("period must be a positive integer")
    out: list[Number | None] = [None] * len(values)
    window: deque[Number] = deque()
    for i, v in enumerate(values):
        window.append(v)
        if len(window) > period:
            window.popleft()
        if len(window) == period:
            out[i] = pick(window)
    return out
