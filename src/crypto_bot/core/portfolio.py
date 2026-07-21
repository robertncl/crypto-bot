"""Portfolio accounting for paper trading.

Tracks a single cash balance (the quote currency, e.g. USDT) plus one position per
symbol. Every fill updates cash, positions, and realized PnL. This is the ledger the
paper broker writes to and the engine reads equity from.

Positions may be LONG or SHORT. Shorts are gated behind ``allow_shorts`` (off by
default) so spot behaviour is unchanged unless derivatives are explicitly enabled:
with the flag off, selling more than you hold still raises, exactly as before.

Collateral model: opening a position of ``amount`` at ``price`` posts ``amount * price``
as margin and returns it on close, so ``equity = cash + Σ(margin + unrealized_pnl)``.
For longs this is algebraically identical to the old ``cash + Σ amount * price`` spot
accounting — the generalization costs nothing in the long-only case.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crypto_bot.core.models import Order, OrderSide, Position, PositionSide


@dataclass
class Portfolio:
    cash: float
    quote_currency: str = "USDT"
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    # Derivatives: allow a SELL with no long to open a short. Off = spot semantics.
    allow_shorts: bool = False
    # Net perpetual funding paid out (negative once we have collected more than we paid).
    funding_paid: float = 0.0

    def base_of(self, symbol: str) -> str:
        return symbol.split("/")[0]

    def has_position(self, symbol: str) -> bool:
        pos = self.positions.get(symbol)
        return pos is not None and pos.amount > 0

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.amount > 0)

    def equity(self, prices: dict[str, float]) -> float:
        """Total value = cash + posted margin + open profit on every position."""
        total = self.cash
        for symbol, pos in self.positions.items():
            if pos.amount <= 0:
                continue
            price = prices.get(symbol, pos.entry_price)
            total += pos.value(price)
        return total

    def apply_fill(self, order: Order) -> None:
        """Update cash/positions/PnL from a filled order. Raises on insufficient funds.

        A fill either *increases* exposure in the direction it implies (BUY with no short
        open, SELL with shorts enabled) or *reduces* the opposite-side position it meets
        (BUY against a short covers it; SELL against a long sells it). Reversals are never
        implicit — the engine closes one side and opens the other as two separate orders,
        so every fill has a single unambiguous meaning in the ledger.
        """
        if not order.is_filled or order.average_price is None:
            return
        price = order.average_price
        amount = order.filled
        pos = self.positions.get(order.symbol)

        if order.side == OrderSide.BUY:
            if pos is not None and pos.is_short:
                self._reduce(pos, amount, price, order.fee)
            else:
                self._increase(order.symbol, amount, price, order.fee, PositionSide.LONG)
        else:  # SELL
            if pos is not None and not pos.is_short:
                self._reduce(pos, amount, price, order.fee)
            elif pos is not None or self.allow_shorts:
                self._increase(order.symbol, amount, price, order.fee, PositionSide.SHORT)
            else:
                raise ValueError(
                    f"cannot sell {amount} {self.base_of(order.symbol)}; only 0.0 held "
                    f"(enable derivatives.allow_shorts to sell short)"
                )

        self.fees_paid += order.fee

    def _increase(
        self, symbol: str, amount: float, price: float, fee: float, side: PositionSide
    ) -> None:
        """Open a new position or add to one already on this side."""
        margin = price * amount
        total_debit = margin + fee
        if total_debit > self.cash + 1e-9:
            raise ValueError(
                f"insufficient cash: need {total_debit:.2f} {self.quote_currency}, "
                f"have {self.cash:.2f}"
            )
        self.cash -= total_debit

        pos = self.positions.get(symbol)
        if pos is None:
            self.positions[symbol] = Position(
                symbol=symbol,
                amount=amount,
                entry_price=price,
                peak_price=price,
                side=side,
                trough_price=price,
            )
            return
        # Weighted-average entry price when adding to an existing position.
        new_amount = pos.amount + amount
        pos.entry_price = (pos.entry_price * pos.amount + price * amount) / new_amount
        pos.amount = new_amount
        pos.peak_price = max(pos.peak_price, price)
        pos.trough_price = min(pos.trough_price, price) if pos.trough_price > 0 else price

    def _reduce(self, pos: Position, amount: float, price: float, fee: float) -> None:
        """Close (or partially close) a position, releasing its margin and booking PnL."""
        if amount > pos.amount + 1e-9:
            verb = "cover" if pos.is_short else "sell"
            raise ValueError(
                f"cannot {verb} {amount} {self.base_of(pos.symbol)}; only {pos.amount} held"
            )
        closed = min(amount, pos.amount)
        realized = (
            (pos.entry_price - price) * closed
            if pos.is_short
            else (price - pos.entry_price) * closed
        )
        self.cash += pos.entry_price * closed + realized - fee
        self.realized_pnl += realized
        pos.amount -= closed
        if pos.amount <= 1e-12:
            del self.positions[pos.symbol]

    def apply_funding(self, rates: dict[str, float], prices: dict[str, float]) -> float:
        """Settle one perpetual funding interval across all open positions.

        ``rates`` is the per-interval funding rate keyed by symbol. A positive rate means
        longs pay shorts (the usual state in a bull market, since perps trade above spot);
        a negative rate reverses it. Each position exchanges ``notional * rate``. Returns
        the net amount paid — negative when we collected more than we paid.

        This is the cash flow that makes a perp position different from spot, and it is the
        edge the ``funding_bias`` strategy trades.
        """
        if not self.positions or not rates:
            return 0.0
        net = 0.0
        for symbol, pos in self.positions.items():
            if pos.amount <= 0:
                continue
            rate = rates.get(symbol, 0.0)
            if not rate:
                continue
            price = prices.get(symbol, pos.entry_price)
            payment = pos.notional(price) * rate
            # Longs pay when the rate is positive; shorts receive the same amount.
            net += -payment if pos.is_short else payment
        if net:
            self.cash -= net
            self.funding_paid += net
        return net

    def snapshot(self, prices: dict[str, float]) -> dict:
        """A serializable summary for logging / status output."""
        return {
            "cash": round(self.cash, 2),
            "equity": round(self.equity(prices), 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "fees_paid": round(self.fees_paid, 2),
            "funding_paid": round(self.funding_paid, 2),
            "open_positions": {
                sym: {
                    "side": pos.side.value,
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
