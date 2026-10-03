"""Dollar-Cost Averaging (DCA) / Auto-Invest strategy.

The software analog of a centralized-exchange "Auto-Invest" *earn* product: instead of
reacting to price, it **accumulates on a schedule** — buy a tranche every ``every``
candles of the configured timeframe, regardless of price, and hold. Over time this
averages your entry across the whole period, smoothing out volatility and removing the
temptation to time the market. Historically hard to beat for long-term holders.

* **BUY** once per scheduled candle.
* It never emits SELL — DCA is accumulate-and-hold. (Whether positions are ever trimmed
  is left to the risk manager's stop-loss / take-profit, which an earn-style profile
  typically disables.)

Unlike the signal strategies, DCA is **schedule-based, not condition-based**, so it is the
one deliberate exception to the "pure, stateless decision function" rule: it remembers the
timestamp of the candle it last bought on, so that polling many times within one candle
still produces exactly one buy per scheduled bar.

Two things make this strategy actually accumulate rather than buy once:

1. ``risk.allow_averaging_in: true`` — lets the risk manager top up a symbol already held
   (signal strategies leave this off). Pair it with ``risk.max_position_pct`` to bound how
   large the stack can grow.
2. Sizing is a fraction of *equity* per tranche (``risk.position_pct``), so this is
   percentage-based DCA rather than a fixed quote amount; a fixed-quote mode would be a
   small risk-manager extension.
"""

from __future__ import annotations

from crypto_bot.core.models import HOLD, Candle, Signal, SignalType
from crypto_bot.strategies.base import Strategy


class DCA(Strategy):
    name = "dca"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.every = int(self.params.get("every", 1))
        if self.every <= 0:
            raise ValueError("every must be a positive integer (buy every N candles)")
        # The engine shares one strategy instance across symbols, so dedupe per symbol:
        # the timestamp of the candle each symbol last bought on.
        self._last_buy_ts: dict[str | None, int] = {}

    @property
    def warmup(self) -> int:
        # Two candles so the bar interval can be inferred from their timestamps.
        return 2

    def generate(self, candles: list[Candle], symbol: str | None = None) -> Signal:
        if len(candles) < self.warmup:
            return HOLD

        latest = candles[-1]
        interval = latest.timestamp - candles[-2].timestamp
        if interval <= 0:
            return HOLD  # non-increasing timestamps: can't place the bar on the schedule

        # Anchor the schedule to the epoch so "every N bars" is deterministic and
        # independent of how many candles happen to be in the rolling window.
        bar_index = latest.timestamp // interval
        if bar_index % self.every != 0:
            return HOLD
        if self._last_buy_ts.get(symbol) == latest.timestamp:
            return HOLD  # already bought this symbol on this candle; don't re-fire across polls

        self._last_buy_ts[symbol] = latest.timestamp
        cadence = "every candle" if self.every == 1 else f"every {self.every} candles"
        return Signal(SignalType.BUY, reason=f"DCA scheduled buy ({cadence})")
