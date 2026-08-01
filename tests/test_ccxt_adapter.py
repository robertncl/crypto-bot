"""crypto_bot.exchanges.ccxt_adapter, exercised against a fake ccxt client.

CCXTAdapter.__init__ does `getattr(ccxt, exchange_id)(config)`, so the fake is wired in
by monkeypatching an attribute named after a throwaway exchange id directly onto the
real `ccxt` module — the adapter then constructs *our* class instead of a real venue
client, and no network call happens anywhere in this file.
"""

from __future__ import annotations

import ccxt
import pytest

from crypto_bot.core.models import OrderRequest, OrderSide, OrderStatus, OrderType
from crypto_bot.exchanges.base import ExchangeError
from crypto_bot.exchanges.ccxt_adapter import CCXTAdapter

EXCHANGE_ID = "fakeccxtvenue"


class FakeClient:
    """Stands in for a ccxt exchange instance; records what it was called with."""

    has = {"fetchFundingRate": True}

    def __init__(self, config: dict):
        self.config = config
        self.apiKey = config.get("apiKey")
        self.secret = config.get("secret")
        self.options = config.get("options", {})
        self.sandbox_mode = None
        self._raise_on_sandbox = False
        self.closed = False

    def set_sandbox_mode(self, flag: bool) -> None:
        if self._raise_on_sandbox:
            raise ccxt.NotSupported("no testnet for this venue")
        self.sandbox_mode = flag

    def load_markets(self):
        return {"BTC/USDT": {}}

    def fetch_ohlcv(self, symbol, timeframe="1h", limit=200, since=None):
        return [[1_700_000_000_000, 10.0, 11.0, 9.0, 10.5, 100.0]]

    def fetch_ticker(self, symbol):
        return {"last": 123.45}

    def fetch_funding_rate(self, symbol):
        return {"fundingRate": 0.0001}

    def fetch_balance(self):
        return {"free": {"USDT": 500.0, "BTC": 0.0}}

    def create_order(self, symbol, type_, side, amount, price):
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

    def cancel_order(self, order_id, symbol):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def fake_venue(monkeypatch):
    monkeypatch.setattr(ccxt, EXCHANGE_ID, FakeClient, raising=False)
    yield EXCHANGE_ID


def test_unknown_exchange_id_is_rejected():
    with pytest.raises(ExchangeError, match="unknown ccxt exchange id"):
        CCXTAdapter("not_a_real_ccxt_exchange_id_xyz")


def test_constructs_client_with_credentials_and_options(fake_venue):
    adapter = CCXTAdapter(
        fake_venue, api_key="k", secret="s", password="p", options={"defaultType": "spot"}
    )
    assert adapter.client.config["apiKey"] == "k"
    assert adapter.client.config["secret"] == "s"
    assert adapter.client.config["password"] == "p"
    assert adapter.client.config["options"] == {"defaultType": "spot"}
    assert adapter.has_credentials is True


def test_no_credentials_means_no_credentials(fake_venue):
    adapter = CCXTAdapter(fake_venue)
    assert adapter.has_credentials is False


def test_sandbox_mode_is_enabled_when_supported(fake_venue):
    adapter = CCXTAdapter(fake_venue, sandbox=True)
    assert adapter.client.sandbox_mode is True


def test_sandbox_mode_raises_a_clear_error_when_unsupported(fake_venue, monkeypatch):
    def _boom(self, flag):
        raise ccxt.NotSupported("nope")

    monkeypatch.setattr(FakeClient, "set_sandbox_mode", _boom)
    with pytest.raises(ExchangeError, match="does not support sandbox"):
        CCXTAdapter(fake_venue, sandbox=True)


def test_load_markets_wraps_ccxt_errors(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue)
    assert adapter.load_markets() == {"BTC/USDT": {}}

    def _boom(self):
        raise ccxt.NetworkError("timeout")

    monkeypatch.setattr(FakeClient, "load_markets", _boom)
    with pytest.raises(ExchangeError, match="failed to load markets"):
        adapter.load_markets()


def test_fetch_candles_converts_ohlcv_rows_to_candles(fake_venue):
    adapter = CCXTAdapter(fake_venue)
    candles = adapter.fetch_candles("BTC/USDT", "1h")
    assert len(candles) == 1
    assert candles[0].close == 10.5
    assert candles[0].timestamp == 1_700_000_000_000


def test_fetch_candles_wraps_ccxt_errors(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue)

    def _boom(self, symbol, timeframe="1h", limit=200, since=None):
        raise ccxt.ExchangeNotAvailable("down")

    monkeypatch.setattr(FakeClient, "fetch_ohlcv", _boom)
    with pytest.raises(ExchangeError, match="fetch_ohlcv failed"):
        adapter.fetch_candles("BTC/USDT", "1h")


def test_fetch_last_price_prefers_last_then_close(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue)
    assert adapter.fetch_last_price("BTC/USDT") == 123.45

    monkeypatch.setattr(FakeClient, "fetch_ticker", lambda self, symbol: {"close": 55.0})
    assert adapter.fetch_last_price("BTC/USDT") == 55.0


def test_fetch_last_price_raises_when_no_price_is_available(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue)
    monkeypatch.setattr(FakeClient, "fetch_ticker", lambda self, symbol: {})
    with pytest.raises(ExchangeError, match="no last price"):
        adapter.fetch_last_price("BTC/USDT")


