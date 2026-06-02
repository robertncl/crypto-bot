from crypto_bot.config import RiskConfig
from crypto_bot.core.models import Position
from crypto_bot.risk.manager import RiskManager


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


def test_size_buy_allocates_fraction_of_equity():
    decision = _rm().size_buy(equity=1000.0, price=50.0, open_positions=0, has_position=False)
    assert decision.approved
    assert decision.amount == 2.0  # (1000 * 0.10) / 50


def test_size_buy_rejected_when_already_holding():
    decision = _rm().size_buy(equity=1000.0, price=50.0, open_positions=1, has_position=True)
    assert not decision.approved


def test_size_buy_rejected_at_max_positions():
    decision = _rm().size_buy(equity=1000.0, price=50.0, open_positions=2, has_position=False)
    assert not decision.approved
    assert "max_open_positions" in decision.reason


def test_stop_loss_triggers():
    pos = Position("BTC/USDT", amount=1.0, entry_price=100.0)
    assert _rm().protective_exit(pos, 94.0) is not None  # -6% <= -5%
    assert "stop-loss" in _rm().protective_exit(pos, 94.0)


def test_take_profit_triggers():
    pos = Position("BTC/USDT", amount=1.0, entry_price=100.0)
    assert "take-profit" in _rm().protective_exit(pos, 116.0)  # +16% >= +15%


def test_no_exit_within_band():
    pos = Position("BTC/USDT", amount=1.0, entry_price=100.0)
    assert _rm().protective_exit(pos, 100.0) is None


def test_drawdown_kill_switch():
    rm = _rm()
    rm.update_equity(1000.0)
    assert rm.is_halted(740.0)  # 26% drawdown >= 25%
    assert not rm.is_halted(800.0)  # 20% drawdown


def test_kill_switch_disabled_when_zero():
    rm = _rm(max_drawdown_pct=0.0)
    rm.update_equity(1000.0)
    assert not rm.is_halted(1.0)
