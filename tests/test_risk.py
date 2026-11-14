import pytest

from crypto_bot.config import RiskConfig
from crypto_bot.core.models import Position, PositionSide
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


def test_size_entry_allocates_fraction_of_equity():
    decision = _rm().size_entry(equity=1000.0, price=50.0, open_positions=0, has_position=False)
    assert decision.approved
    assert decision.amount == 2.0  # (1000 * 0.10) / 50


def test_size_entry_rejected_when_already_holding():
    decision = _rm().size_entry(equity=1000.0, price=50.0, open_positions=1, has_position=True)
    assert not decision.approved


def test_averaging_in_tops_up_a_held_symbol():
    rm = _rm(allow_averaging_in=True)
    decision = rm.size_entry(
        equity=1000.0, price=50.0, open_positions=1, has_position=True, position_notional=100.0
    )
    assert decision.approved
    assert decision.amount == 2.0  # another 10%-of-equity tranche: (1000 * 0.10) / 50
    assert "add" in decision.reason


def test_averaging_in_respects_position_cap():
    # 25% cap with 240 already held: only 10 of equity of room remains, so the tranche trims.
    rm = _rm(allow_averaging_in=True, max_position_pct=0.25)
    decision = rm.size_entry(
        equity=1000.0, price=50.0, open_positions=1, has_position=True, position_notional=240.0
    )
    assert decision.approved
    assert decision.amount == pytest.approx(0.2)  # trimmed to the 10 of remaining room / 50

    # Once the cap is reached, further buys are blocked outright.
    capped = rm.size_entry(
        equity=1000.0, price=50.0, open_positions=1, has_position=True, position_notional=250.0
    )
    assert not capped.approved
    assert "cap" in capped.reason


def test_size_entry_rejected_at_max_positions():
    decision = _rm().size_entry(equity=1000.0, price=50.0, open_positions=2, has_position=False)
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


def test_peak_equity_property():
    risk = RiskManager(RiskConfig())
    assert risk.peak_equity == 0.0
    risk.update_equity(1000.0)
    assert risk.peak_equity == 1000.0


def test_drawdown_is_zero_before_any_equity_recorded():
    risk = RiskManager(RiskConfig())
    assert risk.drawdown(500.0) == 0.0


def test_size_entry_rejects_invalid_price():
    risk = RiskManager(RiskConfig())
    decision = risk.size_entry(
        equity=1000.0, price=0.0, open_positions=0, has_position=False
    )
    assert not decision.approved
    assert "invalid price" in decision.reason


def test_size_entry_rejected_when_notional_rounds_to_zero():
    risk = RiskManager(RiskConfig(position_pct=0.1))
    decision = risk.size_entry(
        equity=0.0, price=100.0, open_positions=0, has_position=False
    )
    assert not decision.approved
    assert "rounds to zero" in decision.reason


# -- volatility-scaled exits ---------------------------------------------------------
def _atr_rm(**overrides):
    base = dict(stop_loss_pct=0.0, take_profit_pct=0.0, atr_stop_mult=3.0)
    return _rm(**{**base, **overrides})


def _long(entry=100.0, atr=2.0, peak=0.0):
    return Position("BTC/USDT", 1.0, entry, peak_price=peak or entry, atr=atr)


def _short_pos(entry=100.0, atr=2.0, trough=0.0):
    return Position("BTC/USDT", 1.0, entry, side=PositionSide.SHORT,
                    trough_price=trough or entry, atr=atr)


def test_atr_stop_sits_a_multiple_of_volatility_from_entry():
    rm = _atr_rm()  # 3 x ATR(2.0) = 6.0 below a 100.0 entry
    assert rm.protective_exit(_long(), 94.1) is None
    assert "stop-loss hit (3 ATR below entry" in rm.protective_exit(_long(), 94.0)
    # The same stop is twice as far away in a market twice as volatile.
    assert rm.protective_exit(_long(atr=4.0), 94.0) is None


def test_atr_stop_mirrors_for_shorts():
    rm = _atr_rm()
    assert rm.protective_exit(_short_pos(), 105.9) is None
    assert "stop-loss hit (3 ATR above entry" in rm.protective_exit(_short_pos(), 106.0)


