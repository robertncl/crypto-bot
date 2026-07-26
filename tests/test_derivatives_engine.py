"""Engine-level derivatives behaviour: short entries, stop-and-reverse, funding accrual.

Uses a stub strategy that emits a fixed signal, so these assert the *engine wiring* rather
than re-testing indicator maths.
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
from crypto_bot.core.broker import PaperBroker
from crypto_bot.core.engine import Engine
from crypto_bot.core.models import HOLD, Candle, PositionSide, Signal, SignalType
from crypto_bot.core.portfolio import Portfolio
from crypto_bot.exchanges.base import ExchangeAdapter
from crypto_bot.risk.manager import RiskManager
from crypto_bot.strategies.base import Strategy


class FakeExchange(ExchangeAdapter):
    name = "fake"

    def __init__(self, closes: list[float], funding: float | None = None):
        self.set_closes(closes)
        self.funding = funding

    def set_closes(self, closes: list[float]) -> None:
        self._candles = [
            Candle(1_000_000 + i * 60_000, c, c, c, c, 1.0) for i, c in enumerate(closes)
        ]

    def load_markets(self) -> dict:
        return {}

    def fetch_candles(self, symbol, timeframe, limit=200, since=None):
        return list(self._candles)

    def fetch_last_price(self, symbol):
        return self._candles[-1].close

    def fetch_funding_rate(self, symbol):
        return self.funding

    def fetch_balance(self):
        return {}

    def create_order(self, request):
        raise NotImplementedError("paper mode should not call the live exchange")

    def cancel_order(self, order_id, symbol):
        pass


class FixedSignal(Strategy):
    """Emits whatever it is told to, so engine wiring can be tested in isolation."""

    name = "fixed"

    def __init__(self, signal_type: SignalType):
        super().__init__({})
        self.signal_type = signal_type

    @property
    def warmup(self) -> int:
        return 1

    def generate(self, candles, symbol=None):
        if self.signal_type == SignalType.HOLD:
            return HOLD
        return Signal(self.signal_type, reason="stub")


def _config(*, allow_shorts=True, funding_rate=0.0, funding_hours=8.0, **risk_overrides):
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
        timeframe="1m",
        poll_seconds=60,
        strategy=StrategyConfig(name="fixed", params={}),
        risk=risk,
        paper=PaperConfig(
            starting_cash=1000.0, quote_currency="USDT", fee_rate=0.0, slippage_pct=0.0
        ),
        logging=LoggingConfig(level="ERROR", file=None),
        derivatives=DerivativesConfig(
            allow_shorts=allow_shorts,
            funding_interval_hours=funding_hours,
            funding_rate=funding_rate,
        ),
    )


def _engine(exchange, config, strategy):
    portfolio = Portfolio(
        cash=config.paper.starting_cash,
        quote_currency="USDT",
        allow_shorts=config.derivatives.allow_shorts,
    )
    engine = Engine(config, exchange, strategy, RiskManager(config.risk), portfolio)
    engine.broker = PaperBroker(engine.last_price, fee_rate=0.0, slippage_pct=0.0)
    return engine


def test_sell_signal_opens_a_short_when_enabled():
    engine = _engine(FakeExchange([10, 10, 10]), _config(), FixedSignal(SignalType.SELL))
    engine.run_once()

    pos = engine.portfolio.positions["BTC/USDT"]
    assert pos.side == PositionSide.SHORT
    assert pos.amount == pytest.approx(50.0)  # 50% of 1000 equity at price 10
    assert engine.portfolio.cash == pytest.approx(500.0)


def test_sell_signal_is_a_noop_when_shorts_disabled():
    # Spot behaviour must be untouched: a SELL with nothing held does nothing at all.
    engine = _engine(
        FakeExchange([10, 10, 10]), _config(allow_shorts=False), FixedSignal(SignalType.SELL)
    )
    engine.run_once()

    assert not engine.portfolio.has_position("BTC/USDT")
    assert engine.portfolio.cash == pytest.approx(1000.0)


def test_short_profits_as_price_falls():
    exchange = FakeExchange([10, 10, 10])
    engine = _engine(exchange, _config(), FixedSignal(SignalType.SELL))
    engine.run_once()  # short 50 @ 10

    exchange.set_closes([10, 10, 8])
    assert engine.portfolio.equity({"BTC/USDT": 8.0}) == pytest.approx(1100.0)  # +50*2


def test_buy_signal_reverses_an_open_short():
    exchange = FakeExchange([10, 10, 10])
    engine = _engine(exchange, _config(), FixedSignal(SignalType.SELL))
    engine.run_once()
    assert engine.portfolio.positions["BTC/USDT"].is_short

    # Flip the strategy: a BUY must cover the short and open a long (stop-and-reverse).
    engine.strategy = FixedSignal(SignalType.BUY)
    exchange.set_closes([10, 10, 8])
    engine.run_once()

    pos = engine.portfolio.positions["BTC/USDT"]
    assert pos.side == PositionSide.LONG
    assert pos.entry_price == pytest.approx(8.0)
    assert engine.portfolio.realized_pnl == pytest.approx(100.0)  # covered 50 @ 8 from 10


def test_repeated_same_side_signal_does_not_stack_positions():
    exchange = FakeExchange([10, 10, 10])
    engine = _engine(exchange, _config(), FixedSignal(SignalType.SELL))
    engine.run_once()
    amount = engine.portfolio.positions["BTC/USDT"].amount

    engine.run_once()  # same signal again
    assert engine.portfolio.positions["BTC/USDT"].amount == pytest.approx(amount)


def test_short_stop_loss_closes_on_a_rally():
    exchange = FakeExchange([10, 10, 10])
    engine = _engine(exchange, _config(stop_loss_pct=0.05), FixedSignal(SignalType.SELL))
    engine.run_once()
    assert engine.portfolio.has_position("BTC/USDT")

    exchange.set_closes([10, 10, 11])  # +10% against the short
    engine.strategy = FixedSignal(SignalType.HOLD)
    engine.run_once()

    assert not engine.portfolio.has_position("BTC/USDT")
    assert engine.portfolio.realized_pnl < 0


def test_engine_settles_funding_once_per_interval():
    # One-minute funding interval and one-minute bars: each new bar is one settlement.
    exchange = FakeExchange([10, 10, 10], funding=0.001)
    config = _config(funding_hours=1 / 60)
    engine = _engine(exchange, config, FixedSignal(SignalType.SELL))

    engine.run_once()  # opens the short; first cycle only anchors the funding clock
    assert engine.portfolio.funding_paid == pytest.approx(0.0)

    exchange.set_closes([10, 10, 10, 10])  # one more bar => one interval elapsed
    engine.strategy = FixedSignal(SignalType.HOLD)
    engine.run_once()

    # Short of 50 @ 10 = 500 notional; +0.1% funding is collected by the short.
    assert engine.portfolio.funding_paid == pytest.approx(-0.5)
    assert engine.portfolio.cash == pytest.approx(500.5)


def test_funding_charges_a_long():
    exchange = FakeExchange([10, 10, 10], funding=0.001)
    engine = _engine(exchange, _config(funding_hours=1 / 60), FixedSignal(SignalType.BUY))
    engine.run_once()

    exchange.set_closes([10, 10, 10, 10])
    engine.strategy = FixedSignal(SignalType.HOLD)
    engine.run_once()

    assert engine.portfolio.funding_paid == pytest.approx(0.5)  # long pays


def test_funding_falls_back_to_the_configured_rate():
    # Venue exposes no funding rate (spot-style adapter) -> configured rate is used.
    exchange = FakeExchange([10, 10, 10], funding=None)
    config = _config(funding_hours=1 / 60, funding_rate=0.002)
    engine = _engine(exchange, config, FixedSignal(SignalType.SELL))
    engine.run_once()

    exchange.set_closes([10, 10, 10, 10])
    engine.strategy = FixedSignal(SignalType.HOLD)
    engine.run_once()

    assert engine.portfolio.funding_paid == pytest.approx(-1.0)  # 500 * 0.002, collected
