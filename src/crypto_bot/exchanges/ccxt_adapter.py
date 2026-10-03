"""ccxt-backed implementation of :class:`ExchangeAdapter`.

This single adapter speaks to Binance, Bybit, Coinbase and 100+ other venues, because
ccxt normalizes their REST APIs. ``ccxt`` is imported lazily (only when this module is
imported) so the rest of the bot — and its unit tests — run without the dependency.

**Concurrency.** The bot itself is synchronous, but market data for many symbols should
not be fetched one round-trip at a time: at ~150 ms per request, 50 symbols is 7.5 s of
pure waiting per cycle. So the adapter drives ccxt's *async* client on a private event
loop running in a daemon thread, and the batch methods (:meth:`fetch_candles_many`,
:meth:`fetch_funding_rates`) fan requests out with ``asyncio.gather``. Rate limiting stays
correct because every request still passes through the one client's leaky-bucket
throttler — concurrency fills the gaps between requests, it never exceeds the venue's
limit. A semaphore bounds the number in flight (``exchange.max_concurrency``).

ccxt's *sync* client is not an option for this: its rate limiter is a per-thread spacing
check, so sharing it across threads would silently break rate limiting.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any

from crypto_bot.core.models import Candle, Order, OrderRequest, OrderStatus, OrderType
from crypto_bot.exchanges.base import ExchangeAdapter, ExchangeError

try:
    import ccxt
    import ccxt.async_support as ccxt_async
except ImportError as exc:  # pragma: no cover - exercised only without the dep installed
    raise ImportError(
        "ccxt is required for live/paper exchange access. Install it with "
        "`pip install ccxt` (or `pip install -r requirements.txt`)."
    ) from exc


DEFAULT_MAX_CONCURRENCY = 10

# Upper bound on any single bridged call. ccxt's own HTTP timeout (10 s by default) fires
# long before this; it only exists so a wedged loop can never hang the bot forever.
_CALL_TIMEOUT_S = 300.0

_CCXT_STATUS = {
    "closed": OrderStatus.FILLED,
    "filled": OrderStatus.FILLED,
    "open": OrderStatus.OPEN,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.CANCELED,
}


class _LoopThread:
    """An asyncio event loop on a daemon thread, so sync code can run coroutines on it."""

    def __init__(self, name: str) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name=name, daemon=True)
        self._thread.start()

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(_CALL_TIMEOUT_S)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise ExchangeError(f"exchange call timed out after {_CALL_TIMEOUT_S:g}s") from exc

    def stop(self) -> None:
        if self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self.loop.close()


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
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        if not hasattr(ccxt_async, exchange_id):
            raise ExchangeError(
                f"unknown ccxt exchange id {exchange_id!r}. "
                "See https://docs.ccxt.com for the list of supported ids."
            )
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
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
        self._closed = False
        self._loop = _LoopThread(f"ccxt-{exchange_id}")
        try:
            # Build the client and semaphore *on* the loop, so anything they bind to a
            # running loop binds to this one.
            self.client, self._sem = self._loop.run(
                _create_client(getattr(ccxt_async, exchange_id), config, max_concurrency)
            )
            if sandbox:
                try:
                    self.client.set_sandbox_mode(True)
                except Exception as exc:  # ccxt raises NotSupported for venues w/o a testnet
                    raise ExchangeError(
                        f"{exchange_id} does not support sandbox/testnet mode via ccxt"
                    ) from exc
        except BaseException:
            self.close()
            raise

    @property
    def has_credentials(self) -> bool:
        return bool(self.client.apiKey and self.client.secret)

    # -- market data -----------------------------------------------------------------
    def load_markets(self) -> dict:
        try:
            return self._run(self.client.load_markets())
        except ccxt.BaseError as exc:
            raise ExchangeError(f"failed to load markets on {self.name}: {exc}") from exc

    def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = 200, since: int | None = None
    ) -> list[Candle]:
        result = self.fetch_candles_many(timeframe, {symbol: (limit, since)})[symbol]
        if isinstance(result, ExchangeError):
            raise result
        return result

    def fetch_candles_many(
        self, timeframe: str, requests: dict[str, tuple[int, int | None]]
    ) -> dict[str, list[Candle] | ExchangeError]:
        return self._run(self._fetch_candles_many(timeframe, requests))

    async def _fetch_candles_many(
        self, timeframe: str, requests: dict[str, tuple[int, int | None]]
    ) -> dict[str, list[Candle] | ExchangeError]:
        async def one(symbol: str, limit: int, since: int | None):
            async with self._sem:
                try:
                    rows = await self.client.fetch_ohlcv(
                        symbol, timeframe=timeframe, limit=limit, since=since
                    )
                except ccxt.BaseError as exc:
                    return symbol, ExchangeError(
                        f"fetch_ohlcv failed for {symbol} on {self.name}: {exc}"
                    )
            return symbol, [Candle.from_ccxt(row) for row in rows]

        pairs = await asyncio.gather(
            *(one(symbol, limit, since) for symbol, (limit, since) in requests.items())
        )
        return dict(pairs)

    def fetch_last_price(self, symbol: str) -> float:
        try:
            ticker = self._run(self.client.fetch_ticker(symbol))
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
        return self.fetch_funding_rates([symbol])[symbol]

    def fetch_funding_rates(self, symbols: list[str]) -> dict[str, float | None]:
        """Funding rates for many symbols: one batched request where the venue offers
        ``fetchFundingRates``, otherwise concurrent single lookups. Spot markets (known
        from the loaded market metadata) are answered ``None`` without a request."""
        if not symbols:
            return {}
        return self._run(self._fetch_funding_rates(list(symbols)))

    async def _fetch_funding_rates(self, symbols: list[str]) -> dict[str, float | None]:
        out: dict[str, float | None] = dict.fromkeys(symbols)
        has = self.client.has
        markets = getattr(self.client, "markets", None) or {}
        wanted = [s for s in symbols if (markets.get(s) or {}).get("contract", True)]
        if not wanted:
            return out

        if has.get("fetchFundingRates"):
            try:
                data = await self.client.fetch_funding_rates(wanted)
            except ccxt.BaseError:
                pass  # fall through to per-symbol lookups
            else:
                for symbol in wanted:
                    out[symbol] = _funding_rate(data.get(symbol))
                return out

        fetcher = getattr(self.client, "fetch_funding_rate", None)
        if not callable(fetcher) or not has.get("fetchFundingRate"):
            return out

        async def one(symbol: str):
            async with self._sem:
                try:
                    return symbol, _funding_rate(await fetcher(symbol))
                except ccxt.BaseError:
                    return symbol, None

        out.update(await asyncio.gather(*(one(s) for s in wanted)))
        return out

    # -- account ---------------------------------------------------------------------
    def fetch_balance(self) -> dict[str, float]:
        try:
            balances = self._run(self.client.fetch_balance())
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
            raw = self._run(
                self.client.create_order(
                    request.symbol,
                    request.type.value,
                    request.side.value,
                    request.amount,
                    price,
                )
            )
        except ccxt.BaseError as exc:
            raise ExchangeError(
                f"create_order failed for {request.symbol} on {self.name}: {exc}"
            ) from exc
        return self._parse_order(raw, request)

    def cancel_order(self, order_id: str, symbol: str) -> None:
        try:
            self._run(self.client.cancel_order(order_id, symbol))
        except ccxt.BaseError as exc:
            raise ExchangeError(f"cancel_order failed on {self.name}: {exc}") from exc

    def close(self) -> None:
        """Close the HTTP session and stop the loop thread. Safe to call repeatedly."""
        if self._closed:
            return
        self._closed = True
        client = getattr(self, "client", None)
        if client is not None:
            try:
                self._loop.run(client.close())  # not _run(): that refuses once closed
            except Exception:  # best-effort cleanup
                pass
        self._loop.stop()

    # -- helpers ---------------------------------------------------------------------
    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        if self._closed:
            coro.close()  # never awaited; close it so Python doesn't warn
            raise ExchangeError(f"{self.name} adapter is closed")
        return self._loop.run(coro)

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


async def _create_client(cls: type, config: dict, max_concurrency: int):
    return cls(config), asyncio.Semaphore(max_concurrency)


def _funding_rate(info: object) -> float | None:
    rate = info.get("fundingRate") if isinstance(info, dict) else None
    return float(rate) if rate is not None else None
