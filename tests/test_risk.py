import pytest

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


def test_averaging_in_tops_up_a_held_symbol():
    rm = _rm(allow_averaging_in=True)
    decision = rm.size_buy(
        equity=1000.0, price=50.0, open_positions=1, has_position=True, position_notional=100.0
    )
    assert decision.approved
    assert decision.amount == 2.0  # another 10%-of-equity tranche: (1000 * 0.10) / 50
    assert "add" in decision.reason


def test_averaging_in_respects_position_cap():
    # 25% cap with 240 already held: only 10 of equity of room remains, so the tranche trims.
    rm = _rm(allow_averaging_in=True, max_position_pct=0.25)
    decision = rm.size_buy(
        equity=1000.0, price=50.0, open_positions=1, has_position=True, position_notional=240.0
    )
    assert decision.approved
    assert decision.amount == pytest.approx(0.2)  # trimmed to the 10 of remaining room / 50

    # Once the cap is reached, further buys are blocked outright.
    capped = rm.size_buy(
        equity=1000.0, price=50.0, open_positions=1, has_position=True, position_notional=250.0
    )
    assert not capped.approved
    assert "cap" in capped.reason


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


def test_trailing_stop_triggers_below_peak():
    rm = _rm(stop_loss_pct=0.0, take_profit_pct=0.0, trailing_stop_pct=0.05)
    pos = Position("BTC/USDT", amount=1.0, entry_price=100.0, peak_price=140.0)
    # 6% below the 140 peak — still 31% above entry, so only the trail catches it.
    assert "trailing-stop" in rm.protective_exit(pos, 131.0)
    # 4% below the peak: inside the trail, no exit.
    assert rm.protective_exit(pos, 135.0) is None


def test_trailing_stop_falls_back_to_entry_when_peak_unset():
    rm = _rm(stop_loss_pct=0.0, take_profit_pct=0.0, trailing_stop_pct=0.05)
    pos = Position("BTC/USDT", amount=1.0, entry_price=100.0)  # peak_price defaults to 0
    assert "trailing-stop" in rm.protective_exit(pos, 94.0)


def test_trailing_stop_disabled_at_zero():
    rm = _rm(stop_loss_pct=0.0, take_profit_pct=0.0, trailing_stop_pct=0.0)
    pos = Position("BTC/USDT", amount=1.0, entry_price=100.0, peak_price=200.0)
    assert rm.protective_exit(pos, 101.0) is None


def test_drawdown_kill_switch():
    rm = _rm()
    rm.update_equity(1000.0)
    assert rm.is_halted(740.0)  # 26% drawdown >= 25%
    assert not rm.is_halted(800.0)  # 20% drawdown


def test_kill_switch_disabled_when_zero():
    rm = _rm(max_drawdown_pct=0.0)
    rm.update_equity(1000.0)
    assert not rm.is_halted(1.0)
