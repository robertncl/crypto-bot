"""Regime-switching ensemble strategy.

The core insight of modern hybrid trading systems: **no single strategy works in all
market conditions** (§3 of the strategy guide). Trend-followers bleed out in chop;
mean-reverters get steamrolled by trends. Instead of picking one and hoping, this
meta-strategy *detects the current regime* and routes each decision to the specialist:

* **ADX** (Average Directional Index) measures trend *strength* on a 0–100 scale,
  regardless of direction. Above the threshold (classically 25) the market is trending;
  below it, ranging.
* In a **trending regime** the signal comes from the configured trend-following
  sub-strategy (default: Supertrend).
* In a **ranging regime** it comes from the mean-reversion sub-strategy
  (default: RSI reversion).

Both legs are ordinary registered strategies, configured by name exactly as they would
be standalone, so any pairing works:

.. code-block:: yaml

    strategy:
      name: regime
      params:
        adx_period: 14
        adx_threshold: 25
        trend: { name: supertrend, params: { period: 10, multiplier: 3.0 } }
        range: { name: rsi_reversion, params: { period: 14 } }

Caveat to understand before using: regimes flip *between* a sub-strategy's entry and
exit. A position opened by the trend leg may see the market go quiet before the trend
leg would have sold — the range leg takes over and may hold it. The protective stops
(stop-loss / trailing stop / take-profit) are the exits of last resort, so run this
with them enabled.
"""

from __future__ import annotations

from crypto_bot.core.models import HOLD, Candle, Signal
from crypto_bot.indicators.ta import adx
from crypto_bot.strategies.base import Strategy

_DEFAULT_TREND = {"name": "supertrend", "params": {}}
_DEFAULT_RANGE = {"name": "rsi_reversion", "params": {}}


class RegimeSwitch(Strategy):
    name = "regime"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.adx_period = int(self.params.get("adx_period", 14))
        self.adx_threshold = float(self.params.get("adx_threshold", 25.0))
        if self.adx_period <= 0:
            raise ValueError("adx_period must be positive")
        if not 0 < self.adx_threshold < 100:
            raise ValueError("adx_threshold must be in (0, 100)")
        self.trend_strategy = self._build_leg("trend", _DEFAULT_TREND)
        self.range_strategy = self._build_leg("range", _DEFAULT_RANGE)

    def _build_leg(self, key: str, default: dict) -> Strategy:
        # Imported here, not at module top: the registry imports this module, so a
        # top-level import would be circular. By construction time it is fully loaded.
        from crypto_bot.strategies.registry import build_strategy

        spec = self.params.get(key, default)
        if not isinstance(spec, dict) or "name" not in spec:
            raise ValueError(f"{key} must be a mapping with a 'name' (and optional 'params')")
        if str(spec["name"]).lower() == self.name:
            raise ValueError("regime cannot nest itself as a sub-strategy")
        return build_strategy(str(spec["name"]), dict(spec.get("params") or {}))

    @property
    def warmup(self) -> int:
        # ADX is first defined at index 2*adx_period - 1, and both legs must be ready
        # regardless of which regime is active on any given bar.
        return max(
            2 * self.adx_period,
            self.trend_strategy.warmup,
            self.range_strategy.warmup,
        )

    def generate(self, candles: list[Candle], symbol: str | None = None) -> Signal:
        if len(candles) < self.warmup:
            return HOLD

        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]
        strength = adx(highs, lows, closes, self.adx_period)[-1]
        if strength is None:
            return HOLD

        trending = strength >= self.adx_threshold
        leg = self.trend_strategy if trending else self.range_strategy
        signal = leg.generate(candles, symbol)
        if not signal.is_actionable:
            return signal
        regime = "trend" if trending else "range"
        return Signal(
            signal.type,
            reason=f"{signal.reason} [ADX({self.adx_period}) {strength:.0f} → {regime} regime]",
        )
