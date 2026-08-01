"""Pure-Python technical indicators.

Each function takes a list of floats (typically closing prices) and returns a list
of the same length, left-padded with ``None`` for the warm-up period where the
indicator is not yet defined. Keeping the output aligned with the input makes it
trivial to zip indicator values back onto candles.

These are intentionally dependency-free (no pandas/numpy) so the strategy and test
layers stay lightweight and fast.

**Performance.** Strategies re-run these over a rolling window on *every* bar, so a
backtest calls them tens of thousands of times and they dominate its runtime. The
implementations therefore keep running state in local scalars instead of intermediate
lists, avoid per-element function calls in the inner loops, and use O(n) algorithms
throughout (notably a monotonic deque for the rolling extremes, which replaces an
O(n × period) scan). Arithmetic is deliberately written in the same order a naive
implementation would evaluate it, so results are bit-for-bit unchanged.
"""

from __future__ import annotations

from collections import deque

Number = float


def sma(values: list[Number], period: int) -> list[Number | None]:
    """Simple moving average."""
    if period <= 0:
        raise ValueError("period must be a positive integer")
    n = len(values)
    out: list[Number | None] = [None] * n
    running = 0.0
    last = period - 1
    for i in range(n):
        running += values[i]
        if i > last:
            running -= values[i - period]
        if i >= last:
            out[i] = running / period
    return out


def ema(values: list[Number], period: int) -> list[Number | None]:
    """Exponential moving average, seeded with the SMA of the first ``period`` values."""
    if period <= 0:
        raise ValueError("period must be a positive integer")
    n = len(values)
    out: list[Number | None] = [None] * n
    if n < period:
        return out
    multiplier = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (values[i] - prev) * multiplier + prev
        out[i] = prev
    return out


def rsi(values: list[Number], period: int = 14) -> list[Number | None]:
    """Relative Strength Index using Wilder's smoothing."""
    if period <= 0:
        raise ValueError("period must be a positive integer")
    n = len(values)
    out: list[Number | None] = [None] * n
    if n <= period:
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
    # RSI from the smoothed averages, inlined here and below: a helper call per bar
    # is a measurable share of this loop's cost.
    out[period] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    # Hot loop: hoist the constant divisor and inline _rsi_from_averages to avoid a
    # function call per bar. Result is identical to the helper.
    prev_weight = period - 1
    prev = values[period]
    for i in range(period + 1, len(values)):
        cur = values[i]
        change = cur - prev
        prev = cur
        if change > 0:
            avg_gain = (avg_gain * prev_weight + change) / period
            avg_loss = (avg_loss * prev_weight) / period
        elif change < 0:
            avg_gain = (avg_gain * prev_weight) / period
            avg_loss = (avg_loss * prev_weight - change) / period
        else:
            avg_gain = (avg_gain * prev_weight) / period
            avg_loss = (avg_loss * prev_weight) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    return out


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
    n = len(values)
    out: list[Number | None] = [None] * n
    running = 0.0
    running_sq = 0.0
    last = period - 1
    for i in range(n):
        v = values[i]
        running += v
        running_sq += v * v
        if i > last:
            old = values[i - period]
            running -= old
            running_sq -= old * old
        if i >= last:
            mean = running / period
            # Clamp tiny negatives from floating-point error before the square root.
            variance = running_sq / period - mean * mean
            out[i] = (variance if variance > 0.0 else 0.0) ** 0.5
    return out


