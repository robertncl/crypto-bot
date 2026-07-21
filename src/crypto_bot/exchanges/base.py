"""The unified exchange interface the bot codes against."""

from __future__ import annotations

from abc import ABC, abstractmethod

from crypto_bot.core.models import Candle, Order, OrderRequest


class ExchangeError(Exception):
    """Raised for any exchange-side failure (network, auth, bad symbol, ...)."""


class ExchangeAdapter(ABC):
    """Adapter over a single exchange.

    Public-data methods (candles, ticker, markets) work without API keys and power
    paper trading. Account methods (balance, create/cancel order) require valid keys
    and are only used in live mode.
    """

    name: str = "base"

    @abstractmethod
    def load_markets(self) -> dict:
        """Load and cache market metadata; call once at startup to validate symbols."""

    @abstractmethod
    def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = 200, since: int | None = None
    ) -> list[Candle]:
        """Return up to ``limit`` OHLCV candles, oldest-first.

        Without ``since`` this returns the most *recent* candles (the live loop's use).
        With ``since`` (epoch ms) it returns candles starting at that time — the
        backtester paginates history with it.
        """

    @abstractmethod
    def fetch_last_price(self, symbol: str) -> float:
        """Return the last traded price for ``symbol``."""

    @abstractmethod
    def fetch_balance(self) -> dict[str, float]:
        """Return free balances keyed by currency code. Requires API keys."""

    @abstractmethod
    def create_order(self, request: OrderRequest) -> Order:
        """Place a real order. Requires API keys (live mode only)."""

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> None:
        """Cancel an open order. Requires API keys."""

    def fetch_funding_rate(self, symbol: str) -> float | None:
        """Current perpetual funding rate for ``symbol``, or None if not applicable.

        Concrete but non-abstract: spot venues, fakes and the backtest replay have no
        funding endpoint, and none of them should be forced to implement one. A ``None``
        return means "unknown", which callers treat as "fall back to the configured rate".
        """
        return None

    def close(self) -> None:  # noqa: B027 - intentional no-op default; ccxt adapter overrides
        """Release any underlying resources. Safe to call multiple times (default: no-op)."""
