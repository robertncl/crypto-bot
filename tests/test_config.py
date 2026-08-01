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


# -- derivatives section ----------------------------------------------------------


def test_derivatives_defaults_to_spot_behaviour(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.derivatives.allow_shorts is False
    assert cfg.derivatives.funding_rate == 0.0
    assert cfg.derivatives.funding_interval_hours == 8.0


def test_derivatives_section_is_parsed(tmp_path):
    raw = VALID + """
derivatives:
  allow_shorts: true
  funding_interval_hours: 4
  funding_rate: 0.0003
"""
    cfg = load_config(_write(tmp_path, raw))
    assert cfg.derivatives.allow_shorts is True
    assert cfg.derivatives.funding_interval_hours == 4.0
    assert cfg.derivatives.funding_rate == pytest.approx(0.0003)


def test_annualized_funding_rate_is_rejected(tmp_path):
    # 0.11 is plausible as an APR but catastrophic as a per-interval rate.
    raw = VALID + "\nderivatives:\n  funding_rate: 0.11\n"
    with pytest.raises(ConfigError, match="annualized"):
        load_config(_write(tmp_path, raw))


def test_non_positive_funding_interval_is_rejected(tmp_path):
    raw = VALID + "\nderivatives:\n  funding_interval_hours: 0\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, raw))


def test_perp_symbols_pass_the_quote_currency_check(tmp_path):
    # BTC/USDT:USDT is a perp settling in USDT and must be accepted in paper mode.
    raw = VALID.replace("  - BTC/USDT\n", "  - BTC/USDT:USDT\n")
    cfg = load_config(_write(tmp_path, raw))
    assert "BTC/USDT:USDT" in cfg.symbols


def test_invalid_yaml_syntax_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="could not parse YAML"):
        load_config(_write(tmp_path, "mode: paper\n  bad: [unterminated"))


def test_non_mapping_root_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="config root must be a mapping"):
        load_config(_write(tmp_path, "- just\n- a\n- list\n"))


def test_empty_symbols_list_is_rejected(tmp_path):
    raw = VALID.replace("  - BTC/USDT\n  - ETH/USDT\n", "")
    with pytest.raises(ConfigError, match="symbols"):
        load_config(_write(tmp_path, raw))


def test_nonpositive_poll_seconds_is_rejected(tmp_path):
    raw = VALID.replace("poll_seconds: 30", "poll_seconds: 0")
    with pytest.raises(ConfigError, match="poll_seconds"):
        load_config(_write(tmp_path, raw))


def test_max_open_positions_below_one_is_rejected(tmp_path):
    raw = VALID + "\nrisk:\n  max_open_positions: 0\n"
    with pytest.raises(ConfigError, match="max_open_positions"):
        load_config(_write(tmp_path, raw))


def test_negative_risk_field_is_rejected(tmp_path):
    raw = VALID + "\nrisk:\n  stop_loss_pct: -0.1\n"
    with pytest.raises(ConfigError, match="stop_loss_pct"):
        load_config(_write(tmp_path, raw))


def test_max_position_pct_above_one_is_rejected(tmp_path):
    raw = VALID + "\nrisk:\n  max_position_pct: 1.5\n"
    with pytest.raises(ConfigError, match="max_position_pct"):
        load_config(_write(tmp_path, raw))


def test_nonpositive_starting_cash_is_rejected(tmp_path):
    raw = VALID + "\npaper:\n  starting_cash: 0\n"
    with pytest.raises(ConfigError, match="starting_cash"):
        load_config(_write(tmp_path, raw))


def test_negative_fee_or_slippage_is_rejected(tmp_path):
    raw = VALID + "\npaper:\n  fee_rate: -0.001\n"
    with pytest.raises(ConfigError, match="fee_rate"):
        load_config(_write(tmp_path, raw))


def test_invalid_logging_level_is_rejected(tmp_path):
    raw = VALID.replace("level: DEBUG", "level: NOT_A_LEVEL")
    with pytest.raises(ConfigError, match="logging.level"):
        load_config(_write(tmp_path, raw))


def test_missing_required_exchange_name_is_rejected(tmp_path):
    raw = VALID.replace("  name: binance\n", "")
    with pytest.raises(ConfigError, match="missing required config"):
        load_config(_write(tmp_path, raw))


def test_non_mapping_exchange_section_is_rejected(tmp_path):
    raw = VALID.replace(
        "exchange:\n  name: binance\n  sandbox: false\n", "exchange: not-a-mapping\n"
    )
    with pytest.raises(ConfigError, match="exchange.*mapping"):
        load_config(_write(tmp_path, raw))
