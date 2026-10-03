"""crypto_bot.exchanges.ccxt_adapter, exercised against a fake *async* ccxt client.

CCXTAdapter.__init__ does `getattr(ccxt.async_support, exchange_id)(config)`, so the fake
is wired in by monkeypatching an attribute named after a throwaway exchange id onto the
real `ccxt.async_support` module — the adapter then constructs *our* class instead of a
real venue client, and no network call happens anywhere in this file.
"""

from __future__ import annotations

import asyncio
import threading
import time

import ccxt
import ccxt.async_support as ccxt_async
import pytest

from crypto_bot.core.models import OrderRequest, OrderSide, OrderStatus, OrderType
from crypto_bot.exchanges.base import ExchangeError
from crypto_bot.exchanges.ccxt_adapter import CCXTAdapter

EXCHANGE_ID = "fakeccxtvenue"


class FakeClient:
    """Stands in for a ccxt async exchange instance; records what it was called with."""

    has = {"fetchFundingRate": True}
    ohlcv_delay = 0.0

    def __init__(self, config: dict):
        self.config = config
        self.apiKey = config.get("apiKey")
        self.secret = config.get("secret")
        self.options = config.get("options", {})
        self.sandbox_mode = None
        self.closed = False
        self.markets = {}
        self.calls: list[tuple] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def set_sandbox_mode(self, flag: bool) -> None:
        self.sandbox_mode = flag

    async def load_markets(self):
        return {"BTC/USDT": {}}

    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=200, since=None):
        self.calls.append(("fetch_ohlcv", symbol, timeframe, limit, since))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.ohlcv_delay)
        finally:
            self.in_flight -= 1
        return [[1_700_000_000_000, 10.0, 11.0, 9.0, 10.5, 100.0]]

    async def fetch_ticker(self, symbol):
        return {"last": 123.45}

    async def fetch_funding_rate(self, symbol):
        self.calls.append(("fetch_funding_rate", symbol))
        return {"fundingRate": 0.0001}

    async def fetch_balance(self):
        return {"free": {"USDT": 500.0, "BTC": 0.0}}

    async def create_order(self, symbol, type_, side, amount, price):
        return {
            "symbol": symbol,
            "status": "closed",
            "amount": amount,
            "filled": amount,
            "average": price or 100.0,
            "id": "abc123",
            "timestamp": 1_700_000_000_000,
            "fee": {"cost": 0.5},
        }

    async def cancel_order(self, order_id, symbol):
        pass

    async def close(self):
        self.closed = True


@pytest.fixture
def make_adapter(monkeypatch):
    monkeypatch.setattr(ccxt_async, EXCHANGE_ID, FakeClient, raising=False)
    created: list[CCXTAdapter] = []

    def _make(**kwargs) -> CCXTAdapter:
        adapter = CCXTAdapter(EXCHANGE_ID, **kwargs)
        created.append(adapter)
        return adapter

    yield _make
    for adapter in created:
        adapter.close()


def test_unknown_exchange_id_is_rejected():
    with pytest.raises(ExchangeError, match="unknown ccxt exchange id"):
        CCXTAdapter("not_a_real_ccxt_exchange_id_xyz")


def test_rejects_a_nonpositive_concurrency_limit(make_adapter):
    with pytest.raises(ValueError, match="max_concurrency"):
        make_adapter(max_concurrency=0)


def test_constructs_client_with_credentials_and_options(make_adapter):
    adapter = make_adapter(api_key="k", secret="s", password="p", options={"defaultType": "spot"})
    assert adapter.client.config["apiKey"] == "k"
    assert adapter.client.config["secret"] == "s"
    assert adapter.client.config["password"] == "p"
    assert adapter.client.config["options"] == {"defaultType": "spot"}
    assert adapter.client.config["enableRateLimit"] is True
    assert adapter.has_credentials is True


def test_no_credentials_means_no_credentials(make_adapter):
    assert make_adapter().has_credentials is False


def test_sandbox_mode_is_enabled_when_supported(make_adapter):
    assert make_adapter(sandbox=True).client.sandbox_mode is True


def test_sandbox_mode_raises_a_clear_error_when_unsupported(make_adapter, monkeypatch):
    def _boom(self, flag):
        raise ccxt.NotSupported("nope")

    monkeypatch.setattr(FakeClient, "set_sandbox_mode", _boom)
    threads_before = threading.active_count()
    with pytest.raises(ExchangeError, match="does not support sandbox"):
        make_adapter(sandbox=True)
    # The failed constructor must not leak its event-loop thread.
    assert threading.active_count() == threads_before


