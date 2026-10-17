"""Grid strategy: ladder mechanics, scale-out signals, and an end-to-end backtest."""

from __future__ import annotations

import math

import pytest

from crypto_bot.backtest import Backtester
from crypto_bot.config import (
    BotConfig,
    ExchangeConfig,
    LoggingConfig,
    PaperConfig,
    RiskConfig,
    StrategyConfig,
)
from crypto_bot.core.models import Candle, SignalType
from crypto_bot.strategies.grid import GridTrader


def _walk(grid: GridTrader, prices: list[float], symbol: str = "BTC/USDT"):
    return [grid.generate([Candle(i, p, p, p, p, 1.0)], symbol) for i, p in enumerate(prices)]


def test_first_price_anchors_the_grid_without_trading():
    assert _walk(GridTrader({"spacing_pct": 0.02}), [100.0])[0].type == SignalType.HOLD


def test_buys_one_tranche_per_line_crossed_down():
    signals = _walk(GridTrader({"spacing_pct": 0.02}), [100.0, 99.0, 97.9, 96.1, 95.0])
    assert [s.type for s in signals] == [
        SignalType.HOLD,  # anchor
        SignalType.HOLD,  # 99 is above the first line (98.04)
        SignalType.BUY,   # through 98.04
        SignalType.BUY,   # through 96.12
        SignalType.HOLD,  # 95 is above the next line (94.23)
    ]
    assert not any(s.reduce_only for s in signals)


def test_sells_a_full_step_above_each_buy_as_a_reduce_only_fraction():
    grid = GridTrader({"spacing_pct": 0.02})
    signals = _walk(grid, [100.0, 97.9, 96.1, 97.0, 98.1, 99.0, 100.1])
    buys = [s for s in signals if s.type == SignalType.BUY]
    sells = [s for s in signals if s.type == SignalType.SELL]
    assert len(buys) == 2
    # 97.0 is back above the 96.12 buy line but below 98.04: no sell yet (no churn).
    assert signals[3].type == SignalType.HOLD
    assert [s.fraction for s in sells] == [pytest.approx(0.5), 1.0]  # half, then the rest
    assert all(s.reduce_only for s in sells)


def test_wobbling_around_one_line_does_not_churn():
    signals = _walk(GridTrader({"spacing_pct": 0.02}), [100.0, 97.9, 98.2, 97.95, 98.3, 97.9])
    assert sum(s.type == SignalType.BUY for s in signals) == 1
    assert not any(s.type == SignalType.SELL for s in signals)


def test_a_multi_line_jump_sells_that_many_tranches():
    grid = GridTrader({"spacing_pct": 0.02})
    prices = [100.0, 97.9, 96.1, 94.2, 92.3]  # four buys
    _walk(grid, prices)
    sell = _walk(grid, [96.2])[0]  # straight back up two lines: 92.3 -> 96.2
    assert sell.type == SignalType.SELL
    assert sell.fraction == pytest.approx(2 / 4)


def test_trails_up_while_flat_and_buys_the_next_dip_from_there():
    grid = GridTrader({"spacing_pct": 0.02})
    signals = _walk(grid, [100.0, 104.1, 106.2, 104.0])
    assert [s.type for s in signals[:3]] == [SignalType.HOLD] * 3  # flat: no sells
    assert signals[3].type == SignalType.BUY  # one line below the trailed level (106.12)


def test_exact_line_prices_are_placed_on_the_line():
    grid = GridTrader({"spacing_pct": 0.05})
    line = 100.0 * 1.05**-1
    assert _walk(grid, [100.0, line])[1].type == SignalType.BUY


def test_each_symbol_has_its_own_ladder_and_repeat_polls_are_idempotent():
    grid = GridTrader({"spacing_pct": 0.02})
    _walk(grid, [100.0], "BTC/USDT")
    _walk(grid, [10.0], "ETH/USDT")
    assert _walk(grid, [97.9], "BTC/USDT")[0].type == SignalType.BUY
    assert _walk(grid, [97.9], "BTC/USDT")[0].type == SignalType.HOLD  # same price again
    assert _walk(grid, [9.79], "ETH/USDT")[0].type == SignalType.BUY


def test_guards_and_validation():
    grid = GridTrader()
    assert grid.spacing_pct == 0.02 and grid.warmup == 1
    assert grid.generate([]).type == SignalType.HOLD
    assert grid.generate([Candle(0, 0, 0, 0, 0.0, 1.0)]).type == SignalType.HOLD
    for bad in (0, 1, -0.1):
        with pytest.raises(ValueError, match="spacing_pct"):
            GridTrader({"spacing_pct": bad})


def _grid_config(**risk) -> BotConfig:
    return BotConfig(
        mode="paper",
        exchange=ExchangeConfig(name="replay"),
        symbols=["BTC/USDT"],
        timeframe="1h",
        poll_seconds=60,
        strategy=StrategyConfig(name="grid", params={"spacing_pct": 0.02}),
        risk=RiskConfig(
            position_pct=0.1, max_open_positions=1, allow_averaging_in=True,
            max_position_pct=0.5, stop_loss_pct=0.0, take_profit_pct=0.0,
            max_drawdown_pct=0.0, **risk,
        ),
        paper=PaperConfig(starting_cash=10_000.0, fee_rate=0.001, slippage_pct=0.0005),
        logging=LoggingConfig(level="ERROR"),
    )


def test_grid_harvests_an_oscillating_market_end_to_end():
    # Price oscillates ±6% around 100 with no trend: buy-and-hold nets ~0, the grid
    # completes round trips and banks roughly one grid step on each.
    closes = [100 * (1 + 0.06 * math.sin(i / 6)) for i in range(400)]
    candles = [Candle(1_700_000_000_000 + i * 3_600_000, c, c, c, c, 1.0)
               for i, c in enumerate(closes)]
    result = Backtester(_grid_config()).run({"BTC/USDT": candles})

    assert result.num_trades >= 10
    assert result.realized_pnl > 0
    # (No win-rate check: trades are scored against the stack's *average* entry, so a sale
    # one step above the latest buy can still book as a loss against earlier, higher buys.)
    assert result.total_return_pct > result.buy_hold_return_pct
    # Partial scale-outs reached the ledger: some sells closed less than the stack.
    amounts = {round(t.amount, 6) for t in result.trades}
    assert len(amounts) > 1


def test_grid_sells_never_open_shorts_even_with_shorts_enabled():
    from crypto_bot.config import DerivativesConfig

    closes = [100.0, 97.9, 100.1, 102.5, 104.6]  # buy, sell out, then keep rising
    candles = [Candle(1_700_000_000_000 + i * 3_600_000, c, c, c, c, 1.0)
               for i, c in enumerate(closes)]
    config = _grid_config()
    config.derivatives = DerivativesConfig(allow_shorts=True)
    result = Backtester(config).run({"BTC/USDT": candles})
    assert result.num_trades == 1 and result.open_positions == 0
