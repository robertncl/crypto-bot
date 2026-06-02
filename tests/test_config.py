import pytest

from crypto_bot.config import ConfigError, load_config

VALID = """
mode: paper
exchange:
  name: binance
  sandbox: false
symbols:
  - BTC/USDT
  - ETH/USDT
timeframe: 1h
poll_seconds: 30
strategy:
  name: ma_crossover
  params:
    fast_period: 5
    slow_period: 20
risk:
  position_pct: 0.2
  max_open_positions: 4
paper:
  starting_cash: 5000
  quote_currency: USDT
logging:
  level: DEBUG
"""


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_loads_valid_config(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.mode == "paper"
    assert cfg.exchange.name == "binance"
    assert cfg.symbols == ["BTC/USDT", "ETH/USDT"]
    assert cfg.poll_seconds == 30
    assert cfg.strategy.params["slow_period"] == 20
    assert cfg.risk.position_pct == 0.2
    assert cfg.paper.starting_cash == 5000
    assert cfg.logging.level == "DEBUG"


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_invalid_mode(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, VALID.replace("mode: paper", "mode: turbo")))


def test_bad_symbol_format(tmp_path):
    bad = VALID.replace("  - BTC/USDT", "  - BTCUSDT")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_position_pct_out_of_range(tmp_path):
    bad = VALID.replace("position_pct: 0.2", "position_pct: 1.5")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_paper_quote_mismatch(tmp_path):
    # ETH/BTC does not quote in USDT -> invalid for the single-cash paper portfolio.
    bad = VALID.replace("  - ETH/USDT", "  - ETH/BTC")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))
