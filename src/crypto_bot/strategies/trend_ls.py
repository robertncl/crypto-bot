"""Filtered long/short breakout — a symmetric trend follower built for perps.

The existing breakout strategy is a pure Donchian channel: buy the N-bar high, sell the
N-bar low. That is the classic turtle entry and it works, but on its own it fires on
*every* channel break, including the endless false breaks that chop produces. This adds
the two filters that historically do the most to clean that up, and makes the whole thing
symmetric so a downtrend is as tradable as an uptrend:

* **Donchian break** (``lookback``) — the entry trigger. The channel is measured as of the
  *previous* bar so the breaking bar itself cannot widen the level it must clear.
* **ADX strength filter** (``adx_threshold``) — only take breaks while the market is
  actually trending. ADX measures trend strength regardless of direction, so this is the
  single cheapest filter for the trend-follower's worst enemy: range-bound chop.
* **EMA regime filter** (``trend_period``) — only go long above the slow EMA and short
  below it, so a counter-trend break in a strong opposing move is ignored.

Signals:

* **BUY** — close breaks the prior ``lookback``-bar high, ADX ≥ threshold, close > EMA.
* **SELL** — close breaks the prior ``lookback``-bar low, ADX ≥ threshold, close < EMA.

**Pairs with ``derivatives.allow_shorts``**: with shorts enabled the SELL leg opens a
short and the engine stop-and-reverses between the two, which is how a trend follower is
meant to run on a perp. With shorts off it degrades gracefully to long-only, where SELL
merely exits.

Risk profile: *trend-following, more selective than plain breakout.* The filters cut the
number of trades substantially — that is the point, since a breakout system's costs come
from false starts — at the price of entering a little later and missing moves that begin
before ADX confirms. Give it room: a wide stop and a generous take-profit, so the trends
it does catch pay for the breaks that fail.
"""

from __future__ import annotations

from crypto_bot.core.models import HOLD, Candle, Signal, SignalType
from crypto_bot.indicators.ta import adx, ema, highest, lowest
from crypto_bot.strategies.base import Strategy


class TrendLongShort(Strategy):
    name = "trend_ls"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.lookback = int(self.params.get("lookback", 20))
        self.adx_period = int(self.params.get("adx_period", 14))
        self.adx_threshold = float(self.params.get("adx_threshold", 20.0))
        self.trend_period = int(self.params.get("trend_period", 100))
        if self.lookback <= 0:
            raise ValueError("lookback must be positive")
        if self.adx_period <= 0:
            raise ValueError("adx_period must be positive")
        if not 0 <= self.adx_threshold < 100:
            raise ValueError("adx_threshold must be in [0, 100)")
        if self.trend_period < 0:
            raise ValueError("trend_period must be >= 0 (0 disables the regime filter)")

    @property
    def warmup(self) -> int:
        # Donchian needs lookback+1 bars (channel as of the previous bar); ADX is first
        # defined at 2*adx_period - 1; the EMA at trend_period - 1.
        return max(self.lookback + 1, 2 * self.adx_period, self.trend_period)

    def generate(self, candles: list[Candle], symbol: str | None = None) -> Signal:
        if len(candles) < self.warmup:
            return HOLD

        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]
        close = closes[-1]

        strength = adx(highs, lows, closes, self.adx_period)[-1]
        if strength is None or strength < self.adx_threshold:
            return HOLD  # ranging: stand aside rather than buy the chop

        # `[-2]` is the channel as of the previous bar, excluding the breaking bar.
        channel_high = highest(highs, self.lookback)[-2]
        channel_low = lowest(lows, self.lookback)[-2]
        if channel_high is None or channel_low is None:
            return HOLD

        regime = self._regime(closes)

        if close > channel_high and (regime is None or close > regime):
            return Signal(
                SignalType.BUY,
                reason=f"close {close:.4f} broke {self.lookback}-bar high "
                f"{channel_high:.4f} (ADX {strength:.0f})",
            )
        if close < channel_low and (regime is None or close < regime):
            return Signal(
                SignalType.SELL,
                reason=f"close {close:.4f} broke {self.lookback}-bar low "
                f"{channel_low:.4f} (ADX {strength:.0f})",
            )
        return HOLD

    def _regime(self, closes: list[float]) -> float | None:
        """Slow-EMA regime reference, or None when the filter is disabled/undefined."""
        if self.trend_period <= 0:
            return None
        return ema(closes, self.trend_period)[-1]
