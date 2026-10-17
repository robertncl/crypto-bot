"""LiveBroker: forwards an OrderRequest to the exchange and confirms what executed."""

from __future__ import annotations

import logging

import pytest

from crypto_bot.core.broker import LiveBroker
from crypto_bot.core.models import Order, OrderRequest, OrderSide, OrderStatus, OrderType
from crypto_bot.exchanges.base import ExchangeAdapter, ExchangeError


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


# -- fill confirmation -------------------------------------------------------------
# Bybit/Coinbase acknowledge an order with an id but no status; ccxt surfaces that as
# status None, which the adapter maps to OPEN with filled=0.


def _order(request, status, filled=0.0, price=None, oid="ord-1", fee=0.0):
    return Order(
        symbol=request.symbol, side=request.side, amount=request.amount, type=request.type,
        status=status, filled=filled, average_price=price, id=oid, fee=fee,
    )


class _AckAdapter(_FakeAdapter):
    """Acknowledges orders without a status, then reports a scripted sequence of states."""

    def __init__(self, states, *, cancel_error=False):
        super().__init__()
        self.states = list(states)  # what successive fetch_order calls return
        self.fetches = 0
        self.cancelled = []
        self.cancel_error = cancel_error

    def create_order(self, request):
        self.last_request = request
        return _order(request, OrderStatus.OPEN)

    def fetch_order(self, order_id, request):
        self.fetches += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(state, Exception):
            raise state
        return state(request)

    def cancel_order(self, order_id, symbol):
        if self.cancel_error:
            raise ExchangeError("cancel rejected")
        self.cancelled.append(order_id)

    def fetch_last_price(self, symbol):
        return 99.0


REQ = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=2.0)


def _broker(adapter, timeout=60.0):
    return LiveBroker(adapter, confirm_timeout=timeout, sleep=lambda _s: None)


def test_an_acknowledged_order_is_polled_until_it_is_filled():
    adapter = _AckAdapter([
        lambda r: _order(r, OrderStatus.OPEN),
        lambda r: _order(r, OrderStatus.FILLED, filled=2.0, price=101.0, fee=0.2),
    ])
    order = _broker(adapter).execute(REQ)
    assert order.is_filled and order.filled == 2.0 and order.average_price == 101.0
    assert order.fee == 0.2
    assert adapter.fetches == 2 and adapter.cancelled == []


def test_a_still_working_order_is_cancelled_at_the_deadline_and_its_fill_booked(caplog):
    adapter = _AckAdapter([
        lambda r: _order(r, OrderStatus.OPEN, filled=0.5, price=100.0),  # deadline hits
        lambda r: _order(r, OrderStatus.CANCELED, filled=0.5, price=100.0),  # after cancel
    ])
    with caplog.at_level(logging.WARNING):
        order = _broker(adapter, timeout=0.0).execute(REQ)
    assert adapter.cancelled == ["ord-1"]
    assert order.is_filled and order.filled == 0.5  # exactly what executed, not 2.0
    assert "partially filled" in caplog.text


def test_a_partially_filled_expired_order_books_the_executed_quantity():
    class _Expired(_FakeAdapter):
        def create_order(self, request):  # e.g. an IOC/market order that ran out of book
            return _order(request, OrderStatus.CANCELED, filled=1.25, price=100.0)

    order = _broker(_Expired()).execute(REQ)
    assert order.is_filled and order.filled == 1.25


def test_an_unfilled_cancelled_order_is_not_booked():
    adapter = _AckAdapter([lambda r: _order(r, OrderStatus.CANCELED)])
    order = _broker(adapter).execute(REQ)
    assert order.status == OrderStatus.CANCELED and not order.is_filled


def test_transient_fetch_errors_are_tolerated():
    adapter = _AckAdapter([
        ExchangeError("timeout"),
        lambda r: _order(r, OrderStatus.FILLED, filled=2.0, price=None),
    ])
    order = _broker(adapter).execute(REQ)
    assert order.is_filled and order.average_price == 99.0  # last-price fallback


def test_an_unconfirmable_order_is_reported_loudly(caplog):
    adapter = _AckAdapter([ExchangeError("venue down")], cancel_error=True)
    with caplog.at_level(logging.WARNING):
        order = _broker(adapter, timeout=0.0).execute(REQ)
    assert order.status == OrderStatus.OPEN and not order.is_filled
    assert "cancel after confirm timeout failed" in caplog.text
    assert "could not confirm order ord-1" in caplog.text


def test_adapters_without_order_lookup_fall_back_to_reporting_unconfirmed():
    class _NoLookup(ExchangeAdapter):  # inherits the base fetch_order, which raises
        name = "nolookup"

        def load_markets(self):
            return {}

        def fetch_candles(self, symbol, timeframe, limit=200, since=None):
            return []

        def fetch_last_price(self, symbol):
            return 1.0

        def fetch_balance(self):
            return {}

        def create_order(self, request):
            return _order(request, OrderStatus.OPEN)

        def cancel_order(self, order_id, symbol):
            pass

    order = _broker(_NoLookup(), timeout=0.0).execute(REQ)
    assert order.status == OrderStatus.OPEN


def test_engine_books_and_protects_a_fill_the_venue_only_acknowledged():
    """End to end: the Bybit-style flow used to leave a real position untracked."""
    from crypto_bot.config import (
        BotConfig,
        ExchangeConfig,
        LoggingConfig,
        PaperConfig,
        RiskConfig,
        StrategyConfig,
    )
    from crypto_bot.core.engine import Engine
    from crypto_bot.core.models import Candle
    from crypto_bot.core.portfolio import Portfolio
    from crypto_bot.risk.manager import RiskManager
    from crypto_bot.strategies.ma_crossover import MACrossover

    closes = [10, 9, 8, 7, 6, 5, 7, 10]  # SMA(2/4) crosses up on the last bar -> BUY

    class _Venue(_AckAdapter):
        def __init__(self):
            super().__init__([lambda r: _order(
                r, OrderStatus.FILLED, filled=r.amount, price=self.closes[-1])])
            self.closes = list(closes)

        def fetch_candles(self, symbol, timeframe, limit=200, since=None):
            return [Candle(1_000_000 + i * 60_000, c, c, c, c, 1.0)
                    for i, c in enumerate(self.closes)]

    venue = _Venue()
    config = BotConfig(
        mode="live", exchange=ExchangeConfig(name="bybit"), symbols=["BTC/USDT"],
        timeframe="1m", poll_seconds=60,
        strategy=StrategyConfig(name="ma_crossover",
                                params={"fast_period": 2, "slow_period": 4, "ma_type": "sma"}),
        risk=RiskConfig(position_pct=0.5, stop_loss_pct=0.05, take_profit_pct=0.0,
                        max_drawdown_pct=0.0),
        paper=PaperConfig(), logging=LoggingConfig(level="ERROR"),
    )
    engine = Engine(config, venue, MACrossover(config.strategy.params),
                    RiskManager(config.risk), Portfolio(cash=1000.0),
                    broker=_broker(venue))
    engine.run_once()
    pos = engine.portfolio.positions["BTC/USDT"]
    assert pos.amount == pytest.approx(50.0) and pos.entry_price == 10.0
    assert engine.portfolio.cash == pytest.approx(500.0)

    venue.closes[-1] = 9.0  # -10%: the stop-loss now sees the position and exits it
    engine.run_once()
    assert not engine.portfolio.has_position("BTC/USDT")
