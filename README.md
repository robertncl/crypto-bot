# crypto-bot

A multi-exchange crypto trading bot in Python. One codebase trades across
**Binance, Bybit, Coinbase** and 100+ other venues via [ccxt](https://github.com/ccxt/ccxt),
with a **paper-trading-first** design so you can validate strategies with zero funds at risk
before ever touching real money.

> ⚠️ **Risk warning.** Crypto trading carries substantial risk of loss. This software is
> provided for educational purposes, with **no warranty**, and is **not financial advice**.
> Start in `paper` mode. Never trade money you cannot afford to lose. See
> [docs/STRATEGY_GUIDE.md](docs/STRATEGY_GUIDE.md) if you're new to trading.

## Features

- **Multi-exchange** — Binance, Bybit, Coinbase, and any other ccxt venue, selected by one
  config line. A clean adapter interface means you can add non-ccxt exchanges too.
- **Paper trading by default** — simulates fills against *live* market data (with
  configurable fees and slippage). No API keys required.
- **Live trading** — gated behind an explicit `--yes-i-understand-live` acknowledgement.
- **Pluggable strategies** — a simple `Strategy` interface plus a registry. Ships with eight:
  trend/momentum **MA crossover**, **Donchian breakout**, **MACD**, and volatility-adaptive
  **Supertrend**; mean-reverting **RSI** and **Bollinger-band**; scheduled **DCA /
  Auto-Invest** accumulation; and an ADX-driven **regime-switching ensemble** that routes
  each bar to a trend or mean-reversion specialist.
- **Backtesting** — `crypto-bot backtest` replays any strategy/config over historical
  candles **through the exact same engine used live** and reports return vs buy-and-hold,
  CAGR, Sharpe/Sortino, max drawdown, win rate, and profit factor (fees and slippage
  included, trade PnL net of fees).
- **Risk management** — fractional position sizing, max-open-positions cap, per-position
  stop-loss / take-profit, a peak-tracking **trailing stop**, optional averaging-in with a
  per-symbol cap, and a portfolio drawdown kill-switch.
- **Dependency-light core** — indicators, strategies, risk, backtester, and the paper engine
  are pure Python and unit-tested without any network or heavy dependencies.

## How it works

```
            ┌────────────┐     candles      ┌───────────────┐  signal  ┌─────────────┐
 exchange ─▶│  Exchange  │ ───────────────▶ │   Strategy    │ ───────▶ │    Risk     │
 (ccxt)     │  Adapter   │                  │  (pluggable)  │          │  Manager    │
            └────────────┘                  └───────────────┘          └──────┬──────┘
                  ▲                                                           │ sized order
                  │ orders                                                    ▼
            ┌────────────┐      fills        ┌──────────────┐         ┌─────────────┐
            │   Broker   │ ◀──────────────── │    Engine    │ ◀────── │  Portfolio  │
            │ paper/live │ ───────────────▶  │  (run loop)  │  book   │  (ledger)   │
            └────────────┘                   └──────────────┘         └─────────────┘
```

Each cycle the engine: fetches candles → marks equity to market (and checks the drawdown
kill-switch) → applies protective exits → asks the strategy for a signal → sizes it through
the risk manager → submits it to the broker → books the fill in the portfolio.

## Quickstart

Requires Python 3.10+.

```bash
# 1. Install pinned, hash-verified dependencies, then the bot itself
python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements-dev.txt   # exact versions, sha256-checked
pip install -e . --no-deps                             # install the crypto-bot package

# 2. Create your config from the template
cp config/config.example.yaml config/config.yaml

# 3. (Optional) keys for LIVE mode only — paper mode needs none
cp .env.example .env        # then edit .env

# 4. Sanity-check the config
crypto-bot validate-config

# 5. Backtest the configured strategy over recent history
crypto-bot backtest --days 90

# 6. Run one paper-trading cycle against live market data
crypto-bot run --once

# 7. Run continuously (Ctrl-C to stop)
crypto-bot run
```

You can also run it as a module: `python -m crypto_bot run --once`.

## Commands

| Command | Description |
| --- | --- |
| `crypto-bot run [--once] [--mode paper\|live] [--config PATH]` | Run the trading loop. `--once` does a single cycle. |
| `crypto-bot backtest [--days N] [--config PATH]` | Replay the configured strategy over the last N days of history and print performance metrics. |
| `crypto-bot validate-config [--config PATH]` | Load and validate the config file. |
| `crypto-bot balance [--config PATH]` | Show paper starting cash, or live exchange balances. |
| `crypto-bot strategies` | List registered strategies. |
| `crypto-bot version` | Print the version. |

## Configuration

Non-secret settings live in `config/config.yaml` (copy from the example). Secrets (API keys)
live in `.env` and are **never** committed. Key sections:

```yaml
mode: paper                 # paper | live
exchange:
  name: binance             # any ccxt id: binance | bybit | coinbase | ...
  sandbox: false            # use the exchange testnet where supported
symbols: [BTC/USDT, ETH/USDT]
timeframe: 1h
poll_seconds: 60
strategy:
  name: ma_crossover
  params: { fast_period: 12, slow_period: 26, ma_type: ema }
risk:
  position_pct: 0.10        # fraction of equity per new position
  max_open_positions: 3
  stop_loss_pct: 0.05       # 0 to disable
  take_profit_pct: 0.15     # 0 to disable
  max_drawdown_pct: 0.25    # halt new entries past this drawdown (0 to disable)
paper:
  starting_cash: 10000.0
  quote_currency: USDT      # in paper mode every symbol must quote in this
  fee_rate: 0.001
  slippage_pct: 0.0005
```

See [`config/config.example.yaml`](config/config.example.yaml) for the fully-commented version.

### Strategies

Ten strategies ship built-in (list them with `crypto-bot strategies`). They cover both
families — trend-following (buy strength) and mean-reversion (buy weakness) — plus a
scheduled accumulator, a regime-switching ensemble that combines the two families, and two
derivatives-oriented strategies for perpetual swaps:

| `name` | Family | Temperament | Key params (defaults) |
| --- | --- | --- | --- |
| `ma_crossover` | Trend-following | Balanced | `fast_period` 12, `slow_period` 26, `ma_type` ema |
| `macd` | Trend / momentum | Balanced | `fast_period` 12, `slow_period` 26, `signal_period` 9 |
| `supertrend` | Trend-following | Balanced / momentum | `period` 10, `multiplier` 3.0 (ATR-based) |
| `breakout` | Trend-following | Aggressive | `lookback` 20 (Donchian channel) |
| `rsi_reversion` | Mean-reversion | Balanced / contrarian | `period` 14, `oversold` 30, `overbought` 70 |
| `bollinger` | Mean-reversion | Conservative | `period` 20, `num_std` 2.0 |
| `dca` | Scheduled accumulation | Earn / buy-and-hold | `every` 1 (buy every N candles) |
| `regime` | Ensemble (ADX-routed) | Adaptive | `adx_period` 14, `adx_threshold` 25, `trend`/`range` legs |
| `trend_ls` | Trend-following (long/short) | Selective | `lookback` 20, `adx_threshold` 20, `trend_period` 100 |
| `funding_bias` | Contrarian carry (perps) | Contrarian | `enter_apr` 0.20, `trend_period` 100 |

The first six are **edge-triggered** (a signal fires once, on the bar the condition flips,
not on every bar after) and long-only; `dca` is **schedule-based** — it buys a tranche every
`every` candles and accumulates (needs `risk.allow_averaging_in: true`); `regime` measures
trend strength with **ADX** each bar and delegates to a trend-following leg in trending
markets and a mean-reversion leg in ranging ones (both legs are ordinary registered
strategies, configured by name).

`trend_ls` and `funding_bias` target **perpetual swaps**. Enable `derivatives.allow_shorts`
and every strategy above becomes long/short (they already emit SELL — it just stops being
only an exit), so a downtrend becomes tradable rather than something to sit out.
`funding_bias` trades the funding rate itself, which is the one signal that has no spot
equivalent. See [docs/DERIVATIVES.md](docs/DERIVATIVES.md) — including what is deliberately
*not* implemented (leverage, liquidation, delta-neutral legs) and why.

See [docs/STRATEGY_GUIDE.md](docs/STRATEGY_GUIDE.md) for how
each one thinks and when it wins or loses.

### Risk profiles

Risk lives in the *combination* of strategy, timeframe, position sizing, and stops — not any
one knob. Six ready-to-run profiles in [`config/profiles/`](config/profiles/) bundle
sensible combinations so you can compare temperaments without hand-tuning:

| Profile | Strategy | Timeframe | Size / max positions | Protective exits |
| --- | --- | --- | --- | --- |
| [`conservative`](config/profiles/conservative.yaml) | `bollinger` | 1d | 5% / 2 | 5% stop / 12% take-profit |
| [`balanced`](config/profiles/balanced.yaml) | `rsi_reversion` | 4h | 10% / 3 | 6% stop / 15% take-profit |
| [`trend`](config/profiles/trend.yaml) | `supertrend` | 4h | 15% / 3 | 7% stop / 21% take-profit |
| [`adaptive`](config/profiles/adaptive.yaml) | `regime` (supertrend ⇄ rsi) | 4h | 10% / 3 | 6% stop / 5% trailing stop |
| [`aggressive`](config/profiles/aggressive.yaml) | `breakout` | 1h | 20% / 5 | 8% stop / 30% take-profit |
| [`dca`](config/profiles/dca.yaml) | `dca` | 1d | 5% / 2 (averages in) | off (accumulate & hold) |

Backtest any of them before running it:

```bash
crypto-bot backtest --config config/profiles/adaptive.yaml --days 180
```

```bash
crypto-bot run --once --config config/profiles/conservative.yaml
```

All three are `paper` mode — validate any of them with zero funds at risk before considering
live. They're starting points to learn from, **not** recommendations; change one thing at a
time and watch what it does.

### API keys

For live mode, set per-exchange variables in `.env` (see `.env.example`):
`BINANCE_API_KEY` / `BINANCE_API_SECRET`, `BYBIT_API_KEY` / `BYBIT_API_SECRET`,
`COINBASE_API_KEY` / `COINBASE_API_SECRET` (+ `COINBASE_API_PASSWORD` if your key needs it).

**Create keys with trade permission only — never enable withdrawals — and IP-restrict them.**

## Going live (read this twice)

1. Validate your strategy in `paper` mode over a meaningful period.
2. Switch to the exchange **testnet** (`sandbox: true`) with test keys.
3. Only then set `mode: live`, fund a key, and run with the explicit flag:
   ```bash
   crypto-bot run --mode live --yes-i-understand-live
   ```

Live mode seeds its cash from your real quote-currency balance. **Pre-existing coin holdings
are not imported as tracked positions** — start from a clean quote balance, or extend
`build_engine` to reconcile positions. The starter assumes market orders fill fully.

## Extending

**Add a strategy** — subclass `Strategy`, implement `warmup` and `generate`, and register it:

```python
# src/crypto_bot/strategies/my_strategy.py
from crypto_bot.core.models import HOLD, Signal, SignalType
from crypto_bot.strategies.base import Strategy
from crypto_bot.strategies.registry import register_strategy

@register_strategy
class MyStrategy(Strategy):
    name = "my_strategy"
    @property
    def warmup(self) -> int: return 50
    def generate(self, candles): ...
```

Import it once (e.g. in `strategies/registry.py`) so the decorator runs, then reference
`name: my_strategy` in config.

**Add an exchange** — for ccxt venues just change `exchange.name`. For a non-ccxt venue,
implement the `ExchangeAdapter` interface in `src/crypto_bot/exchanges/`.

## Dependencies & reproducible installs

Dependencies are **fully pinned and hash-locked** for reproducible, tamper-evident installs:

- `requirements.in` / `requirements-dev.in` — the loose, human-edited source lists.
- `requirements.txt` / `requirements-dev.txt` — generated lock files pinning every package
  (including transitive deps) to an exact version **and** its `sha256` hashes.
- `pyproject.toml` carries compatible lower-bound (`>=`) ranges for packaging.

Install exactly what's locked, with hash verification (pip rejects any package whose hash
doesn't match):

```bash
pip install --require-hashes -r requirements-dev.txt   # dev (includes runtime)
pip install --require-hashes -r requirements.txt       # runtime only
```

Upgrade everything to the latest and regenerate the locks (needs `pip-tools`):

```bash
pip install pip-tools
pip-compile --generate-hashes --upgrade --allow-unsafe requirements.in
pip-compile --generate-hashes --upgrade --allow-unsafe requirements-dev.in
```

## Testing

```bash
pytest            # 60 tests, no network or ccxt required
ruff check .      # lint
```

The core (indicators, strategies, risk, portfolio, paper broker, engine) is tested against an
in-memory fake exchange, so the suite is fast and offline.

## Project layout

```
src/crypto_bot/
  cli.py            # command-line interface
  config.py         # YAML + env config loading & validation
  indicators/       # pure-Python SMA / EMA / RSI / stddev / Bollinger / Donchian / MACD / ATR / Supertrend / ADX
  strategies/       # Strategy interface, registry, 8 built-in strategies
  risk/             # position sizing, stops (incl. trailing), averaging-in, drawdown kill-switch
  backtest/         # replay engine + performance metrics (Sharpe, drawdown, trade stats)
  exchanges/        # ExchangeAdapter interface + ccxt implementation
  core/             # models, portfolio, brokers (paper/live), engine
config/profiles/    # ready-to-run conservative / balanced / trend / adaptive / aggressive / dca configs
tests/              # offline unit + engine tests
docs/STRATEGY_GUIDE.md   # beginner-friendly trading-strategy primer
```

## Roadmap

- More strategies (grid trading; fixed-quote DCA). The regime ensemble, DCA/Auto-Invest,
  MACD, Supertrend, RSI mean-reversion, Donchian breakout, and Bollinger bands are done —
  as is the backtesting engine (`crypto-bot backtest`).
- Walk-forward / out-of-sample splits and parameter sweeps on top of the backtester.
- Intrabar stop resolution in backtests (stops currently evaluate on bar closes).
- `Decimal` money math respecting per-market precision.
- Live position reconciliation from exchange state; partial-fill handling.
- Persistence (SQLite) for trade history and crash recovery; notifications.

## License

MIT.
