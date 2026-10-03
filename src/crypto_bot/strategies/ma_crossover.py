"""Moving-average crossover strategy.

Emits BUY when the fast MA crosses *above* the slow MA, and SELL when it crosses
*below*. A signal fires only on the bar where the cross happens (edge-triggered), not
on every bar the fast MA stays above/below the slow MA.
"""

from __future__ import annotations

from crypto_bot.core.models import HOLD, Candle, MarketContext, Signal, SignalType
from crypto_bot.indicators.stream import moving_average
from crypto_bot.strategies.base import Strategy, StrategyState


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

    def new_state(self, symbol: str | None = None) -> StrategyState:
        return _MACrossoverState(self)


class _MACrossoverState(StrategyState):
    __slots__ = ("_s", "_fast", "_slow", "_fast_prev", "_slow_prev", "_bars", "_up", "_down")

    def __init__(self, strategy: MACrossover) -> None:
        self._s = strategy
        self._fast = moving_average(strategy.fast_period, strategy.ma_type)
        self._slow = moving_average(strategy.slow_period, strategy.ma_type)
        self._fast_prev: float | None = None
        self._slow_prev: float | None = None
        self._bars = 0
        label = f"fast {strategy.ma_type.upper()}({strategy.fast_period}) crossed"
        self._up = Signal(SignalType.BUY, reason=f"{label} above slow({strategy.slow_period})")
        self._down = Signal(
            SignalType.SELL, reason=f"{label} below slow({strategy.slow_period})"
        )

    def update(self, candle: Candle, context: MarketContext | None = None) -> Signal:
        close = candle.close
        fast_now = self._fast.update(close)
        slow_now = self._slow.update(close)
        fast_prev, slow_prev = self._fast_prev, self._slow_prev
        self._fast_prev, self._slow_prev = fast_now, slow_now
        self._bars += 1
        if self._bars < self._s.warmup or fast_prev is None or slow_prev is None:
            return HOLD

        if fast_prev <= slow_prev and fast_now > slow_now:
            return self._up
        if fast_prev >= slow_prev and fast_now < slow_now:
            return self._down
        return HOLD
