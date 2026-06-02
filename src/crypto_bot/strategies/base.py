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

    def __init__(self, params: dict | None = None) -> None:
        self.params = params or {}

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Minimum number of candles required before :meth:`generate` is meaningful."""

    @abstractmethod
    def generate(self, candles: list[Candle]) -> Signal:
        """Return a trading signal for the latest candle in ``candles``.

        ``candles`` is ordered oldest-first. Implementations should return
        :data:`crypto_bot.core.models.HOLD` when there is not enough data or no edge.
        """

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}({self.params})"
