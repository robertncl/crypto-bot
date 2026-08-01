"""LiveBroker: forwards an OrderRequest to the exchange adapter, no logic of its own."""

from __future__ import annotations

from crypto_bot.core.broker import LiveBroker
from crypto_bot.core.models import Order, OrderRequest, OrderSide, OrderStatus, OrderType


class _FakeAdapter:
    name = "fake"

    def __init__(self):
        self.last_request = None

    def create_order(self, request: OrderRequest) -> Order:
        self.last_request = request
        return Order(
            symbol=request.symbol,
            side=request.side,
            amount=request.amount,
            type=request.type,
            status=OrderStatus.FILLED,
            filled=request.amount,
            average_price=100.0,
        )


def test_live_broker_forwards_the_request_to_the_exchange():
    adapter = _FakeAdapter()
    broker = LiveBroker(adapter)
    request = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0, type=OrderType.MARKET)

    order = broker.execute(request)

    assert adapter.last_request is request
    assert order.is_filled
    assert order.average_price == 100.0


def test_live_broker_fills_in_filled_amount_when_the_venue_omits_it():
    class _NoFilledAdapter(_FakeAdapter):
        def create_order(self, request):
            return Order(
                symbol=request.symbol, side=request.side, amount=request.amount,
                type=request.type, status=OrderStatus.FILLED, filled=0.0,
                average_price=100.0,
            )

    broker = LiveBroker(_NoFilledAdapter())
    order = broker.execute(OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=2.0))
    assert order.filled == 2.0  # backfilled from the request amount


def test_live_broker_fetches_last_price_when_the_venue_omits_average_price():
    class _NoPriceAdapter(_FakeAdapter):
        def create_order(self, request):
            return Order(
                symbol=request.symbol, side=request.side, amount=request.amount,
                type=request.type, status=OrderStatus.FILLED, filled=request.amount,
                average_price=None,
            )

        def fetch_last_price(self, symbol):
            return 42.0

    broker = LiveBroker(_NoPriceAdapter())
    order = broker.execute(OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0))
    assert order.average_price == 42.0
