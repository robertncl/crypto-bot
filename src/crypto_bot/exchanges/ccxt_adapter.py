"""ccxt-backed implementation of :class:`ExchangeAdapter`.

This single adapter speaks to Binance, Bybit, Coinbase and 100+ other venues, because
ccxt normalizes their REST APIs. ``ccxt`` is imported lazily (only when this module is
imported) so the rest of the bot — and its unit tests — run without the dependency.
"""

from __future__ import annotations

from crypto_bot.core.models import Candle, Order, OrderRequest, OrderStatus, OrderType
from crypto_bot.exchanges.base import ExchangeAdapter, ExchangeError

try:
    import ccxt
except ImportError as exc:  # pragma: no cover - exercised only without the dep installed
    raise ImportError(
        "ccxt is required for live/paper exchange access. Install it with "
        "`pip install ccxt` (or `pip install -r requirements.txt`)."
    ) from exc


_CCXT_STATUS = {
    "closed": OrderStatus.FILLED,
    "filled": OrderStatus.FILLED,
    "open": OrderStatus.OPEN,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.CANCELED,
}


class CCXTAdapter(ExchangeAdapter):
    def __init__(
        self,
        exchange_id: str,
        *,
        api_key: str | None = None,
        secret: str | None = None,
        password: str | None = None,
        sandbox: bool = False,
        options: dict | None = None,
    ) -> None:
        if not hasattr(ccxt, exchange_id):
            raise ExchangeError(
                f"unknown ccxt exchange id {exchange_id!r}. "
                "See https://docs.ccxt.com for the list of supported ids."
            )
        config: dict = {"enableRateLimit": True}
        if api_key:
            config["apiKey"] = api_key
        if secret:
            config["secret"] = secret
        if password:
            config["password"] = password
        if options:
            config["options"] = options

        self.name = exchange_id
        self.client = getattr(ccxt, exchange_id)(config)

        if sandbox:
            try:
                self.client.set_sandbox_mode(True)
            except Exception as exc:  # ccxt raises NotSupported for venues without a testnet
                raise ExchangeError(
                    f"{exchange_id} does not support sandbox/testnet mode via ccxt"
                ) from exc

    @property
    def has_credentials(self) -> bool:
        return bool(self.client.apiKey and self.client.secret)

    def load_markets(self) -> dict:
        try:
            return self.client.load_markets()
        except ccxt.BaseError as exc:
            raise ExchangeError(f"failed to load markets on {self.name}: {exc}") from exc

    def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = 200, since: int | None = None
    ) -> list[Candle]:
        try:
            rows = self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, since=since)
        except ccxt.BaseError as exc:
            raise ExchangeError(f"fetch_ohlcv failed for {symbol} on {self.name}: {exc}") from exc
        return [Candle.from_ccxt(row) for row in rows]

    def fetch_last_price(self, symbol: str) -> float:
        try:
            ticker = self.client.fetch_ticker(symbol)
        except ccxt.BaseError as exc:
            raise ExchangeError(f"fetch_ticker failed for {symbol} on {self.name}: {exc}") from exc
        last = ticker.get("last") or ticker.get("close")
        if last is None:
            raise ExchangeError(f"no last price available for {symbol} on {self.name}")
        return float(last)

    def fetch_funding_rate(self, symbol: str) -> float | None:
        """Current funding rate for a perp, or None if this market/venue has none.

        Failures are swallowed to None rather than raised: funding is an *enrichment*, and
        a venue hiccup here should degrade the strategy to its configured fallback rather
        than kill a polling cycle that could still manage open positions.
        """
        fetcher = getattr(self.client, "fetch_funding_rate", None)
        if not callable(fetcher) or not self.client.has.get("fetchFundingRate"):
            return None
        try:
            info = fetcher(symbol)
        except ccxt.BaseError:
            return None
        rate = info.get("fundingRate") if isinstance(info, dict) else None
        return float(rate) if rate is not None else None

    def fetch_balance(self) -> dict[str, float]:
        try:
            balances = self.client.fetch_balance()
        except ccxt.BaseError as exc:
            raise ExchangeError(f"fetch_balance failed on {self.name}: {exc}") from exc
        free = balances.get("free", {})
        return {cur: float(amt) for cur, amt in free.items() if amt}

    def create_order(self, request: OrderRequest) -> Order:
        if not self.has_credentials:
            raise ExchangeError(
                f"cannot place a live order on {self.name} without API credentials"
            )
        price = request.price if request.type == OrderType.LIMIT else None
        try:
            raw = self.client.create_order(
                request.symbol,
                request.type.value,
                request.side.value,
                request.amount,
                price,
            )
        except ccxt.BaseError as exc:
            raise ExchangeError(
                f"create_order failed for {request.symbol} on {self.name}: {exc}"
            ) from exc
        return self._parse_order(raw, request)

    def cancel_order(self, order_id: str, symbol: str) -> None:
        try:
            self.client.cancel_order(order_id, symbol)
        except ccxt.BaseError as exc:
            raise ExchangeError(f"cancel_order failed on {self.name}: {exc}") from exc

    def close(self) -> None:
        closer = getattr(self.client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass

    @staticmethod
    def _parse_order(raw: dict, request: OrderRequest) -> Order:
        status = _CCXT_STATUS.get(str(raw.get("status")).lower(), OrderStatus.OPEN)
        fee = 0.0
        fee_obj = raw.get("fee")
        if isinstance(fee_obj, dict) and fee_obj.get("cost") is not None:
            fee = float(fee_obj["cost"])
        elif raw.get("fees"):
            fee = sum(float(f.get("cost", 0) or 0) for f in raw["fees"])
        return Order(
            symbol=raw.get("symbol", request.symbol),
            side=request.side,
            amount=float(raw.get("amount") or request.amount),
            type=request.type,
            status=status,
            filled=float(raw.get("filled") or 0.0),
            average_price=float(raw["average"]) if raw.get("average") else raw.get("price"),
            fee=fee,
            id=str(raw.get("id")) if raw.get("id") is not None else None,
            timestamp=int(raw.get("timestamp") or 0),
            info=raw,
        )
