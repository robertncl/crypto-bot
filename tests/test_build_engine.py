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


# -- exchange-side leverage on perps (live only) ----------------------------------
PERP = "BTC/USDT:USDT"


class _PerpExchange(_FakeExchange):
    def __init__(self, leverage):
        super().__init__(markets={PERP: {"contract": True}, "ETH/USDT": {"contract": False}},
                         balance={"USDT": 500.0})
        self.leverage = leverage
        self.asked: list[str] = []

    def fetch_leverage(self, symbol):
        self.asked.append(symbol)
        return self.leverage


def _perp_config(mode="live", **derivatives) -> BotConfig:
    config = _config(mode=mode)
    config.symbols = [PERP, "ETH/USDT"]
    config.derivatives = DerivativesConfig(allow_shorts=True, **derivatives)
    return config


def _build(monkeypatch, exchange, config):
    monkeypatch.setattr("crypto_bot.core.engine.build_exchange", lambda *a, **k: exchange)
    return build_engine(config)


def test_live_perps_above_1x_exchange_leverage_refuse_to_start(monkeypatch):
    exchange = _PerpExchange(leverage=20.0)
    with pytest.raises(ExchangeError, match=r"BTC/USDT:USDT \(20x\).*liquidate.*1x"):
        _build(monkeypatch, exchange, _perp_config())
    assert exchange.asked == [PERP]  # only contract markets are checked


def test_live_perps_at_1x_start_normally(monkeypatch):
    engine = _build(monkeypatch, _PerpExchange(leverage=1.0), _perp_config())
    assert isinstance(engine.broker, LiveBroker)


def test_exchange_leverage_can_be_explicitly_accepted(monkeypatch, caplog):
    config = _perp_config(allow_exchange_leverage=True)
    engine = _build(monkeypatch, _PerpExchange(leverage=10.0), config)
    assert isinstance(engine.broker, LiveBroker)
    assert "leverage above 1x" in caplog.text


def test_unreadable_exchange_leverage_warns_and_starts(monkeypatch, caplog):
    engine = _build(monkeypatch, _PerpExchange(leverage=None), _perp_config())
    assert isinstance(engine.broker, LiveBroker)
    assert "could not read BTC/USDT:USDT leverage" in caplog.text


def test_paper_mode_never_checks_exchange_leverage(monkeypatch):
    exchange = _PerpExchange(leverage=50.0)
    engine = _build(monkeypatch, exchange, _perp_config(mode="paper"))
    assert isinstance(engine.broker, PaperBroker) and exchange.asked == []


def test_allow_exchange_leverage_is_parsed_from_config(tmp_path):
    from crypto_bot.config import load_config

    path = tmp_path / "c.yaml"
    path.write_text(
        "exchange: {name: binance}\nsymbols: [BTC/USDT]\nstrategy: {name: dca}\n"
        "derivatives: {allow_exchange_leverage: true}\n"
    )
    assert load_config(path).derivatives.allow_exchange_leverage is True
    assert DerivativesConfig().allow_exchange_leverage is False
