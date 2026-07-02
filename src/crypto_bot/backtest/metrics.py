"""Performance metrics for backtests.

Pure functions over an equity curve and a list of fills — no pandas/numpy, matching the
indicator layer. Conventions:

* Returns are per-bar simple returns; Sharpe/Sortino are annualized by the number of
  bars per year implied by the timeframe.
* ``max_drawdown`` is the largest peak-to-trough fall, as a positive fraction.
* Trade PnL is **net of fees** (both the sell leg's fee and the proportional share of
  the entry fees), so win-rate and profit factor aren't flattered by ignoring costs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from crypto_bot.core.models import Order, OrderSide

_MS_PER_YEAR = 365 * 24 * 3600 * 1000

_TIMEFRAME_UNITS_MS = {
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 7 * 86_400_000,
}


def timeframe_to_ms(timeframe: str) -> int:
    """Convert a ccxt-style timeframe ('1m', '4h', '1d', '1w') to milliseconds."""
    tf = timeframe.strip()
    if len(tf) < 2 or tf[-1] not in _TIMEFRAME_UNITS_MS or not tf[:-1].isdigit():
        raise ValueError(
            f"unsupported timeframe {timeframe!r} (expected e.g. 1m, 15m, 1h, 4h, 1d, 1w)"
        )
    return int(tf[:-1]) * _TIMEFRAME_UNITS_MS[tf[-1]]


def bars_per_year(timeframe: str) -> float:
    return _MS_PER_YEAR / timeframe_to_ms(timeframe)


def bar_returns(equity: list[float]) -> list[float]:
    """Simple per-bar returns of an equity curve."""
    out = []
    for prev, cur in zip(equity, equity[1:], strict=False):
        out.append(cur / prev - 1.0 if prev > 0 else 0.0)
    return out


def max_drawdown(equity: list[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction (0.25 = -25%)."""
    peak = float("-inf")
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def sharpe_ratio(returns: list[float], periods_per_year: float) -> float:
    """Annualized Sharpe (risk-free rate 0). 0.0 when undefined (no variance)."""
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    if variance == 0:
        return 0.0
    return mean / math.sqrt(variance) * math.sqrt(periods_per_year)


def sortino_ratio(returns: list[float], periods_per_year: float) -> float:
    """Annualized Sortino: like Sharpe but penalizing only downside deviation.

    ``inf`` when there are gains and literally zero down bars; 0.0 when undefined.
    """
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    downside_sq = sum(min(r, 0.0) ** 2 for r in returns) / len(returns)
    if downside_sq == 0:
        return math.inf if mean > 0 else 0.0
    return mean / math.sqrt(downside_sq) * math.sqrt(periods_per_year)


def cagr(start_equity: float, end_equity: float, elapsed_ms: int) -> float:
    """Compound annual growth rate over the tested span (0.0 if the span is empty)."""
    if start_equity <= 0 or end_equity <= 0 or elapsed_ms <= 0:
        return 0.0
    years = elapsed_ms / _MS_PER_YEAR
    return (end_equity / start_equity) ** (1.0 / years) - 1.0


@dataclass(frozen=True)
class TradeRecord:
    """One completed round trip (a sell closing some or all of a position)."""

    symbol: str
    amount: float
    entry_price: float
    exit_price: float
    pnl: float  # net of both legs' fees
    return_pct: float


def trades_from_orders(orders: list[Order]) -> list[TradeRecord]:
    """Pair fills into round-trip trades, mirroring the portfolio's weighted-average
    entry accounting. Each SELL closes against the running average entry; its PnL nets
    out the sell fee plus the proportional share of accumulated entry fees."""
    # per symbol: [amount, avg_entry_price, entry_fees_remaining]
    open_lots: dict[str, list[float]] = {}
    trades: list[TradeRecord] = []
    for order in orders:
        if not order.is_filled or order.average_price is None or order.filled <= 0:
            continue
        price, amount = order.average_price, order.filled
        if order.side == OrderSide.BUY:
            lot = open_lots.setdefault(order.symbol, [0.0, 0.0, 0.0])
            new_amount = lot[0] + amount
            lot[1] = (lot[1] * lot[0] + price * amount) / new_amount
            lot[0] = new_amount
            lot[2] += order.fee
        else:
            lot = open_lots.get(order.symbol)
            if lot is None or lot[0] <= 0:
                continue  # sell with no tracked entry (shouldn't happen in a backtest)
            closed = min(amount, lot[0])
            entry_fee_share = lot[2] * (closed / lot[0])
            pnl = (price - lot[1]) * closed - order.fee - entry_fee_share
            cost_basis = lot[1] * closed
            trades.append(
                TradeRecord(
                    symbol=order.symbol,
                    amount=closed,
                    entry_price=lot[1],
                    exit_price=price,
                    pnl=pnl,
                    return_pct=pnl / cost_basis if cost_basis > 0 else 0.0,
                )
            )
            lot[0] -= closed
            lot[2] -= entry_fee_share
            if lot[0] <= 1e-12:
                del open_lots[order.symbol]
    return trades


def win_rate(trades: list[TradeRecord]) -> float:
    """Fraction of trades with positive net PnL (0.0 when there are no trades)."""
    if not trades:
        return 0.0
    return sum(1 for t in trades if t.pnl > 0) / len(trades)


def profit_factor(trades: list[TradeRecord]) -> float:
    """Gross wins / gross losses (net of fees). ``inf`` if there are wins and no losses."""
    wins = sum(t.pnl for t in trades if t.pnl > 0)
    losses = -sum(t.pnl for t in trades if t.pnl < 0)
    if losses == 0:
        return math.inf if wins > 0 else 0.0
    return wins / losses