def test_atr_trailing_stop_follows_the_best_price():
    rm = _atr_rm(atr_stop_mult=0.0, atr_trailing_mult=2.0)
    assert rm.protective_exit(_long(peak=120.0), 116.1) is None
    assert "trailing-stop hit (2 ATR below peak 120" in rm.protective_exit(_long(peak=120.0), 116.0)
    assert "trailing-stop hit (2 ATR above trough 80" in rm.protective_exit(
        _short_pos(trough=80.0), 84.0
    )
    assert rm.protective_exit(_short_pos(trough=80.0), 83.9) is None
    # A short whose trough was never recorded trails from its entry.
    unset = Position("BTC/USDT", 1.0, 100.0, side=PositionSide.SHORT, atr=2.0)
    assert "above trough 100" in rm.protective_exit(unset, 104.0)


def test_atr_exits_need_a_known_atr():
    rm = _atr_rm(atr_trailing_mult=2.0)
    assert rm.protective_exit(_long(atr=0.0), 1.0) is None  # no ATR recorded: no ATR stop


def test_stop_level_is_the_nearest_active_stop():
    assert _rm(stop_loss_pct=0.0, take_profit_pct=0.0).stop_level(_long()) is None
    assert _rm().stop_level(_long()) == (95.0, "stop-loss")  # 5% below entry
    # ATR stop at 94 vs 5% stop at 95: the 5% stop is closer, so it fires first.
    assert _rm(atr_stop_mult=3.0).stop_level(_long()) == (95.0, "stop-loss")
    # Once price has run to 120, a 10% trail (108) is nearer than either entry stop.
    rm = _rm(atr_stop_mult=3.0, trailing_stop_pct=0.10, atr_trailing_mult=8.0)
    assert rm.stop_level(_long(peak=120.0)) == (108.0, "trailing-stop")
    assert _atr_rm(atr_trailing_mult=2.0).stop_level(_long(peak=120.0)) == (116.0, "trailing-stop")


def test_stop_level_mirrors_for_shorts():
    assert _rm().stop_level(_short_pos()) == (105.0, "stop-loss")
    rm = _rm(stop_loss_pct=0.0, trailing_stop_pct=0.10, atr_stop_mult=3.0)
    assert rm.stop_level(_short_pos(trough=80.0)) == (pytest.approx(88.0), "trailing-stop")
    assert rm.stop_level(_short_pos()) == (106.0, "stop-loss")
    unset = Position("BTC/USDT", 1.0, 100.0, side=PositionSide.SHORT)
    assert rm.stop_level(unset) == (pytest.approx(110.0), "trailing-stop")


@pytest.mark.parametrize("position", [_long(peak=130.0), _short_pos(trough=70.0)])
def test_protective_exit_fires_at_the_stop_level(position):
    rm = _rm(stop_loss_pct=0.05, take_profit_pct=0.0, trailing_stop_pct=0.08,
             atr_stop_mult=3.0, atr_trailing_mult=2.0)
    level, _kind = rm.stop_level(position)
    away = 1.001 if position.is_short else 0.999  # a hair beyond the level
    assert rm.protective_exit(position, level * away) is not None
    assert rm.protective_exit(position, level / away) is None


def test_take_profit_level():
    assert _rm(take_profit_pct=0.0).take_profit_level(_long()) is None
    assert _rm().take_profit_level(_long()) == pytest.approx(115.0)
    assert _rm().take_profit_level(_short_pos()) == pytest.approx(85.0)


# -- risk-based sizing ---------------------------------------------------------------
def _size(rm, **kwargs):
    args = dict(equity=10_000.0, price=100.0, open_positions=0, has_position=False)
    return rm.size_entry(**{**args, **kwargs})


def test_risk_based_sizing_makes_a_stop_out_cost_a_fixed_share_of_equity():
    rm = _atr_rm(position_pct=0.5, risk_per_trade_pct=0.01)
    decision = _size(rm, atr=2.0)  # stop 6% away; risking 1% => 16.7% of equity
    assert decision.approved
    stop_distance = 3.0 * 2.0
    assert decision.amount * stop_distance == pytest.approx(100.0)  # 1% of 10,000
    # A market twice as volatile gets half the position.
    assert _size(rm, atr=4.0).amount == pytest.approx(decision.amount / 2)


