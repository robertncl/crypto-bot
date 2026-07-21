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


class PositionSide(str, Enum):
    """Direction of an open position.

    Spot trading only ever produces LONG. On derivatives (perpetual swaps/futures) a
    SELL signal with ``derivatives.allow_shorts`` enabled opens a SHORT instead of being
    a no-op, which is what makes a downtrend tradable rather than merely sit-out-able.
    """

    LONG = "long"
    SHORT = "short"


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


@dataclass(frozen=True)
class MarketContext:
    """Non-OHLCV market data for one symbol on one bar.

    Candle-only strategies ignore this entirely. Derivatives strategies need more than
    price — the funding rate above all — so the engine passes a context to strategies that
    opt in via ``Strategy.wants_context``. ``funding_rate`` is ``None`` when the venue does
    not publish one (spot markets) or the lookup failed.
    """

    symbol: str
    funding_rate: float | None = None  # per interval; positive = longs pay shorts
    funding_interval_hours: float = 8.0

    @property
    def funding_apr(self) -> float | None:
        """Funding annualized — the natural scale for thresholds and for humans."""
        if self.funding_rate is None:
            return None
        intervals_per_year = 24.0 / self.funding_interval_hours * 365.0
        return self.funding_rate * intervals_per_year


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
    """An open position in one symbol.

    ``amount`` is always **positive** — direction lives in ``side``, so size and sign never
    get tangled. A LONG profits as price rises, a SHORT as it falls.

    Collateral model: opening a position posts ``amount * entry_price`` of cash as margin
    and returns it on close, so equity is ``cash + margin + unrealized_pnl``. For a long at
    1x this reduces exactly to the spot identity ``cash + amount * price``, which is why
    enabling derivatives does not disturb existing spot behaviour.
    """

    symbol: str
    amount: float          # base currency held (always positive)
    entry_price: float     # average entry price in quote currency
    peak_price: float = 0.0  # highest price seen while open (long trailing stop)
    side: PositionSide = PositionSide.LONG
    trough_price: float = 0.0  # lowest price seen while open (short trailing stop)

    @property
    def is_short(self) -> bool:
        return self.side == PositionSide.SHORT

    def notional(self, price: float) -> float:
        """Absolute market exposure, regardless of direction."""
        return self.amount * price

    def margin(self) -> float:
        """Cash collateral posted to hold this position (returned on close)."""
        return self.amount * self.entry_price

    def value(self, price: float) -> float:
        """This position's contribution to equity: posted margin plus open profit."""
        return self.margin() + self.unrealized_pnl(price)

    def unrealized_pnl(self, price: float) -> float:
        if self.is_short:
            return (self.entry_price - price) * self.amount
        return (price - self.entry_price) * self.amount

    def unrealized_pnl_pct(self, price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        if self.is_short:
            return (self.entry_price - price) / self.entry_price
        return (price - self.entry_price) / self.entry_price
