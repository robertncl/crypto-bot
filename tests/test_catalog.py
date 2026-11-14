"""Bot catalog: metadata parsing, discovery, the shipped profiles, and comparisons."""

from __future__ import annotations

from pathlib import Path

import pytest

from crypto_bot.backtest.compare import compare_bots
from crypto_bot.catalog import PROFILES_DIR, bot_path, load_catalog
from crypto_bot.config import RETURN_POTENTIALS, RISK_LEVELS, ConfigError, load_config
from crypto_bot.core.models import Candle
from crypto_bot.exchanges.base import ExchangeError
from crypto_bot.strategies.registry import build_strategy

REPO = Path(__file__).resolve().parents[1]
SHIPPED = REPO / PROFILES_DIR

BASE = """
mode: paper
exchange: {{name: binance}}
symbols: [{symbols}]
timeframe: {timeframe}
strategy: {{name: {strategy}}}
"""

BOT = """
bot:
  name: {name}
  category: trend
  risk_level: {risk}
  return_potential: high
  summary: "  spread
    over lines  "
"""


def _write(directory: Path, stem: str, *, risk=3, bot=True, symbols="BTC/USDT",
           timeframe="1h", strategy="ma_crossover", extra="") -> Path:
    text = BASE.format(symbols=symbols, timeframe=timeframe, strategy=strategy)
    if bot:
        text += BOT.format(name=stem.title(), risk=risk)
    path = directory / f"{stem}.yaml"
    path.write_text(text + extra)
    return path


# -- metadata -------------------------------------------------------------------
def test_bot_metadata_is_parsed_and_normalised(tmp_path):
    bot = load_config(_write(tmp_path, "x", risk=4)).bot
    assert (bot.name, bot.category, bot.risk_level, bot.return_potential) == (
        "X", "trend", 4, "high"
    )
    assert bot.risk_label == "high"
    assert bot.summary == "spread over lines"  # whitespace collapsed
    assert bot.best_for == "" and bot.main_risk == ""


def test_configs_without_bot_metadata_still_load(tmp_path):
    assert load_config(_write(tmp_path, "plain", bot=False)).bot is None


@pytest.mark.parametrize(
    "bot_yaml,match",
    [
        ("bot: [1, 2]", "must be a mapping"),
        ("bot: {name: A, category: c, return_potential: low}", "bot.risk_level"),
        ("bot: {name: A, category: c, risk_level: x, return_potential: low}", "integer"),
        ("bot: {name: A, category: c, risk_level: 6, return_potential: low}", "1-5"),
        ("bot: {name: A, category: c, risk_level: 2, return_potential: huge}", "return_potential"),
        ("bot: {category: c, risk_level: 2, return_potential: low}", "bot.name"),
    ],
)
def test_bot_metadata_is_validated(tmp_path, bot_yaml, match):
    with pytest.raises(ConfigError, match=match):
        load_config(_write(tmp_path, "bad", bot=False, extra="\n" + bot_yaml + "\n"))


# -- discovery ------------------------------------------------------------------
def test_catalog_sorts_by_risk_filters_and_reports_problems(tmp_path):
    _write(tmp_path, "zeta", risk=1)
    _write(tmp_path, "alpha", risk=3)
    _write(tmp_path, "beta", risk=1)
    _write(tmp_path, "plain", bot=False)  # not a catalog bot: skipped silently
    (tmp_path / "broken.yaml").write_text("mode: sideways\n")
    entries, problems = load_catalog(tmp_path)
    assert [e.bot_id for e in entries] == ["beta", "zeta", "alpha"]
    assert [p.name for p, _ in problems] == ["broken.yaml"]
    assert entries[0].profile.name == "Beta"

    low, _ = load_catalog(tmp_path, max_risk=2)
    assert [e.bot_id for e in low] == ["beta", "zeta"]


