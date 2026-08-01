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
