"""CLI smoke tests, covering every subcommand.

Network-touching pieces (fetch_history, build_exchange, build_engine) are patched at
the point the CLI imports them, so these never hit ccxt or the network; the rest of
each command (config loading, argument handling, printed output, exit codes) runs for
real.
"""

from __future__ import annotations

import logging

import pytest

from crypto_bot import __version__
from crypto_bot.cli import main
from crypto_bot.core.models import Candle
from crypto_bot.logging_setup import LOGGER_NAME

VALID_CONFIG = """
mode: paper
exchange:
  name: binance
symbols:
  - BTC/USDT
timeframe: 1h
poll_seconds: 30
strategy:
  name: ma_crossover
  params:
    fast_period: 3
    slow_period: 8
paper:
  starting_cash: 1000
  quote_currency: USDT
logging:
  level: INFO
"""

LIVE_CONFIG = VALID_CONFIG.replace("mode: paper", "mode: live")


@pytest.fixture(autouse=True)
def _reset_logger():
    # Every _cmd_* calls setup_logging(), which is idempotent per-process; reset the
    # shared logger's handlers around each test so runs don't interfere.
    logger = logging.getLogger(LOGGER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    yield
    for h in list(logger.handlers):
        logger.removeHandler(h)


def _write(tmp_path, text=VALID_CONFIG):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return str(path)


def test_no_command_prints_help_and_returns_1(capsys):
    assert main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_version_command(capsys):
    assert main(["version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_strategies_command_lists_the_registry(capsys):
    assert main(["strategies"]) == 0
    out = capsys.readouterr().out
    assert "ma_crossover" in out
    assert "available strategies" in out


def test_validate_config_command(tmp_path, capsys):
    assert main(["validate-config", "--config", _write(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "OK: config valid" in out
    assert "ma_crossover" in out


def test_config_error_is_caught_and_reported(tmp_path, capsys):
    missing = str(tmp_path / "does-not-exist.yaml")
    code = main(["validate-config", "--config", missing])
    assert code == 2
    assert "config error" in capsys.readouterr().err


def test_balance_command_paper_mode(tmp_path, capsys):
    assert main(["balance", "--config", _write(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "PAPER balance" in out
    assert "1000.00" in out


def test_balance_command_live_mode(tmp_path, capsys, monkeypatch):
    class FakeExchange:
        name = "binance"

        def fetch_balance(self):
            return {"USDT": 250.5, "BTC": 0.01}

        def close(self):
            self.closed = True

    fake = FakeExchange()
    monkeypatch.setattr("crypto_bot.exchanges.factory.build_exchange", lambda *a, **k: fake)
    code = main(["balance", "--config", _write(tmp_path, LIVE_CONFIG)])
    assert code == 0
    out = capsys.readouterr().out
    assert "LIVE balances on binance" in out
    assert "USDT" in out and "250.5" in out
    assert fake.closed is True


def test_balance_command_live_mode_with_no_balances(tmp_path, capsys, monkeypatch):
    class FakeExchange:
        name = "binance"

        def fetch_balance(self):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(
        "crypto_bot.exchanges.factory.build_exchange", lambda *a, **k: FakeExchange()
    )
    code = main(["balance", "--config", _write(tmp_path, LIVE_CONFIG)])
    assert code == 0
    assert "no non-zero balances" in capsys.readouterr().out


def test_run_refuses_live_mode_without_acknowledgement(tmp_path, capsys):
    code = main(["run", "--config", _write(tmp_path, LIVE_CONFIG), "--once"])
    assert code == 3
    assert "Refusing to start in LIVE mode" in capsys.readouterr().err


def test_run_once_invokes_run_once_and_closes_the_exchange(tmp_path, monkeypatch):
    calls = {"run_once": 0, "run": 0, "closed": False}

    class FakeEngine:
        class _Exchange:
            def close(self_inner):
                calls["closed"] = True

        exchange = _Exchange()

        def run_once(self):
            calls["run_once"] += 1

        def run(self):
            calls["run"] += 1

    monkeypatch.setattr("crypto_bot.core.engine.build_engine", lambda config: FakeEngine())
    code = main(["run", "--config", _write(tmp_path), "--once"])
    assert code == 0
    assert calls == {"run_once": 1, "run": 0, "closed": True}


def test_run_without_once_invokes_the_polling_loop(tmp_path, monkeypatch):
    calls = {"run_once": 0, "run": 0, "closed": False}

    class FakeEngine:
        class _Exchange:
            def close(self_inner):
                calls["closed"] = True

        exchange = _Exchange()

        def run_once(self):
            calls["run_once"] += 1

        def run(self):
            calls["run"] += 1

    monkeypatch.setattr("crypto_bot.core.engine.build_engine", lambda config: FakeEngine())
    code = main(["run", "--config", _write(tmp_path)])
    assert code == 0
    assert calls == {"run_once": 0, "run": 1, "closed": True}


def test_run_closes_the_exchange_even_if_the_engine_raises(tmp_path, monkeypatch):
    calls = {"closed": False}

    class FakeEngine:
        class _Exchange:
            def close(self_inner):
                calls["closed"] = True

        exchange = _Exchange()

        def run_once(self):
            raise RuntimeError("boom")

    monkeypatch.setattr("crypto_bot.core.engine.build_engine", lambda config: FakeEngine())
    with pytest.raises(RuntimeError):
        main(["run", "--config", _write(tmp_path), "--once"])
    assert calls["closed"] is True


def test_run_mode_override(tmp_path, monkeypatch):
    seen = {}

    class FakeEngine:
        class _Exchange:
            def close(self_inner):
                pass

        exchange = _Exchange()

        def run_once(self):
            pass

    def fake_build_engine(config):
        seen["mode"] = config.mode
        return FakeEngine()

    monkeypatch.setattr("crypto_bot.core.engine.build_engine", fake_build_engine)
    code = main(["run", "--config", _write(tmp_path), "--once", "--mode", "paper"])
    assert code == 0
    assert seen["mode"] == "paper"


def _candles(n, price=100.0):
    return [Candle(1_700_000_000_000 + i * 3_600_000, price, price, price, price, 1.0)
            for i in range(n)]


def test_backtest_command_runs_end_to_end(tmp_path, monkeypatch, capsys):
    class FakeExchange:
        def close(self):
            pass

    monkeypatch.setattr(
        "crypto_bot.exchanges.factory.build_exchange", lambda *a, **k: FakeExchange()
    )
    monkeypatch.setattr(
        "crypto_bot.backtest.fetch_history",
        lambda exchange, symbol, timeframe, since_ms, **k: _candles(30),
    )
    code = main(["backtest", "--config", _write(tmp_path), "--days", "5"])
    assert code == 0
    out = capsys.readouterr().out
    assert "fetched 30 1h candles for BTC/USDT" in out
    assert "Backtest report" in out


def test_backtest_command_rejects_nonpositive_days(tmp_path, capsys):
    code = main(["backtest", "--config", _write(tmp_path), "--days", "0"])
    assert code == 2
    assert "--days must be positive" in capsys.readouterr().err


def test_backtest_command_aborts_when_a_symbol_has_no_history(tmp_path, monkeypatch, capsys):
    class FakeExchange:
        def close(self):
            pass

    monkeypatch.setattr(
        "crypto_bot.exchanges.factory.build_exchange", lambda *a, **k: FakeExchange()
    )
    monkeypatch.setattr(
        "crypto_bot.backtest.fetch_history",
        lambda exchange, symbol, timeframe, since_ms, **k: [],
    )
    code = main(["backtest", "--config", _write(tmp_path)])
    assert code == 1
    assert "no history returned for BTC/USDT" in capsys.readouterr().err


def test_backtest_command_forces_paper_mode_even_if_config_says_live(tmp_path, monkeypatch):
    class FakeExchange:
        def close(self):
            pass

    seen = {}

    def fake_build_exchange(cfg, **kwargs):
        seen["require_credentials"] = kwargs.get("require_credentials")
        return FakeExchange()

    monkeypatch.setattr("crypto_bot.exchanges.factory.build_exchange", fake_build_exchange)
    monkeypatch.setattr(
        "crypto_bot.backtest.fetch_history",
        lambda exchange, symbol, timeframe, since_ms, **k: _candles(30),
    )
    code = main(["backtest", "--config", _write(tmp_path, LIVE_CONFIG)])
    assert code == 0
    assert seen["require_credentials"] is False


def test_main_swallows_keyboard_interrupt_as_exit_130(tmp_path, monkeypatch):
    def _raise_ki(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr("crypto_bot.cli._cmd_run", _raise_ki)
    assert main(["run", "--config", _write(tmp_path), "--once"]) == 130


def test_main_tolerates_missing_dotenv(monkeypatch, capsys):
    import sys

    # A None entry in sys.modules makes `import dotenv` raise ImportError, simulating
    # python-dotenv not being installed; main() must still work (it's optional).
    monkeypatch.setitem(sys.modules, "dotenv", None)
    assert main(["version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_dotenv_is_loaded_from_the_working_directory(tmp_path, monkeypatch):
    # Credentials come from the .env where the bot is run (alongside config/config.yaml),
    # not from directories above wherever the package happens to be installed.
    import os

    (tmp_path / ".env").write_text("CRYPTO_BOT_TEST_KEY=from-cwd\n")
    monkeypatch.setenv("CRYPTO_BOT_TEST_KEY", "placeholder")  # restored after the test
    monkeypatch.delenv("CRYPTO_BOT_TEST_KEY")
    monkeypatch.chdir(tmp_path)
    assert main(["version"]) == 0
    assert os.environ["CRYPTO_BOT_TEST_KEY"] == "from-cwd"


# -- bot catalog ------------------------------------------------------------------
BOT_BLOCK = """
bot:
  name: {name}
  category: {category}
  risk_level: {risk}
  return_potential: {potential}
  summary: Does a thing.
  best_for: Some markets.
  main_risk: Other markets.
"""


def _profiles(tmp_path, monkeypatch, bots=((1, "calm"), (4, "wild"))):
    """A config/profiles directory in a fresh working directory."""
    directory = tmp_path / "config" / "profiles"
    directory.mkdir(parents=True)
    for risk, stem in bots:
        block = BOT_BLOCK.format(name=stem.title(), category="trend", risk=risk,
                                 potential="high")
        (directory / f"{stem}.yaml").write_text(VALID_CONFIG + block)
    monkeypatch.chdir(tmp_path)
    return directory


def test_bots_lists_the_catalog_lowest_risk_first(tmp_path, monkeypatch, capsys):
    _profiles(tmp_path, monkeypatch, bots=((4, "wild"), (1, "calm")))
    assert main(["bots"]) == 0
    out = capsys.readouterr().out
    assert out.index("calm") < out.index("wild")
    assert "1 very low" in out and "4 high" in out and "ma_crossover 1h" in out
    assert "main risk" not in out  # details only on request


def test_bots_details_and_risk_filter(tmp_path, monkeypatch, capsys):
    _profiles(tmp_path, monkeypatch)
    assert main(["bots", "--details", "--max-risk", "2"]) == 0
    out = capsys.readouterr().out
    assert "calm - Calm" in out and "main risk  Other markets." in out
    assert "wild" not in out


def test_bots_reports_broken_files_and_an_empty_catalog(tmp_path, monkeypatch, capsys):
    directory = _profiles(tmp_path, monkeypatch, bots=())
    (directory / "broken.yaml").write_text("mode: sideways\n")
    assert main(["bots"]) == 1
    err = capsys.readouterr().err
    assert "skipping" in err and "broken.yaml" in err and "no bots found" in err


def test_bot_flag_resolves_a_catalog_bot(tmp_path, monkeypatch, capsys):
    _profiles(tmp_path, monkeypatch)
    assert main(["validate-config", "--bot", "calm"]) == 0
    assert "OK: config valid" in capsys.readouterr().out


def test_unknown_bot_is_a_config_error(tmp_path, monkeypatch, capsys):
    _profiles(tmp_path, monkeypatch)
    assert main(["backtest", "--bot", "nope"]) == 2
    assert "unknown bot 'nope'; available: calm, wild" in capsys.readouterr().err


def test_bot_and_config_are_mutually_exclusive(tmp_path, monkeypatch):
    _profiles(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        main(["run", "--bot", "calm", "--config", "x.yaml"])


def test_every_shipped_bot_validates_through_the_cli(monkeypatch, capsys):
    from pathlib import Path

    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    for name in ("conservative", "grid", "momentum", "perp-trend"):
        assert main(["validate-config", "--bot", name]) == 0
    assert capsys.readouterr().out.count("OK: config valid") == 4


def test_compare_prints_a_ranked_table_and_error_rows(tmp_path, monkeypatch, capsys):
    from crypto_bot.backtest import Backtester
    from crypto_bot.backtest.compare import Comparison

    _profiles(tmp_path, monkeypatch)
    calls = {}

    def fake_compare(entries, since_ms, build_exchange, *, on_fetch=None):
        calls["bots"] = [e.bot_id for e in entries]
        on_fetch("BTC/USDT", "1h", 42)
        good = Backtester(entries[0].config).run({"BTC/USDT": _candles(30)})
        return [Comparison(entries[0], result=good), Comparison(entries[1], error="boom")]

    monkeypatch.setattr("crypto_bot.backtest.compare.compare_bots", fake_compare)
    monkeypatch.setattr("crypto_bot.exchanges.factory.build_exchange", lambda *a, **k: None)
    assert main(["compare", "--days", "30"]) == 0
    out = capsys.readouterr().out
    assert calls["bots"] == ["calm", "wild"]
    assert "fetched 42 1h candles for BTC/USDT" in out
    assert "MAX DD" in out and "+0.0%" in out
    assert "wild: boom" in out and "not a forecast" in out


def test_compare_validates_days_and_needs_bots(tmp_path, monkeypatch, capsys):
    _profiles(tmp_path, monkeypatch, bots=())
    assert main(["compare", "--days", "0"]) == 2
    assert main(["compare"]) == 1
    assert "no bots found" in capsys.readouterr().err


def test_bots_details_skip_empty_fields(tmp_path, monkeypatch, capsys):
    directory = _profiles(tmp_path, monkeypatch, bots=())
    minimal = "\nbot: {name: Bare, category: grid, risk_level: 3, return_potential: low}\n"
    (directory / "bare.yaml").write_text(VALID_CONFIG + minimal)
    assert main(["bots", "--details"]) == 0
    out = capsys.readouterr().out
    assert "bare - Bare" in out and "what" not in out and "best for" not in out


def test_compare_skips_broken_profiles(tmp_path, monkeypatch, capsys):
    from crypto_bot.backtest.compare import Comparison

    directory = _profiles(tmp_path, monkeypatch, bots=((2, "calm"),))
    (directory / "broken.yaml").write_text("mode: sideways\n")
    monkeypatch.setattr(
        "crypto_bot.backtest.compare.compare_bots",
        lambda entries, *a, **k: [Comparison(entries[0], error="no data")],
    )
    monkeypatch.setattr("crypto_bot.exchanges.factory.build_exchange", lambda *a, **k: None)
    assert main(["compare", "--max-risk", "2"]) == 0
    captured = capsys.readouterr()
    assert "skipping" in captured.err and "broken.yaml" in captured.err
    assert "calm: no data" in captured.out
