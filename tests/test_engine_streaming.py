"""Engine behaviour specific to streaming: closed bars, incremental fetches, batching."""

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
from crypto_bot.core.engine import BAR_CLOSE_WAKE_S, CATCHUP_LIMIT, CLOSE_GRACE_MS, Engine
from crypto_bot.core.models import HOLD, Candle, Signal, SignalType
from crypto_bot.core.portfolio import Portfolio
from crypto_bot.exchanges.base import ExchangeAdapter, ExchangeError
from crypto_bot.risk.manager import RiskManager
from crypto_bot.strategies.base import Strategy, StrategyState

TF = 3_600_000  # 1h
T0 = 1_700_000_000_000 - 1_700_000_000_000 % TF


class Venue(ExchangeAdapter):
    """Serves per-symbol candles honouring limit/since, and records every request."""

    name = "venue"

    def __init__(self, candles: dict[str, list[Candle]], funding: float | None = None):
        self.candles = candles
        self.funding = funding
        self.requests: list[dict] = []
        self.funding_calls: list[list[str]] = []
        self.fail: set[str] = set()

    def fetch_candles_many(self, timeframe, requests):
        self.requests.append(dict(requests))
        return super().fetch_candles_many(timeframe, requests)

    def fetch_candles(self, symbol, timeframe, limit=200, since=None):
        if symbol in self.fail:
            raise ExchangeError(f"{symbol} unavailable")
        series = self.candles[symbol]
        if since is not None:
            series = [c for c in series if c.timestamp >= since]
            return series[:limit]
        return series[-limit:]

    def fetch_funding_rates(self, symbols):
        self.funding_calls.append(list(symbols))
        return dict.fromkeys(symbols, self.funding)

    def load_markets(self):
        return {}

    def fetch_last_price(self, symbol):
        return self.candles[symbol][-1].close

    def fetch_balance(self):
        return {}

    def create_order(self, request):
        raise NotImplementedError

    def cancel_order(self, order_id, symbol):
        pass


def bars(closes: list[float], start: int = T0) -> list[Candle]:
    return [Candle(start + i * TF, c, c, c, c, 1.0) for i, c in enumerate(closes)]


class Recorder(Strategy):
    """Streaming stub: records every candle/context it is fed and replays a script."""

    name = "recorder"

    def __init__(self, script: dict[int, SignalType] | None = None, wants_context=False):
        super().__init__({})
        self.script = script or {}
        self.fed: list[tuple[str | None, int]] = []
        self.contexts: list = []
        self.wants_context = wants_context

    @property
    def warmup(self) -> int:
        return 1

    def new_state(self, symbol=None):
        strategy = self

        class _State(StrategyState):
            def update(self, candle, context=None):
                strategy.fed.append((symbol, candle.timestamp))
                strategy.contexts.append(context)
                kind = strategy.script.get(candle.timestamp)
                return Signal(kind, "scripted") if kind else HOLD

        return _State()


def config(symbols=("BTC/USDT",), **derivatives) -> BotConfig:
    return BotConfig(
        mode="paper",
        exchange=ExchangeConfig(name="venue"),
        symbols=list(symbols),
        timeframe="1h",
        poll_seconds=60,
        strategy=StrategyConfig(name="recorder"),
        risk=RiskConfig(position_pct=0.2, stop_loss_pct=0.0, take_profit_pct=0.0,
                        max_drawdown_pct=0.0),
        paper=PaperConfig(starting_cash=1000.0, fee_rate=0.0, slippage_pct=0.0),
        logging=LoggingConfig(level="ERROR"),
        derivatives=DerivativesConfig(**derivatives),
        history_bars=50,
    )


class Clock:
    def __init__(self, now: int):
        self.now = now

    def __call__(self) -> int:
        return self.now


def engine_for(venue, strategy, cfg=None, now=None):
    cfg = cfg or config()
    clock = Clock(now if now is not None else T0)
    portfolio = Portfolio(cash=1000.0, allow_shorts=cfg.derivatives.allow_shorts)
    engine = Engine(cfg, venue, strategy, RiskManager(cfg.risk), portfolio, clock=clock)
    engine.broker = PaperBroker(engine.last_price, fee_rate=0.0, slippage_pct=0.0)
    return engine, clock


