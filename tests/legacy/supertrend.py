"""Supertrend strategy.

A **volatility-adaptive trend-following** strategy and one of the most popular indicators
in modern crypto algo-trading. It trails a line ``multiplier × ATR`` away from the mid
price — below price in an uptrend, above it in a downtrend — that ratchets in the trend's
favour and only flips when price *closes* through it. Because the offset scales with the
Average True Range, the line gives a trend room to breathe when volatility is high and
tightens when the market is calm.

* **BUY** when the trend flips up (price closes above the down-trending line).
* **SELL** when the trend flips down (price closes below the up-trending line).

Signals are **edge-triggered**: one fires on the bar the trend direction flips, not on
every bar the trend persists.

Risk profile: *trend-following, between MA crossover and breakout.* It stays in a move far
longer than an oscillator would and rarely picks tops or bottoms; its weakness is the usual
trend-follower's curse — repeated small losses in sideways chop. Classic defaults are an
ATR period of 10 and a multiplier of 3.0; lower the multiplier for more (earlier) flips.
"""

from __future__ import annotations

from crypto_bot.core.models import HOLD, Candle, Signal, SignalType
from crypto_bot.indicators.ta import supertrend
from crypto_bot.strategies.base import Strategy


class Supertrend(Strategy):
    name = "supertrend"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.period = int(self.params.get("period", 10))
        self.multiplier = float(self.params.get("multiplier", 3.0))
        if self.period <= 0:
            raise ValueError("period must be positive")
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive")

    @property
    def warmup(self) -> int:
        # ATR (and the seed direction) is defined at index period-1; we need two genuinely
        # evaluated direction bars after the seed to detect a flip without acting on it.
        return self.period + 2

    def generate(self, candles: list[Candle], symbol: str | None = None) -> Signal:
        if len(candles) < self.warmup:
            return HOLD

        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]
        _line, direction = supertrend(highs, lows, closes, self.period, self.multiplier)

        now, prev = direction[-1], direction[-2]
        if now is None or prev is None:
            return HOLD

        if prev == -1 and now == 1:
            return Signal(
                SignalType.BUY,
                reason=f"Supertrend({self.period},{self.multiplier:g}) flipped up",
            )
        if prev == 1 and now == -1:
            return Signal(
                SignalType.SELL,
                reason=f"Supertrend({self.period},{self.multiplier:g}) flipped down",
            )
        return HOLD
