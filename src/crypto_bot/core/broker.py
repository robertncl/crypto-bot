"""Order execution.

Two implementations behind one interface:

* :class:`PaperBroker` simulates a market fill against the latest price, applying
  configurable slippage and fees. It is pure — it builds an :class:`Order` but does not
  mutate the portfolio (the engine applies fills, keeping the portfolio the single
  source of truth for funds).
* :class:`LiveBroker` forwards the request to a real exchange via the adapter.

Both return a filled (or rejected) :class:`Order` the engine can book identically.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable

from crypto_bot.core.models import Order, OrderRequest, OrderSide, OrderStatus
from crypto_bot.exchanges.base import ExchangeAdapter


class Broker(ABC):
    is_paper: bool

    @abstractmethod
    def execute(self, request: OrderRequest) -> Order:
        """Submit an order request and return the resulting Order."""


class PaperBroker(Broker):
    is_paper = True

    def __init__(
        self,
        price_provider: Callable[[str], float],
        *,
        fee_rate: float = 0.001,
        slippage_pct: float = 0.0005,
    ) -> None:
        self._price_provider = price_provider
        self.fee_rate = fee_rate
        self.slippage_pct = slippage_pct

    def execute(self, request: OrderRequest) -> Order:
        reference = request.price or self._price_provider(request.symbol)
        # Market orders cross the spread: buys fill a bit higher, sells a bit lower.
        if request.side == OrderSide.BUY:
            fill_price = reference * (1 + self.slippage_pct)
        else:
            fill_price = reference * (1 - self.slippage_pct)

        notional = fill_price * request.amount
        fee = notional * self.fee_rate
        return Order(
            symbol=request.symbol,
            side=request.side,
            amount=request.amount,
            type=request.type,
            status=OrderStatus.FILLED,
            filled=request.amount,
            average_price=fill_price,
            fee=fee,
            id=f"paper-{uuid.uuid4().hex[:12]}",
            timestamp=int(time.time() * 1000),
            info={"paper": True, "reason": request.reason},
        )


class LiveBroker(Broker):
    is_paper = False

    def __init__(self, exchange: ExchangeAdapter) -> None:
        self.exchange = exchange

    def execute(self, request: OrderRequest) -> Order:
        order = self.exchange.create_order(request)
        # Market orders should fill immediately; if the venue didn't echo an average
        # price, fall back to the last trade so the portfolio can be booked.
        if order.is_filled and order.average_price is None:
            order.average_price = self.exchange.fetch_last_price(request.symbol)
        if order.filled == 0 and order.is_filled:
            order.filled = order.amount
        return order
