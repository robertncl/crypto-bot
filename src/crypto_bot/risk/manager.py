"""Risk management: turns signals into safe, sized actions.

Responsibilities:
* **Position sizing** — a fixed fraction of equity per new position, or (with
  ``risk_per_trade_pct``) whatever size makes a stop-out cost a fixed fraction of equity.
* **Exposure caps** — limit how many positions can be open at once.
* **Trend filter** — optionally allow entries only in the direction of the long-run trend.
* **Protective exits** — stop-loss, trailing stop and take-profit on each open position,
  as fixed percentages and/or scaled by volatility (ATR).
* **Drawdown kill-switch** — stop opening new positions once equity falls too far
  below its peak, optionally re-arming after a cooldown.
"""

from __future__ import annotations

from dataclasses import dataclass

from crypto_bot.config import RiskConfig
from crypto_bot.core.models import Position, PositionSide

# Share of cash left unspent on an entry, covering the fee and slippage on the fill.
_CASH_BUFFER = 0.01
# An entry that cash would shrink below this share of its intended size is declined
# instead: a sliver of a position is not the trade the strategy asked for.
_MIN_FILL_SHARE = 0.5


@dataclass
class RiskDecision:
    approved: bool
    amount: float = 0.0
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg
        self._peak_equity: float = 0.0
        self._halted_since: int | None = None  # ms timestamp the kill-switch tripped

    def update_equity(self, equity: float, now_ms: int | None = None) -> None:
        """Record equity so the drawdown kill-switch can track the high-water mark.

        With ``drawdown_cooldown_days`` set and a clock supplied, a halt that has lasted
        the whole cooldown is lifted by restarting the high-water mark at today's equity:
        the loss stays booked, but the bot is allowed to trade again.
        """
        self._peak_equity = max(self._peak_equity, equity)
        if now_ms is None or self.cfg.drawdown_cooldown_days <= 0:
            return
        if not self.is_halted(equity):
            self._halted_since = None
        elif self._halted_since is None:
            self._halted_since = now_ms
        elif now_ms - self._halted_since >= self.cfg.drawdown_cooldown_days * 86_400_000:
            self._peak_equity = equity
            self._halted_since = None

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

    def size_entry(
        self,
        *,
        equity: float,
        price: float,
        open_positions: int,
        has_position: bool,
        position_notional: float = 0.0,
        side: PositionSide = PositionSide.LONG,
        atr: float | None = None,
        cash: float | None = None,
    ) -> RiskDecision:
        """Decide whether and how large an entry may be, long or short.

        Sizing is direction-agnostic — a short risks the same fraction of equity as a long,
        so ``side`` only colours the reason string. What differs between the two is the
        *exit* (see :meth:`protective_exit`), not the stake.

        By default only *new* positions are allowed: holding the symbol blocks the entry,
        which keeps signal strategies from re-entering on every poll. When
        ``allow_averaging_in`` is set (for accumulate/DCA strategies) a held symbol may be
        topped up, optionally capped at ``max_position_pct`` of equity via
        ``position_notional`` (the symbol's current mark-to-market exposure).

        With ``risk_per_trade_pct`` set, the stake is sized from volatility instead: the
        notional at which a stop-out ``atr_stop_mult`` ATRs away loses that fraction of
        equity, capped at ``position_pct``. ``atr`` is the symbol's current ATR; without
        it the entry is declined rather than sized blind.

        ``cash`` is the quote balance available to pay for the entry. The stake never
        exceeds it (less a small buffer for fees): positions are sized on *equity*, which
        includes money already tied up in other positions, so the intended stake can be
        more than is actually free. A stake cash would cut below half its intended size
        is declined.
        """
        if price <= 0:
            return RiskDecision(False, reason="invalid price")

        if has_position:
            if not self.cfg.allow_averaging_in:
                return RiskDecision(False, reason="already holding this symbol")
        elif open_positions >= self.cfg.max_open_positions:
            # The cap governs how many *distinct* symbols can be open; averaging into a
            # symbol already counted against it is fine.
            return RiskDecision(
                False, reason=f"max_open_positions ({self.cfg.max_open_positions}) reached"
            )

        notional = equity * self.cfg.position_pct
        if self.cfg.risk_per_trade_pct > 0:
            if not atr or atr <= 0:
                return RiskDecision(False, reason="volatility (ATR) not yet known")
            stop_distance_pct = self.cfg.atr_stop_mult * atr / price
            notional = min(notional, equity * self.cfg.risk_per_trade_pct / stop_distance_pct)
        if has_position and self.cfg.max_position_pct > 0:
            room = equity * self.cfg.max_position_pct - position_notional
            if room <= 0:
                return RiskDecision(
                    False,
                    reason=f"position cap ({self.cfg.max_position_pct:.0%} of equity) reached",
                )
            notional = min(notional, room)
        if cash is not None:
            affordable = max(cash, 0.0) * (1 - _CASH_BUFFER)
            if affordable < notional * _MIN_FILL_SHARE:
                return RiskDecision(
                    False, reason=f"insufficient cash ({cash:.2f} free, {notional:.2f} wanted)"
                )
            notional = min(notional, affordable)

        amount = notional / price
        if amount <= 0:
            return RiskDecision(False, reason="position size rounds to zero")
        verb = "add" if has_position else "allocate"
        direction = " short" if side == PositionSide.SHORT else ""
        return RiskDecision(
            True,
            amount=amount,
            reason=f"{verb}{direction} {notional / equity:.0%} of equity ({notional:.2f})",
        )

    def trend_allows(self, side: PositionSide, price: float, average: float | None) -> bool:
        """Whether the trend filter permits an entry on ``side``.

        ``average`` is the symbol's moving average over ``trend_filter_period`` candles
        (``None`` while there is not enough history, which blocks entries: an unknown
        trend is not a confirmed one). Longs need price above it, shorts below it.
        """
        if self.cfg.trend_filter_period <= 0:
            return True
        if average is None:
            return False
        return price < average if side == PositionSide.SHORT else price > average

    def protective_exit(self, position: Position, price: float) -> str | None:
        """Return a reason string if this position should be force-closed, else None.

        Stop-loss and take-profit read :meth:`Position.unrealized_pnl_pct`, which is already
        direction-aware, so they need no branching. The trailing stop does: it ratchets from
        the *most favourable* price seen, which is the high-water mark for a long and the
        low-water mark for a short.
        """
        pnl_pct = position.unrealized_pnl_pct(price)
        if self.cfg.stop_loss_pct > 0 and pnl_pct <= -self.cfg.stop_loss_pct:
            return f"stop-loss hit ({pnl_pct:.2%} <= -{self.cfg.stop_loss_pct:.2%})"
        atr_exit = self._atr_exit(position, price)
        if atr_exit:
            return atr_exit
        if self.cfg.trailing_stop_pct > 0:
            trail = self._trailing_exit(position, price)
            if trail:
                return trail
        if self.cfg.take_profit_pct > 0 and pnl_pct >= self.cfg.take_profit_pct:
            return f"take-profit hit ({pnl_pct:.2%} >= {self.cfg.take_profit_pct:.2%})"
        return None

    def stop_level(self, position: Position) -> tuple[float, str] | None:
        """The price at which the nearest protective stop fires, with its kind.

        The same rules as :meth:`protective_exit`, expressed as a price level: the highest
        of the active stops for a long, the lowest for a short. Used by the backtester to
        fill a stop where it would have triggered inside a bar. ``None`` if no stop is set.
        """
        sign = -1.0 if position.is_short else 1.0  # which way price hurts
        entry = position.entry_price
        if position.is_short:
            best = position.trough_price if position.trough_price > 0 else entry
            best = min(best, entry)
        else:
            best = max(position.peak_price, entry)
        cfg = self.cfg
        levels: list[tuple[float, str]] = []
        if cfg.stop_loss_pct > 0:
            levels.append((entry * (1 - sign * cfg.stop_loss_pct), "stop-loss"))
        if cfg.atr_stop_mult > 0 and position.atr > 0:
            levels.append((entry - sign * cfg.atr_stop_mult * position.atr, "stop-loss"))
        if cfg.trailing_stop_pct > 0:
            levels.append((best * (1 - sign * cfg.trailing_stop_pct), "trailing-stop"))
        if cfg.atr_trailing_mult > 0 and position.atr > 0:
            levels.append((best - sign * cfg.atr_trailing_mult * position.atr, "trailing-stop"))
        if not levels:
            return None
        # The stop that triggers first is the one closest to the price.
        return min(levels, key=lambda lv: lv[0]) if position.is_short else max(
            levels, key=lambda lv: lv[0]
        )

    def take_profit_level(self, position: Position) -> float | None:
        """The price at which take-profit fires, or ``None`` if it is off."""
        if self.cfg.take_profit_pct <= 0:
            return None
        sign = -1.0 if position.is_short else 1.0
        return position.entry_price * (1 + sign * self.cfg.take_profit_pct)

    def _atr_exit(self, position: Position, price: float) -> str | None:
        """Volatility-scaled stop-loss and trailing stop (ATR measured at entry)."""
        atr = position.atr
        if atr <= 0:
            return None
        cfg = self.cfg
        entry = position.entry_price
        if position.is_short:
            if cfg.atr_stop_mult > 0 and price >= entry + cfg.atr_stop_mult * atr:
                return f"stop-loss hit ({cfg.atr_stop_mult:g} ATR above entry {entry:.4f})"
            trough = position.trough_price if position.trough_price > 0 else entry
            trough = min(trough, entry)
            if cfg.atr_trailing_mult > 0 and price >= trough + cfg.atr_trailing_mult * atr:
                return (
                    f"trailing-stop hit ({cfg.atr_trailing_mult:g} ATR above trough "
                    f"{trough:.4f})"
                )
            return None
        if cfg.atr_stop_mult > 0 and price <= entry - cfg.atr_stop_mult * atr:
            return f"stop-loss hit ({cfg.atr_stop_mult:g} ATR below entry {entry:.4f})"
        peak = max(position.peak_price, entry)
        if cfg.atr_trailing_mult > 0 and price <= peak - cfg.atr_trailing_mult * atr:
            return f"trailing-stop hit ({cfg.atr_trailing_mult:g} ATR below peak {peak:.4f})"
        return None

    def _trailing_exit(self, position: Position, price: float) -> str | None:
        # The extreme ratchets with price (the engine maintains it); fall back to entry so
        # a position created before this feature still gets a floor.
        if position.is_short:
            trough = position.trough_price if position.trough_price > 0 else position.entry_price
            trough = min(trough, position.entry_price)
            if trough > 0 and price >= trough * (1 + self.cfg.trailing_stop_pct):
                rise = (price - trough) / trough
                return (
                    f"trailing-stop hit ({rise:.2%} above trough {trough:.4f} "
                    f">= {self.cfg.trailing_stop_pct:.2%})"
                )
            return None
        peak = max(position.peak_price, position.entry_price)
        if peak > 0 and price <= peak * (1 - self.cfg.trailing_stop_pct):
            drop = (peak - price) / peak
            return (
                f"trailing-stop hit ({drop:.2%} below peak {peak:.4f} "
                f">= {self.cfg.trailing_stop_pct:.2%})"
            )
        return None
