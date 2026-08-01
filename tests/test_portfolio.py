import pytest

from crypto_bot.core.models import Order, OrderSide, OrderStatus, OrderType
from crypto_bot.core.portfolio import Portfolio


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


def test_buy_then_sell_realizes_pnl():
    pf = Portfolio(cash=1000.0, quote_currency="USDT")

    pf.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 1.0, 100.0, fee=1.0))
    assert pf.cash == pytest.approx(899.0)  # 1000 - 100 - 1 fee
    assert pf.has_position("BTC/USDT")
    assert pf.positions["BTC/USDT"].entry_price == pytest.approx(100.0)

    pf.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 1.0, 110.0, fee=1.1))
    assert pf.cash == pytest.approx(899.0 + 110.0 - 1.1)
    assert pf.realized_pnl == pytest.approx(10.0)
    assert pf.fees_paid == pytest.approx(2.1)
    assert not pf.has_position("BTC/USDT")


def test_weighted_average_entry_on_add():
    pf = Portfolio(cash=10_000.0)
    pf.apply_fill(_fill("ETH/USDT", OrderSide.BUY, 2.0, 100.0))
    pf.apply_fill(_fill("ETH/USDT", OrderSide.BUY, 2.0, 200.0))
    pos = pf.positions["ETH/USDT"]
    assert pos.amount == pytest.approx(4.0)
    assert pos.entry_price == pytest.approx(150.0)


def test_equity_marks_to_market():
    pf = Portfolio(cash=500.0)
    pf.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 1.0, 100.0))
    # cash now 400, position worth 1 * price
    assert pf.equity({"BTC/USDT": 100.0}) == pytest.approx(500.0)
    assert pf.equity({"BTC/USDT": 150.0}) == pytest.approx(550.0)


def test_insufficient_cash_raises():
    pf = Portfolio(cash=50.0)
    with pytest.raises(ValueError):
        pf.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 1.0, 100.0))


def test_oversell_raises():
    pf = Portfolio(cash=1000.0)
    pf.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 1.0, 100.0))
    with pytest.raises(ValueError):
        pf.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 2.0, 100.0))


def test_equity_falls_back_to_entry_price_when_no_market_price_given():
    p = Portfolio(cash=500.0)
    p.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 1.0, 100.0))
    # No price supplied for BTC/USDT: equity() should mark it at its entry price.
    assert p.equity({}) == pytest.approx(500.0 - 100.0 + 100.0)


def test_apply_fill_ignores_an_unfilled_order():
    p = Portfolio(cash=1000.0)
    unfilled = Order(
        symbol="BTC/USDT", side=OrderSide.BUY, amount=1.0, type=OrderType.MARKET,
        status=OrderStatus.REJECTED, filled=0.0, average_price=None,
    )
    p.apply_fill(unfilled)
    assert p.cash == 1000.0
    assert p.positions == {}


def test_apply_funding_is_a_noop_when_the_symbol_has_no_matching_rate():
    p = Portfolio(cash=1000.0)
    p.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 1.0, 100.0))
    # ETH/USDT has no position; a rate for it should not affect anything.
    net = p.apply_funding({"ETH/USDT": 0.001}, {"BTC/USDT": 100.0})
    assert net == 0.0
    assert p.cash == pytest.approx(900.0)
    assert p.funding_paid == 0.0


def test_snapshot_reports_cash_equity_and_open_positions():
    p = Portfolio(cash=1000.0)
    p.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 1.0, 100.0))
    snap = p.snapshot({"BTC/USDT": 110.0})
    assert snap["cash"] == 900.0
    assert snap["equity"] == pytest.approx(1010.0)
    assert snap["open_positions"]["BTC/USDT"]["side"] == "long"
    assert snap["open_positions"]["BTC/USDT"]["amount"] == 1.0
    assert snap["open_positions"]["BTC/USDT"]["unrealized_pnl"] == pytest.approx(10.0)


def test_snapshot_omits_closed_positions():
    p = Portfolio(cash=1000.0)
    p.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 1.0, 100.0))
    p.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 1.0, 110.0))
    snap = p.snapshot({})
    assert snap["open_positions"] == {}


def test_equity_skips_a_zero_amount_position_left_in_the_dict():
    # Defensive: apply_fill always deletes a position once its amount hits ~0, but
    # equity() shouldn't blow up (or double count) if one were left behind some other
    # way.
    from crypto_bot.core.models import Position

    p = Portfolio(cash=1000.0)
    p.positions["BTC/USDT"] = Position(symbol="BTC/USDT", amount=0.0, entry_price=100.0)
    assert p.equity({"BTC/USDT": 200.0}) == 1000.0


def test_apply_funding_skips_a_zero_amount_position_left_in_the_dict():
    from crypto_bot.core.models import Position

    p = Portfolio(cash=1000.0)
    p.positions["BTC/USDT"] = Position(symbol="BTC/USDT", amount=0.0, entry_price=100.0)
    net = p.apply_funding({"BTC/USDT": 0.001}, {"BTC/USDT": 100.0})
    assert net == 0.0
    assert p.cash == 1000.0


def test_partial_sell_leaves_the_rest_of_the_position_open():
    # Every other sell in these tests closes a position outright; this covers the
    # partial-reduction path, where margin and PnL are released pro-rata and the
    # remainder keeps its original entry price.
    p = Portfolio(cash=1000.0)
    p.apply_fill(_fill("BTC/USDT", OrderSide.BUY, 2.0, 100.0))
    assert p.cash == pytest.approx(800.0)  # 200 posted as margin

    p.apply_fill(_fill("BTC/USDT", OrderSide.SELL, 1.0, 110.0))

    pos = p.positions["BTC/USDT"]
    assert pos.amount == pytest.approx(1.0)  # half still held
    assert pos.entry_price == pytest.approx(100.0)  # a partial close never re-averages
    assert p.realized_pnl == pytest.approx(10.0)
    assert p.cash == pytest.approx(910.0)  # 100 margin released + 10 profit
    assert p.equity({"BTC/USDT": 110.0}) == pytest.approx(1020.0)
