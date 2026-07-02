"""Domain models shared across the bot.

Money/quantity values use ``float`` for simplicity. For a production system handling
real capital, migrating to ``decimal.Decimal`` (and respecting each market's price/amount
precision via ccxt) is a recommended hardening step.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class SignalType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class Candle:
    """A single OHLCV bar. ``timestamp`` is epoch milliseconds (ccxt convention)."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_ccxt(cls, row: list) -> Candle:
        ts, o, h, low, c, v = row[:6]
        return cls(int(ts), float(o), float(h), float(low), float(c), float(v))


@dataclass(frozen=True)
class Signal:
    """A strategy's decision for one symbol on one bar."""

    type: SignalType
    reason: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.type in (SignalType.BUY, SignalType.SELL)


HOLD = Signal(SignalType.HOLD)


@dataclass
class OrderRequest:
    """An intent to trade, produced by the engine and handed to a broker."""

    symbol: str
    side: OrderSide
    amount: float  # in base currency
    type: OrderType = OrderType.MARKET
    price: float | None = None  # required for LIMIT orders
    reason: str = ""


@dataclass
class Order:
    """The result of submitting an OrderRequest (paper or live)."""

    symbol: str
    side: OrderSide
    amount: float
    type: OrderType
    status: OrderStatus
    filled: float = 0.0
    average_price: float | None = None
    fee: float = 0.0
    id: str | None = None
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    info: dict = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED


@dataclass
class Position:
    """An open spot position in one symbol (long-only for the starter bot)."""

    symbol: str
    amount: float          # base currency held
    entry_price: float     # average entry price in quote currency
    peak_price: float = 0.0  # highest price seen while open (drives the trailing stop)

    def notional(self, price: float) -> float:
        return self.amount * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.amount

    def unrealized_pnl_pct(self, price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return (price - self.entry_price) / self.entry_price
