"""Engine wiring of the volatility-aware risk layer and completed-candle signals."""

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

HOUR = 3_600_000


class _Venue(ExchangeAdapter):
    """Serves the most recent ``limit`` candles and records the limit asked for."""

    name = "fake"

    def __init__(self, candles: list[Candle]):
        self.candles = candles
        self.limits: list[int] = []

    def load_markets(self):
        return {}

    def fetch_candles(self, symbol, timeframe, limit=200, since=None):
        self.limits.append(limit)
        return self.candles[-limit:]

    def fetch_last_price(self, symbol):
        return self.candles[-1].close

    def fetch_balance(self):
        return {}

    def create_order(self, request):
        raise NotImplementedError

    def cancel_order(self, order_id, symbol):
        pass


class _Scripted(Strategy):
    """Emits the signal it was handed, and records the candles it was shown."""

    name = "scripted"
    warmup = 1

    def __init__(self, signal=HOLD):
        super().__init__({})
        self.signal = signal
        self.seen: list[list[Candle]] = []

    def generate(self, candles, symbol=None):
        self.seen.append(candles)
        return self.signal


def _candles(closes, start=0, spread=1.0):
    """Hourly candles whose high/low sit ``spread`` either side of the close."""
    return [Candle(start + i * HOUR, c, c + spread, c - spread, c, 1.0)
            for i, c in enumerate(closes)]


def _engine(venue, strategy, *, shorts=False, signal_on_close=False, timeframe="1h", **risk):
    base = dict(position_pct=0.5, max_open_positions=3, stop_loss_pct=0.0, take_profit_pct=0.0,
                max_drawdown_pct=0.0)
    config = BotConfig(
        mode="paper", exchange=ExchangeConfig(name="fake"), symbols=["BTC/USDT"],
        timeframe=timeframe, poll_seconds=60, strategy=StrategyConfig(name="scripted"),
        risk=RiskConfig(**{**base, **risk}),
        paper=PaperConfig(starting_cash=10_000.0, fee_rate=0.0, slippage_pct=0.0),
        logging=LoggingConfig(level="ERROR"),
        derivatives=DerivativesConfig(allow_shorts=shorts),
        signal_on_close=signal_on_close,
    )
    engine = Engine(config, venue, strategy, RiskManager(config.risk),
                    Portfolio(cash=10_000.0, allow_shorts=shorts))
    engine.broker = PaperBroker(engine.last_price, fee_rate=0.0, slippage_pct=0.0)
    return engine


BUY = Signal(SignalType.BUY, "go")
SELL = Signal(SignalType.SELL, "go")


# -- trend filter -------------------------------------------------------------------
def test_trend_filter_blocks_longs_below_the_average_and_allows_them_above():
    falling = _candles([200.0 - i for i in range(60)])  # last close far below its average
    engine = _engine(_Venue(falling), _Scripted(BUY), trend_filter_period=50)
    engine.run_once()
    assert not engine.portfolio.positions

    rising = _candles([100.0 + i for i in range(60)])
    engine = _engine(_Venue(rising), _Scripted(BUY), trend_filter_period=50)
    engine.run_once()
    assert engine.portfolio.has_position("BTC/USDT")


def test_trend_filter_gates_shorts_the_other_way():
    rising = _candles([100.0 + i for i in range(60)])
    engine = _engine(_Venue(rising), _Scripted(SELL), shorts=True, trend_filter_period=50)
    engine.run_once()
    assert not engine.portfolio.positions  # no shorting an uptrend

    falling = _candles([200.0 - i for i in range(60)])
    engine = _engine(_Venue(falling), _Scripted(SELL), shorts=True, trend_filter_period=50)
    engine.run_once()
    assert engine.portfolio.positions["BTC/USDT"].side == PositionSide.SHORT


def test_trend_filter_never_blocks_an_exit():
    rising = _candles([100.0 + i for i in range(60)])
    venue = _Venue(rising)
    strategy = _Scripted(BUY)
    engine = _engine(venue, strategy, trend_filter_period=50)
    engine.run_once()
    venue.candles = _candles([200.0 - i for i in range(60)])  # now a downtrend
    strategy.signal = SELL
    engine.run_once()
    assert not engine.portfolio.positions


def test_trend_filter_blocks_entries_until_there_is_enough_history():
    engine = _engine(_Venue(_candles([100.0 + i for i in range(20)])), _Scripted(BUY),
                     trend_filter_period=50)
    engine.run_once()
    assert not engine.portfolio.positions


# -- candle window ------------------------------------------------------------------
def test_engine_fetches_enough_for_the_filter_but_the_strategy_window_is_unchanged():
    venue = _Venue(_candles([100.0 + i * 0.1 for i in range(700)]))
    strategy = _Scripted()
    engine = _engine(venue, strategy, trend_filter_period=500)
    engine.run_once()
    assert venue.limits == [501]
    assert len(strategy.seen[0]) == 200  # same window as with no filter at all
    assert strategy.seen[0][-1] is venue.candles[-1]

    plain = _Venue(venue.candles)
    _engine(plain, _Scripted()).run_once()
    assert plain.limits == [200]


# -- ATR ------------------------------------------------------------------------------
def test_entries_are_stamped_with_atr_and_stopped_by_it():
    # Candles span 2.0 high-to-low with flat closes: true range, and so ATR, is 2.0.
    venue = _Venue(_candles([100.0] * 80))
    strategy = _Scripted(BUY)
    engine = _engine(venue, strategy, atr_stop_mult=3.0)
    engine.run_once()
    position = engine.portfolio.positions["BTC/USDT"]
    assert position.atr == pytest.approx(2.0)

    strategy.signal = HOLD
    venue.candles = _candles([100.0] * 79 + [94.5])  # 5.5 below entry: inside 3 ATR
    engine.run_once()
    assert engine.portfolio.has_position("BTC/USDT")
    venue.candles = _candles([100.0] * 79 + [93.9])  # 6.1 below: through the ATR stop
    engine.run_once()
    assert not engine.portfolio.positions


