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
