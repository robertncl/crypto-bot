"""End-to-end engine tests using an in-memory fake exchange (no ccxt, no network)."""

from __future__ import annotations

import pytest

from crypto_bot.config import (
    BotConfig,
    ExchangeConfig,
    LoggingConfig,
    PaperConfig,
    RiskConfig,
    StrategyConfig,
)
from crypto_bot.core.broker import PaperBroker
from crypto_bot.core.engine import Engine
from crypto_bot.core.models import Candle, OrderRequest
from crypto_bot.core.portfolio import Portfolio
from crypto_bot.exchanges.base import ExchangeAdapter
from crypto_bot.risk.manager import RiskManager
from crypto_bot.strategies.ma_crossover import MACrossover


class FakeExchange(ExchangeAdapter):
    name = "fake"

    def __init__(self, closes: list[float]):
        self.set_closes(closes)

    def set_closes(self, closes: list[float]) -> None:
        self._candles = [
            Candle(1_000_000 + i * 60_000, c, c, c, c, 1.0) for i, c in enumerate(closes)
        ]

    def load_markets(self) -> dict:
        return {}

    def fetch_candles(self, symbol, timeframe, limit=200):
        return list(self._candles)

    def fetch_last_price(self, symbol):
        return self._candles[-1].close

    def fetch_balance(self):
        return {}

    def create_order(self, request: OrderRequest):
        raise NotImplementedError("paper mode should not call the live exchange")

    def cancel_order(self, order_id, symbol):
        pass


def _config(**risk_overrides) -> BotConfig:
    risk = RiskConfig(
        position_pct=0.5,
        max_open_positions=3,
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        max_drawdown_pct=0.0,
    )
    for k, v in risk_overrides.items():
        setattr(risk, k, v)
    return BotConfig(
        mode="paper",
        exchange=ExchangeConfig(name="fake"),
        symbols=["BTC/USDT"],
        timeframe="1h",
        poll_seconds=60,
        strategy=StrategyConfig(
            name="ma_crossover",
            params={"fast_period": 2, "slow_period": 4, "ma_type": "sma"},
        ),
        risk=risk,
        paper=PaperConfig(starting_cash=1000.0, quote_currency="USDT", fee_rate=0.0,
                          slippage_pct=0.0),
        logging=LoggingConfig(level="INFO", file=None),
    )


def _engine(exchange, config) -> Engine:
    strategy = MACrossover(config.strategy.params)
    portfolio = Portfolio(cash=config.paper.starting_cash, quote_currency="USDT")
    engine = Engine(config, exchange, strategy, RiskManager(config.risk), portfolio)
    engine.broker = PaperBroker(engine.last_price, fee_rate=0.0, slippage_pct=0.0)
    return engine


def test_engine_opens_position_on_buy_signal():
    exchange = FakeExchange([10, 9, 8, 7, 6, 5, 7, 10])  # triggers BUY on last bar
    config = _config()
    engine = _engine(exchange, config)

    engine.run_once()

    assert engine._last_prices["BTC/USDT"] == 10
    pos = engine.portfolio.positions["BTC/USDT"]
    # 50% of 1000 equity at price 10 => 50 units
    assert pos.amount == 50.0
    assert pos.entry_price == 10.0
    assert engine.portfolio.cash == 500.0


def test_engine_protective_stop_loss_closes_position():
    exchange = FakeExchange([10, 9, 8, 7, 6, 5, 7, 10])
    config = _config(stop_loss_pct=0.05)
    engine = _engine(exchange, config)

    engine.run_once()  # opens at price 10
    assert engine.portfolio.has_position("BTC/USDT")

    # Price collapses to 9 (-10% vs entry) -> stop-loss should flatten the position.
    exchange.set_closes([10, 10, 10, 10, 10, 10, 10, 9])
    engine.run_once()

    assert not engine.portfolio.has_position("BTC/USDT")
    assert engine.portfolio.realized_pnl < 0


def test_engine_trailing_stop_locks_in_profit():
    exchange = FakeExchange([10, 9, 8, 7, 6, 5, 7, 10])  # BUY at 10
    config = _config(trailing_stop_pct=0.05)
    engine = _engine(exchange, config)
    engine.run_once()
    assert engine.portfolio.has_position("BTC/USDT")

    # Price runs to 20 (engine ratchets the peak), then pulls back 10% — the trailing
    # stop flattens the position well above the 10.0 entry.
    exchange.set_closes([10, 10, 10, 10, 10, 10, 10, 20])
    engine.run_once()
    assert engine.portfolio.positions["BTC/USDT"].peak_price == 20.0

    exchange.set_closes([10, 10, 10, 10, 10, 10, 20, 18])
    engine.run_once()
    assert not engine.portfolio.has_position("BTC/USDT")
    assert engine.portfolio.realized_pnl > 0  # sold at 18 vs entry 10


def test_engine_respects_drawdown_kill_switch():
    exchange = FakeExchange([10, 9, 8, 7, 6, 5, 7, 10])
    # Kill-switch trips immediately; no position should be opened.
    config = _config(max_drawdown_pct=0.0001)
    engine = _engine(exchange, config)
    engine.risk.update_equity(10_000.0)  # pretend we had a much higher peak

    engine.run_once()

    assert not engine.portfolio.has_position("BTC/USDT")


def test_engine_dca_averages_in_up_to_the_position_cap():
    # DCA with averaging-in on: each new candle adds a 10%-of-equity tranche to the same
    # symbol (weighted-average entry), and the per-symbol cap halts growth at 30% of equity.
    from crypto_bot.strategies.dca import DCA

    config = _config(
        position_pct=0.10,
        allow_averaging_in=True,
        max_position_pct=0.30,
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        max_drawdown_pct=0.0,
    )
    config.strategy = StrategyConfig(name="dca", params={"every": 1})

    exchange = FakeExchange([10, 10])
    strategy = DCA(config.strategy.params)
    portfolio = Portfolio(cash=1000.0, quote_currency="USDT")
    engine = Engine(config, exchange, strategy, RiskManager(config.risk), portfolio)
    engine.broker = PaperBroker(engine.last_price, fee_rate=0.0, slippage_pct=0.0)

    for n in range(2, 8):  # a fresh candle each cycle
        exchange.set_closes([10] * n)
        engine.run_once()

    pos = portfolio.positions["BTC/USDT"]
    assert pos.notional(10.0) == pytest.approx(300.0)  # capped at 30% of 1000 equity
    assert pos.entry_price == pytest.approx(10.0)
    assert portfolio.cash == pytest.approx(700.0)


def test_engine_runs_a_registered_breakout_strategy():
    # Proves a newly-added strategy flows registry -> engine -> risk -> broker -> portfolio,
    # not just in isolation. The engine is strategy-agnostic, so this guards the wiring.
    from crypto_bot.strategies.registry import build_strategy

    exchange = FakeExchange([5, 5, 5, 5, 10])  # close breaks the prior 3-bar high -> BUY
    config = _config()
    config.strategy = StrategyConfig(name="breakout", params={"lookback": 3})

    strategy = build_strategy(config.strategy.name, config.strategy.params)
    portfolio = Portfolio(cash=config.paper.starting_cash, quote_currency="USDT")
    engine = Engine(config, exchange, strategy, RiskManager(config.risk), portfolio)
    engine.broker = PaperBroker(engine.last_price, fee_rate=0.0, slippage_pct=0.0)

    engine.run_once()

    pos = engine.portfolio.positions["BTC/USDT"]
    assert pos.amount == 50.0  # 50% of 1000 equity at price 10
    assert pos.entry_price == 10.0
