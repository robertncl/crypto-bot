"""Build a configured :class:`ExchangeAdapter` from config + environment secrets.

API keys are read from environment variables (loaded from ``.env``) named after the
exchange, e.g. ``BINANCE_API_KEY`` / ``BINANCE_API_SECRET`` /
``BINANCE_API_PASSWORD``. Keys are only required when ``require_credentials`` is True
(i.e. live mode).
"""

from __future__ import annotations

import os

from crypto_bot.config import ExchangeConfig
from crypto_bot.exchanges.base import ExchangeAdapter, ExchangeError


def _env(exchange_name: str, suffix: str) -> str | None:
    value = os.environ.get(f"{exchange_name.upper()}_API_{suffix}")
    return value.strip() if value else None


def build_exchange(
    cfg: ExchangeConfig, *, require_credentials: bool = False
) -> ExchangeAdapter:
    # Imported here so unit tests and paper logic don't require ccxt to be installed.
    from crypto_bot.exchanges.ccxt_adapter import CCXTAdapter

    api_key = _env(cfg.name, "KEY")
    secret = _env(cfg.name, "SECRET")
    password = _env(cfg.name, "PASSWORD")

    if require_credentials and not (api_key and secret):
        raise ExchangeError(
            f"live mode needs API credentials for {cfg.name}. Set "
            f"{cfg.name.upper()}_API_KEY and {cfg.name.upper()}_API_SECRET in your .env "
            "(copy from .env.example)."
        )

    return CCXTAdapter(
        cfg.name,
        api_key=api_key,
        secret=secret,
        password=password,
        sandbox=cfg.sandbox,
        options=cfg.options,
    )
