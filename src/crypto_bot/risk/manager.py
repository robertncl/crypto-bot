"""Risk management: turns signals into safe, sized actions.

Responsibilities:
* **Position sizing** — allocate a fixed fraction of equity per new position.
* **Exposure caps** — limit how many positions can be open at once.
* **Protective exits** — stop-loss and take-profit on each open position.
* **Drawdown kill-switch** — stop opening new positions once equity falls too far
  below its peak.
"""

from __future__ import annotations

from dataclasses import dataclass

from crypto_bot.config import RiskConfig
from crypto_bot.core.models import Position


@dataclass
class RiskDecision:
    approved: bool
    amount: float = 0.0
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg
        self._peak_equity: float = 0.0

    def update_equity(self, equity: float) -> None:
        """Record equity so the drawdown kill-switch can track the high-water mark."""
        self._peak_equity = max(self._peak_equity, equity)

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    def drawdown(self, equity: float) -> float:
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, (self._peak_equity - equity) / self._peak_equity)

    def is_halted(self, equity: float) -> bool:
        """True once drawdown breaches the configured maximum (0 disables the switch)."""
        if self.cfg.max_drawdown_pct <= 0:
            return False
        return self.drawdown(equity) >= self.cfg.max_drawdown_pct

    def size_buy(
        self,
        *,
        equity: float,
        price: float,
        open_positions: int,
        has_position: bool,
    ) -> RiskDecision:
        """Decide whether and how large a new long position may be."""
        if has_position:
            return RiskDecision(False, reason="already holding this symbol")
        if open_positions >= self.cfg.max_open_positions:
            return RiskDecision(
                False, reason=f"max_open_positions ({self.cfg.max_open_positions}) reached"
            )
        if price <= 0:
            return RiskDecision(False, reason="invalid price")

        notional = equity * self.cfg.position_pct
        amount = notional / price
        if amount <= 0:
            return RiskDecision(False, reason="position size rounds to zero")
        return RiskDecision(
            True,
            amount=amount,
            reason=f"allocate {self.cfg.position_pct:.0%} of equity ({notional:.2f})",
        )

    def protective_exit(self, position: Position, price: float) -> str | None:
        """Return a reason string if this position should be force-closed, else None."""
        pnl_pct = position.unrealized_pnl_pct(price)
        if self.cfg.stop_loss_pct > 0 and pnl_pct <= -self.cfg.stop_loss_pct:
            return f"stop-loss hit ({pnl_pct:.2%} <= -{self.cfg.stop_loss_pct:.2%})"
        if self.cfg.take_profit_pct > 0 and pnl_pct >= self.cfg.take_profit_pct:
            return f"take-profit hit ({pnl_pct:.2%} >= {self.cfg.take_profit_pct:.2%})"
        return None
