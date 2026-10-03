"""Backtesting engine: replay history through the *real* trading engine.

Design principle: a backtest that reimplements the trading loop will silently drift
from what the bot actually does live. So this module doesn't reimplement anything —
it feeds historical candles through the same :class:`~crypto_bot.core.engine.Engine`,
:class:`~crypto_bot.risk.manager.RiskManager`, :class:`~crypto_bot.core.portfolio.
Portfolio` and :class:`~crypto_bot.core.broker.PaperBroker` used for paper/live
trading, one bar at a time:

* :class:`ReplayExchange` is an :class:`ExchangeAdapter` whose clock is a cursor into
  pre-fetched history; each ``fetch_candles`` call returns candles up to the cursor,
  exactly like polling a venue as time passes (honouring ``since``, so the engine's
  incremental polling pulls one new bar per step).
* :class:`RecordingBroker` wraps the paper broker to keep every fill (stamped with
  bar time, not wall-clock) for trade-level statistics.
* :class:`Backtester` advances the cursor, calls ``engine.run_once()`` per bar,
  records the equity curve, and summarizes it into a :class:`BacktestResult`. The
  engine's clock is pinned to the replayed bar's close, so each bar is decided the
  moment it closes, as live.

Because strategies keep incremental state, a replay costs O(1) per bar and symbol — a
backtest's runtime grows linearly with its length rather than with length × window.

Fill model = the paper broker's: market orders at the bar close, adjusted for
configured slippage and fees. Same caveats as any close-fill backtest: no intrabar
stop resolution, no order-book depth.
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from crypto_bot.backtest import metrics as m
from crypto_bot.config import BotConfig
from crypto_bot.core.broker import Broker, PaperBroker
from crypto_bot.core.engine import CLOSE_GRACE_MS, Engine
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
        self._stamps = {s: [c.timestamp for c in series] for s, series in self._data.items()}
        self.cursor = 0  # index of the "current" bar
        # History is fixed for the life of a replay, so measure it once instead of on
        # every advance()/current_timestamp() — those run per bar, per backtest.
        self._total_bars = min((len(c) for c in self._data.values()), default=0)
        self._clock = next(iter(self._data.values()), [])

    @property
    def total_bars(self) -> int:
        return self._total_bars

    def advance(self) -> bool:
        """Move to the next bar; False once history is exhausted."""
        if self.cursor + 1 >= self._total_bars:
            return False
        self.cursor += 1
        return True

    def current_timestamp(self) -> int:
        return self._clock[self.cursor].timestamp

    def load_markets(self) -> dict:
        return {}

    def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = 200, since: int | None = None
    ) -> list[Candle]:
        series = self._data[symbol]
        end = self.cursor + 1
        if since is None:
            return series[max(0, end - limit) : end]
        start = bisect_left(self._stamps[symbol], since, 0, end)
        return series[start : min(end, start + limit)]

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
    funding_paid: float
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
            f"realized pnl    {self.realized_pnl:+,.2f}   fees {self.fees_paid:,.2f}"
            + (f"   funding {self.funding_paid:+,.2f}" if self.funding_paid else ""),
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
            allow_shorts=self.config.derivatives.allow_shorts,
        )
        # Silence the engine's per-cycle INFO chatter; fills and warnings still surface
        # through the backtest logger at DEBUG for troubleshooting.
        engine_log = logging.getLogger(f"{LOGGER_NAME}.backtest.engine")
        engine_log.setLevel(logging.ERROR)
        tf_ms = m.timeframe_to_ms(self.config.timeframe)
        engine = Engine(
            self.config,
            replay,
            strategy,
            RiskManager(self.config.risk),
            portfolio,
            logger=engine_log,
            # "Now" is the instant the replayed bar closed: that bar is decided, no later.
            clock=lambda: replay.current_timestamp() + tf_ms + CLOSE_GRACE_MS,
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
            funding_paid=portfolio.funding_paid,
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
        if _absorb_page(out, batch, until_ms, page_size):
            return out
        cursor = out[-1].timestamp + tf_ms


def fetch_history_many(
    exchange: ExchangeAdapter,
    symbols: list[str],
    timeframe: str,
    since_ms: int,
    until_ms: int | None = None,
    page_size: int = 1000,
) -> dict[str, list[Candle]]:
    """:func:`fetch_history` for many symbols at once.

    All symbols paginate in lockstep and each round of pages is one
    :meth:`~ExchangeAdapter.fetch_candles_many` call, so on a concurrent adapter the
    download takes about as long as the longest single symbol's, not the sum of all.
    Raises the first per-symbol :class:`ExchangeError`.
    """
    tf_ms = m.timeframe_to_ms(timeframe)
    out: dict[str, list[Candle]] = {symbol: [] for symbol in symbols}
    cursors = dict.fromkeys(symbols, since_ms)
    while cursors:
        pages = exchange.fetch_candles_many(
            timeframe, {symbol: (page_size, cursor) for symbol, cursor in cursors.items()}
        )
        for symbol in list(cursors):
            page = pages[symbol]
            if isinstance(page, Exception):
                raise page
            if _absorb_page(out[symbol], page, until_ms, page_size):
                del cursors[symbol]
            else:
                cursors[symbol] = out[symbol][-1].timestamp + tf_ms
    return out


def _absorb_page(
    out: list[Candle], batch: list[Candle], until_ms: int | None, page_size: int
) -> bool:
    """Append one fetched page to ``out``; return True once pagination is finished."""
    if not batch:
        return True
    # Guard against venues echoing the same page forever.
    fresh = [c for c in batch if not out or c.timestamp > out[-1].timestamp]
    if not fresh:
        return True
    out.extend(fresh)
    if until_ms is not None and out[-1].timestamp >= until_ms:
        while out[-1].timestamp > until_ms:  # ascending, so trim from the end
            out.pop()
        return True
    return len(batch) < page_size