def test_bot_path_resolves_names_and_rejects_paths(tmp_path):
    path = _write(tmp_path, "grid")
    assert bot_path("grid", tmp_path) == path
    with pytest.raises(ConfigError, match="unknown bot 'nope'; available: grid"):
        bot_path("nope", tmp_path)
    for bad in ("../grid", "grid.yaml", "a/b", "", "-x"):
        with pytest.raises(ConfigError, match="invalid bot name"):
            bot_path(bad, tmp_path)


# -- the shipped catalog ----------------------------------------------------------
def test_every_shipped_profile_is_a_valid_catalog_bot():
    paths = sorted(SHIPPED.glob("*.yaml"))
    entries, problems = load_catalog(SHIPPED)
    assert problems == []
    assert len(entries) == len(paths)  # nothing in profiles/ lacks metadata
    for entry in entries:
        p = entry.profile
        assert p.name and p.category and p.summary and p.best_for and p.main_risk
        assert p.return_potential in RETURN_POTENTIALS
        assert entry.config.mode == "paper"  # shipped bots never default to live
        build_strategy(entry.config.strategy.name, entry.config.strategy.params)


def test_shipped_catalog_spans_every_risk_level():
    entries, _ = load_catalog(SHIPPED)
    assert {e.profile.risk_level for e in entries} == set(RISK_LEVELS)


def test_shipped_bots_carry_the_settings_their_category_depends_on():
    entries = {e.bot_id: e.config for e in load_catalog(SHIPPED)[0]}
    grid = entries["grid"]
    assert grid.risk.allow_averaging_in and grid.risk.max_position_pct > 0
    assert grid.risk.take_profit_pct == 0  # the grid takes profit line by line
    assert not grid.derivatives.allow_shorts

    perp = entries["perp-trend"]
    assert perp.derivatives.allow_shorts
    assert all(":" in s for s in perp.symbols)  # perpetual swaps, not spot pairs

    assert entries["dca"].risk.allow_averaging_in


# -- comparison -------------------------------------------------------------------
class _History:
    """Public-data adapter double: serves a sine wave and counts downloads."""

    def __init__(self, fail: set[str] = frozenset(), empty: set[str] = frozenset()):
        self.fail, self.empty = fail, empty
        self.requests: list[tuple[str, str]] = []
        self.closed = 0

    def __call__(self, cfg):
        return self

    def fetch_candles(self, symbol, timeframe, limit=200, since=None):
        import math

        self.requests.append((symbol, timeframe))
        if symbol in self.fail:
            raise ExchangeError("venue down")
        if symbol in self.empty or since is None or since > 1_700_000_000_000:
            return []
        return [Candle(1_700_000_000_000 + i * 3_600_000, c, c, c, c, 1.0)
                for i, c in ((i, 100 + 10 * math.sin(i / 8)) for i in range(300))]

    def close(self):
        self.closed += 1


def test_compare_shares_history_between_bots_and_scores_each(tmp_path):
    _write(tmp_path, "one", risk=1, symbols="BTC/USDT, ETH/USDT")
    _write(tmp_path, "two", risk=2, symbols="BTC/USDT", strategy="breakout")
    entries, _ = load_catalog(tmp_path)
    venue = _History()
    seen = []
    results = compare_bots(entries, 1_700_000_000_000, venue,
                           on_fetch=lambda s, tf, n: seen.append((s, tf, n)))
    assert [r.entry.bot_id for r in results] == ["one", "two"]
    assert all(r.error is None and r.result.bars > 0 for r in results)
    # BTC/USDT 1h is downloaded once and shared by both bots.
    assert sorted(set(venue.requests)) == [("BTC/USDT", "1h"), ("ETH/USDT", "1h")]
    assert [s for s, _, _ in seen] == ["BTC/USDT", "ETH/USDT"]
    assert venue.closed == 1
    assert all(r.entry.config.mode == "paper" for r in results)


