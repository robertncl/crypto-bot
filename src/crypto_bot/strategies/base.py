"""Strategy interface.

A strategy is a decision function: given the history of one symbol, it returns a
:class:`Signal`. Strategies never touch the exchange, place orders, or size positions —
that is the job of the risk manager and broker. This separation keeps strategies easy to
unit-test and backtest.

**Streaming.** The engine drives strategies *incrementally*: it asks for one
:class:`StrategyState` per symbol (:meth:`Strategy.new_state`) and folds each newly
closed candle into it with :meth:`StrategyState.update`. Built-in strategies keep their
indicators as running state (see :mod:`crypto_bot.indicators.stream`), so each bar costs
O(1) instead of recomputing every indicator over a rolling window. That is what lets a
backtest run in time linear in its length and the live loop scale to many symbols.

Two ways to write a strategy:

* **Streaming (preferred):** implement :meth:`Strategy.new_state` returning a
  :class:`StrategyState`. :meth:`Strategy.generate` then works for free by replaying the
  candles through a fresh state.
* **Windowed (legacy):** implement only :meth:`Strategy.generate(candles, symbol)`. The
  engine wraps it in :class:`WindowedState`, which keeps a rolling buffer of recent
  candles and calls ``generate`` once per closed bar. Simple to write; O(window) per bar.

Implement at least one of the two.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque

from crypto_bot.core.models import HOLD, Candle, MarketContext, Signal

#: Candles a :class:`WindowedState` keeps for a legacy ``generate`` strategy (at least).
LEGACY_WINDOW = 200


class StrategyState(ABC):
    """Per-symbol incremental state for one strategy instance."""

    __slots__ = ()

    @abstractmethod
    def update(self, candle: Candle, context: MarketContext | None = None) -> Signal:
        """Fold in the next closed candle (oldest-first, each exactly once); return the
        signal for that bar. ``context`` is only supplied to ``wants_context`` strategies,
        and only on bars the engine may act on — warm-up bars get ``None``."""


class Strategy(ABC):
    name: str = "base"

    #: Opt-in flag. When True the engine supplies a
    #: :class:`~crypto_bot.core.models.MarketContext` carrying non-OHLCV data (funding
    #: rate). Candle-only strategies leave this False, so adding derivatives data costs
    #: them nothing.
    wants_context: bool = False

    def __init__(self, params: dict | None = None) -> None:
        self.params = params or {}

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Minimum number of candles required before a signal is meaningful."""

    def new_state(self, symbol: str | None = None) -> StrategyState:
        """Fresh incremental state for one symbol. Override for O(1)-per-bar strategies.

        The default wraps a legacy :meth:`generate` implementation in a rolling window.
        (Each default is written in terms of the other, so a subclass overriding neither
        gets a TypeError here rather than infinite recursion.)
        """
        if type(self).generate is Strategy.generate:
            raise TypeError(
                f"{type(self).__name__} must implement new_state() or generate()"
            )
        window = max(self.warmup + 5, LEGACY_WINDOW)
        return WindowedState(self, symbol, window)

    def generate(
        self,
        candles: list[Candle],
        symbol: str | None = None,
        context: MarketContext | None = None,
    ) -> Signal:
        """Return the signal for the latest candle in ``candles`` (oldest-first).

        The default replays ``candles`` through a fresh :meth:`new_state`, so a streaming
        strategy gets this stateless convenience for free. Every candle shapes the
        indicators — this is the strategy's answer given exactly this history. ``context``
        applies to the last candle only. Returns ``HOLD`` when there is not enough data.
        """
        if type(self).new_state is Strategy.new_state:
            raise TypeError(
                f"{type(self).__name__} must implement new_state() or generate()"
            )
        state = self.new_state(symbol)
        signal = HOLD
        last = len(candles) - 1
        for i, candle in enumerate(candles):
            signal = state.update(candle, context if i == last else None)
        return signal

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}({self.params})"


class WindowedState(StrategyState):
    """Adapter running a legacy ``generate(candles)`` strategy over a rolling window."""

    __slots__ = ("_strategy", "_symbol", "_buffer")

    def __init__(self, strategy: Strategy, symbol: str | None, window: int) -> None:
        self._strategy = strategy
        self._symbol = symbol
        self._buffer: deque[Candle] = deque(maxlen=window)

    def update(self, candle: Candle, context: MarketContext | None = None) -> Signal:
        self._buffer.append(candle)
        candles = list(self._buffer)
        if self._strategy.wants_context:
            return self._strategy.generate(candles, self._symbol, context)
        return self._strategy.generate(candles, self._symbol)