def test_load_markets_wraps_ccxt_errors(make_adapter, monkeypatch):
    adapter = make_adapter()
    assert adapter.load_markets() == {"BTC/USDT": {}}

    async def _boom(self):
        raise ccxt.NetworkError("timeout")

    monkeypatch.setattr(FakeClient, "load_markets", _boom)
    with pytest.raises(ExchangeError, match="failed to load markets"):
        adapter.load_markets()


def test_fetch_candles_converts_ohlcv_rows_to_candles(make_adapter):
    adapter = make_adapter()
    candles = adapter.fetch_candles("BTC/USDT", "1h", limit=50, since=123)
    assert len(candles) == 1
    assert candles[0].close == 10.5
    assert candles[0].timestamp == 1_700_000_000_000
    assert adapter.client.calls == [("fetch_ohlcv", "BTC/USDT", "1h", 50, 123)]


def test_fetch_candles_wraps_ccxt_errors(make_adapter, monkeypatch):
    adapter = make_adapter()

    async def _boom(self, symbol, timeframe="1h", limit=200, since=None):
        raise ccxt.ExchangeNotAvailable("down")

    monkeypatch.setattr(FakeClient, "fetch_ohlcv", _boom)
    with pytest.raises(ExchangeError, match="fetch_ohlcv failed"):
        adapter.fetch_candles("BTC/USDT", "1h")


def test_fetch_candles_many_runs_requests_concurrently(make_adapter, monkeypatch):
    monkeypatch.setattr(FakeClient, "ohlcv_delay", 0.1)
    adapter = make_adapter(max_concurrency=20)
    requests = {f"S{i}/USDT": (5, None) for i in range(20)}
    started = time.perf_counter()
    result = adapter.fetch_candles_many("1h", requests)
    elapsed = time.perf_counter() - started
    assert set(result) == set(requests)
    assert all(len(rows) == 1 for rows in result.values())
    # 20 x 100 ms sequentially would be 2 s; concurrently it is about one round-trip.
    assert elapsed < 1.0
    assert adapter.client.max_in_flight == 20


def test_fetch_candles_many_respects_the_concurrency_limit(make_adapter, monkeypatch):
    monkeypatch.setattr(FakeClient, "ohlcv_delay", 0.02)
    adapter = make_adapter(max_concurrency=3)
    adapter.fetch_candles_many("1h", {f"S{i}/USDT": (5, None) for i in range(12)})
    assert adapter.client.max_in_flight == 3


def test_fetch_candles_many_isolates_per_symbol_failures(make_adapter, monkeypatch):
    adapter = make_adapter()
    real = FakeClient.fetch_ohlcv

    async def _flaky(self, symbol, timeframe="1h", limit=200, since=None):
        if symbol == "BAD/USDT":
            raise ccxt.BadSymbol("delisted")
        return await real(self, symbol, timeframe, limit, since)

    monkeypatch.setattr(FakeClient, "fetch_ohlcv", _flaky)
    result = adapter.fetch_candles_many("1h", {"BTC/USDT": (5, None), "BAD/USDT": (5, None)})
    assert len(result["BTC/USDT"]) == 1
    assert isinstance(result["BAD/USDT"], ExchangeError)
    assert "delisted" in str(result["BAD/USDT"])


def test_fetch_last_price_prefers_last_then_close(make_adapter, monkeypatch):
    adapter = make_adapter()
    assert adapter.fetch_last_price("BTC/USDT") == 123.45

    async def _close_only(self, symbol):
        return {"close": 55.0}

    monkeypatch.setattr(FakeClient, "fetch_ticker", _close_only)
    assert adapter.fetch_last_price("BTC/USDT") == 55.0


def test_fetch_last_price_raises_when_no_price_is_available(make_adapter, monkeypatch):
    adapter = make_adapter()

    async def _empty(self, symbol):
        return {}

    monkeypatch.setattr(FakeClient, "fetch_ticker", _empty)
    with pytest.raises(ExchangeError, match="no last price"):
        adapter.fetch_last_price("BTC/USDT")


def test_fetch_last_price_wraps_ccxt_errors(make_adapter, monkeypatch):
    adapter = make_adapter()

    async def _boom(self, symbol):
        raise ccxt.BadSymbol("nope")

    monkeypatch.setattr(FakeClient, "fetch_ticker", _boom)
    with pytest.raises(ExchangeError, match="fetch_ticker failed"):
        adapter.fetch_last_price("BTC/USDT")


def test_fetch_funding_rate_returns_the_rate(make_adapter):
    assert make_adapter().fetch_funding_rate("BTC/USDT") == pytest.approx(0.0001)


def test_fetch_funding_rate_is_none_when_venue_does_not_support_it(make_adapter, monkeypatch):
    monkeypatch.setattr(FakeClient, "has", {"fetchFundingRate": False})
    assert make_adapter().fetch_funding_rate("BTC/USDT") is None


