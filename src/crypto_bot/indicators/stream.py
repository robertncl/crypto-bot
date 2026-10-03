"""Streaming (incremental) indicators: O(1) work per bar instead of O(window).

The batch functions in :mod:`crypto_bot.indicators.ta` recompute an indicator over a whole
list. That is the right shape for analysis, but a trading loop only ever needs the
*latest* value, one new bar at a time. Re-running a 200-bar EMA on every bar to read its
last element is O(window) per bar, and in a backtest that recomputation was ~95% of the
runtime.

Each class here holds the indicator's running state and folds in one value (or bar) per
:meth:`update` call, returning the newest value (``None`` until warmed up, exactly where
the batch output is ``None``).

**Exactness contract.** Feeding a series from its first element produces, at every step,
*bit-for-bit* the value the matching batch function returns for that prefix. The
arithmetic is written in the same order as the batch code, and the Wilder/EMA seeds are
summed with :func:`sum` over the same short buffer, so even the floating-point rounding
agrees. ``tests/test_indicator_stream.py`` pins this for every class. Strategies rely on it:
edge-triggered signals compare values that can differ by one ULP near a crossover.

Bollinger Bands have no class here on purpose: they depend only on a short trailing
window, so the strategy keeps that window and calls the batch function on it directly.
"""

from __future__ import annotations

from collections import deque


class EMA:
    """Exponential moving average seeded with the SMA of the first ``period`` values."""

    __slots__ = ("period", "value", "_mult", "_seed")

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("period must be a positive integer")
        self.period = period
        self.value: float | None = None
        self._mult = 2.0 / (period + 1)
        self._seed: list[float] | None = []

    def update(self, x: float) -> float | None:
        prev = self.value
        if prev is None:
            seed = self._seed
            seed.append(x)
            if len(seed) == self.period:
                self.value = sum(seed) / self.period
                self._seed = None
            return self.value
        prev = (x - prev) * self._mult + prev
        self.value = prev
        return prev


class SMA:
    """Simple moving average over a trailing window (running sum, as the batch SMA)."""

    __slots__ = ("period", "value", "_window", "_running")

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("period must be a positive integer")
        self.period = period
        self.value: float | None = None
        self._window: deque[float] = deque()
        self._running = 0.0

    def update(self, x: float) -> float | None:
        window = self._window
        self._running += x
        window.append(x)
        if len(window) > self.period:
            self._running -= window.popleft()
        if len(window) == self.period:
            self.value = self._running / self.period
        return self.value


def moving_average(period: int, kind: str = "ema") -> EMA | SMA:
    """Streaming counterpart of :func:`crypto_bot.indicators.ta.moving_average`."""
    kind = kind.lower()
    if kind == "ema":
        return EMA(period)
    if kind == "sma":
        return SMA(period)
    raise ValueError(f"unknown moving-average kind: {kind!r} (expected 'ema' or 'sma')")


class RSI:
    """Relative Strength Index with Wilder's smoothing."""

    __slots__ = ("period", "value", "_prev", "_changes", "_gains", "_losses",
                 "_avg_gain", "_avg_loss")

    def __init__(self, period: int = 14) -> None:
        if period <= 0:
            raise ValueError("period must be a positive integer")
        self.period = period
        self.value: float | None = None
        self._prev: float | None = None
        self._changes = 0
        self._gains = 0.0
        self._losses = 0.0
        self._avg_gain = 0.0
        self._avg_loss = 0.0

    def update(self, x: float) -> float | None:
        prev = self._prev
        self._prev = x
        if prev is None:
            return None
        change = x - prev
        period = self.period

        if self.value is None:
            # Seed: plain average of the first `period` gains and losses.
            if change >= 0:
                self._gains += change
            else:
                self._losses -= change
            self._changes += 1
            if self._changes < period:
                return None
            avg_gain = self._gains / period
            avg_loss = self._losses / period
        else:
            prev_weight = period - 1
            avg_gain = self._avg_gain
            avg_loss = self._avg_loss
            if change > 0:
                avg_gain = (avg_gain * prev_weight + change) / period
                avg_loss = (avg_loss * prev_weight) / period
            elif change < 0:
                avg_gain = (avg_gain * prev_weight) / period
                avg_loss = (avg_loss * prev_weight - change) / period
            else:
                avg_gain = (avg_gain * prev_weight) / period
                avg_loss = (avg_loss * prev_weight) / period

        self._avg_gain = avg_gain
        self._avg_loss = avg_loss
        value = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        self.value = value
        return value