def test_first_cycle_loads_history_then_polls_incrementally():
    venue = Venue({"BTC/USDT": bars([10.0] * 5)})
    strategy = Recorder()
    # "Now" is mid-way through bar 4, so bars 0-3 are closed and bar 4 is forming.
    engine, clock = engine_for(venue, strategy, now=T0 + 4 * TF + 60_000)

    engine.run_once()
    assert venue.requests[0] == {"BTC/USDT": (50, None)}  # max(history_bars=50, warmup + 5)
    assert [ts for _, ts in strategy.fed] == [T0 + i * TF for i in range(4)]
    assert engine.last_price("BTC/USDT") == 10.0  # priced from the forming bar

    engine.run_once()  # nothing new has closed
    assert venue.requests[1] == {"BTC/USDT": (CATCHUP_LIMIT, T0 + 3 * TF + 1)}
    assert len(strategy.fed) == 4


def test_forming_bar_is_not_traded_until_it_closes():
    venue = Venue({"BTC/USDT": bars([10.0, 10.0, 12.0])})
    strategy = Recorder({T0 + 2 * TF: SignalType.BUY})
    engine, clock = engine_for(venue, strategy, now=T0 + 2 * TF + 1_000)

    engine.run_once()
    assert not engine.portfolio.has_position("BTC/USDT")  # bar 2 still forming

    # The bar closes (and the venue lists the next one): its signal is acted on.
    venue.candles["BTC/USDT"] = bars([10.0, 10.0, 12.0, 12.5])
    clock.now = T0 + 3 * TF + 1_000
    engine.run_once()
    assert engine.portfolio.has_position("BTC/USDT")
    assert engine.portfolio.positions["BTC/USDT"].entry_price == 12.5  # fills at market


def test_a_quiet_market_bar_closes_by_time():
    venue = Venue({"BTC/USDT": bars([10.0, 11.0])})
    strategy = Recorder()
    engine, clock = engine_for(venue, strategy, now=T0 + 2 * TF + CLOSE_GRACE_MS - 1)
    engine.run_once()
    assert len(strategy.fed) == 1  # bar 1 ended, but within the grace period

    clock.now += 1
    engine.run_once()
    assert [ts for _, ts in strategy.fed] == [T0, T0 + TF]  # closed with no newer bar


def test_only_the_newest_closed_bar_can_trade():
    # Warm-up history contains a BUY on an old bar: stale, so it must not trade.
    venue = Venue({"BTC/USDT": bars([10.0] * 6)})
    strategy = Recorder({T0 + 2 * TF: SignalType.BUY})
    engine, _ = engine_for(venue, strategy, now=T0 + 6 * TF + CLOSE_GRACE_MS)
    engine.run_once()
    assert len(strategy.fed) == 6
    assert not engine.portfolio.has_position("BTC/USDT")


def test_catch_up_backlog_is_folded_in_but_not_traded():
    venue = Venue({"BTC/USDT": bars([10.0] * 2)})
    strategy = Recorder()
    engine, clock = engine_for(venue, strategy, now=T0 + 2 * TF + CLOSE_GRACE_MS)
    engine.run_once()
    assert len(strategy.fed) == 2

    # The bot was down: a full page of backlog arrives, with a BUY on its newest bar.
    backlog = bars([10.0] * (2 + CATCHUP_LIMIT + 10))
    venue.candles["BTC/USDT"] = backlog
    strategy.script = {backlog[1 + CATCHUP_LIMIT].timestamp: SignalType.BUY}
    clock.now = backlog[-1].timestamp + TF + CLOSE_GRACE_MS
    engine.run_once()
    assert len(strategy.fed) == 2 + CATCHUP_LIMIT
    assert not engine.portfolio.has_position("BTC/USDT")  # stale: we are not caught up

    engine.run_once()  # the rest of the backlog: now current
    assert len(strategy.fed) == len(backlog)


