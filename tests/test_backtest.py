"""Backtester tests: metrics math plus a full offline replay through the real engine."""

from __future__ import annotations

import math

import pytest

from crypto_bot.backtest import Backtester, align_candles, fetch_history
from crypto_bot.backtest.metrics import (
    bar_returns,
    cagr,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    timeframe_to_ms,
    trades_from_orders,
    win_rate,
)
from crypto_bot.config import (
    BotConfig,
    ExchangeConfig,
    LoggingConfig,
    PaperConfig,
    RiskConfig,
    StrategyConfig,
)
from crypto_bot.core.models import Candle, Order, OrderSide, OrderStatus, OrderType

HOUR_MS = 3_600_000


def _candles(closes: list[float], start: int = 1_700_000_000_000) -> list[Candle]:
    return [Candle(start + i * HOUR_MS, c, c, c, c, 1.0) for i, c in enumerate(closes)]


def _config(**overrides) -> BotConfig:
    config = BotConfig(
        mode="paper",
        exchange=ExchangeConfig(name="replay"),
        symbols=["BTC/USDT"],
        timeframe="1h",
        poll_seconds=60,
        strategy=StrategyConfig(
            name="ma_crossover", params={"fast_period": 5, "slow_period": 12, "ma_type": "sma"}
        ),
        risk=RiskConfig(
            position_pct=0.5,
            max_open_positions=3,
            stop_loss_pct=0.0,
            take_profit_pct=0.0,
            max_drawdown_pct=0.0,
        ),
        paper=PaperConfig(
            starting_cash=10_000.0, quote_currency="USDT", fee_rate=0.001, slippage_pct=0.0005
        ),
        logging=LoggingConfig(level="INFO", file=None),
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


# ── metrics ──────────────────────────────────────────────────────────────────


def test_timeframe_to_ms():
    assert timeframe_to_ms("1m") == 60_000
    assert timeframe_to_ms("4h") == 4 * HOUR_MS
    assert timeframe_to_ms("1d") == 24 * HOUR_MS
    with pytest.raises(ValueError):
        timeframe_to_ms("4x")


def test_max_drawdown_known_curve():
    # Peak 120 -> trough 90 is the deepest fall: 25%.
    assert max_drawdown([100, 120, 90, 130, 110]) == pytest.approx(0.25)
    assert max_drawdown([1, 2, 3]) == 0.0


def test_sharpe_and_sortino_edge_cases():
    flat = [0.0, 0.0, 0.0]
    assert sharpe_ratio(flat, 8760) == 0.0
    assert sortino_ratio(flat, 8760) == 0.0
    all_gains = [0.01, 0.02, 0.01]
    assert sortino_ratio(all_gains, 8760) == math.inf
    mixed = [0.02, -0.01, 0.02, -0.01]
    assert sharpe_ratio(mixed, 8760) > 0
    assert sortino_ratio(mixed, 8760) > sharpe_ratio(mixed, 8760)


def test_bar_returns_and_cagr():
    assert bar_returns([100, 110, 99]) == [pytest.approx(0.1), pytest.approx(-0.1)]
    year_ms = 365 * 24 * HOUR_MS
    assert cagr(100, 121, 2 * year_ms) == pytest.approx(0.1)  # +21% over 2y = 10%/y


def _order(side: OrderSide, amount: float, price: float, fee: float = 0.0) -> Order:
    return Order(
        symbol="BTC/USDT",
        side=side,
        amount=amount,
        type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        filled=amount,
        average_price=price,
        fee=fee,
    )


def test_trades_from_orders_nets_fees_across_both_legs():
    orders = [
        _order(OrderSide.BUY, 1.0, 100.0, fee=1.0),
        _order(OrderSide.BUY, 1.0, 110.0, fee=1.0),  # avg entry 105, entry fees 2
        _order(OrderSide.SELL, 2.0, 120.0, fee=2.0),
    ]
    trades = trades_from_orders(orders)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.entry_price == pytest.approx(105.0)
    # (120-105)*2 = 30 gross, minus 2 sell fee and 2 entry fees = 26 net.
    assert trade.pnl == pytest.approx(26.0)
    assert win_rate(trades) == 1.0
    assert profit_factor(trades) == math.inf


def test_trades_from_orders_partial_close():
    orders = [
        _order(OrderSide.BUY, 2.0, 100.0, fee=2.0),
        _order(OrderSide.SELL, 1.0, 90.0),  # half the lot, at a loss
        _order(OrderSide.SELL, 1.0, 130.0),  # the rest, at a gain
    ]
    trades = trades_from_orders(orders)
    assert len(trades) == 2
    assert trades[0].pnl == pytest.approx(-10.0 - 1.0)  # loss + its half of entry fees
    assert trades[1].pnl == pytest.approx(30.0 - 1.0)
    assert win_rate(trades) == 0.5
    assert profit_factor(trades) == pytest.approx(29.0 / 11.0)


# ── alignment & history fetch ────────────────────────────────────────────────


def test_align_candles_keeps_only_common_timestamps():
    a = _candles([1, 2, 3, 4])
    b = _candles([10, 20, 30], start=1_700_000_000_000 + HOUR_MS)  # offset by one bar
    aligned = align_candles({"A": a, "B": b})
    stamps_a = [c.timestamp for c in aligned["A"]]
    stamps_b = [c.timestamp for c in aligned["B"]]
    assert stamps_a == stamps_b
    assert len(stamps_a) == 3


def test_align_candles_rejects_disjoint_series():
    a = _candles([1, 2])
    b = _candles([1, 2], start=1_800_000_000_000)
    with pytest.raises(ValueError):
        align_candles({"A": a, "B": b})


class _PagedExchange:
    """Fake adapter returning history in fixed pages, for pagination tests."""

    def __init__(self, candles: list[Candle]):
        self._candles = candles
        self.calls = 0

    def fetch_candles(self, symbol, timeframe, limit=200, since=None):
        self.calls += 1
        eligible = [c for c in self._candles if since is None or c.timestamp >= since]
        return eligible[:limit]


def test_fetch_history_paginates_without_duplicates():
    history = _candles(list(range(250)))
    exchange = _PagedExchange(history)
    out = fetch_history(exchange, "BTC/USDT", "1h", history[0].timestamp, page_size=100)
    assert [c.timestamp for c in out] == [c.timestamp for c in history]
    assert exchange.calls == 3  # 100 + 100 + 50


# ── end-to-end replay ────────────────────────────────────────────────────────


def test_backtester_runs_strategy_through_real_engine():
    # A slow sine wave: the MA crossover should buy troughs-ish and sell crests-ish,
    # beating buy & hold (which nets ~0 over whole cycles).
    closes = [100 + 15 * math.sin(i / 8) for i in range(200)]
    config = _config()
    result = Backtester(config).run({"BTC/USDT": _candles(closes)})

    warmup = 13  # slow_period 12 + 1
    assert result.bars == 200 - warmup + 1
    assert len(result.equity_curve) == result.bars
    assert result.num_trades >= 3
    assert result.ending_equity == pytest.approx(
        result.starting_cash * (1 + result.total_return_pct)
    )
    assert result.total_return_pct > result.buy_hold_return_pct
    assert 0 <= result.max_drawdown_pct < 1
    assert result.fees_paid > 0
    report = result.format_report()
    assert "total return" in report and "sharpe" in report


def test_backtester_applies_trailing_stop():
    # Buy-and-hold (DCA) into a pump-and-crash. Without protection the position rides
    # the crash down; with a 10% trailing stop the backtest exits near the top.
    closes = [10.0] * 5 + [float(i) for i in range(10, 61)] + [float(c) for c in range(60, 20, -1)]
    strategy = StrategyConfig(name="dca", params={"every": 1})

    def _risk(trailing: float) -> RiskConfig:
        return RiskConfig(
            position_pct=0.5, max_open_positions=3, stop_loss_pct=0.0,
            take_profit_pct=0.0, trailing_stop_pct=trailing, max_drawdown_pct=0.0,
        )

    stopped = Backtester(_config(strategy=strategy, risk=_risk(0.10))).run(
        {"BTC/USDT": _candles(closes)}
    )
    unstopped = Backtester(_config(strategy=strategy, risk=_risk(0.0))).run(
        {"BTC/USDT": _candles(closes)}
    )
    # The trail exits near the top (first trade is the big winner); DCA then re-enters
    # on later bars and gets stopped again — several small trades, still far ahead.
    assert stopped.num_trades >= 1
    assert stopped.trades[0].pnl > 0  # sold ~10% below the 60 peak, far above the ~10 entry
    assert stopped.realized_pnl > 0
    assert unstopped.num_trades == 0  # never sold; rode the crash with an open position
    assert stopped.max_drawdown_pct < unstopped.max_drawdown_pct
    assert stopped.ending_equity > unstopped.ending_equity


def test_backtester_rejects_insufficient_history():
    with pytest.raises(ValueError):
        Backtester(_config()).run({"BTC/USDT": _candles([1, 2, 3])})


def test_sharpe_and_sortino_need_at_least_two_returns():
    assert sharpe_ratio([0.05], 8760) == 0.0
    assert sortino_ratio([0.05], 8760) == 0.0
    assert sharpe_ratio([], 8760) == 0.0
    assert sortino_ratio([], 8760) == 0.0


def test_cagr_is_zero_for_a_degenerate_span():
    assert cagr(0.0, 100.0, 1000) == 0.0  # non-positive start
    assert cagr(100.0, 0.0, 1000) == 0.0  # non-positive end
    assert cagr(100.0, 110.0, 0) == 0.0  # zero elapsed time
    assert cagr(100.0, 110.0, -1) == 0.0  # negative elapsed time


def test_trades_from_orders_skips_unfilled_and_zero_fill_orders():
    orders = [
        _order(OrderSide.BUY, 1.0, 100.0),
        Order(
            symbol="BTC/USDT", side=OrderSide.SELL, amount=1.0, type=OrderType.MARKET,
            status=OrderStatus.REJECTED, filled=0.0, average_price=None,
        ),
        _order(OrderSide.SELL, 1.0, 110.0),
    ]
    trades = trades_from_orders(orders)
    assert len(trades) == 1
    assert trades[0].pnl == pytest.approx(10.0)


def test_trades_from_orders_ignores_a_sell_with_no_tracked_lot():
    # A sell for a symbol never bought (shouldn't happen in a real backtest, but the
    # pairing logic must not misattribute it to some other symbol's lot).
    orders = [_order(OrderSide.SELL, 1.0, 100.0)]
    assert trades_from_orders(orders) == []


def test_backtester_rejects_empty_candle_data():
    with pytest.raises(ValueError, match="no candle data supplied"):
        Backtester(_config()).run({})


def test_fetch_history_stops_when_the_exchange_returns_no_batch():
    class _EmptyExchange:
        def fetch_candles(self, symbol, timeframe, limit=200, since=None):
            return []

    out = fetch_history(_EmptyExchange(), "BTC/USDT", "1h", 1_700_000_000_000)
    assert out == []


def test_fetch_history_stops_on_a_stale_repeated_page():
    class _StaleExchange:
        def __init__(self, candles):
            self._candles = candles

        def fetch_candles(self, symbol, timeframe, limit=200, since=None):
            # Always echoes the very first page, regardless of `since` — simulates a
            # venue that ignores pagination and would otherwise loop forever. The page
            # is exactly `limit` long so the first call doesn't already stop via the
            # "short page" branch, and the second call's candles are then all stale.
            return self._candles[: limit]

    history = _candles(list(range(10)))
    out = fetch_history(
        _StaleExchange(history), "BTC/USDT", "1h", history[0].timestamp, page_size=2
    )
    assert [c.timestamp for c in out] == [c.timestamp for c in history[:2]]


def test_fetch_history_truncates_at_until_ms():
    history = _candles(list(range(10)))
    exchange = _PagedExchange(history)
    until_ms = history[4].timestamp
    out = fetch_history(
        exchange, "BTC/USDT", "1h", history[0].timestamp, until_ms=until_ms, page_size=3
    )
    assert [c.timestamp for c in out] == [c.timestamp for c in history[:5]]
    assert out[-1].timestamp <= until_ms


def test_replay_exchange_stub_methods():
    from crypto_bot.backtest.engine import ReplayExchange
    from crypto_bot.core.models import OrderRequest, OrderSide

    replay = ReplayExchange({"BTC/USDT": _candles([1, 2, 3])})
    assert replay.load_markets() == {}
    assert replay.fetch_balance() == {}
    assert replay.fetch_last_price("BTC/USDT") == 1.0  # cursor starts at 0

    replay.cancel_order("id", "BTC/USDT")  # a no-op; must not raise

    with pytest.raises(NotImplementedError):
        replay.create_order(
            OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0)
        )


