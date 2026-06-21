"""Strategy registry: map config names to strategy classes.

Register a new strategy with the :func:`register_strategy` decorator, then reference it
by ``name`` in config. ``build_strategy`` is what the engine calls at startup.
"""

from __future__ import annotations

from crypto_bot.strategies.base import Strategy
from crypto_bot.strategies.bollinger import BollingerReversion
from crypto_bot.strategies.breakout import Breakout
from crypto_bot.strategies.dca import DCA
from crypto_bot.strategies.ma_crossover import MACrossover
from crypto_bot.strategies.macd import MACDMomentum
from crypto_bot.strategies.rsi_reversion import RSIReversion
from crypto_bot.strategies.supertrend import Supertrend

_REGISTRY: dict[str, type[Strategy]] = {}


def register_strategy(cls: type[Strategy]) -> type[Strategy]:
    """Class decorator that registers a strategy under its ``name`` attribute."""
    key = cls.name.lower()
    if key in _REGISTRY and _REGISTRY[key] is not cls:
        raise ValueError(f"strategy name {cls.name!r} is already registered")
    _REGISTRY[key] = cls
    return cls


def build_strategy(name: str, params: dict | None = None) -> Strategy:
    key = name.lower()
    if key not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise KeyError(f"unknown strategy {name!r}; available: {available}")
    return _REGISTRY[key](params)


def available_strategies() -> list[str]:
    return sorted(_REGISTRY)


# Built-in strategies.
register_strategy(MACrossover)
register_strategy(RSIReversion)
register_strategy(Breakout)
register_strategy(BollingerReversion)
register_strategy(MACDMomentum)
register_strategy(Supertrend)
register_strategy(DCA)