def test_fetch_funding_rate_swallows_errors_to_none(make_adapter, monkeypatch):
    adapter = make_adapter()

    async def _boom(self, symbol):
        raise ccxt.ExchangeError("temporary glitch")

    monkeypatch.setattr(FakeClient, "fetch_funding_rate", _boom)
    assert adapter.fetch_funding_rate("BTC/USDT") is None


def test_fetch_funding_rate_is_none_when_payload_lacks_the_field(make_adapter, monkeypatch):
    adapter = make_adapter()

    async def _empty(self, symbol):
        return {}

    monkeypatch.setattr(FakeClient, "fetch_funding_rate", _empty)
    assert adapter.fetch_funding_rate("BTC/USDT") is None


def test_fetch_funding_rates_uses_the_batch_endpoint_when_available(make_adapter, monkeypatch):
    seen = []

    async def _batch(self, symbols):
        seen.append(list(symbols))
        return {"BTC/USDT:USDT": {"fundingRate": 0.0002}}

    monkeypatch.setattr(FakeClient, "has", {"fetchFundingRates": True, "fetchFundingRate": True})
    monkeypatch.setattr(FakeClient, "fetch_funding_rates", _batch, raising=False)
    adapter = make_adapter()
    rates = adapter.fetch_funding_rates(["BTC/USDT:USDT", "ETH/USDT:USDT"])
    assert rates == {"BTC/USDT:USDT": pytest.approx(0.0002), "ETH/USDT:USDT": None}
    assert seen == [["BTC/USDT:USDT", "ETH/USDT:USDT"]]  # one request for both
    assert not [c for c in adapter.client.calls if c[0] == "fetch_funding_rate"]


def test_fetch_funding_rates_falls_back_to_single_lookups(make_adapter, monkeypatch):
    async def _batch_fails(self, symbols):
        raise ccxt.NotSupported("mixed market types")

    monkeypatch.setattr(FakeClient, "has", {"fetchFundingRates": True, "fetchFundingRate": True})
    monkeypatch.setattr(FakeClient, "fetch_funding_rates", _batch_fails, raising=False)
    rates = make_adapter().fetch_funding_rates(["A/USDT:USDT", "B/USDT:USDT"])
    assert rates == {"A/USDT:USDT": pytest.approx(1e-4), "B/USDT:USDT": pytest.approx(1e-4)}


def test_fetch_funding_rates_skips_known_spot_markets(make_adapter):
    adapter = make_adapter()
    adapter.client.markets = {"BTC/USDT": {"contract": False}, "BTC/USDT:USDT": {"contract": True}}
    rates = adapter.fetch_funding_rates(["BTC/USDT", "BTC/USDT:USDT"])
    assert rates == {"BTC/USDT": None, "BTC/USDT:USDT": pytest.approx(1e-4)}
    assert adapter.client.calls == [("fetch_funding_rate", "BTC/USDT:USDT")]
    assert make_adapter().fetch_funding_rates([]) == {}

    adapter.client.markets = {"BTC/USDT": {"contract": False}}
    adapter.client.calls.clear()
    assert adapter.fetch_funding_rates(["BTC/USDT"]) == {"BTC/USDT": None}
    assert adapter.client.calls == []  # nothing to ask the venue


def test_fetch_balance_keeps_only_nonzero_free_balances(make_adapter):
    assert make_adapter().fetch_balance() == {"USDT": 500.0}


def test_fetch_balance_wraps_ccxt_errors(make_adapter, monkeypatch):
    adapter = make_adapter()

    async def _boom(self):
        raise ccxt.AuthenticationError("bad key")

    monkeypatch.setattr(FakeClient, "fetch_balance", _boom)
    with pytest.raises(ExchangeError, match="fetch_balance failed"):
        adapter.fetch_balance()


def test_create_order_requires_credentials(make_adapter):
    adapter = make_adapter()  # no api_key/secret
    request = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0)
    with pytest.raises(ExchangeError, match="without API credentials"):
        adapter.create_order(request)


def test_create_order_returns_a_parsed_order(make_adapter):
    adapter = make_adapter(api_key="k", secret="s")
    request = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=2.0)
    order = adapter.create_order(request)
    assert order.status == OrderStatus.FILLED
    assert order.filled == 2.0
    assert order.average_price == 100.0
    assert order.fee == 0.5
    assert order.id == "abc123"


def test_create_order_wraps_ccxt_errors(make_adapter, monkeypatch):
    adapter = make_adapter(api_key="k", secret="s")

    async def _boom(self, symbol, type_, side, amount, price):
        raise ccxt.InsufficientFunds("no funds")

    monkeypatch.setattr(FakeClient, "create_order", _boom)
    request = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0)
    with pytest.raises(ExchangeError, match="create_order failed"):
        adapter.create_order(request)


