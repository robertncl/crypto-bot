"""Donchian-channel breakout strategy.

A **trend-following / momentum** strategy: it buys *strength*, entering when price pushes
past its own recent range on the theory that a breakout marks the start of a new move.

The Donchian channel is simply the highest high and lowest low of the last ``lookback``
candles. Comparing against the window that *ends on the previous bar* (the current bar is
excluded) makes a breakout unambiguous:

* **BUY** when the latest close prints *above* the prior ``lookback``-bar high.
* **SELL** when the latest close prints *below* the prior ``lookback``-bar low.

Risk profile: *aggressive.* Breakouts ride big trends hard, but in choppy, range-bound
markets they produce false starts ("whipsaw") that bleed fees — give it a wide stop and a
generous take-profit so the winners pay for the losers.
"""

from __future__ import annotations

from crypto_bot.core.models import HOLD, Candle, Signal, SignalType
from crypto_bot.indicators.ta import highest, lowest
from crypto_bot.strategies.base import Strategy


class Breakout(Strategy):
    name = "breakout"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.lookback = int(self.params.get("lookback", 20))
        if self.lookback <= 0:
            raise ValueError("lookback must be positive")

    @property
    def warmup(self) -> int:
        # Need `lookback` bars to form the channel, plus the current bar that breaks it.
        return self.lookback + 1

    def generate(self, candles: list[Candle]) -> Signal:
        if len(candles) < self.warmup:
            return HOLD

        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        close = candles[-1].close

        # `[-2]` is the channel as of the previous bar, so the current bar is excluded.
        channel_high = highest(highs, self.lookback)[-2]
        channel_low = lowest(lows, self.lookback)[-2]
        if channel_high is None or channel_low is None:
            return HOLD

        if close > channel_high:
            return Signal(
                SignalType.BUY,
                reason=f"close {close:.4f} broke {self.lookback}-bar high {channel_high:.4f}",
            )
        if close < channel_low:
            return Signal(
                SignalType.SELL,
                reason=f"close {close:.4f} broke {self.lookback}-bar low {channel_low:.4f}",
            )
        return HOLD