class ATR:
    """Average True Range (Wilder), seeded with the mean of the first ``period`` TRs."""

    __slots__ = ("period", "value", "_prev_close", "_seed")

    def __init__(self, period: int = 14) -> None:
        if period <= 0:
            raise ValueError("period must be a positive integer")
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._seed: list[float] | None = []

    def update(self, high: float, low: float, close: float) -> float | None:
        # True range, written exactly as ta.true_range() computes it.
        prev_close = self._prev_close
        tr = high - low
        if prev_close is not None:
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
        self._prev_close = close

        prev = self.value
        if prev is None:
            seed = self._seed
            seed.append(tr)
            if len(seed) == self.period:
                self.value = sum(seed) / self.period
                self._seed = None
            return self.value
        prev = (prev * (self.period - 1) + tr) / self.period
        self.value = prev
        return prev


class Supertrend:
    """Supertrend line and direction (+1 up, -1 down; ``None`` during ATR warm-up)."""

    __slots__ = ("period", "multiplier", "line", "direction", "_atr", "_prev_close",
                 "_upper", "_lower")

    def __init__(self, period: int = 10, multiplier: float = 3.0) -> None:
        if period <= 0:
            raise ValueError("period must be a positive integer")
        if multiplier <= 0:
            raise ValueError("multiplier must be positive")
        self.period = period
        self.multiplier = multiplier
        self.line: float | None = None
        self.direction: int | None = None
        self._atr = ATR(period)
        self._prev_close: float | None = None
        self._upper = 0.0
        self._lower = 0.0

    def update(self, high: float, low: float, close: float) -> int | None:
        a = self._atr.update(high, low, close)
        prev_close = self._prev_close
        self._prev_close = close
        if a is None:
            return None

        hl2 = (high + low) / 2
        basic_upper = hl2 + self.multiplier * a
        basic_lower = hl2 - self.multiplier * a

        prev_dir = self.direction
        if prev_dir is None:
            # First bar with a defined ATR: seed the bands and assume an uptrend.
            self._upper = basic_upper
            self._lower = basic_lower
            self.direction = 1
            self.line = basic_lower
            return 1

        prev_upper = self._upper
        prev_lower = self._lower
        upper = (
            basic_upper if basic_upper < prev_upper or prev_close > prev_upper else prev_upper
        )
        lower = (
            basic_lower if basic_lower > prev_lower or prev_close < prev_lower else prev_lower
        )
        if prev_dir == 1:
            now = -1 if close < lower else 1
        else:
            now = 1 if close > upper else -1
        self.direction = now
        self.line = lower if now == 1 else upper
        self._upper = upper
        self._lower = lower
        return now


