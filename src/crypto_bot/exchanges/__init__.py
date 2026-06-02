"""Exchange abstraction layer.

``ExchangeAdapter`` is the unified interface the rest of the bot codes against.
``CCXTAdapter`` implements it on top of the ccxt library, giving instant access to
Binance, Bybit, Coinbase and 100+ other venues. To add a non-ccxt venue, implement
``ExchangeAdapter`` directly.
"""

from crypto_bot.exchanges.base import ExchangeAdapter, ExchangeError

__all__ = ["ExchangeAdapter", "ExchangeError"]
