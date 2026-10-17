"""Grid trading strategy (spot "infinity grid").

The classic exchange *grid bot*: instead of predicting direction, it lays a ladder of
price lines ``spacing_pct`` apart and harvests oscillation. Every fall through a line
buys one tranche; every rise through a line sells the share of the stack bought on the
way down. Each completed round trip earns roughly one grid step, minus fees.

* **BUY** (one tranche) when the close falls through the next line *below* the last line
  traded.
* **SELL** (``reduce_only``, a ``fraction`` of the position) when the close rises through
  the next line *above* it — one tranche's worth per line crossed.

Buys and sells sit a full step apart, so a price wobbling around a single line does not
churn fees: the bot buys at line *n* and only sells at line *n + 1*. While the bot holds
nothing, the grid trails price upward, so the next dip is measured from the recent level
rather than from wherever the bot started ("infinity" grid: no upper or lower bound).

**Configure the risk manager for it.** Grid buys add to a held symbol, so the profile
needs ``risk.allow_averaging_in: true``; each tranche is ``risk.position_pct`` of equity
and ``risk.max_position_pct`` caps the stack. Set ``take_profit_pct`` to 0 — the grid
takes profit itself, line by line. Spot / long-only by design: its SELLs are
``reduce_only``, so they never open shorts even with ``derivatives.allow_shorts``.

Risk profile: *range harvester.* Steady small gains while price oscillates; in a
sustained downtrend it keeps buying until the stack hits its cap and then holds a losing
position, so pair it with a wide stop-loss and the drawdown kill-switch. In a strong
uptrend it sells early and sits out the rest of the move.

The strategy tracks its own tranche count per symbol from the signals it emits, so if the
risk manager declines a buy (cap reached) or a protective stop flattens the position, its
sell fractions run conservative — it can under-sell, never over-sell.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from crypto_bot.core.models import HOLD, Candle, Signal, SignalType
from crypto_bot.strategies.base import Strategy

# Rounding guard: log() of a price sitting exactly on a line can land a hair under the
# integer, which would misplace it by a whole line.
_LINE_EPS = 1e-9


@dataclass
class _Ladder:
    anchor: float  # price of line 0
    line: int  # index of the line last traded (or trailed to)
    held: int = 0  # tranches bought and not yet sold, per this strategy's own signals


class GridTrader(Strategy):
    name = "grid"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self.spacing_pct = float(self.params.get("spacing_pct", 0.02))
        if not 0.0 < self.spacing_pct < 1.0:
            raise ValueError("spacing_pct must be in (0, 1), e.g. 0.02 for a 2% grid")
        self._log_step = math.log1p(self.spacing_pct)
        # One instance serves every symbol, so each symbol keeps its own ladder.
        self._ladders: dict[str | None, _Ladder] = {}

    @property
    def warmup(self) -> int:
        return 1  # the first close anchors the grid

    def generate(self, candles: list[Candle], symbol: str | None = None) -> Signal:
        if len(candles) < self.warmup:
            return HOLD
        price = candles[-1].close
        if price <= 0:
            return HOLD
        ladder = self._ladders.get(symbol)
        if ladder is None:
            self._ladders[symbol] = _Ladder(anchor=price, line=0)
            return HOLD

        # Position of the price on the ladder, in lines from the anchor.
        position = math.log(price / ladder.anchor) / self._log_step

        lowest_touched = math.ceil(position - _LINE_EPS)
        if lowest_touched <= ladder.line - 1:
            steps = ladder.line - lowest_touched
            ladder.line = lowest_touched
            ladder.held += 1  # one tranche per signal, however many lines were crossed
            return Signal(
                SignalType.BUY,
                reason=f"grid buy: fell {steps} line(s) to {self._price(ladder):.4f} "
                f"({self.spacing_pct:.1%} grid, {ladder.held} tranche(s) held)",
            )

        highest_touched = math.floor(position + _LINE_EPS)
        if highest_touched >= ladder.line + 1:
            steps = highest_touched - ladder.line
            ladder.line = highest_touched  # trails up even when flat
            if ladder.held == 0:
                return HOLD
            sold = min(steps, ladder.held)
            fraction = sold / ladder.held
            ladder.held -= sold
            return Signal(
                SignalType.SELL,
                reason=f"grid sell: rose {steps} line(s) to {self._price(ladder):.4f} "
                f"(selling {sold} tranche(s), {ladder.held} left)",
                fraction=fraction,
                reduce_only=True,
            )
        return HOLD

    def _price(self, ladder: _Ladder) -> float:
        return ladder.anchor * (1.0 + self.spacing_pct) ** ladder.line
