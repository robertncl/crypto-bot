"""Moving-average crossover strategy.

Emits BUY when the fast MA crosses *above* the slow MA, and SELL when it crosses
*below*. A signal fires only on the bar where the cross happens (edge-triggered), not
on every bar the fast MA stays above/below the slow MA.
"""

from __future__ import annotations

from crypto_bot.core.models import HOLD, Candle, Signal, SignalType
from crypto_bot.indicators.ta import moving_average
from crypto_bot.strategies.base import Strategy


class MACrossover(Strategy):
    name = "ma_crossover"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.fast_period = int(self.params.get("fast_period", 12))
        self.slow_period = int(self.params.get("slow_period", 26))
        self.ma_type = str(self.params.get("ma_type", "ema")).lower()
        if self.fast_period <= 0 or self.slow_period <= 0:
            raise ValueError("fast_period and slow_period must be positive")
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be smaller than slow_period")

    @property
    def warmup(self) -> int:
        # Need two consecutive bars where the slow MA is defined to detect a cross.
        return self.slow_period + 1

    def generate(self, candles: list[Candle]) -> Signal:
        if len(candles) < self.warmup:
            return HOLD

        closes = [c.close for c in candles]
        fast = moving_average(closes, self.fast_period, self.ma_type)
        slow = moving_average(closes, self.slow_period, self.ma_type)

        fast_now, fast_prev = fast[-1], fast[-2]
        slow_now, slow_prev = slow[-1], slow[-2]
        if None in (fast_now, fast_prev, slow_now, slow_prev):
            return HOLD

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up:
            return Signal(
                SignalType.BUY,
                reason=f"fast {self.ma_type.upper()}({self.fast_period}) crossed above "
                f"slow({self.slow_period})",
            )
        if crossed_down:
            return Signal(
                SignalType.SELL,
                reason=f"fast {self.ma_type.upper()}({self.fast_period}) crossed below "
                f"slow({self.slow_period})",
            )
        return HOLD