def test_fetch_last_price_wraps_ccxt_errors(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue)

    def _boom(self, symbol):
        raise ccxt.BadSymbol("nope")

    monkeypatch.setattr(FakeClient, "fetch_ticker", _boom)
    with pytest.raises(ExchangeError, match="fetch_ticker failed"):
        adapter.fetch_last_price("BTC/USDT")


def test_fetch_funding_rate_returns_the_rate(fake_venue):
    adapter = CCXTAdapter(fake_venue)
    assert adapter.fetch_funding_rate("BTC/USDT") == pytest.approx(0.0001)


def test_fetch_funding_rate_is_none_when_venue_does_not_support_it(fake_venue, monkeypatch):
    monkeypatch.setattr(FakeClient, "has", {"fetchFundingRate": False})
    adapter = CCXTAdapter(fake_venue)
    assert adapter.fetch_funding_rate("BTC/USDT") is None


def test_fetch_funding_rate_swallows_errors_to_none(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue)

    def _boom(self, symbol):
        raise ccxt.ExchangeError("temporary glitch")

    monkeypatch.setattr(FakeClient, "fetch_funding_rate", _boom)
    assert adapter.fetch_funding_rate("BTC/USDT") is None


def test_fetch_funding_rate_is_none_when_payload_lacks_the_field(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue)
    monkeypatch.setattr(FakeClient, "fetch_funding_rate", lambda self, symbol: {})
    assert adapter.fetch_funding_rate("BTC/USDT") is None


def test_fetch_balance_keeps_only_nonzero_free_balances(fake_venue):
    adapter = CCXTAdapter(fake_venue)
    assert adapter.fetch_balance() == {"USDT": 500.0}


def test_fetch_balance_wraps_ccxt_errors(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue)

    def _boom(self):
        raise ccxt.AuthenticationError("bad key")

    monkeypatch.setattr(FakeClient, "fetch_balance", _boom)
    with pytest.raises(ExchangeError, match="fetch_balance failed"):
        adapter.fetch_balance()


def test_create_order_requires_credentials(fake_venue):
    adapter = CCXTAdapter(fake_venue)  # no api_key/secret
    request = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0)
    with pytest.raises(ExchangeError, match="without API credentials"):
        adapter.create_order(request)


def test_create_order_returns_a_parsed_order(fake_venue):
    adapter = CCXTAdapter(fake_venue, api_key="k", secret="s")
    request = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=2.0)
    order = adapter.create_order(request)
    assert order.status == OrderStatus.FILLED
    assert order.filled == 2.0
    assert order.average_price == 100.0
    assert order.fee == 0.5
    assert order.id == "abc123"


def test_create_order_wraps_ccxt_errors(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue, api_key="k", secret="s")

    def _boom(self, symbol, type_, side, amount, price):
        raise ccxt.InsufficientFunds("no funds")

    monkeypatch.setattr(FakeClient, "create_order", _boom)
    request = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0)
    with pytest.raises(ExchangeError, match="create_order failed"):
        adapter.create_order(request)


def test_create_order_uses_the_limit_price_only_for_limit_orders(fake_venue):
    adapter = CCXTAdapter(fake_venue, api_key="k", secret="s")
    request = OrderRequest(
        symbol="BTC/USDT", side=OrderSide.SELL, amount=1.0, type=OrderType.LIMIT, price=42.0
    )
    order = adapter.create_order(request)
    assert order.average_price == 42.0  # FakeClient echoes back whatever price it got


def test_cancel_order_wraps_ccxt_errors(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue)
    adapter.cancel_order("id1", "BTC/USDT")  # no exception on the happy path

    def _boom(self, order_id, symbol):
        raise ccxt.OrderNotFound("gone")

    monkeypatch.setattr(FakeClient, "cancel_order", _boom)
    with pytest.raises(ExchangeError, match="cancel_order failed"):
        adapter.cancel_order("id1", "BTC/USDT")


def test_close_calls_the_underlying_client(fake_venue):
    adapter = CCXTAdapter(fake_venue)
    adapter.close()
    assert adapter.client.closed is True


def test_close_is_a_best_effort_noop_on_failure(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue)
    monkeypatch.setattr(
        FakeClient, "close", lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    adapter.close()  # must not raise


def test_close_is_a_noop_when_the_client_has_no_close_method(fake_venue, monkeypatch):
    adapter = CCXTAdapter(fake_venue)
    monkeypatch.delattr(FakeClient, "close")
    adapter.close()  # must not raise


def test_parse_order_falls_back_to_raw_price_with_no_average(fake_venue):
    adapter = CCXTAdapter(fake_venue, api_key="k", secret="s")
    raw = {"status": "open", "amount": 1.0, "filled": 0.0, "id": None, "price": 200.0}
    request = OrderRequest(symbol="ETH/USDT", side=OrderSide.BUY, amount=1.0)
    order = adapter._parse_order(raw, request)
    assert order.status == OrderStatus.OPEN
    assert order.average_price == 200.0
    assert order.id is None
    assert order.symbol == "ETH/USDT"  # falls back to the request symbol (raw lacks one)


def test_parse_order_sums_a_fees_list_when_no_single_fee_object(fake_venue):
    adapter = CCXTAdapter(fake_venue, api_key="k", secret="s")
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
def test_parse_order_maps_every_ccxt_status(fake_venue, status, expected):
    adapter = CCXTAdapter(fake_venue, api_key="k", secret="s")
    raw = {"status": status, "amount": 1.0, "filled": 1.0}
    request = OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0)
    assert adapter._parse_order(raw, request).status == expected