def bollinger_bands(
    values: list[Number], period: int = 20, num_std: float = 2.0
) -> tuple[list[Number | None], list[Number | None], list[Number | None]]:
    """Bollinger Bands: ``(lower, middle, upper)`` where middle is the SMA and the
    bands sit ``num_std`` population standard deviations either side of it.

    Each returned list is aligned with ``values`` and left-padded with ``None`` over
    the warm-up period.
    """
    if period <= 0:
        raise ValueError("period must be a positive integer")
    n = len(values)
    lower: list[Number | None] = [None] * n
    middle: list[Number | None] = [None] * n
    upper: list[Number | None] = [None] * n
    # One fused pass: the mean and the rolling variance share the same window sums, so
    # computing them together avoids three separate traversals of `values`.
    running = 0.0
    running_sq = 0.0
    last = period - 1
    for i in range(n):
        v = values[i]
        running += v
        running_sq += v * v
        if i > last:
            old = values[i - period]
            running -= old
            running_sq -= old * old
        if i >= last:
            mean = running / period
            variance = running_sq / period - mean * mean
            offset = num_std * (variance if variance > 0.0 else 0.0) ** 0.5
            middle[i] = mean
            lower[i] = mean - offset
            upper[i] = mean + offset
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

    n = len(values)
    macd_line: list[Number | None] = [None] * n
    signal_line: list[Number | None] = [None] * n
    histogram: list[Number | None] = [None] * n
    if n < slow:
        return macd_line, signal_line, histogram

    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    # Both EMAs are defined from `slow - 1` onward (fast is defined earlier still), so
    # the MACD line's warm-up is known outright — no need to scan for it.
    start = slow - 1
    for i in range(start, n):
        macd_line[i] = fast_ema[i] - slow_ema[i]

    # The MACD line is defined over a single contiguous tail; take the EMA of just
    # that region, then re-align the result back onto the full-length output.
    sig = ema(macd_line[start:], signal)
    for offset in range(signal - 1, len(sig)):
        s = sig[offset]
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
    if n == 0:
        return []
    # Inline abs()/max() as comparisons and carry prev_close forward: this is a per-bar
    # hot loop in every ATR/ADX/Supertrend evaluation, and the builtin-call overhead
    # dominated it. Values are identical to max(h-l, |h-pc|, |l-pc|).
    out: list[Number | None] = [highs[0] - lows[0]]
    prev_close = closes[0]
    for i in range(1, n):
        high = highs[i]
        low = lows[i]
        tr = high - low
        hc = high - prev_close
        if hc < 0.0:
            hc = -hc
        if hc > tr:
            tr = hc
        lc = prev_close - low
        if lc < 0.0:
            lc = -lc
        if lc > tr:
            tr = lc
        out.append(tr)
        prev_close = closes[i]
    return out