def test_risk_based_sizing_uses_the_current_atr():
    venue = _Venue(_candles([100.0] * 80))
    engine = _engine(venue, _Scripted(BUY), atr_stop_mult=3.0, risk_per_trade_pct=0.01)
    engine.run_once()
    # Risk 1% of 10,000 = 100 over a 6.0 stop distance => 16.67 units.
    assert engine.portfolio.positions["BTC/USDT"].amount == pytest.approx(100.0 / 6.0)


def test_atr_is_left_alone_when_no_atr_setting_is_on():
    engine = _engine(_Venue(_candles([100.0] * 80)), _Scripted(BUY))
    engine.run_once()
    assert engine.portfolio.positions["BTC/USDT"].atr == 0.0


def test_atr_unknown_with_too_little_history_blocks_risk_based_entries():
    engine = _engine(_Venue(_candles([100.0] * 5)), _Scripted(BUY),
                     atr_stop_mult=3.0, risk_per_trade_pct=0.01)
    engine.run_once()
    assert not engine.portfolio.positions


# -- signal_on_close ------------------------------------------------------------------
def _closed_engine(venue, strategy, now_ms, **risk):
    engine = _engine(venue, strategy, signal_on_close=True, **risk)
    engine._clock = lambda: now_ms / 1000
    return engine


def test_signal_on_close_keeps_the_forming_candle_from_the_strategy():
    candles = _candles([100.0, 101.0, 102.0, 150.0])  # the last one is still forming
    strategy = _Scripted()
    engine = _closed_engine(_Venue(candles), strategy, now_ms=3 * HOUR + 60_000)
    engine.run_once()
    assert [c.close for c in strategy.seen[0]] == [100.0, 101.0, 102.0]
    assert engine.last_price("BTC/USDT") == 150.0  # ...but it still prices the book


def test_signal_on_close_decides_once_per_completed_candle():
    venue = _Venue(_candles([100.0, 101.0, 102.0]))
    strategy = _Scripted(BUY)
    engine = _closed_engine(venue, strategy, now_ms=2 * HOUR + 60_000, stop_loss_pct=0.05)
    engine.run_once()  # candle 1 is the newest completed one: BUY at the latest price
    assert engine.portfolio.has_position("BTC/USDT") and len(strategy.seen) == 1

    engine.run_once()  # same completed candle: the strategy is not asked again
    assert len(strategy.seen) == 1

    # The forming candle collapses: the stop still fires on this poll...
    venue.candles = _candles([100.0, 101.0, 90.0])
    engine.run_once()
    assert not engine.portfolio.positions
    # ...and the stale BUY from candle 1 does not re-enter on the next one.
    engine.run_once()
    assert not engine.portfolio.positions and len(strategy.seen) == 1

    # A new candle completes: now there is something new to decide.
    venue.candles = _candles([100.0, 101.0, 90.0, 91.0])
    engine._clock = lambda: (3 * HOUR + 60_000) / 1000
    engine.run_once()
    assert len(strategy.seen) == 2 and engine.portfolio.has_position("BTC/USDT")


def test_signal_on_close_treats_a_finished_last_candle_as_completed():
    # Just after the hour the venue may not list the new candle yet: the last one is done.
    strategy = _Scripted()
    engine = _closed_engine(_Venue(_candles([100.0, 101.0])), strategy, now_ms=2 * HOUR + 500)
    engine.run_once()
    assert [c.close for c in strategy.seen[0]] == [100.0, 101.0]


def test_signal_on_close_skips_when_only_a_forming_candle_exists():
    strategy = _Scripted(BUY)
    engine = _closed_engine(_Venue(_candles([100.0])), strategy, now_ms=60_000)
    engine.run_once()
    assert strategy.seen == [] and not engine.portfolio.positions


def test_default_engine_still_evaluates_the_forming_candle_every_poll():
    strategy = _Scripted()
    engine = _engine(_Venue(_candles([100.0, 101.0, 150.0])), strategy)
    engine.run_once()
    engine.run_once()
    assert len(strategy.seen) == 2 and strategy.seen[0][-1].close == 150.0


def test_signal_on_close_ignores_a_stale_candle_at_startup():
    # Started 40 minutes into an hourly candle: the last completed candle's decision is
    # old news at a price the market has left, so the bot waits for the next one.
    venue = _Venue(_candles([100.0, 101.0, 102.0]))
    strategy = _Scripted(BUY)
    engine = _closed_engine(venue, strategy, now_ms=2 * HOUR + 40 * 60_000)
    engine.run_once()
    engine.run_once()
    assert strategy.seen == [] and not engine.portfolio.positions

    venue.candles = _candles([100.0, 101.0, 102.0, 103.0])
    engine._clock = lambda: (3 * HOUR + 30_000) / 1000  # 30 s after the next candle closes
    engine.run_once()
    assert len(strategy.seen) == 1 and engine.portfolio.has_position("BTC/USDT")


def test_signal_freshness_allows_at_least_three_polls():
    venue = _Venue(_candles([100.0, 101.0]))
    hourly = _engine(venue, _Scripted(), signal_on_close=True)
    assert hourly._max_signal_age_ms == 6 * 60_000  # 10% of an hourly candle
    # On a 1-minute chart 10% of a candle is 6 s, less than one 60 s poll: the floor of
    # three polls keeps a normally-polling bot from missing every candle.
    minutely = _engine(venue, _Scripted(), signal_on_close=True, timeframe="1m")
    assert minutely._max_signal_age_ms == 180_000
