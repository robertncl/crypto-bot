"""RSI mean-reversion strategy.

A **mean-reversion** strategy (the contrarian counterpart to the trend-following MA
crossover): it bets that price stretched to an extreme will snap back toward its average.
It reads the Relative Strength Index — a 0–100 momentum gauge — and trades the *recovery*
out of an extreme rather than the extreme itself:

* **BUY** when RSI climbs back *above* the oversold line (e.g. up through 30). Waiting for
  the cross-back, instead of buying the instant RSI dips below 30, avoids "catching a
  falling knife" while momentum is still collapsing.
* **SELL** when RSI rolls back *below* the overbought line (e.g. down through 70).

Like the MA crossover, signals are **edge-triggered**: they fire once, on the bar the line
is crossed, not on every bar RSI stays beyond it.

Risk profile: *balanced / contrarian.* Shines in calm, range-bound markets; its weakness
is a strong trend, where "oversold" keeps getting more oversold — pair it with a stop-loss.
"""

from __future__ import annotations

from crypto_bot.core.models import HOLD, Candle, Signal, SignalType
from crypto_bot.indicators.ta import rsi
from crypto_bot.strategies.base import Strategy


class RSIReversion(Strategy):
    name = "rsi_reversion"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.period = int(self.params.get("period", 14))
        self.oversold = float(self.params.get("oversold", 30.0))
        self.overbought = float(self.params.get("overbought", 70.0))
        if self.period <= 0:
            raise ValueError("period must be positive")
        if not 0 < self.oversold < self.overbought < 100:
            raise ValueError("require 0 < oversold < overbought < 100")

    @property
    def warmup(self) -> int:
        # rsi() needs period+1 closes for its first value; we need two in a row to see a cross.
        return self.period + 2

    def generate(self, candles: list[Candle], symbol: str | None = None) -> Signal:
        if len(candles) < self.warmup:
            return HOLD

        closes = [c.close for c in candles]
        values = rsi(closes, self.period)
        now, prev = values[-1], values[-2]
        if now is None or prev is None:
            return HOLD

        crossed_up = prev <= self.oversold and now > self.oversold
        crossed_down = prev >= self.overbought and now < self.overbought

        if crossed_up:
            return Signal(
                SignalType.BUY,
                reason=f"RSI({self.period}) recovered above oversold "
                f"{self.oversold:.0f} ({prev:.1f}->{now:.1f})",
            )
        if crossed_down:
            return Signal(
                SignalType.SELL,
                reason=f"RSI({self.period}) fell below overbought "
                f"{self.overbought:.0f} ({prev:.1f}->{now:.1f})",
            )
        return HOLD