def test_max_drawdown_ignores_non_positive_equity():
    # Peak never rises above zero, so there is no meaningful percentage drawdown to
    # report (and no division by a non-positive peak).
    assert max_drawdown([-5.0, -3.0, -10.0]) == 0.0
    assert max_drawdown([0.0, 0.0]) == 0.0


def test_recording_broker_does_not_record_an_unfilled_order():
    from crypto_bot.backtest.engine import RecordingBroker
    from crypto_bot.core.models import OrderRequest

    class _RejectingBroker:
        def execute(self, request):
            return Order(
                symbol=request.symbol, side=request.side, amount=request.amount,
                type=OrderType.MARKET, status=OrderStatus.REJECTED, filled=0.0,
            )

    broker = RecordingBroker(_RejectingBroker(), lambda: 1_700_000_000_000)
    order = broker.execute(OrderRequest(symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0))

    assert order.timestamp == 1_700_000_000_000  # still stamped in bar time
    assert broker.orders == []  # but never counted as a trade


def test_buy_and_hold_skips_a_symbol_with_a_non_positive_first_close():
    # Buy-and-hold divides by each symbol's first post-warm-up close, so a symbol whose
    # bar there is zero must drop out of the benchmark rather than blow it up.
    warmup_bar = 13 - 1  # ma_crossover slow_period 12 + 1, minus one for the cursor
    btc = [100.0] * 30
    btc[warmup_bar] = 0.0
    eth = [100.0] * 30
    eth[-1] = 200.0  # a clean +100% over the tested window

    result = Backtester(_config(symbols=["BTC/USDT", "ETH/USDT"])).run(
        {"BTC/USDT": _candles(btc), "ETH/USDT": _candles(eth)}
    )

    # Only ETH contributes: +100%. Averaging BTC in would have halved this.
    assert result.buy_hold_return_pct == pytest.approx(1.0)
