"""Funding-rate positioning strategy — the one edge that only exists on derivatives.

A perpetual swap has no expiry, so it is tethered to spot by a periodic **funding**
payment between the two sides. When the perp trades above spot the rate goes positive and
longs pay shorts; when it trades below, shorts pay longs. That single number is doing two
useful jobs at once:

1. **It is a crowding gauge.** Funding only goes sharply positive because leveraged longs
   are queueing up and bidding the perp over spot. Persistently extreme funding is the
   cleanest public read on one-sided positioning, and crowded books are fragile — they
   unwind through liquidation cascades, which is precisely the move a contrarian wants.
2. **It is a cash flow.** Taking the unpopular side *earns* the funding for as long as you
   hold it. The position is paid to wait, which is what separates this from an ordinary
   contrarian trade that only wins if price moves.

So:

* **SELL (short)** when annualized funding rises above ``enter_apr`` — longs are crowded
  and paying us to fade them.
* **BUY (long)** when it falls below ``-enter_apr`` — shorts are crowded and paying us.

**Requires ``derivatives.allow_shorts``** to express the short leg; with it off, only the
long side can ever fire and the strategy is half-blind.

Risk profile: *contrarian carry.* The failure mode is the honest one — funding can stay
extreme for a long time while price keeps trending, so a naive fade gets run over even
while collecting carry. ``trend_period`` guards against that by refusing to fight an
established trend (short only below the EMA, long only above it); set it to 0 to trade
funding alone. Keep a stop-loss on: the carry is small and steady, the tail is not.

Two structural caveats worth stating plainly:

* Real funding arbitrage is **delta-neutral** (short perp against long spot), which
  collects the carry with the price risk hedged out. This bot holds one directional
  position per symbol, so this is a *directional* trade with a funding tailwind — a
  weaker, more volatile cousin of the real carry trade.
* Signals are **level-triggered**, not edge-triggered: the strategy keeps asking for the
  same side while funding stays extreme. That is deliberate and idempotent — the engine
  declines an entry that matches an open position — and it means a position re-establishes
  itself if a protective stop took it out while the imbalance persists.
"""

from __future__ import annotations

from crypto_bot.core.models import HOLD, Candle, MarketContext, Signal, SignalType
from crypto_bot.indicators.ta import ema
from crypto_bot.strategies.base import Strategy


class FundingBias(Strategy):
    name = "funding_bias"
    wants_context = True

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.enter_apr = float(self.params.get("enter_apr", 0.20))
        self.trend_period = int(self.params.get("trend_period", 100))
        if self.enter_apr <= 0:
            raise ValueError("enter_apr must be positive (annualized, e.g. 0.20 = 20%/yr)")
        if self.trend_period < 0:
            raise ValueError("trend_period must be >= 0 (0 disables the trend guard)")

    @property
    def warmup(self) -> int:
        # Without the trend guard a single candle is enough — the signal lives in the
        # funding rate, not in price history.
        return max(self.trend_period, 1)

    def generate(
        self,
        candles: list[Candle],
        symbol: str | None = None,
        context: MarketContext | None = None,
    ) -> Signal:
        if len(candles) < self.warmup:
            return HOLD
        if context is None or context.funding_rate is None:
            return HOLD  # spot market, or the venue exposes no funding rate

        apr = context.funding_apr
        if apr is None or abs(apr) < self.enter_apr:
            return HOLD  # funding is unremarkable; no positioning edge to trade

        trend = self._trend(candles)
        close = candles[-1].close

        if apr > 0:
            # Longs are crowded and paying. Fade them, unless price is in an uptrend the
            # trend guard says not to fight.
            if trend is not None and close > trend:
                return HOLD
            return Signal(
                SignalType.SELL,
                reason=f"funding {apr:+.1%}/yr — crowded longs paying shorts",
            )

        if trend is not None and close < trend:
            return HOLD
        return Signal(
            SignalType.BUY,
            reason=f"funding {apr:+.1%}/yr — crowded shorts paying longs",
        )

    def _trend(self, candles: list[Candle]) -> float | None:
        """Trend reference price, or None when the guard is disabled/undefined."""
        if self.trend_period <= 0:
            return None
        closes = [c.close for c in candles]
        return ema(closes, self.trend_period)[-1]
