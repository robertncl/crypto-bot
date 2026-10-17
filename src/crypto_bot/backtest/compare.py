"""Backtest several bots on the same history, so their risk and return can be compared.

Each bot keeps its own symbols and timeframe; history is downloaded once per distinct
``(exchange, symbol, timeframe)`` and shared by every bot that trades it, so the bots
are scored on identical data. A bot that cannot be scored (data unavailable, too little
history for its warm-up) gets an error row instead of aborting the comparison.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from crypto_bot.backtest.engine import Backtester, BacktestResult, fetch_history
from crypto_bot.catalog import CatalogEntry
from crypto_bot.config import ExchangeConfig
from crypto_bot.core.models import Candle
from crypto_bot.exchanges.base import ExchangeAdapter, ExchangeError

ExchangeFactory = Callable[[ExchangeConfig], ExchangeAdapter]


@dataclass
class Comparison:
    entry: CatalogEntry
    result: BacktestResult | None = None
    error: str | None = None


def _exchange_key(cfg: ExchangeConfig) -> tuple:
    return (cfg.name, cfg.sandbox, json.dumps(cfg.options, sort_keys=True, default=str))


def compare_bots(
    entries: list[CatalogEntry],
    since_ms: int,
    build_exchange: ExchangeFactory,
    *,
    on_fetch: Callable[[str, str, int], None] | None = None,
) -> list[Comparison]:
    """Backtest every entry from ``since_ms`` to now on shared history.

    ``build_exchange`` makes a public-data adapter for an exchange config (one per
    distinct exchange). ``on_fetch(symbol, timeframe, n_candles)`` reports progress.
    """
    series: dict[tuple, list[Candle] | str] = {}  # key -> candles, or an error message
    adapters: dict[tuple, ExchangeAdapter] = {}
    try:
        for entry in entries:
            cfg = entry.config
            ex_key = _exchange_key(cfg.exchange)
            for symbol in cfg.symbols:
                key = (ex_key, symbol, cfg.timeframe)
                if key in series:
                    continue
                try:
                    if ex_key not in adapters:
                        adapters[ex_key] = build_exchange(cfg.exchange)
                    candles = fetch_history(adapters[ex_key], symbol, cfg.timeframe, since_ms)
                except ExchangeError as exc:
                    series[key] = f"could not fetch {symbol} {cfg.timeframe}: {exc}"
                    continue
                series[key] = candles
                if on_fetch is not None:
                    on_fetch(symbol, cfg.timeframe, len(candles))
    finally:
        for adapter in adapters.values():
            adapter.close()

    results: list[Comparison] = []
    for entry in entries:
        cfg = entry.config
        cfg.mode = "paper"  # a comparison must never touch live order endpoints
        ex_key = _exchange_key(cfg.exchange)
        candles_by_symbol: dict[str, list[Candle]] = {}
        error = None
        for symbol in cfg.symbols:
            data = series[(ex_key, symbol, cfg.timeframe)]
            if isinstance(data, str):
                error = data
                break
            if not data:
                error = f"no history returned for {symbol} {cfg.timeframe}"
                break
            candles_by_symbol[symbol] = data
        if error is not None:
            results.append(Comparison(entry, error=error))
            continue
        try:
            results.append(Comparison(entry, result=Backtester(cfg).run(candles_by_symbol)))
        except ValueError as exc:  # e.g. not enough history for the strategy's warm-up
            results.append(Comparison(entry, error=str(exc)))
    return results