def test_one_failing_symbol_does_not_block_the_others():
    venue = Venue({"BTC/USDT": bars([10.0, 10.0]), "ETH/USDT": bars([5.0, 5.0])})
    venue.fail.add("BTC/USDT")
    strategy = Recorder({T0 + TF: SignalType.BUY})
    cfg = config(symbols=("BTC/USDT", "ETH/USDT"))
    engine, _ = engine_for(venue, strategy, cfg, now=T0 + 2 * TF + CLOSE_GRACE_MS)

    engine.run_once()
    assert engine.portfolio.has_position("ETH/USDT")
    assert not engine.portfolio.has_position("BTC/USDT")
    assert "BTC/USDT" not in engine._last_prices
    assert venue.requests[0]["BTC/USDT"] == (50, None)

    venue.fail.clear()  # recovers: BTC loads its history on the next cycle
    engine.run_once()
    assert ("BTC/USDT", T0 + TF) in strategy.fed


def test_funding_rates_are_fetched_once_per_cycle_in_one_batch():
    symbols = ("A/USDT", "B/USDT", "C/USDT")
    venue = Venue({s: bars([10.0, 10.0]) for s in symbols}, funding=2e-4)
    strategy = Recorder(wants_context=True)
    cfg = config(symbols=symbols)
    engine, _ = engine_for(venue, strategy, cfg, now=T0 + 2 * TF + CLOSE_GRACE_MS)

    engine.run_once()
    assert venue.funding_calls == [list(symbols)]
    # Warm-up bars get no context; the bar that may trade gets the venue's rate.
    acting = [c for c in strategy.contexts if c is not None]
    assert len(acting) == 3 and all(c.funding_rate == pytest.approx(2e-4) for c in acting)
    assert strategy.contexts.count(None) == 3


def test_no_funding_requests_without_context_or_positions():
    venue = Venue({"BTC/USDT": bars([10.0, 10.0])}, funding=1e-4)
    engine, _ = engine_for(venue, Recorder(), now=T0 + 2 * TF + CLOSE_GRACE_MS)
    engine.run_once()
    engine.run_once()
    assert venue.funding_calls == []


def test_swapping_the_strategy_rewarms_it_from_history():
    venue = Venue({"BTC/USDT": bars([10.0] * 4)})
    engine, _ = engine_for(venue, Recorder(), now=T0 + 4 * TF + CLOSE_GRACE_MS)
    engine.run_once()

    replacement = Recorder()
    engine.strategy = replacement
    engine.run_once()
    assert venue.requests[-1] == {"BTC/USDT": (50, None)}
    assert len(replacement.fed) == 4


def test_sleeps_until_just_after_the_next_bar_close_when_sooner_than_a_poll():
    venue = Venue({"BTC/USDT": bars([10.0])})
    engine, clock = engine_for(venue, Recorder())
    clock.now = T0 + TF - 10_000  # 10 s before the bar closes; poll is 60 s
    assert engine._seconds_until_next_cycle(0.0) == pytest.approx(10 + BAR_CLOSE_WAKE_S)
    clock.now = T0 + 1_000  # just after a close: the poll interval wins
    assert engine._seconds_until_next_cycle(5.0) == pytest.approx(55.0)
    assert engine._seconds_until_next_cycle(500.0) == 0.0  # overran: go again now


def test_settlement_and_strategy_context_share_one_funding_fetch_per_cycle():
    venue = Venue({"BTC/USDT": bars([10.0, 10.0])}, funding=1e-4)
    strategy = Recorder({T0 + TF: SignalType.BUY}, wants_context=True)
    cfg = config(funding_interval_hours=1.0)
    engine, clock = engine_for(venue, strategy, cfg, now=T0 + 2 * TF + CLOSE_GRACE_MS)
    engine.run_once()  # anchors the funding clock, fetches context, opens the long
    assert engine.portfolio.has_position("BTC/USDT")

    venue.candles["BTC/USDT"] = bars([10.0, 10.0, 10.0])
    clock.now += TF
    engine.run_once()  # funding settles AND the new bar wants context: one fetch
    assert venue.funding_calls == [["BTC/USDT"], ["BTC/USDT"]]
    assert engine.portfolio.funding_paid > 0

    engine.run_once()  # nothing new closed and no settlement due: no fetch at all
    assert len(venue.funding_calls) == 2