def test_create_order_uses_the_limit_price_only_for_limit_orders(make_adapter):
    adapter = make_adapter(api_key="k", secret="s")
    request = OrderRequest(
        symbol="BTC/USDT", side=OrderSide.SELL, amount=1.0, type=OrderType.LIMIT, price=42.0
    )
    order = adapter.create_order(request)
    assert order.average_price == 42.0  # FakeClient echoes back whatever price it got


def test_cancel_order_wraps_ccxt_errors(make_adapter, monkeypatch):
    adapter = make_adapter()
    adapter.cancel_order("id1", "BTC/USDT")  # no exception on the happy path

    async def _boom(self, order_id, symbol):
        raise ccxt.OrderNotFound("gone")

    monkeypatch.setattr(FakeClient, "cancel_order", _boom)
    with pytest.raises(ExchangeError, match="cancel_order failed"):
        adapter.cancel_order("id1", "BTC/USDT")


def test_close_closes_the_client_and_stops_the_loop(make_adapter):
    adapter = make_adapter()
    loop = adapter._loop.loop
    adapter.close()
    assert adapter.client.closed is True
    assert loop.is_closed()
    adapter.close()  # idempotent
    with pytest.raises(ExchangeError, match="closed"):
        adapter.fetch_balance()


def test_close_is_best_effort_when_the_client_close_fails(make_adapter, monkeypatch):
    adapter = make_adapter()

    async def _boom(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(FakeClient, "close", _boom)
    adapter.close()  # must not raise
    assert adapter._loop.loop.is_closed()


def test_parse_order_falls_back_to_raw_price_with_no_average(make_adapter):
    adapter = make_adapter(api_key="k", secret="s")
    raw = {"status": "open", "amount": 1.0, "filled": 0.0, "id": None, "price": 200.0}
    request = OrderRequest(symbol="ETH/USDT", side=OrderSide.BUY, amount=1.0)
    order = adapter._parse_order(raw, request)
    assert order.status == OrderStatus.OPEN
    assert order.average_price == 200.0
    assert order.id is None
    assert order.symbol == "ETH/USDT"  # falls back to the request symbol (raw lacks one)


def test_parse_order_sums_a_fees_list_when_no_single_fee_object(make_adapter):
    adapter = make_adapter(api_key="k", secret="s")
    raw = {
        "status": "closed",
        "amount": 1.0,
        "filled": 1.0,
        "average": 10.0,
        "fees": [{"cost": 0.1}, {"cost": 0.2}, {}],
    }
    request = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0)
    order = adapter._parse_order(raw, request)
    assert order.fee == pytest.approx(0.3)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("closed", OrderStatus.FILLED),
        ("filled", OrderStatus.FILLED),
        ("open", OrderStatus.OPEN),
        ("canceled", OrderStatus.CANCELED),
        ("cancelled", OrderStatus.CANCELED),
        ("rejected", OrderStatus.REJECTED),
        ("expired", OrderStatus.CANCELED),
        ("something_unrecognized", OrderStatus.OPEN),
    ],
)
def test_parse_order_maps_every_ccxt_status(make_adapter, status, expected):
    adapter = make_adapter(api_key="k", secret="s")
    raw = {"status": status, "amount": 1.0, "filled": 1.0}
    request = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0)
    assert adapter._parse_order(raw, request).status == expected


def test_a_wedged_call_times_out_instead_of_hanging(make_adapter, monkeypatch):
    import crypto_bot.exchanges.ccxt_adapter as mod

    adapter = make_adapter()

    async def _hang(self):
        await asyncio.sleep(10)

    monkeypatch.setattr(mod, "_CALL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(FakeClient, "fetch_balance", _hang)
    with pytest.raises(ExchangeError, match="timed out"):
        adapter.fetch_balance()


def test_a_client_construction_failure_cleans_up_the_loop(make_adapter, monkeypatch):
    def _boom(self, config):
        raise RuntimeError("bad config")

    monkeypatch.setattr(FakeClient, "__init__", _boom)
    threads_before = threading.active_count()
    with pytest.raises(RuntimeError, match="bad config"):
        make_adapter()
    assert threading.active_count() == threads_before


def test_loop_stop_is_idempotent_and_tolerates_a_stuck_thread(make_adapter):
    from types import SimpleNamespace

    from crypto_bot.exchanges.ccxt_adapter import _LoopThread

    runner = _LoopThread("test-loop")
    runner.stop()
    runner.stop()  # already closed: no-op
    assert runner.loop.is_closed()

    stuck = _LoopThread("stuck-loop")
    real_thread = stuck._thread
    stuck._thread = SimpleNamespace(join=lambda timeout: None, is_alive=lambda: True)
    stuck.stop()
    assert not stuck.loop.is_closed()  # never close a loop that may still be running
    real_thread.join(timeout=5)
    stuck.loop.close()
