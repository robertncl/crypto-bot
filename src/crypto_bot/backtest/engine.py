"""Backtesting engine: replay history through the *real* trading engine.

Design principle: a backtest that reimplements the trading loop will silently drift
from what the bot actually does live. So this module doesn't reimplement anything —
it feeds historical candles through the same :class:`~crypto_bot.core.engine.Engine`,
:class:`~crypto_bot.risk.manager.RiskManager`, :class:`~crypto_bot.core.portfolio.
Portfolio` and :class:`~crypto_bot.core.broker.PaperBroker` used for paper/live
trading, one bar at a time:

* :class:`ReplayExchange` is an :class:`ExchangeAdapter` whose clock is a cursor into
  pre-fetched history; each ``fetch_candles`` call returns the window ending at the
  cursor, exactly like polling a venue as time passes.
* :class:`RecordingBroker` wraps the paper broker to keep every fill (stamped with
  bar time, not wall-clock) for trade-level statistics.
* :class:`Backtester` advances the cursor, calls ``engine.run_once()`` per bar,
  records the equity curve, and summarizes it into a :class:`BacktestResult`.

Fill model = the paper broker's: market orders at the bar close, adjusted for
configured slippage and fees. Same caveats as any close-fill backtest: no intrabar
stop resolution, no order-book depth.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from crypto_bot.backtest import metrics as m
from crypto_bot.config import BotConfig
from crypto_bot.core.broker import Broker, PaperBroker
from crypto_bot.core.engine import Engine
from crypto_bot.core.models import Candle, Order, OrderRequest
from crypto_bot.core.portfolio import Portfolio
from crypto_bot.exchanges.base import ExchangeAdapter
from crypto_bot.logging_setup import LOGGER_NAME
from crypto_bot.risk.manager import RiskManager
from crypto_bot.strategies.registry import build_strategy


class ReplayExchange(ExchangeAdapter):
    """Serves pre-fetched history one bar at a time. ``advance()`` is the clock."""

    name = "replay"

    def __init__(self, candles_by_symbol: dict[str, list[Candle]]) -> None:
        self._data = candles_by_symbol
        self.cursor = 0  # index of the "current" bar

    @property
    def total_bars(self) -> int:
        return min(len(c) for c in self._data.values()) if self._data else 0

    def advance(self) -> bool:
        """Move to the next bar; False once history is exhausted."""
        if self.cursor + 1 >= self.total_bars:
            return False
        self.cursor += 1
        return True

    def current_timestamp(self) -> int:
        first = next(iter(self._data.values()))
        return first[self.cursor].timestamp

    def load_markets(self) -> dict:
        return {}

    def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = 200, since: int | None = None
    ) -> list[Candle]:
        series = self._data[symbol]
        end = self.cursor + 1
        return series[max(0, end - limit) : end]

    def fetch_last_price(self, symbol: str) -> float:
        return self._data[symbol][self.cursor].close

    def fetch_balance(self) -> dict[str, float]:
        return {}

    def create_order(self, request: OrderRequest) -> Order:
        raise NotImplementedError("backtests never place live orders")

    def cancel_order(self, order_id: str, symbol: str) -> None:
        pass


class RecordingBroker(Broker):
    """Delegates to an inner broker and keeps every filled order, stamped in bar time."""

    is_paper = True

    def __init__(self, inner: Broker, timestamp_provider: Callable[[], int]) -> None:
        self._inner = inner
        self._now = timestamp_provider
        self.orders: list[Order] = []

    def execute(self, request: OrderRequest) -> Order:
        order = self._inner.execute(request)
        order.timestamp = self._now()
        if order.is_filled:
            self.orders.append(order)
        return order


@dataclass
class BacktestResult:
    symbols: list[str]
    timeframe: str
    strategy: str
    bars: int
    start_ts: int
    end_ts: int
    starting_cash: float
    ending_equity: float
    total_return_pct: float
    buy_hold_return_pct: float
    cagr_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    num_trades: int
    win_rate_pct: float
    profit_factor: float
    realized_pnl: float
    fees_paid: float
    open_positions: int
    quote_currency: str
    equity_curve: list[tuple[int, float]] = field(repr=False, default_factory=list)
    trades: list[m.TradeRecord] = field(repr=False, default_factory=list)

    def format_report(self) -> str:
        def _date(ts: int) -> str:
            return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        def _ratio(value: float) -> str:
            return "inf" if value == float("inf") else f"{value:.2f}"

        lines = [
            "── Backtest report ──────────────────────────────────────",
            f"period          {_date(self.start_ts)} → {_date(self.end_ts)}"
            f"  ({self.bars} bars of {self.timeframe})",
            f"symbols         {', '.join(self.symbols)}",
            f"strategy        {self.strategy}",
            f"start equity    {self.starting_cash:,.2f} {self.quote_currency}",
            f"end equity      {self.ending_equity:,.2f} {self.quote_currency}",
            f"total return    {self.total_return_pct:+.2%}"
            f"   (buy & hold: {self.buy_hold_return_pct:+.2%})",
            f"CAGR            {self.cagr_pct:+.2%}",
            f"sharpe          {_ratio(self.sharpe)}   sortino {_ratio(self.sortino)}",
            f"max drawdown    {self.max_drawdown_pct:.2%}",
            f"trades          {self.num_trades}"
            f"   (win rate {self.win_rate_pct:.0%}, profit factor {_ratio(self.profit_factor)})",
            f"realized pnl    {self.realized_pnl:+,.2f}   fees {self.fees_paid:,.2f}",
            f"open at end     {self.open_positions} position(s)",
            "─────────────────────────────────────────────────────────",
        ]
        return "\n".join(lines)


class Backtester:
    """Replays pre-fetched candles through the real engine and scores the result."""

    def __init__(self, config: BotConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.log = logger or logging.getLogger(f"{LOGGER_NAME}.backtest")

    def run(self, candles_by_symbol: dict[str, list[Candle]]) -> BacktestResult:
        if not candles_by_symbol:
            raise ValueError("no candle data supplied")
        aligned = align_candles(candles_by_symbol)

        strategy = build_strategy(self.config.strategy.name, self.config.strategy.params)
        replay = ReplayExchange(aligned)
        portfolio = Portfolio(
            cash=self.config.paper.starting_cash,
            quote_currency=self.config.paper.quote_currency,
        )
        # Silence the engine's per-cycle INFO chatter; fills and warnings still surface
        # through the backtest logger at DEBUG for troubleshooting.
        engine_log = logging.getLogger(f"{LOGGER_NAME}.backtest.engine")
        engine_log.setLevel(logging.ERROR)
        engine = Engine(
            self.config,
            replay,
            strategy,
            RiskManager(self.config.risk),
            portfolio,
            logger=engine_log,
        )
        engine.broker = RecordingBroker(
            PaperBroker(
                engine.last_price,
                fee_rate=self.config.paper.fee_rate,
                slippage_pct=self.config.paper.slippage_pct,
            ),
            replay.current_timestamp,
        )

        total = replay.total_bars
        if total < strategy.warmup + 1:
            raise ValueError(
                f"not enough history: {total} bars, but strategy warmup is {strategy.warmup}"
            )

        # Fast-forward past the warm-up (no strategy could act there), then step
        # bar by bar through the exact live decision cycle.
        replay.cursor = strategy.warmup - 1
        equity_curve: list[tuple[int, float]] = []
        while True:
            engine.run_once()
            equity_curve.append(
                (replay.current_timestamp(), portfolio.equity(engine._last_prices))
            )
            if not replay.advance():
                break

        return self._summarize(aligned, equity_curve, engine.broker.orders, portfolio)

    def _summarize(
        self,
        aligned: dict[str, list[Candle]],
        equity_curve: list[tuple[int, float]],
        orders: list[Order],
        portfolio: Portfolio,
    ) -> BacktestResult:
        equity = [e for _, e in equity_curve]
        returns = m.bar_returns(equity)
        periods = m.bars_per_year(self.config.timeframe)
        trades = m.trades_from_orders(orders)
        start_ts, end_ts = equity_curve[0][0], equity_curve[-1][0]
        starting_cash = self.config.paper.starting_cash
        ending_equity = equity[-1]

        # Equal-weight buy-and-hold over the same (post-warm-up) window as the strategy.
        first_bar = len(next(iter(aligned.values()))) - len(equity_curve)
        holds = []
        for series in aligned.values():
            first_close = series[first_bar].close
            if first_close > 0:
                holds.append(series[-1].close / first_close - 1.0)
        buy_hold = sum(holds) / len(holds) if holds else 0.0

        return BacktestResult(
            symbols=list(aligned),
            timeframe=self.config.timeframe,
            strategy=f"{self.config.strategy.name} {self.config.strategy.params}",
            bars=len(equity_curve),
            start_ts=start_ts,
            end_ts=end_ts,
            starting_cash=starting_cash,
            ending_equity=ending_equity,
            total_return_pct=ending_equity / starting_cash - 1.0,
            buy_hold_return_pct=buy_hold,
            cagr_pct=m.cagr(starting_cash, ending_equity, end_ts - start_ts),
            sharpe=m.sharpe_ratio(returns, periods),
            sortino=m.sortino_ratio(returns, periods),
            max_drawdown_pct=m.max_drawdown(equity),
            num_trades=len(trades),
            win_rate_pct=m.win_rate(trades),
            profit_factor=m.profit_factor(trades),
            realized_pnl=portfolio.realized_pnl,
            fees_paid=portfolio.fees_paid,
            open_positions=portfolio.open_position_count,
            quote_currency=self.config.paper.quote_currency,
            equity_curve=equity_curve,
            trades=trades,
        )


def align_candles(candles_by_symbol: dict[str, list[Candle]]) -> dict[str, list[Candle]]:
    """Restrict every symbol's series to their *common* timestamps, sorted ascending.

    Venues list assets at different times and occasionally skip bars; trading logic
    assumes bar N means the same instant for every symbol, so mismatches are dropped.
    """
    common: set[int] | None = None
    for series in candles_by_symbol.values():
        stamps = {c.timestamp for c in series}
        common = stamps if common is None else common & stamps
    if not common:
        raise ValueError("symbols share no common candle timestamps; check the data")
    return {
        symbol: sorted(
            (c for c in series if c.timestamp in common), key=lambda c: c.timestamp
        )
        for symbol, series in candles_by_symbol.items()
    }


def fetch_history(
    exchange: ExchangeAdapter,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int | None = None,
    page_size: int = 1000,
) -> list[Candle]:
    """Download candles from ``since_ms`` forward, paginating until ``until_ms`` (or now)."""
    tf_ms = m.timeframe_to_ms(timeframe)
    out: list[Candle] = []
    cursor = since_ms
    while True:
        batch = exchange.fetch_candles(symbol, timeframe, limit=page_size, since=cursor)
        if not batch:
            break
        # Guard against venues echoing the same page forever.
        fresh = [c for c in batch if not out or c.timestamp > out[-1].timestamp]
        if not fresh:
            break
        out.extend(fresh)
        if until_ms is not None and out[-1].timestamp >= until_ms:
            out = [c for c in out if c.timestamp <= until_ms]
            break
        if len(batch) < page_size:
            break
        cursor = out[-1].timestamp + tf_ms
    return out
