"""Configuration loading and validation.

Config comes from a YAML file (non-secret settings) plus environment variables /
``.env`` (secrets). ``load_config`` returns a fully-validated :class:`BotConfig` or
raises :class:`ConfigError` with an actionable message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from crypto_bot.core.timeframes import timeframe_to_ms


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
    # Trailing stop: exit if price falls this far below the position's peak (0 = off).
    # Unlike the fixed stop-loss (anchored to entry), this ratchets up with the price,
    # locking in profit while a trend runs.
    trailing_stop_pct: float = 0.0
    max_drawdown_pct: float = 0.25
    # Averaging in (for accumulate/DCA strategies). Off by default so signal strategies
    # keep their one-position-per-symbol behaviour and don't re-buy on every poll.
    allow_averaging_in: bool = False
    # When averaging in, cap a single symbol's total notional at this fraction of equity
    # (0 disables the cap). Stops a scheduled strategy piling unbounded into one symbol.
    max_position_pct: float = 0.0

    # -- volatility-aware risk (all off by default) -------------------------------
    # A fixed-percent stop ignores how much the market normally moves: 5% is far away on
    # a quiet day and inside the noise on a wild one. These scale stops and position
    # size by the Average True Range (ATR) measured when the position is opened.
    atr_period: int = 14
    # Stop-loss this many ATRs from the entry price (0 = off). Works alongside
    # stop_loss_pct; whichever level is hit first exits.
    atr_stop_mult: float = 0.0
    # Trailing stop this many ATRs behind the best price seen since entry (0 = off).
    atr_trailing_mult: float = 0.0
    # Risk-based sizing: size each new position so that being stopped out at the ATR
    # stop loses this fraction of equity (0 = off; needs atr_stop_mult). position_pct
    # becomes the cap on the resulting notional, so quiet markets cannot over-size.
    risk_per_trade_pct: float = 0.0

    # Trend filter: only open (or add to) longs while price is above its simple moving
    # average over this many candles, and shorts only while below it (0 = off). Keeps
    # long-only strategies out of sustained downtrends. Exits are never blocked.
    trend_filter_period: int = 0

    # Kill-switch re-arm: once max_drawdown_pct has halted new entries for this many
    # days, measure drawdown from the current equity again (0 = stay halted until the
    # bot is restarted, the original behaviour).
    drawdown_cooldown_days: float = 0.0


@dataclass
class DerivativesConfig:
    """Perpetual-swap settings. All off by default, so spot behaviour is the baseline."""

    # Let a SELL with no long open a short instead of being a no-op. This is what makes
    # downtrends tradable; every existing strategy becomes long/short when it is on.
    allow_shorts: bool = False
    # Hours between funding settlements (8h on Binance/Bybit/OKX perps).
    funding_interval_hours: float = 8.0
    # Per-interval funding rate used for paper/backtest accrual when a live venue rate is
    # unavailable. Positive = longs pay shorts. 0.0001 (1bp/8h) is roughly the long-run
    # crypto average, which works out to ~11%/yr paid by longs.
    funding_rate: float = 0.0
    # The bot sizes and stops positions as if they were unlevered (1x). In live mode it
    # refuses to trade a perp whose *exchange-side* leverage is above 1x, because with
    # isolated margin the venue can liquidate the position before the bot's stop-loss is
    # ever reached. Set true only if you understand that risk (e.g. cross margin).
    allow_exchange_leverage: bool = False


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


#: Longest trend filter the engine can compute from a single candle request.
MAX_TREND_FILTER_PERIOD = 900

#: Risk levels used by the bot catalog, lowest to highest.
RISK_LEVELS = {1: "very low", 2: "low", 3: "moderate", 4: "high", 5: "very high"}
#: Return-potential vocabulary. "market" = tracks the traded assets (buy-and-hold beta).
RETURN_POTENTIALS = ("low", "moderate", "high", "very high", "market")


@dataclass
class BotProfile:
    """Catalog metadata for a ready-made bot (the optional ``bot:`` section of a config).

    Purely descriptive: it never changes how the bot trades. ``risk_level`` (1–5) and
    ``return_potential`` are judgements about the *recipe* — exposure, stops, timeframe,
    two-sidedness — not forecasts; ``crypto-bot compare`` measures them on real history.
    """

    name: str
    category: str
    risk_level: int
    return_potential: str
    summary: str = ""
    best_for: str = ""
    main_risk: str = ""

    @property
    def risk_label(self) -> str:
        return RISK_LEVELS[self.risk_level]


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
    derivatives: DerivativesConfig = field(default_factory=DerivativesConfig)
    bot: BotProfile | None = None  # catalog metadata, for ready-made bot profiles
    # Evaluate the strategy once per *completed* candle instead of on every poll of the
    # still-forming one. This is what a backtest does, so it keeps live behaviour in line
    # with backtested results (a forming daily candle can cross a level at noon and
    # un-cross it by the close). Protective stops still check the latest price every poll.
    # A bot started mid-candle waits for the next candle to complete before deciding.
    signal_on_close: bool = False

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
    derivatives = _build_derivatives(raw.get("derivatives") or {})

    timeframe = str(raw.get("timeframe", "1h"))
    signal_on_close = bool(raw.get("signal_on_close", False))
    if signal_on_close:
        try:  # the engine needs the candle length to tell a forming candle from a closed one
            timeframe_to_ms(timeframe)
        except ValueError as exc:
            raise ConfigError(f"signal_on_close: {exc}") from exc
    poll_seconds = int(raw.get("poll_seconds", 60))
    if poll_seconds <= 0:
        raise ConfigError("poll_seconds must be a positive integer")

    # The paper portfolio tracks a single cash currency, so every symbol must quote in it.
    # Perp symbols carry a settlement suffix (BTC/USDT:USDT) — compare the quote only.
    if mode == "paper":
        bad = [s for s in symbols if s.split("/")[1].split(":")[0] != paper.quote_currency]
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
        derivatives=derivatives,
        bot=_build_bot(raw["bot"]) if raw.get("bot") is not None else None,
        signal_on_close=signal_on_close,
    )


def _build_bot(raw: object) -> BotProfile:
    if not isinstance(raw, dict):
        raise ConfigError("`bot` section must be a mapping")
    try:
        risk_level = int(_require(raw, "risk_level", "bot.risk_level"))
    except (TypeError, ValueError) as exc:
        raise ConfigError("bot.risk_level must be an integer from 1 to 5") from exc
    if risk_level not in RISK_LEVELS:
        raise ConfigError(f"bot.risk_level must be 1-5 ({_describe_levels()}), got {risk_level}")
    potential = str(_require(raw, "return_potential", "bot.return_potential")).lower()
    if potential not in RETURN_POTENTIALS:
        raise ConfigError(
            f"bot.return_potential must be one of {list(RETURN_POTENTIALS)}, got {potential!r}"
        )
    return BotProfile(
        name=str(_require(raw, "name", "bot.name")),
        category=str(_require(raw, "category", "bot.category")).lower(),
        risk_level=risk_level,
        return_potential=potential,
        summary=" ".join(str(raw.get("summary") or "").split()),
        best_for=" ".join(str(raw.get("best_for") or "").split()),
        main_risk=" ".join(str(raw.get("main_risk") or "").split()),
    )


def _describe_levels() -> str:
    return ", ".join(f"{n} = {label}" for n, label in RISK_LEVELS.items())


def _build_derivatives(raw: dict) -> DerivativesConfig:
    cfg = DerivativesConfig(
        allow_shorts=bool(raw.get("allow_shorts", False)),
        funding_interval_hours=float(raw.get("funding_interval_hours", 8.0)),
        funding_rate=float(raw.get("funding_rate", 0.0)),
        allow_exchange_leverage=bool(raw.get("allow_exchange_leverage", False)),
    )
    if cfg.funding_interval_hours <= 0:
        raise ConfigError("derivatives.funding_interval_hours must be positive")
    # A per-interval rate beyond ±1% is almost certainly an annualized figure pasted in
    # by mistake, which would silently drain the account over a backtest.
    if abs(cfg.funding_rate) > 0.01:
        raise ConfigError(
            f"derivatives.funding_rate is a *per-interval* rate (e.g. 0.0001 = 1bp per "
            f"{cfg.funding_interval_hours:g}h); {cfg.funding_rate} looks annualized"
        )
    return cfg


def _build_risk(raw: dict) -> RiskConfig:
    risk = RiskConfig(
        position_pct=float(raw.get("position_pct", 0.10)),
        max_open_positions=int(raw.get("max_open_positions", 3)),
        stop_loss_pct=float(raw.get("stop_loss_pct", 0.05)),
        take_profit_pct=float(raw.get("take_profit_pct", 0.15)),
        trailing_stop_pct=float(raw.get("trailing_stop_pct", 0.0)),
        max_drawdown_pct=float(raw.get("max_drawdown_pct", 0.25)),
        allow_averaging_in=bool(raw.get("allow_averaging_in", False)),
        max_position_pct=float(raw.get("max_position_pct", 0.0)),
        atr_period=int(raw.get("atr_period", 14)),
        atr_stop_mult=float(raw.get("atr_stop_mult", 0.0)),
        atr_trailing_mult=float(raw.get("atr_trailing_mult", 0.0)),
        risk_per_trade_pct=float(raw.get("risk_per_trade_pct", 0.0)),
        trend_filter_period=int(raw.get("trend_filter_period", 0)),
        drawdown_cooldown_days=float(raw.get("drawdown_cooldown_days", 0.0)),
    )
    if not 0 < risk.position_pct <= 1:
        raise ConfigError("risk.position_pct must be in (0, 1]")
    if risk.max_open_positions < 1:
        raise ConfigError("risk.max_open_positions must be >= 1")
    for name in (
        "stop_loss_pct",
        "take_profit_pct",
        "trailing_stop_pct",
        "max_drawdown_pct",
        "max_position_pct",
        "atr_stop_mult",
        "atr_trailing_mult",
        "risk_per_trade_pct",
        "trend_filter_period",
        "drawdown_cooldown_days",
    ):
        if getattr(risk, name) < 0:
            raise ConfigError(f"risk.{name} must be >= 0")
    if risk.max_position_pct > 1:
        raise ConfigError("risk.max_position_pct must be in [0, 1] (0 disables the cap)")
    if risk.atr_period < 1:
        raise ConfigError("risk.atr_period must be >= 1")
    if risk.risk_per_trade_pct > 0 and risk.atr_stop_mult <= 0:
        raise ConfigError(
            "risk.risk_per_trade_pct sizes positions from the ATR stop distance, so it "
            "needs risk.atr_stop_mult > 0"
        )
    if risk.risk_per_trade_pct > 0.1:
        raise ConfigError("risk.risk_per_trade_pct above 0.1 (10% of equity per trade) is unsafe")
    # The engine reads the filter from one candle request; venues cap those near 1000.
    if risk.trend_filter_period > MAX_TREND_FILTER_PERIOD:
        raise ConfigError(
            f"risk.trend_filter_period must be <= {MAX_TREND_FILTER_PERIOD} candles "
            "(use a longer timeframe for a slower filter)"
        )
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
