"""The trading engine: the loop that turns market data into orders.

Each iteration (:meth:`Engine.run_once`) does, per symbol:

1. Fetch recent candles and cache the latest price.
2. Mark equity to market and update the drawdown kill-switch.
3. Apply protective exits (stop-loss / take-profit) to open positions.
4. Ask the strategy for a signal and, if actionable and risk-approved, place an order.

The engine is deliberately exchange- and broker-agnostic: swap in a :class:`PaperBroker`
or :class:`LiveBroker` and everything else is identical.
"""

from __future__ import annotations

import logging

from crypto_bot.config import BotConfig
from crypto_bot.core.broker import Broker, LiveBroker, PaperBroker
from crypto_bot.core.models import (
    MarketContext,
    Order,
    OrderRequest,
    OrderSide,
    PositionSide,
    SignalType,
)
from crypto_bot.core.portfolio import Portfolio
from crypto_bot.exchanges.base import ExchangeAdapter, ExchangeError
from crypto_bot.exchanges.factory import build_exchange
from crypto_bot.logging_setup import LOGGER_NAME
from crypto_bot.risk.manager import RiskManager
from crypto_bot.strategies.base import Strategy
from crypto_bot.strategies.registry import build_strategy


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
    ) -> None:
        self.config = config
        self.exchange = exchange
        self.strategy = strategy
        self.risk = risk
        self.portfolio = portfolio
        self.broker = broker
        self.log = logger or logging.getLogger(LOGGER_NAME)
        self._last_prices: dict[str, float] = {}
        self._candle_limit = max(strategy.warmup + 5, 200)
        self._running = False
        self._allow_shorts = config.derivatives.allow_shorts
        # Timestamp (ms) of the last funding settlement, so accrual happens once per
        # interval rather than once per poll.
        self._last_funding_ts: int | None = None

    # -- price access used by the paper broker ---------------------------------
    def last_price(self, symbol: str) -> float:
        return self._last_prices[symbol]

    # -- main loop -------------------------------------------------------------
    def run(self) -> None:
        """Poll forever until interrupted (Ctrl-C)."""
        import time

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
                try:
                    self.run_once()
                except ExchangeError as exc:
                    self.log.error("exchange error this cycle (will retry): %s", exc)
                except Exception:  # keep the bot alive across unexpected per-cycle errors
                    self.log.exception("unexpected error this cycle (will retry)")
                time.sleep(self.config.poll_seconds)
        except KeyboardInterrupt:
            self.log.info("interrupted by user; shutting down")
        finally:
            self.stop()
            self.log.info("final %s", self.portfolio.snapshot(self._last_prices))

    def stop(self) -> None:
        self._running = False

    def run_once(self) -> None:
        """Execute exactly one decision cycle across all symbols."""
        # 1. Refresh data and cache the latest prices.
        candles_by_symbol = {}
        for symbol in self.config.symbols:
            candles = self.exchange.fetch_candles(
                symbol, self.config.timeframe, limit=self._candle_limit
            )
            if not candles:
                self.log.warning("no candles returned for %s; skipping", symbol)
                continue
            candles_by_symbol[symbol] = candles
            self._last_prices[symbol] = candles[-1].close

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

        # 2b. Settle perpetual funding before exits, so a position that funding pushes
        # through its stop is exited on this bar rather than the next.
        self._settle_funding(candles_by_symbol)

        # 3. Protective exits first (risk has priority over fresh entries).
        exited_this_cycle: set[str] = set()
        for symbol, _candles in candles_by_symbol.items():
            if not self.portfolio.has_position(symbol):
                continue
            price = self._last_prices[symbol]
            position = self.portfolio.positions[symbol]
            # Ratchet the favourable extreme in the direction the position profits.
            position.peak_price = max(position.peak_price, price)
            position.trough_price = (
                min(position.trough_price, price) if position.trough_price > 0 else price
            )
            reason = self.risk.protective_exit(position, price)
            if reason:
                self._close_position(symbol, reason)
                exited_this_cycle.add(symbol)

        # 4. Strategy-driven entries/exits.
        for symbol, candles in candles_by_symbol.items():
            signal = self._ask_strategy(candles, symbol)
            if signal.type == SignalType.HOLD:
                continue
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

        self.log.info("cycle complete | %s", self.portfolio.snapshot(self._last_prices))

    # -- helpers ---------------------------------------------------------------
    def _ask_strategy(self, candles: list, symbol: str):
        """Get a signal, supplying market context only to strategies that asked for it."""
        if not getattr(self.strategy, "wants_context", False):
            return self.strategy.generate(candles, symbol)
        return self.strategy.generate(candles, symbol, self._context_for(symbol))

    def _context_for(self, symbol: str) -> MarketContext:
        rate = self.exchange.fetch_funding_rate(symbol)
        if rate is None and self.config.derivatives.funding_rate:
            # Fall back to the configured rate so backtests and venues without a funding
            # endpoint still expose a usable signal.
            rate = self.config.derivatives.funding_rate
        return MarketContext(
            symbol=symbol,
            funding_rate=rate,
            funding_interval_hours=self.config.derivatives.funding_interval_hours,
        )

    def _settle_funding(self, candles_by_symbol: dict) -> None:
        """Charge/credit perpetual funding once per funding interval."""
        if not candles_by_symbol:
            return
        interval_ms = int(self.config.derivatives.funding_interval_hours * 3_600_000)
        if interval_ms <= 0:
            return
        now = max(c[-1].timestamp for c in candles_by_symbol.values())
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
        for symbol in self.portfolio.positions:
            rate = self.exchange.fetch_funding_rate(symbol)
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
