import pytest

from crypto_bot.core.broker import PaperBroker
from crypto_bot.core.models import OrderRequest, OrderSide, OrderStatus


def _broker(price=100.0, fee_rate=0.001, slippage_pct=0.001):
    return PaperBroker(lambda _s: price, fee_rate=fee_rate, slippage_pct=slippage_pct)


def test_buy_applies_positive_slippage_and_fee():
    order = _broker().execute(OrderRequest("BTC/USDT", OrderSide.BUY, 2.0))
    assert order.status == OrderStatus.FILLED
    assert order.filled == pytest.approx(2.0)
    assert order.average_price == pytest.approx(100.1)  # 100 * (1 + 0.001)
    assert order.fee == pytest.approx(100.1 * 2 * 0.001)
    assert order.id.startswith("paper-")


def test_sell_applies_negative_slippage():
    order = _broker().execute(OrderRequest("BTC/USDT", OrderSide.SELL, 1.0))
    assert order.average_price == pytest.approx(99.9)  # 100 * (1 - 0.001)


def test_limit_price_overrides_reference():
    order = _broker().execute(
        OrderRequest("BTC/USDT", OrderSide.BUY, 1.0, price=50.0)
    )
    # uses provided price as reference, then slippage
    assert order.average_price == pytest.approx(50.0 * 1.001)
