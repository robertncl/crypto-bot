"""Bollinger-band mean-reversion strategy.

A volatility-aware **mean-reversion** strategy. Bollinger Bands wrap a moving average with
an envelope set ``num_std`` standard deviations wide, so the band automatically widens in
volatile markets and tightens in calm ones. A close outside the envelope is statistically
"stretched" and tends to revert toward the mean:

* **BUY** when the close pierces *below* the lower band (stretched cheap).
* **SELL** when the close pierces *above* the upper band (stretched rich).

Signals are **edge-triggered** — they fire on the bar price crosses the band, not on every
bar it stays outside.

Risk profile: *conservative.* Because the band adapts to volatility it trades less in quiet
markets and demands a real stretch before acting; on slow timeframes and major pairs it is
a measured, low-frequency approach. Like all mean-reversion it can be wrong-footed by a
sustained trend, so keep a protective stop.
"""

from __future__ import annotations

from crypto_bot.core.models import HOLD, Candle, Signal, SignalType
from crypto_bot.indicators.ta import bollinger_bands
from crypto_bot.strategies.base import Strategy


class BollingerReversion(Strategy):
    name = "bollinger"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.period = int(self.params.get("period", 20))
        self.num_std = float(self.params.get("num_std", 2.0))
        if self.period <= 0:
            raise ValueError("period must be positive")
        if self.num_std <= 0:
            raise ValueError("num_std must be positive")

    @property
    def warmup(self) -> int:
        # Bands need `period` closes; we compare two consecutive bars to detect a pierce.
        return self.period + 1

    def generate(self, candles: list[Candle]) -> Signal:
        if len(candles) < self.warmup:
            return HOLD

        closes = [c.close for c in candles]
        lower, _middle, upper = bollinger_bands(closes, self.period, self.num_std)
        if None in (lower[-1], lower[-2], upper[-1], upper[-2]):
            return HOLD

        close_now, close_prev = closes[-1], closes[-2]

        pierced_low = close_prev >= lower[-2] and close_now < lower[-1]
        pierced_high = close_prev <= upper[-2] and close_now > upper[-1]

        if pierced_low:
            return Signal(
                SignalType.BUY,
                reason=f"close pierced lower Bollinger band "
                f"({self.period}, {self.num_std:g}σ)",
            )
        if pierced_high:
            return Signal(
                SignalType.SELL,
                reason=f"close pierced upper Bollinger band "
                f"({self.period}, {self.num_std:g}σ)",
            )
        return HOLD
