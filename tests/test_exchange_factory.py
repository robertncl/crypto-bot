"""crypto_bot.exchanges.factory: env-var credential lookup and adapter construction.

No network and no real ccxt client: build_exchange is exercised end-to-end (it really
does construct a CCXTAdapter around ccxt's `binance` class), but ccxt itself never
opens a connection until a method that hits the network is called, which none of
these tests do.
"""

from __future__ import annotations

import pytest

from crypto_bot.config import ExchangeConfig
from crypto_bot.exchanges.base import ExchangeError
from crypto_bot.exchanges.factory import _env, build_exchange


def test_env_reads_the_uppercased_exchange_prefixed_variable(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "  abc123  ")
    assert _env("binance", "KEY") == "abc123"  # stripped


def test_env_missing_variable_is_none(monkeypatch):
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    assert _env("kraken", "SECRET") is None


def test_env_blank_variable_strips_to_empty_string(monkeypatch):
    # Truthy but blank input strips to "" rather than None; build_exchange's
    # `api_key and secret` check treats that "" as falsy either way.
    monkeypatch.setenv("KRAKEN_API_SECRET", "   ")
    assert _env("kraken", "SECRET") == ""


def test_build_exchange_without_credentials_does_not_require_them(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    adapter = build_exchange(ExchangeConfig(name="binance"), require_credentials=False)
    assert adapter.name == "binance"
    assert adapter.has_credentials is False


def test_build_exchange_passes_credentials_and_options_through(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "key123")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret123")
    adapter = build_exchange(
        ExchangeConfig(name="binance", sandbox=False, options={"defaultType": "spot"}),
        require_credentials=True,
    )
    assert adapter.has_credentials is True
    assert adapter.client.options.get("defaultType") == "spot"


def test_build_exchange_requires_credentials_in_live_mode(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    with pytest.raises(ExchangeError, match="live mode needs API credentials"):
        build_exchange(ExchangeConfig(name="binance"), require_credentials=True)


def test_build_exchange_requires_both_key_and_secret(monkeypatch):
    # A key with no secret must still be treated as "not enough" for live mode.
    monkeypatch.setenv("BINANCE_API_KEY", "key-only")
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    with pytest.raises(ExchangeError):
        build_exchange(ExchangeConfig(name="binance"), require_credentials=True)
