"""Order execution.

Two implementations behind one interface:

* :class:`PaperBroker` simulates a market fill against the latest price, applying
  configurable slippage and fees. It is pure — it builds an :class:`Order` but does not
  mutate the portfolio (the engine applies fills, keeping the portfolio the single
  source of truth for funds).
* :class:`LiveBroker` forwards the request to a real exchange via the adapter and
  confirms the outcome before reporting it.

Both return a filled (or rejected) :class:`Order` the engine can book identically.
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable

from crypto_bot.core.models import Order, OrderRequest, OrderSide, OrderStatus
from crypto_bot.exchanges.base import ExchangeAdapter, ExchangeError
from crypto_bot.logging_setup import LOGGER_NAME

log = logging.getLogger(f"{LOGGER_NAME}.broker")


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
    """Places real orders and reports what *actually* executed.

    Many venues (Bybit and Coinbase among them) acknowledge an order before they report
    its outcome: ccxt's ``create_order`` then returns an id with no status and no fill.
    Taking that at face value would treat an executed market order as unfilled — the
    position would never be booked, so stops would never protect it, an opposing signal
    could not close it, and the next entry signal would buy again at full size. So an
    unconfirmed order is polled with ``fetch_order`` until it reaches a final state. If it
    is still working at the deadline the remainder is cancelled, so nothing can fill
    behind the bot's back, and whatever did execute is booked.
    """

    is_paper = False

    def __init__(
        self,
        exchange: ExchangeAdapter,
        *,
        confirm_timeout: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.exchange = exchange
        self.confirm_timeout = confirm_timeout
        self._sleep = sleep

    def execute(self, request: OrderRequest) -> Order:
        order = self.exchange.create_order(request)
        if order.status == OrderStatus.OPEN and order.id is not None:
            order = self._confirm(order, request)
        if order.status in (OrderStatus.CANCELED, OrderStatus.OPEN) and order.filled > 0:
            # Partially executed, then cancelled/expired (or cancellation unconfirmed):
            # the executed quantity is real exposure, so book exactly that.
            log.warning(
                "order %s %s partially filled: %.8f of %.8f (status %s); booking the fill",
                order.id, request.symbol, order.filled, order.amount, order.status.value,
            )
            order.status = OrderStatus.FILLED
        # Market orders should fill immediately; if the venue didn't echo an average
        # price, fall back to the last trade so the portfolio can be booked.
        if order.is_filled and order.average_price is None:
            order.average_price = self.exchange.fetch_last_price(request.symbol)
        if order.filled == 0 and order.is_filled:
            order.filled = order.amount
        if order.status == OrderStatus.OPEN:
            log.error(
                "could not confirm order %s (%s %s %.8f); it may still execute — check the "
                "exchange and reconcile manually",
                order.id, request.side.value, request.symbol, request.amount,
            )
        return order

    def _confirm(self, order: Order, request: OrderRequest) -> Order:
        """Poll an acknowledged order until final; cancel the remainder at the deadline."""
        order_id = order.id
        deadline = time.monotonic() + self.confirm_timeout
        delay = 0.2
        while True:
            self._sleep(delay)
            order = self._refresh(order_id, request, order)
            if order.status != OrderStatus.OPEN or time.monotonic() >= deadline:
                break
            delay = min(delay * 2, 2.0)
        if order.status == OrderStatus.OPEN:
            try:
                self.exchange.cancel_order(order_id, request.symbol)
            except ExchangeError as exc:
                log.warning("cancel after confirm timeout failed for %s: %s", order_id, exc)
            order = self._refresh(order_id, request, order)
        return order

    def _refresh(self, order_id: str, request: OrderRequest, last: Order) -> Order:
        try:
            return self.exchange.fetch_order(order_id, request)
        except ExchangeError as exc:
            log.warning("fetch_order failed for %s: %s", order_id, exc)
            return last
