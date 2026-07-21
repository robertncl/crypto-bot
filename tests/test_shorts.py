"""Short-side position accounting, direction-aware exits, and funding settlement."""

from __future__ import annotations

import pytest

from crypto_bot.config import RiskConfig
from crypto_bot.core.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
)
from crypto_bot.core.portfolio import Portfolio
from crypto_bot.risk.manager import RiskManager


def _fill(symbol, side, amount, price, fee=0.0):
    return Order(
        symbol=symbol,
        side=side,
        amount=amount,
        type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        filled=amount,
        average_price=price,
        fee=fee,
    )


def _short_pf(cash=1000.0):
    return Portfolio(cash=cash, quote_currency="USDT", allow_shorts=True)


# -- position maths ---------------------------------------------------------------


def test_short_pnl_is_inverted():
    pos = Position("BTC/USDT", amount=2.0, entry_price=100.0, side=PositionSide.SHORT)
    assert pos.is_short
    assert pos.unrealized_pnl(90.0) == pytest.approx(20.0)  # price fell: short profits
    assert pos.unrealized_pnl(110.0) == pytest.approx(-20.0)
    assert pos.unrealized_pnl_pct(90.0) == pytest.approx(0.10)


def test_long_value_still_equals_spot_marking():
    # The margin model must reduce exactly to the old cash + amount*price for longs.
    pos = Position("BTC/USDT", amount=3.0, entry_price=100.0)
    assert pos.value(120.0) == pytest.approx(pos.notional(120.0))


# -- portfolio --------------------------------------------------------------------


def test_sell_without_position_opens_a_short():
    pf = _short_pf()
    pf.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 2.0, 100.0, fee=1.0))
    pos = pf.positions["BTC/USDT"]
    assert pos.side == PositionSide.SHORT
    assert pos.amount == pytest.approx(2.0)
    assert pf.cash == pytest.approx(1000.0 - 200.0 - 1.0)  # margin posted + fee


def test_short_then_cover_realizes_gain_when_price_falls():
    pf = _short_pf()
    pf.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 2.0, 100.0))
    pf.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 2.0, 90.0))
    assert not pf.has_position("BTC/USDT")
    assert pf.realized_pnl == pytest.approx(20.0)
    assert pf.cash == pytest.approx(1020.0)


def test_short_loses_when_price_rises():
    pf = _short_pf()
    pf.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 1.0, 100.0))
    pf.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 1.0, 115.0))
    assert pf.realized_pnl == pytest.approx(-15.0)
    assert pf.cash == pytest.approx(985.0)


def test_short_equity_marks_to_market():
    pf = _short_pf()
    pf.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 2.0, 100.0))
    assert pf.equity({"BTC/USDT": 100.0}) == pytest.approx(1000.0)  # flat at entry
    assert pf.equity({"BTC/USDT": 90.0}) == pytest.approx(1020.0)  # price fell: gain
    assert pf.equity({"BTC/USDT": 110.0}) == pytest.approx(980.0)


def test_adding_to_a_short_weights_the_entry():
    pf = _short_pf(cash=10_000.0)
    pf.apply_fill(_fill("ETH/USDT", OrderSide.SELL, 2.0, 100.0))
    pf.apply_fill(_fill("ETH/USDT", OrderSide.SELL, 2.0, 200.0))
    pos = pf.positions["ETH/USDT"]
    assert pos.side == PositionSide.SHORT
    assert pos.amount == pytest.approx(4.0)
    assert pos.entry_price == pytest.approx(150.0)


def test_shorting_stays_disabled_by_default():
    pf = Portfolio(cash=1000.0)  # allow_shorts defaults to False
    with pytest.raises(ValueError, match="allow_shorts"):
        pf.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 1.0, 100.0))


def test_cannot_cover_more_than_the_open_short():
    pf = _short_pf()
    pf.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 1.0, 100.0))
    with pytest.raises(ValueError, match="cover"):
        pf.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 2.0, 100.0))


# -- funding ----------------------------------------------------------------------


def test_funding_charges_longs_and_pays_shorts():
    rates = {"BTC/USDT": 0.001}  # +0.1% per interval: longs pay

    long_pf = Portfolio(cash=1000.0)
    long_pf.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 2.0, 100.0))
    paid = long_pf.apply_funding(rates, {"BTC/USDT": 100.0})
    assert paid == pytest.approx(0.2)  # 200 notional * 0.001
    assert long_pf.cash == pytest.approx(800.0 - 0.2)
    assert long_pf.funding_paid == pytest.approx(0.2)

    short_pf = _short_pf()
    short_pf.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 2.0, 100.0))
    received = short_pf.apply_funding(rates, {"BTC/USDT": 100.0})
    assert received == pytest.approx(-0.2)  # collected, not paid
    assert short_pf.cash == pytest.approx(800.0 + 0.2)


def test_negative_funding_reverses_who_pays():
    pf = _short_pf()
    pf.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 1.0, 100.0))
    paid = pf.apply_funding({"BTC/USDT": -0.002}, {"BTC/USDT": 100.0})
    assert paid == pytest.approx(0.2)  # shorts pay when funding is negative


def test_funding_is_a_noop_without_rates_or_positions():
    pf = _short_pf()
    assert pf.apply_funding({"BTC/USDT": 0.001}, {}) == 0.0  # no positions
    pf.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 1.0, 100.0))
    assert pf.apply_funding({}, {"BTC/USDT": 100.0}) == 0.0  # no rates
    assert pf.apply_funding({"ETH/USDT": 0.01}, {"BTC/USDT": 100.0}) == 0.0  # other symbol


# -- direction-aware protective exits ---------------------------------------------


def _rm(**overrides):
    cfg = RiskConfig(
        position_pct=0.10,
        max_open_positions=2,
        stop_loss_pct=0.05,
        take_profit_pct=0.15,
        max_drawdown_pct=0.25,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return RiskManager(cfg)


def _short(**kwargs):
    kwargs.setdefault("entry_price", 100.0)
    return Position("BTC/USDT", amount=1.0, side=PositionSide.SHORT, **kwargs)


def test_short_stop_loss_triggers_when_price_rises():
    assert "stop-loss" in _rm().protective_exit(_short(), 106.0)  # -6% for a short
    assert _rm().protective_exit(_short(), 94.0) is None  # a fall is profit, not a stop


def test_short_take_profit_triggers_when_price_falls():
    assert "take-profit" in _rm().protective_exit(_short(), 84.0)  # +16% for a short


def test_short_trailing_stop_ratchets_from_the_trough():
    rm = _rm(stop_loss_pct=0.0, take_profit_pct=0.0, trailing_stop_pct=0.05)
    pos = _short(trough_price=60.0)
    # 6% above the 60 trough — still far below the 100 entry, so only the trail catches it.
    assert "trailing-stop" in rm.protective_exit(pos, 63.6)
    assert rm.protective_exit(pos, 62.0) is None  # 3.3% above trough: inside the trail


def test_short_trailing_stop_falls_back_to_entry_when_trough_unset():
    rm = _rm(stop_loss_pct=0.0, take_profit_pct=0.0, trailing_stop_pct=0.05)
    assert "trailing-stop" in rm.protective_exit(_short(), 106.0)


def test_sizing_is_direction_agnostic():
    long_side = _rm().size_entry(equity=1000.0, price=50.0, open_positions=0, has_position=False)
    short_side = _rm().size_entry(
        equity=1000.0,
        price=50.0,
        open_positions=0,
        has_position=False,
        side=PositionSide.SHORT,
    )
    assert long_side.amount == short_side.amount == 2.0
    assert "short" in short_side.reason