def test_risk_based_sizing_is_capped_by_position_pct():
    rm = _atr_rm(position_pct=0.10, risk_per_trade_pct=0.01)
    assert _size(rm, atr=0.1).amount == pytest.approx(10.0)  # 10% cap, not 333%


def test_risk_based_sizing_needs_a_known_atr():
    rm = _atr_rm(risk_per_trade_pct=0.01)
    for missing in (None, 0.0):
        decision = _size(rm, atr=missing)
        assert not decision.approved and "ATR" in decision.reason


# -- cash --------------------------------------------------------------------------
def test_entries_never_exceed_available_cash():
    rm = _rm(position_pct=0.20)  # wants 2,000 of a 10,000 equity
    assert _size(rm).amount == pytest.approx(20.0)  # no cash given: unchanged
    assert _size(rm, cash=5_000.0).amount == pytest.approx(20.0)  # plenty
    assert _size(rm, cash=1_500.0).amount == pytest.approx(14.85)  # 1,500 less a 1% buffer


def test_an_entry_cash_would_shrink_below_half_is_declined():
    rm = _rm(position_pct=0.20)
    for cash in (900.0, 0.0, -5.0):
        decision = _size(rm, cash=cash)
        assert not decision.approved and "insufficient cash" in decision.reason


# -- trend filter ---------------------------------------------------------------------
def test_trend_filter_allows_longs_above_and_shorts_below_the_average():
    rm = _rm(trend_filter_period=200)
    assert rm.trend_allows(PositionSide.LONG, 101.0, 100.0)
    assert not rm.trend_allows(PositionSide.LONG, 99.0, 100.0)
    assert rm.trend_allows(PositionSide.SHORT, 99.0, 100.0)
    assert not rm.trend_allows(PositionSide.SHORT, 101.0, 100.0)
    assert not rm.trend_allows(PositionSide.LONG, 101.0, None)  # unknown trend blocks


def test_trend_filter_off_allows_everything():
    assert _rm().trend_allows(PositionSide.LONG, 1.0, None)


# -- kill-switch cooldown -------------------------------------------------------------
DAY = 86_400_000


def test_kill_switch_stays_halted_without_a_cooldown():
    rm = _rm()
    rm.update_equity(1000.0, 0)
    rm.update_equity(700.0, DAY)
    rm.update_equity(700.0, 400 * DAY)
    assert rm.is_halted(700.0)  # original behaviour: halted until restart


def test_kill_switch_rearms_after_the_cooldown():
    rm = _rm(drawdown_cooldown_days=30)
    rm.update_equity(1000.0, 0)
    rm.update_equity(700.0, DAY)  # -30%: halted from day 1
    assert rm.is_halted(700.0)
    rm.update_equity(700.0, 30 * DAY)  # 29 days in
    assert rm.is_halted(700.0)
    rm.update_equity(700.0, 31 * DAY)  # 30 days in: the high-water mark restarts here
    assert not rm.is_halted(700.0)
    assert rm.peak_equity == 700.0
    rm.update_equity(520.0, 32 * DAY)  # and a fresh -25% from there halts again
    assert rm.is_halted(520.0)


def test_a_halt_that_recovers_resets_the_cooldown_clock():
    rm = _rm(drawdown_cooldown_days=30)
    rm.update_equity(1000.0, 0)
    rm.update_equity(700.0, DAY)
    rm.update_equity(900.0, 10 * DAY)  # recovered above the limit: no longer halted
    assert not rm.is_halted(900.0)
    rm.update_equity(700.0, 35 * DAY)  # halted again: the 30 days start now, not at day 1
    rm.update_equity(700.0, 40 * DAY)
    assert rm.is_halted(700.0)


def test_cooldown_needs_a_clock():
    rm = _rm(drawdown_cooldown_days=30)
    rm.update_equity(1000.0)
    rm.update_equity(700.0)
    assert rm.is_halted(700.0)
