"""build_engine(): wires exchange + strategy + risk + portfolio + broker from config.

The exchange adapter is monkeypatched at the point core.engine imports it, so this
never touches ccxt or the network.
"""

from __future__ import annotations

import pytest

from crypto_bot.config import (
    BotConfig,
    DerivativesConfig,
    ExchangeConfig,
    LoggingConfig,
    PaperConfig,
    RiskConfig,
    StrategyConfig,
)
from crypto_bot.core.broker import LiveBroker, PaperBroker
from crypto_bot.core.engine import _validate_symbols, build_engine
from crypto_bot.exchanges.base import ExchangeError


class _FakeExchange:
    name = "fake"

    def __init__(self, markets=None, balance=None):
        self._markets = markets if markets is not None else {"BTC/USDT": {}}
        self._balance = balance or {}

    def load_markets(self):
        return self._markets

    def fetch_balance(self):
        return self._balance

    def close(self):
        pass


def _config(mode="paper", quote_currency="USDT") -> BotConfig:
    return BotConfig(
        mode=mode,
        exchange=ExchangeConfig(name="fake"),
        symbols=["BTC/USDT"],
        timeframe="1h",
        poll_seconds=60,
        strategy=StrategyConfig(name="ma_crossover", params={"fast_period": 2, "slow_period": 4}),
        risk=RiskConfig(),
        paper=PaperConfig(starting_cash=1000.0, quote_currency=quote_currency),
        logging=LoggingConfig(),
        derivatives=DerivativesConfig(),
    )


def test_build_engine_wires_a_paper_broker_by_default(monkeypatch):
    exchange = _FakeExchange()
    monkeypatch.setattr("crypto_bot.core.engine.build_exchange", lambda *a, **k: exchange)

    engine = build_engine(_config())

    assert isinstance(engine.broker, PaperBroker)
    assert engine.portfolio.cash == 1000.0
    assert engine.portfolio.quote_currency == "USDT"


def test_build_engine_wires_a_live_broker_and_seeds_cash_from_the_exchange(monkeypatch):
    exchange = _FakeExchange(balance={"USDT": 555.0})
    monkeypatch.setattr("crypto_bot.core.engine.build_exchange", lambda *a, **k: exchange)

    engine = build_engine(_config(mode="live"))

    assert isinstance(engine.broker, LiveBroker)
    assert engine.portfolio.cash == 555.0


def test_build_engine_seeds_zero_cash_when_the_quote_currency_balance_is_absent(monkeypatch):
    exchange = _FakeExchange(balance={"BTC": 0.5})  # no USDT entry
    monkeypatch.setattr("crypto_bot.core.engine.build_exchange", lambda *a, **k: exchange)

    engine = build_engine(_config(mode="live"))

    assert engine.portfolio.cash == 0.0


def test_build_engine_skips_symbol_validation_when_markets_are_empty(monkeypatch):
    # An adapter that returns no market metadata (e.g. the backtest ReplayExchange)
    # can't validate symbols, so build_engine must not reject anything in that case.
    exchange = _FakeExchange(markets={})
    monkeypatch.setattr("crypto_bot.core.engine.build_exchange", lambda *a, **k: exchange)

    engine = build_engine(_config())
    assert engine.config.symbols == ["BTC/USDT"]


def test_build_engine_rejects_a_symbol_the_exchange_does_not_list(monkeypatch):
    exchange = _FakeExchange(markets={"ETH/USDT": {}})  # BTC/USDT is not listed
    monkeypatch.setattr("crypto_bot.core.engine.build_exchange", lambda *a, **k: exchange)

    with pytest.raises(ExchangeError, match="not available on this exchange"):
        build_engine(_config())


def test_validate_symbols_noop_with_no_market_metadata():
    _validate_symbols(["BTC/USDT"], {}, __import__("logging").getLogger("test"))  # must not raise


def test_validate_symbols_passes_when_all_symbols_are_known():
    _validate_symbols(["BTC/USDT"], {"BTC/USDT": {}}, __import__("logging").getLogger("test"))
