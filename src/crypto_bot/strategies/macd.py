"""MACD momentum strategy.

MACD (Moving Average Convergence Divergence) turns two trend-following EMAs into a
**momentum oscillator**. The MACD line is the gap between a fast and a slow EMA; the
signal line is an EMA of the MACD line. When the gap is *widening* in the up direction
momentum is building; the signal-line crossover is the canonical way to time that:

* **BUY** when the MACD line crosses *above* its signal line (momentum turning up).
* **SELL** when the MACD line crosses *below* its signal line (momentum turning down).

Equivalently, this is the bar on which the MACD *histogram* (MACD − signal) flips sign.
Signals are **edge-triggered** — they fire once, on the bar of the cross.

Risk profile: *balanced trend/momentum.* Like the MA crossover it rides trends and gets
whipsawed in chop, but the second EMA-smoothing of the signal line filters some of the
noise a raw price crossover would emit. Defaults are the classic 12 / 26 / 9.
"""

from __future__ import annotations

from crypto_bot.core.models import HOLD, Candle, Signal, SignalType
from crypto_bot.indicators.ta import macd
from crypto_bot.strategies.base import Strategy


class MACDMomentum(Strategy):
    name = "macd"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.fast_period = int(self.params.get("fast_period", 12))
        self.slow_period = int(self.params.get("slow_period", 26))
        self.signal_period = int(self.params.get("signal_period", 9))
        if min(self.fast_period, self.slow_period, self.signal_period) <= 0:
            raise ValueError("fast_period, slow_period and signal_period must be positive")
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be smaller than slow_period")

    @property
    def warmup(self) -> int:
        # Signal line is first defined at index slow + signal - 2; we need two consecutive
        # defined bars to detect a cross, so slow + signal candles in total.
        return self.slow_period + self.signal_period

    def generate(self, candles: list[Candle]) -> Signal:
        if len(candles) < self.warmup:
            return HOLD

        closes = [c.close for c in candles]
        macd_line, signal_line, _hist = macd(
            closes, self.fast_period, self.slow_period, self.signal_period
        )
        macd_now, macd_prev = macd_line[-1], macd_line[-2]
        sig_now, sig_prev = signal_line[-1], signal_line[-2]
        if None in (macd_now, macd_prev, sig_now, sig_prev):
            return HOLD

        crossed_up = macd_prev <= sig_prev and macd_now > sig_now
        crossed_down = macd_prev >= sig_prev and macd_now < sig_now

        if crossed_up:
            return Signal(
                SignalType.BUY,
                reason=f"MACD({self.fast_period},{self.slow_period},{self.signal_period}) "
                f"crossed above signal",
            )
        if crossed_down:
            return Signal(
                SignalType.SELL,
                reason=f"MACD({self.fast_period},{self.slow_period},{self.signal_period}) "
                f"crossed below signal",
            )
        return HOLD
