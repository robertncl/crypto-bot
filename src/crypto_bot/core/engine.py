"""The trading engine: the loop that turns market data into orders.

Each iteration (:meth:`Engine.run_once`):

1. Fetch every symbol's newest candles in **one batched request** (concurrent on venues
   that support it). The first cycle loads ``history_bars`` of warm-up history; after
   that only bars newer than the last one processed are requested, typically 1–2.
2. Mark equity to market at the latest price and update the drawdown kill-switch.
3. Settle perpetual funding once per elapsed funding interval.
4. Apply protective exits (stop-loss / trailing stop / take-profit) to open positions.
   These run **every cycle** against the latest (possibly still-forming) price.
5. Fold each newly **closed** candle into that symbol's incremental strategy state and,
   on the newest bar, act on the signal if it is actionable and risk-approved.

**Closed bars only.** Strategy signals are evaluated once per bar, when it closes — the
same bars a backtest sees — rather than re-evaluated on a still-forming candle every poll.
A forming candle can cross a level and un-cross it before the close; trading that is the
classic live-vs-backtest divergence. Protective exits are the deliberate exception: risk
reacts intrabar.

**Scaling.** Per-symbol work per cycle is O(1): strategies keep running indicator state
instead of re-deriving it from a candle window, the engine keeps no candle buffers, and
each poll moves a couple of candles per symbol over the wire. Combined with concurrent
fetching, the cycle time is dominated by one network round-trip regardless of how many
symbols are traded (up to the venue's rate limit).

The engine is deliberately exchange- and broker-agnostic: swap in a :class:`PaperBroker`
or :class:`LiveBroker` and everything else is identical.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from crypto_bot.config import BotConfig
from crypto_bot.core.broker import Broker, LiveBroker, PaperBroker
from crypto_bot.core.models import (
    Candle,
    MarketContext,
    Order,
    OrderRequest,
    OrderSide,
    PositionSide,
    Signal,
    SignalType,
)
from crypto_bot.core.portfolio import Portfolio
from crypto_bot.core.timeframes import timeframe_to_ms
from crypto_bot.exchanges.base import ExchangeAdapter, ExchangeError
from crypto_bot.exchanges.factory import build_exchange
from crypto_bot.logging_setup import LOGGER_NAME
from crypto_bot.risk.manager import RiskManager
from crypto_bot.strategies.base import Strategy, StrategyState
from crypto_bot.strategies.registry import build_strategy

#: A candle counts as closed once the clock is this far past its end, even if the venue
#: has not yet listed a newer candle (quiet markets, or a slightly fast local clock).
CLOSE_GRACE_MS = 1_000
#: The run loop wakes this long after each bar boundary, so a closed bar is picked up
#: within seconds instead of up to a full ``poll_seconds`` later.
BAR_CLOSE_WAKE_S = 2.0
#: Page size for incremental fetches. A full page means the engine is catching up after
#: downtime; those bars are folded into strategy state but never traded on. Kept under
#: 100 because venues price candle requests by size (Binance futures: weight 1 up to 99,
#: then 2), and a poll normally needs only one or two candles anyway.
CATCHUP_LIMIT = 99


def _wall_clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class _Feed:
    """Per-symbol stream position and strategy state."""

    state: StrategyState
    last_ts: int | None = None  # timestamp of the newest closed candle folded in


class Engine:
    def __init__(
        self,
        config: BotConfig,
        exchange: ExchangeAdapter,
        strategy: Strategy,
        risk: RiskManager,
        portfolio: Portfolio,
        broker: Broker | None = None,
        logger: logging.Logger | None = None,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.config = config
        self.exchange = exchange
        self.risk = risk
        self.portfolio = portfolio
        self.broker = broker
        self.log = logger or logging.getLogger(LOGGER_NAME)
        self._last_prices: dict[str, float] = {}
        self._running = False
        self._allow_shorts = config.derivatives.allow_shorts
        self._tf_ms = timeframe_to_ms(config.timeframe)
        # Epoch-ms "now". Live: the wall clock. Backtests: the replayed bar's close.
        self._clock = clock or _wall_clock_ms
        # Timestamp (ms) of the last funding settlement, so accrual happens once per
        # interval rather than once per poll.
        self._last_funding_ts: int | None = None
        # Funding rates fetched this cycle, shared by settlement and strategy context.
        self._cycle_rates: dict[str, float | None] = {}
        self.strategy = strategy  # property: also (re)initialises per-symbol state

    # -- strategy ----------------------------------------------------------------
    @property
    def strategy(self) -> Strategy:
        return self._strategy

    @strategy.setter
    def strategy(self, strategy: Strategy) -> None:
        """Install a strategy. Per-symbol state belongs to the old one, so it is dropped
        and the next cycle re-warms the new strategy from full history."""
        self._strategy = strategy
        self._wants_context = getattr(strategy, "wants_context", False)
        self._history_bars = max(self.config.history_bars, strategy.warmup + 5)
        self._feeds: dict[str, _Feed] = {}

    # -- price access used by the paper broker ---------------------------------
    def last_price(self, symbol: str) -> float:
        return self._last_prices[symbol]

    # -- main loop -------------------------------------------------------------
    def run(self) -> None:
        """Poll until interrupted (Ctrl-C), waking early to catch each bar close."""
        self._running = True
        mode = "LIVE" if self.config.is_live else "PAPER"
        self.log.info(
            "starting %s on %s | mode=%s | symbols=%s | timeframe=%s | strategy=%s",
            type(self.strategy).__name__,
            self.exchange.name,
            mode,
            ",".join(self.config.symbols),
            self.config.timeframe,
            self.config.strategy.name,
        )
        try:
            while self._running:
                started = time.monotonic()
                try:
                    self.run_once()
                except ExchangeError as exc:
                    self.log.error("exchange error this cycle (will retry): %s", exc)
                except Exception:  # keep the bot alive across unexpected per-cycle errors
                    self.log.exception("unexpected error this cycle (will retry)")
                time.sleep(self._seconds_until_next_cycle(time.monotonic() - started))
        except KeyboardInterrupt:
            self.log.info("interrupted by user; shutting down")
        finally:
            self.stop()
            self.log.info("final %s", self.portfolio.snapshot(self._last_prices))

    def stop(self) -> None:
        self._running = False

    def _seconds_until_next_cycle(self, elapsed: float) -> float:
        """Next poll tick, or just after the next bar boundary if that comes sooner."""
        until_poll = self.config.poll_seconds - elapsed
        now = self._clock()
        until_close = (self._tf_ms - now % self._tf_ms) / 1000 + BAR_CLOSE_WAKE_S
        return max(0.0, min(until_poll, until_close))

    def run_once(self) -> None:
        """Execute exactly one decision cycle across all symbols."""
        self._cycle_rates = {}
        now = self._clock()

        # 1. Fetch every symbol in one batch: warm-up history first, then only new bars.
        requests: dict[str, tuple[int, int | None]] = {}
        for symbol in self.config.symbols:
            feed = self._feeds.get(symbol)
            if feed is None:
                feed = self._feeds[symbol] = _Feed(self.strategy.new_state(symbol))
            if feed.last_ts is None:
                requests[symbol] = (self._history_bars, None)
            else:
                requests[symbol] = (CATCHUP_LIMIT, feed.last_ts + 1)
        results = self.exchange.fetch_candles_many(self.config.timeframe, requests)

        close_cutoff = now - self._tf_ms - CLOSE_GRACE_MS
        priced: list[str] = []
        fresh: dict[str, list[Candle]] = {}  # newly closed bars, per symbol
        current: dict[str, bool] = {}  # is the newest fresh bar the present one?
        latest_ts: int | None = None
        for symbol, (limit, since) in requests.items():
            rows = results.get(symbol)
            if isinstance(rows, Exception):
                self.log.warning("market data failed for %s; skipping: %s", symbol, rows)
                continue
            if not rows:
                self.log.warning("no candles returned for %s; skipping", symbol)
                continue
            newest = rows[-1]
            self._last_prices[symbol] = newest.close
            priced.append(symbol)
            if latest_ts is None or newest.timestamp > latest_ts:
                latest_ts = newest.timestamp
            closed = _closed_after(rows, self._feeds[symbol].last_ts, close_cutoff)
            if closed:
                fresh[symbol] = closed
                # A full incremental page means more history is still queued behind it.
                current[symbol] = since is None or len(rows) < limit

        # 2. Mark to market and update the kill-switch.
        equity = self.portfolio.equity(self._last_prices)
        self.risk.update_equity(equity)
        halted = self.risk.is_halted(equity)
        if halted:
            self.log.warning(
                "DRAWDOWN KILL-SWITCH active: drawdown %.2f%% >= %.2f%%; "
                "no new positions will be opened",
                self.risk.drawdown(equity) * 100,
                self.config.risk.max_drawdown_pct * 100,
            )

        # 3. Settle perpetual funding before exits, so a position that funding pushes
        # through its stop is exited on this bar rather than the next.
        self._settle_funding(latest_ts)

        # 4. Protective exits first (risk has priority over fresh entries).
        exited_this_cycle: set[str] = set()
        for symbol in priced:
            position = self.portfolio.positions.get(symbol)
            if position is None or position.amount <= 0:
                continue
            price = self._last_prices[symbol]
            # Ratchet the favourable extreme in the direction the position profits.
            position.peak_price = max(position.peak_price, price)
            position.trough_price = (
                min(position.trough_price, price) if position.trough_price > 0 else price
            )
            reason = self.risk.protective_exit(position, price)
            if reason:
                self._close_position(symbol, reason)
                exited_this_cycle.add(symbol)

        # 5. Strategy-driven entries/exits on newly closed bars.
        signals = self._advance_strategy(fresh, current)
        for symbol, signal in signals.items():
            price = self._last_prices[symbol]

            # A signal against an open position always closes it first. With shorts
            # enabled the engine then re-enters the other way (stop-and-reverse); the
            # close and the entry stay separate orders so the ledger is unambiguous.
            position = self.portfolio.positions.get(symbol)
            if position is not None and position.amount > 0:
                opposes = (
                    signal.type == SignalType.SELL
                    if not position.is_short
                    else signal.type == SignalType.BUY
                )
                if opposes:
                    self._close_position(symbol, f"strategy {signal.type.value} ({signal.reason})")
                # A signal that *agrees* with the open position falls through to sizing,
                # where allow_averaging_in decides whether to top it up (DCA) or decline.

            wants_short = signal.type == SignalType.SELL
            if wants_short and not self._allow_shorts:
                continue  # spot mode: a SELL is purely an exit
            if symbol in exited_this_cycle:
                continue  # don't re-enter a symbol we just stopped out of this cycle
            if halted:
                continue

            side = PositionSide.SHORT if wants_short else PositionSide.LONG
            has_position = self.portfolio.has_position(symbol)
            position_notional = (
                self.portfolio.positions[symbol].notional(price) if has_position else 0.0
            )
            decision = self.risk.size_entry(
                equity=equity,
                price=price,
                open_positions=self.portfolio.open_position_count,
                has_position=has_position,
                position_notional=position_notional,
                side=side,
            )
            if not decision.approved:
                self.log.debug("%s %s skipped: %s", side.value, symbol, decision.reason)
                continue
            request = OrderRequest(
                symbol=symbol,
                side=OrderSide.SELL if wants_short else OrderSide.BUY,
                amount=decision.amount,
                reason=f"{signal.reason} | {decision.reason}",
            )
            self._submit(request)

        # snapshot() walks every position and rounds a dict of dicts; logging would
        # discard that work whenever INFO is off (backtests silence the engine), so
        # don't do it in the first place.
        if self.log.isEnabledFor(logging.INFO):
            self.log.info("cycle complete | %s", self.portfolio.snapshot(self._last_prices))

    # -- helpers ---------------------------------------------------------------
    def _advance_strategy(
        self, fresh: dict[str, list[Candle]], current: dict[str, bool]
    ) -> dict[str, Signal]:
        """Fold new closed bars into each symbol's state; return actionable signals.

        Only the newest bar of a symbol that is up to date can produce a trade. Older
        bars (warm-up history, or a catch-up backlog) shape the indicators but their
        signals are stale by definition and are discarded.
        """
        contexts: dict[str, MarketContext] = {}
        if self._wants_context:
            acting = [symbol for symbol, ok in current.items() if ok]
            if acting:
                contexts = self._contexts_for(acting)

        signals: dict[str, Signal] = {}
        for symbol, bars in fresh.items():
            feed = self._feeds[symbol]
            update = feed.state.update
            for candle in bars[:-1]:
                update(candle)
            signal = update(bars[-1], contexts.get(symbol))
            feed.last_ts = bars[-1].timestamp
            if current[symbol] and signal.type != SignalType.HOLD:
                signals[symbol] = signal
        return signals

    def _funding_rates(self, symbols: list[str]) -> dict[str, float | None]:
        """Venue funding rates, fetched at most once per symbol per cycle (batched)."""
        missing = [s for s in symbols if s not in self._cycle_rates]
        if missing:
            fetched = self.exchange.fetch_funding_rates(missing)
            for symbol in missing:
                self._cycle_rates[symbol] = fetched.get(symbol)
        return {s: self._cycle_rates[s] for s in symbols}

    def _contexts_for(self, symbols: list[str]) -> dict[str, MarketContext]:
        fallback = self.config.derivatives.funding_rate
        out: dict[str, MarketContext] = {}
        for symbol, rate in self._funding_rates(symbols).items():
            if rate is None and fallback:
                # Fall back to the configured rate so backtests and venues without a
                # funding endpoint still expose a usable signal.
                rate = fallback
            out[symbol] = MarketContext(
                symbol=symbol,
                funding_rate=rate,
                funding_interval_hours=self.config.derivatives.funding_interval_hours,
            )
        return out

    def _settle_funding(self, now: int | None) -> None:
        """Charge/credit perpetual funding once per funding interval.

        ``now`` is the newest candle timestamp this cycle (``None`` if no data arrived).
        """
        if now is None:
            return
        interval_ms = int(self.config.derivatives.funding_interval_hours * 3_600_000)
        if interval_ms <= 0:
            return
        # Anchor the clock on the first cycle even when flat, so the first position
        # opened is not immediately charged for time it was not held.
        if self._last_funding_ts is None:
            self._last_funding_ts = now
            return
        elapsed = now - self._last_funding_ts
        if elapsed < interval_ms:
            return

        # Catch up whole intervals, so a coarse timeframe (e.g. 1d bars with 8h funding)
        # still pays the right number of settlements. The clock advances whether or not we
        # hold anything, otherwise a flat spell would bank intervals and over-charge later.
        intervals = int(elapsed // interval_ms)
        self._last_funding_ts += intervals * interval_ms
        if not self.portfolio.positions:
            return

        fallback = self.config.derivatives.funding_rate
        rates: dict[str, float] = {}
        for symbol, rate in self._funding_rates(list(self.portfolio.positions)).items():
            if rate is None:
                rate = fallback
            if rate:
                rates[symbol] = rate
        if not rates:
            return
        for _ in range(intervals):
            paid = self.portfolio.apply_funding(rates, self._last_prices)
            if paid:
                self.log.debug("funding settled: %+.4f %s", -paid, self.portfolio.quote_currency)

    def _close_position(self, symbol: str, reason: str) -> None:
        position = self.portfolio.positions[symbol]
        # Closing a short means buying it back.
        side = OrderSide.BUY if position.is_short else OrderSide.SELL
        request = OrderRequest(
            symbol=symbol,
            side=side,
            amount=position.amount,
            reason=reason,
        )
        self._submit(request)

    def _submit(self, request: OrderRequest) -> Order | None:
        assert self.broker is not None, "engine has no broker configured"
        try:
            order = self.broker.execute(request)
        except ExchangeError as exc:
            self.log.error("order failed (%s %s): %s", request.side.value, request.symbol, exc)
            return None

        if not order.is_filled:
            self.log.warning(
                "order not filled (%s %s): status=%s", request.side.value, request.symbol,
                order.status.value,
            )
            return order

        try:
            self.portfolio.apply_fill(order)
        except ValueError as exc:
            self.log.warning("fill rejected by portfolio: %s", exc)
            return order

        self.log.info(
            "%s %s %.8f @ %.4f (fee %.4f) — %s",
            request.side.value.upper(),
            request.symbol,
            order.filled,
            order.average_price or 0.0,
            order.fee,
            request.reason,
        )
        return order


def _closed_after(rows: list[Candle], last_ts: int | None, close_cutoff: int) -> list[Candle]:
    """Candles in ``rows`` (oldest-first) newer than ``last_ts`` that have closed.

    A candle is closed once the venue lists a newer one after it, or once its period has
    ended (``timestamp <= close_cutoff``) — the latter covers quiet markets where no
    newer candle appears until the next trade.
    """
    last = len(rows) - 1
    return [
        candle
        for i, candle in enumerate(rows)
        if (last_ts is None or candle.timestamp > last_ts)
        and (i < last or candle.timestamp <= close_cutoff)
    ]


def build_engine(config: BotConfig, logger: logging.Logger | None = None) -> Engine:
    """Wire up a fully-configured engine from a :class:`BotConfig`."""
    log = logger or logging.getLogger(LOGGER_NAME)

    exchange = build_exchange(config.exchange, require_credentials=config.is_live)
    markets = exchange.load_markets()
    _validate_symbols(config.symbols, markets, log)

    strategy = build_strategy(config.strategy.name, config.strategy.params)
    risk = RiskManager(config.risk)

    if config.is_live:
        balances = exchange.fetch_balance()
        cash = balances.get(config.paper.quote_currency, 0.0)
        log.warning(
            "LIVE mode: seeding cash from %s balance = %.2f %s. "
            "Pre-existing coin holdings are NOT imported as positions.",
            exchange.name,
            cash,
            config.paper.quote_currency,
        )
        portfolio = Portfolio(
            cash=cash,
            quote_currency=config.paper.quote_currency,
            allow_shorts=config.derivatives.allow_shorts,
        )
        broker: Broker = LiveBroker(exchange)
    else:
        portfolio = Portfolio(
            cash=config.paper.starting_cash,
            quote_currency=config.paper.quote_currency,
            allow_shorts=config.derivatives.allow_shorts,
        )
        broker = None  # set below, needs the engine's price cache

    engine = Engine(config, exchange, strategy, risk, portfolio, broker=broker, logger=log)

    if not config.is_live:
        engine.broker = PaperBroker(
            price_provider=engine.last_price,
            fee_rate=config.paper.fee_rate,
            slippage_pct=config.paper.slippage_pct,
        )
    return engine


def _validate_symbols(symbols: list[str], markets: dict, log: logging.Logger) -> None:
    if not markets:
        return
    unknown = [s for s in symbols if s not in markets]
    if unknown:
        raise ExchangeError(
            f"these symbols are not available on this exchange: {unknown}. "
            "Check the BASE/QUOTE spelling for this venue."
        )
