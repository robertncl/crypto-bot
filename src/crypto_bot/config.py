"""Configuration loading and validation.

Config comes from a YAML file (non-secret settings) plus environment variables /
``.env`` (secrets). ``load_config`` returns a fully-validated :class:`BotConfig` or
raises :class:`ConfigError` with an actionable message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass
class ExchangeConfig:
    name: str
    sandbox: bool = False
    options: dict = field(default_factory=dict)


@dataclass
class StrategyConfig:
    name: str
    params: dict = field(default_factory=dict)


@dataclass
class RiskConfig:
    position_pct: float = 0.10
    max_open_positions: int = 3
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.15
    max_drawdown_pct: float = 0.25
    # Averaging in (for accumulate/DCA strategies). Off by default so signal strategies
    # keep their one-position-per-symbol behaviour and don't re-buy on every poll.
    allow_averaging_in: bool = False
    # When averaging in, cap a single symbol's total notional at this fraction of equity
    # (0 disables the cap). Stops a scheduled strategy piling unbounded into one symbol.
    max_position_pct: float = 0.0


@dataclass
class PaperConfig:
    starting_cash: float = 10_000.0
    quote_currency: str = "USDT"
    fee_rate: float = 0.001
    slippage_pct: float = 0.0005


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str | None = None


@dataclass
class BotConfig:
    mode: str
    exchange: ExchangeConfig
    symbols: list[str]
    timeframe: str
    poll_seconds: int
    strategy: StrategyConfig
    risk: RiskConfig
    paper: PaperConfig
    logging: LoggingConfig

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


_VALID_MODES = {"paper", "live"}


def load_config(path: str | Path) -> BotConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}. Copy config/config.example.yaml to "
            f"{path} and edit it."
        )
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")

    return _build_config(raw)


def _build_config(raw: dict) -> BotConfig:
    mode = str(raw.get("mode", "paper")).lower()
    if mode not in _VALID_MODES:
        raise ConfigError(f"mode must be one of {sorted(_VALID_MODES)}, got {mode!r}")

    exchange_raw = _require_mapping(raw, "exchange")
    exchange = ExchangeConfig(
        name=str(_require(exchange_raw, "name", "exchange.name")).lower(),
        sandbox=bool(exchange_raw.get("sandbox", False)),
        options=dict(exchange_raw.get("options") or {}),
    )

    symbols = raw.get("symbols") or []
    if not isinstance(symbols, list) or not symbols:
        raise ConfigError("`symbols` must be a non-empty list of BASE/QUOTE pairs")
    symbols = [str(s).upper() for s in symbols]
    for sym in symbols:
        if "/" not in sym:
            raise ConfigError(f"symbol {sym!r} must be in BASE/QUOTE form, e.g. BTC/USDT")

    strategy_raw = _require_mapping(raw, "strategy")
    strategy = StrategyConfig(
        name=str(_require(strategy_raw, "name", "strategy.name")),
        params=dict(strategy_raw.get("params") or {}),
    )

    risk = _build_risk(raw.get("risk") or {})
    paper = _build_paper(raw.get("paper") or {})
    logging_cfg = _build_logging(raw.get("logging") or {})

    timeframe = str(raw.get("timeframe", "1h"))
    poll_seconds = int(raw.get("poll_seconds", 60))
    if poll_seconds <= 0:
        raise ConfigError("poll_seconds must be a positive integer")

    # The paper portfolio tracks a single cash currency, so every symbol must quote in it.
    if mode == "paper":
        bad = [s for s in symbols if s.split("/")[1] != paper.quote_currency]
        if bad:
            raise ConfigError(
                f"in paper mode every symbol must quote in paper.quote_currency "
                f"({paper.quote_currency}); offending: {bad}"
            )

    return BotConfig(
        mode=mode,
        exchange=exchange,
        symbols=symbols,
        timeframe=timeframe,
        poll_seconds=poll_seconds,
        strategy=strategy,
        risk=risk,
        paper=paper,
        logging=logging_cfg,
    )


def _build_risk(raw: dict) -> RiskConfig:
    risk = RiskConfig(
        position_pct=float(raw.get("position_pct", 0.10)),
        max_open_positions=int(raw.get("max_open_positions", 3)),
        stop_loss_pct=float(raw.get("stop_loss_pct", 0.05)),
        take_profit_pct=float(raw.get("take_profit_pct", 0.15)),
        max_drawdown_pct=float(raw.get("max_drawdown_pct", 0.25)),
        allow_averaging_in=bool(raw.get("allow_averaging_in", False)),
        max_position_pct=float(raw.get("max_position_pct", 0.0)),
    )
    if not 0 < risk.position_pct <= 1:
        raise ConfigError("risk.position_pct must be in (0, 1]")
    if risk.max_open_positions < 1:
        raise ConfigError("risk.max_open_positions must be >= 1")
    for name in ("stop_loss_pct", "take_profit_pct", "max_drawdown_pct", "max_position_pct"):
        if getattr(risk, name) < 0:
            raise ConfigError(f"risk.{name} must be >= 0")
    if risk.max_position_pct > 1:
        raise ConfigError("risk.max_position_pct must be in [0, 1] (0 disables the cap)")
    return risk


def _build_paper(raw: dict) -> PaperConfig:
    paper = PaperConfig(
        starting_cash=float(raw.get("starting_cash", 10_000.0)),
        quote_currency=str(raw.get("quote_currency", "USDT")).upper(),
        fee_rate=float(raw.get("fee_rate", 0.001)),
        slippage_pct=float(raw.get("slippage_pct", 0.0005)),
    )
    if paper.starting_cash <= 0:
        raise ConfigError("paper.starting_cash must be positive")
    if paper.fee_rate < 0 or paper.slippage_pct < 0:
        raise ConfigError("paper.fee_rate and paper.slippage_pct must be >= 0")
    return paper


def _build_logging(raw: dict) -> LoggingConfig:
    level = str(raw.get("level", "INFO")).upper()
    valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if level not in valid:
        raise ConfigError(f"logging.level must be one of {sorted(valid)}")
    file = raw.get("file")
    return LoggingConfig(level=level, file=str(file) if file else None)


def _require(mapping: dict, key: str, label: str | None = None):
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"missing required config: {label or key}")
    return mapping[key]


def _require_mapping(raw: dict, key: str) -> dict:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"`{key}` section is required and must be a mapping")
    return value
