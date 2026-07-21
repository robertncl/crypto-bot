"""Strategy interface.

A strategy is a pure decision function: given the most recent candles for one symbol,
it returns a :class:`Signal`. Strategies never touch the exchange, place orders, or size
positions — that is the job of the risk manager and broker. This separation keeps
strategies easy to unit-test and backtest.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from crypto_bot.core.models import Candle, Signal


class Strategy(ABC):
    name: str = "base"

    #: Opt-in flag. When True the engine calls ``generate(candles, symbol, context)`` with
    #: a :class:`~crypto_bot.core.models.MarketContext` carrying non-OHLCV data (funding
    #: rate). Candle-only strategies leave this False and keep the two-argument signature,
    #: so adding derivatives data costs the existing strategies nothing.
    wants_context: bool = False

    def __init__(self, params: dict | None = None) -> None:
        self.params = params or {}

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Minimum number of candles required before :meth:`generate` is meaningful."""

    @abstractmethod
    def generate(self, candles: list[Candle], symbol: str | None = None) -> Signal:
        """Return a trading signal for the latest candle in ``candles``.

        ``candles`` is ordered oldest-first. Implementations should return
        :data:`crypto_bot.core.models.HOLD` when there is not enough data or no edge.

        ``symbol`` is the market these candles belong to. Condition-based strategies are
        pure functions of ``candles`` and ignore it; it exists for the rare *stateful*
        strategy (e.g. scheduled DCA) that must key per-symbol state, because the engine
        shares one strategy instance across all symbols.
        """

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}({self.params})"