def atr(
    highs: list[Number], lows: list[Number], closes: list[Number], period: int = 14
) -> list[Number | None]:
    """Average True Range using Wilder's smoothing, seeded with the simple mean of the
    first ``period`` true ranges. Defined from index ``period - 1`` onward."""
    if period <= 0:
        raise ValueError("period must be a positive integer")
    tr = true_range(highs, lows, closes)
    n = len(closes)
    out: list[Number | None] = [None] * n
    if n < period:
        return out
    seed = sum(tr[:period]) / period  # type: ignore[arg-type]  # tr has no None values
    out[period - 1] = seed
    prev = seed
    decay = period - 1
    for i in range(period, n):
        prev = (prev * decay + tr[i]) / period
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

    line: list[Number | None] = [None] * n
    direction: list[int | None] = [None] * n
    if n < period:
        return line, direction

    atr_vals = atr(highs, lows, closes, period)
    # The bands are a pure carry-forward from the previous bar, so hold them in scalars
    # rather than materialising two more full-length lists.
    prev_upper = 0.0
    prev_lower = 0.0
    prev_dir: int | None = None

    # ATR — and therefore the whole indicator — is first defined at index period - 1.
    for i in range(period - 1, n):
        a = atr_vals[i]
        hl2 = (highs[i] + lows[i]) / 2
        basic_upper = hl2 + multiplier * a
        basic_lower = hl2 - multiplier * a

        if prev_dir is None:
            # First bar with a defined ATR: seed the bands and assume an uptrend.
            # The seed is arbitrary, hence the strategy's extra warm-up bar.
            prev_upper = basic_upper
            prev_lower = basic_lower
            prev_dir = 1
            direction[i] = 1
            line[i] = basic_lower
            continue

        prev_close = closes[i - 1]
        upper = (
            basic_upper if basic_upper < prev_upper or prev_close > prev_upper else prev_upper
        )
        lower = (
            basic_lower if basic_lower > prev_lower or prev_close < prev_lower else prev_lower
        )

        if prev_dir == 1:
            now = -1 if closes[i] < lower else 1
        else:
            now = 1 if closes[i] > upper else -1
        direction[i] = now
        line[i] = lower if now == 1 else upper
        prev_upper = upper
        prev_lower = lower
        prev_dir = now

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

    # True range, both directional-movement legs, DX and the ADX average are all
    # produced and consumed in strict index order, so this runs as one streaming pass
    # over the bars — no full-length intermediate lists, and each bar is touched once.
    #
    # Wilder's smoothing is seeded from plain sums over the first `period` movement
    # bars; only those short seed windows are buffered. They are added with sum()
    # rather than a hand-rolled running total because sum() compensates float error,
    # and an ULP of drift here is enough to move a signal.
    seed_tr: list[float] = []
    seed_pdm: list[float] = []
    seed_mdm: list[float] = []
    dx_seed: list[float] = []
    smooth_tr = 0.0
    smooth_pdm = 0.0
    smooth_mdm = 0.0
    prev = 0.0
    prev_high = highs[0]
    prev_low = lows[0]
    ready = 2 * period
    decay = period - 1

    for i in range(1, n):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        # True range, as in true_range() above: max(h, pc) - min(l, pc).
        hi = high if high > prev_close else prev_close
        lo = low if low < prev_close else prev_close
        tr = hi - lo

        up = high - prev_high
        down = prev_low - low
        prev_high = high
        prev_low = low
        if up > down and up > 0:
            plus_dm = up
            minus_dm = 0.0
        elif down > up and down > 0:
            plus_dm = 0.0
            minus_dm = down
        else:
            plus_dm = 0.0
            minus_dm = 0.0

        if i <= period:
            seed_tr.append(tr)
            seed_pdm.append(plus_dm)
            seed_mdm.append(minus_dm)
            if i < period:
                continue
            smooth_tr = sum(seed_tr)
            smooth_pdm = sum(seed_pdm)
            smooth_mdm = sum(seed_mdm)
        else:
            smooth_tr += tr - smooth_tr / period
            smooth_pdm += plus_dm - smooth_pdm / period
            smooth_mdm += minus_dm - smooth_mdm / period

        if smooth_tr == 0:
            dx = 0.0
        else:
            plus_di = 100.0 * smooth_pdm / smooth_tr
            minus_di = 100.0 * smooth_mdm / smooth_tr
            total = plus_di + minus_di
            if total == 0:
                dx = 0.0
            else:
                gap = plus_di - minus_di
                dx = 100.0 * (gap if gap >= 0.0 else -gap) / total

        if i < ready:
            dx_seed.append(dx)
            if i == ready - 1:
                prev = sum(dx_seed) / period
                out[i] = prev
        else:
            prev = (prev * decay + dx) / period
            out[i] = prev
    return out


def highest(values: list[Number], period: int) -> list[Number | None]:
    """Rolling maximum over a trailing window of ``period`` values (Donchian upper)."""
    return _rolling_extreme(values, period, want_max=True)


def lowest(values: list[Number], period: int) -> list[Number | None]:
    """Rolling minimum over a trailing window of ``period`` values (Donchian lower)."""
    return _rolling_extreme(values, period, want_max=False)


def _rolling_extreme(values: list[Number], period: int, want_max: bool) -> list[Number | None]:
    if period <= 0:
        raise ValueError("period must be a positive integer")
    out: list[Number | None] = [None] * len(values)
    # Monotonic deque of *indices*: the front is always the window's extreme, so each bar
    # is O(1) amortized instead of O(period) from re-scanning the window with max()/min().
    dq: deque[int] = deque()
    last = period - 1
    for i, v in enumerate(values):
        if want_max:
            while dq and values[dq[-1]] <= v:
                dq.pop()
        else:
            while dq and values[dq[-1]] >= v:
                dq.pop()
        dq.append(i)
        if dq[0] <= i - period:
            dq.popleft()
        if i >= last:
            out[i] = values[dq[0]]
    return out
