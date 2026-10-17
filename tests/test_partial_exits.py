"""Engine handling of scale-out signals: ``Signal.fraction`` and ``Signal.reduce_only``."""

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


class _Venue(ExchangeAdapter):
    name = "fake"

    def __init__(self, price: float = 10.0):
        self.price = price

    def load_markets(self):
        return {}

    def fetch_candles(self, symbol, timeframe, limit=200, since=None):
        p = self.price
        return [Candle(1_000_000, p, p, p, p, 1.0)]

    def fetch_last_price(self, symbol):
        return self.price

    def fetch_balance(self):
        return {}

    def create_order(self, request):
        raise NotImplementedError

    def cancel_order(self, order_id, symbol):
        pass


class _Next(Strategy):
    """Emits whatever signal it was last handed."""

    name = "next"
    warmup = 1

    def __init__(self):
        super().__init__({})
        self.signal = HOLD

    def generate(self, candles, symbol=None):
        return self.signal


def _engine(allow_shorts=False):
    config = BotConfig(
        mode="paper", exchange=ExchangeConfig(name="fake"), symbols=["BTC/USDT"],
        timeframe="1h", poll_seconds=60, strategy=StrategyConfig(name="next"),
        risk=RiskConfig(position_pct=0.5, stop_loss_pct=0.0, take_profit_pct=0.0,
                        max_drawdown_pct=0.0, allow_averaging_in=True),
        paper=PaperConfig(starting_cash=1000.0, fee_rate=0.0, slippage_pct=0.0),
        logging=LoggingConfig(level="ERROR"),
        derivatives=DerivativesConfig(allow_shorts=allow_shorts),
    )
    strategy = _Next()
    engine = Engine(config, _Venue(), strategy, RiskManager(config.risk),
                    Portfolio(cash=1000.0, allow_shorts=allow_shorts))
    engine.broker = PaperBroker(engine.last_price, fee_rate=0.0, slippage_pct=0.0)
    return engine, strategy


def _cycle(engine, strategy, signal):
    strategy.signal = signal
    engine.run_once()


def test_a_fractional_exit_closes_that_share_of_the_position():
    engine, strategy = _engine()
    _cycle(engine, strategy, Signal(SignalType.BUY, "in"))  # 50 units @ 10
    _cycle(engine, strategy, Signal(SignalType.SELL, "trim", fraction=0.4, reduce_only=True))
    assert engine.portfolio.positions["BTC/USDT"].amount == pytest.approx(30.0)
    assert engine.portfolio.cash == pytest.approx(500.0 + 200.0)


def test_a_full_exit_leaves_no_dust():
    engine, strategy = _engine()
    _cycle(engine, strategy, Signal(SignalType.BUY, "in"))
    _cycle(engine, strategy, Signal(SignalType.SELL, "out", fraction=1.0, reduce_only=True))
    assert "BTC/USDT" not in engine.portfolio.positions


def test_reduce_only_never_reverses_into_a_short():
    engine, strategy = _engine(allow_shorts=True)
    _cycle(engine, strategy, Signal(SignalType.BUY, "in"))
    _cycle(engine, strategy, Signal(SignalType.SELL, "out", reduce_only=True))
    assert not engine.portfolio.positions  # closed, but no short opened

    # Without reduce_only the same SELL stop-and-reverses, as before.
    _cycle(engine, strategy, Signal(SignalType.BUY, "in"))
    _cycle(engine, strategy, Signal(SignalType.SELL, "flip"))
    assert engine.portfolio.positions["BTC/USDT"].side == PositionSide.SHORT


def test_reduce_only_never_opens_or_adds():
    engine, strategy = _engine(allow_shorts=True)
    _cycle(engine, strategy, Signal(SignalType.SELL, "nothing held", reduce_only=True))
    _cycle(engine, strategy, Signal(SignalType.BUY, "nothing held", reduce_only=True))
    assert not engine.portfolio.positions

    _cycle(engine, strategy, Signal(SignalType.BUY, "in"))
    _cycle(engine, strategy, Signal(SignalType.BUY, "agrees", reduce_only=True))
    assert engine.portfolio.positions["BTC/USDT"].amount == pytest.approx(50.0)  # not added


@pytest.mark.parametrize("fraction", [0.0, -0.5, 1.5])
def test_signal_fraction_must_be_in_range(fraction):
    with pytest.raises(ValueError, match="fraction"):
        Signal(SignalType.SELL, fraction=fraction)