def test_compare_reports_per_bot_errors_without_aborting(tmp_path):
    _write(tmp_path, "down", risk=1, symbols="SOL/USDT")
    _write(tmp_path, "empty", risk=2, symbols="XRP/USDT")
    _write(tmp_path, "short", risk=3, symbols="BTC/USDT")
    _write(tmp_path, "ok", risk=4, symbols="BTC/USDT", strategy="breakout")
    entries, _ = load_catalog(tmp_path)
    entries[2].config.strategy.params = {"fast_period": 5, "slow_period": 400}  # > history
    results = {r.entry.bot_id: r
               for r in compare_bots(entries, 1_700_000_000_000,
                                     _History(fail={"SOL/USDT"}, empty={"XRP/USDT"}))}
    assert "could not fetch SOL/USDT 1h: venue down" in results["down"].error
    assert results["empty"].error == "no history returned for XRP/USDT 1h"
    assert "not enough history" in results["short"].error
    assert results["ok"].result is not None


def test_rebuilt_signal_bots_follow_the_rebuild_principles():
    """What the 2021-2025 research showed, pinned so it cannot be quietly undone.

    Fixed-percent trailing stops and take-profits sat inside normal crypto swings and
    cut winners short; fast timeframes paid the edge away in fees; and deciding on a
    still-forming candle diverges from what a backtest measures.
    """
    entries = {e.bot_id: e.config for e in load_catalog(SHIPPED)[0]}
    signal_bots = ["conservative", "balanced", "adaptive", "trend", "momentum", "aggressive",
                   "perp-trend"]
    for name in signal_bots:
        cfg = entries[name]
        risk = cfg.risk
        assert cfg.signal_on_close, name  # decide on completed candles, as backtested
        assert cfg.timeframe in ("4h", "1d"), name  # slow enough for fees not to dominate
        assert risk.atr_stop_mult > 0 and risk.stop_loss_pct == 0, name  # volatility-scaled stop
        assert risk.trailing_stop_pct == 0 and risk.take_profit_pct == 0, name  # winners run
        assert risk.drawdown_cooldown_days > 0, name  # the kill-switch re-arms
    for name in signal_bots[:-1] + ["grid"]:  # every long-only bot avoids downtrends
        assert entries[name].risk.trend_filter_period > 0, name
    assert not entries["grid"].signal_on_close  # a grid reacts to live prices by design
    # DCA is deliberately untouched: it is the buy-and-hold yardstick.
    dca = entries["dca"].risk
    assert (dca.atr_stop_mult, dca.trend_filter_period, dca.stop_loss_pct) == (0, 0, 0)


def test_compare_fetches_warm_up_history_before_the_window(tmp_path):
    from crypto_bot.backtest import warmup_ms

    # One bot with a long trend filter, one without, sharing a series: it is fetched once,
    # from far enough back for the bot that needs the most warm-up.
    _write(tmp_path, "filtered", risk=1, extra="\nrisk: {trend_filter_period: 500}\n")
    _write(tmp_path, "plain", risk=2, strategy="breakout")
    entries, _ = load_catalog(tmp_path)
    since = 1_700_000_000_000 + 250 * 3_600_000
    seen = []

    class _Venue(_History):
        def fetch_candles(self, symbol, timeframe, limit=200, since=None):
            seen.append(since)
            return super().fetch_candles(symbol, timeframe, limit, 1_700_000_000_000)

    results = compare_bots(entries, since, _Venue())
    by_id = {r.entry.bot_id: r for r in results}
    lead = warmup_ms(by_id["filtered"].entry.config)
    assert lead == 501 * 3_600_000 > warmup_ms(by_id["plain"].entry.config) == 200 * 3_600_000
    assert seen[0] == since - lead and len(seen) == 1
    # Both bots trade only the requested window (50 of the 300 candles), warm from bar one.
    assert all(r.result.bars == 50 and r.result.start_ts == since for r in results)
