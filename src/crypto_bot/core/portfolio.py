"""Portfolio accounting for paper trading.

Tracks a single cash balance (the quote currency, e.g. USDT) plus one long-only spot
position per symbol. Every fill updates cash, positions, and realized PnL. This is the
ledger the paper broker writes to and the engine reads equity from.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crypto_bot.core.models import Order, OrderSide, Position


@dataclass
class Portfolio:
    cash: float
    quote_currency: str = "USDT"
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0

    def base_of(self, symbol: str) -> str:
        return symbol.split("/")[0]

    def has_position(self, symbol: str) -> bool:
        pos = self.positions.get(symbol)
        return pos is not None and pos.amount > 0

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.amount > 0)

    def equity(self, prices: dict[str, float]) -> float:
        """Total value = cash + mark-to-market of every open position."""
        total = self.cash
        for symbol, pos in self.positions.items():
            if pos.amount <= 0:
                continue
            price = prices.get(symbol, pos.entry_price)
            total += pos.notional(price)
        return total

    def apply_fill(self, order: Order) -> None:
        """Update cash/positions/PnL from a filled order. Raises on insufficient funds."""
        if not order.is_filled or order.average_price is None:
            return
        price = order.average_price
        amount = order.filled
        cost = price * amount

        if order.side == OrderSide.BUY:
            total_debit = cost + order.fee
            if total_debit > self.cash + 1e-9:
                raise ValueError(
                    f"insufficient cash: need {total_debit:.2f} {self.quote_currency}, "
                    f"have {self.cash:.2f}"
                )
            self.cash -= total_debit
            self._add_to_position(order.symbol, amount, price)
        else:  # SELL
            pos = self.positions.get(order.symbol)
            if pos is None or amount > pos.amount + 1e-9:
                held = pos.amount if pos else 0.0
                raise ValueError(
                    f"cannot sell {amount} {self.base_of(order.symbol)}; only {held} held"
                )
            self.cash += cost - order.fee
            self.realized_pnl += (price - pos.entry_price) * amount
            pos.amount -= amount
            if pos.amount <= 1e-12:
                del self.positions[order.symbol]

        self.fees_paid += order.fee

    def _add_to_position(self, symbol: str, amount: float, price: float) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            self.positions[symbol] = Position(
                symbol=symbol, amount=amount, entry_price=price, peak_price=price
            )
            return
        # Weighted-average entry price when adding to an existing position.
        new_amount = pos.amount + amount
        pos.entry_price = (pos.entry_price * pos.amount + price * amount) / new_amount
        pos.amount = new_amount
        pos.peak_price = max(pos.peak_price, price)

    def snapshot(self, prices: dict[str, float]) -> dict:
        """A serializable summary for logging / status output."""
        return {
            "cash": round(self.cash, 2),
            "equity": round(self.equity(prices), 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "fees_paid": round(self.fees_paid, 2),
            "open_positions": {
                sym: {
                    "amount": pos.amount,
                    "entry_price": round(pos.entry_price, 4),
                    "unrealized_pnl": round(
                        pos.unrealized_pnl(prices.get(sym, pos.entry_price)), 2
                    ),
                }
                for sym, pos in self.positions.items()
                if pos.amount > 0
            },
        }