class ADX:
    """Average Directional Index (Wilder); first defined on bar ``2 * period``."""

    __slots__ = ("period", "value", "_bars", "_prev_high", "_prev_low", "_prev_close",
                 "_seed_tr", "_seed_pdm", "_seed_mdm", "_dx_seed",
                 "_smooth_tr", "_smooth_pdm", "_smooth_mdm")

    def __init__(self, period: int = 14) -> None:
        if period <= 0:
            raise ValueError("period must be a positive integer")
        self.period = period
        self.value: float | None = None
        self._bars = 0
        self._prev_high = 0.0
        self._prev_low = 0.0
        self._prev_close = 0.0
        # Seed windows are summed with sum(), exactly as ta.adx() does.
        self._seed_tr: list[float] = []
        self._seed_pdm: list[float] = []
        self._seed_mdm: list[float] = []
        self._dx_seed: list[float] = []
        self._smooth_tr = 0.0
        self._smooth_pdm = 0.0
        self._smooth_mdm = 0.0

    def update(self, high: float, low: float, close: float) -> float | None:
        i = self._bars
        self._bars = i + 1
        if i == 0:
            self._prev_high = high
            self._prev_low = low
            self._prev_close = close
            return None

        period = self.period
        prev_close = self._prev_close
        hi = high if high > prev_close else prev_close
        lo = low if low < prev_close else prev_close
        tr = hi - lo

        up = high - self._prev_high
        down = self._prev_low - low
        self._prev_high = high
        self._prev_low = low
        self._prev_close = close
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
            self._seed_tr.append(tr)
            self._seed_pdm.append(plus_dm)
            self._seed_mdm.append(minus_dm)
            if i < period:
                return None
            smooth_tr = sum(self._seed_tr)
            smooth_pdm = sum(self._seed_pdm)
            smooth_mdm = sum(self._seed_mdm)
            self._seed_tr = self._seed_pdm = self._seed_mdm = []
        else:
            smooth_tr = self._smooth_tr
            smooth_pdm = self._smooth_pdm
            smooth_mdm = self._smooth_mdm
            smooth_tr += tr - smooth_tr / period
            smooth_pdm += plus_dm - smooth_pdm / period
            smooth_mdm += minus_dm - smooth_mdm / period
        self._smooth_tr = smooth_tr
        self._smooth_pdm = smooth_pdm
        self._smooth_mdm = smooth_mdm

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

        ready = 2 * period
        if i < ready:
            self._dx_seed.append(dx)
            if i == ready - 1:
                self.value = sum(self._dx_seed) / period
                self._dx_seed = []
            return self.value
        prev = (self.value * (period - 1) + dx) / period
        self.value = prev
        return prev


class RollingExtreme:
    """Rolling max (or min) over the last ``period`` values via a monotonic deque."""

    __slots__ = ("period", "value", "_want_max", "_dq", "_i")

    def __init__(self, period: int, *, want_max: bool) -> None:
        if period <= 0:
            raise ValueError("period must be a positive integer")
        self.period = period
        self.value: float | None = None
        self._want_max = want_max
        self._dq: deque[tuple[int, float]] = deque()  # (index, value), extreme at the front
        self._i = 0

    def update(self, x: float) -> float | None:
        dq = self._dq
        i = self._i
        self._i = i + 1
        if self._want_max:
            while dq and dq[-1][1] <= x:
                dq.pop()
        else:
            while dq and dq[-1][1] >= x:
                dq.pop()
        dq.append((i, x))
        if dq[0][0] <= i - self.period:
            dq.popleft()
        if i >= self.period - 1:
            self.value = dq[0][1]
        return self.value


def highest(period: int) -> RollingExtreme:
    """Streaming Donchian upper (rolling maximum)."""
    return RollingExtreme(period, want_max=True)


def lowest(period: int) -> RollingExtreme:
    """Streaming Donchian lower (rolling minimum)."""
    return RollingExtreme(period, want_max=False)


class MACD:
    """MACD line, signal line and histogram, as :func:`crypto_bot.indicators.ta.macd`."""

    __slots__ = ("macd", "signal", "histogram", "_fast", "_slow", "_signal")

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        if fast <= 0 or slow <= 0 or signal <= 0:
            raise ValueError("fast, slow and signal periods must be positive")
        if fast >= slow:
            raise ValueError("fast period must be smaller than slow period")
        self.macd: float | None = None
        self.signal: float | None = None
        self.histogram: float | None = None
        self._fast = EMA(fast)
        self._slow = EMA(slow)
        self._signal = EMA(signal)

    def update(self, x: float) -> float | None:
        fast = self._fast.update(x)
        slow = self._slow.update(x)
        if slow is None:
            return None
        line = fast - slow
        self.macd = line
        sig = self._signal.update(line)
        if sig is not None:
            self.signal = sig
            self.histogram = line - sig
        return line

