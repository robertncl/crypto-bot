"""Backtesting: replay historical candles through the real trading engine."""

from crypto_bot.backtest.engine import (
    Backtester,
    BacktestResult,
    align_candles,
    fetch_history,
    warmup_ms,
)

__all__ = ["Backtester", "BacktestResult", "align_candles", "fetch_history", "warmup_ms"]
