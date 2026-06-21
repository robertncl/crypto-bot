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
- **Pluggable strategies** — a simple `Strategy` interface plus a registry. Ships with six:
  trend/momentum **MA crossover**, **Donchian breakout**, **MACD**, and volatility-adaptive
  **Supertrend**, plus mean-reverting **RSI** and **Bollinger-band** strategies — spanning
  conservative to aggressive risk profiles.
- **Risk management** — fractional position sizing, max-open-positions cap, per-position
  stop-loss / take-profit, and a portfolio drawdown kill-switch.
- **Dependency-light core** — indicators, strategies, risk, and the paper engine are pure
  Python and unit-tested without any network or heavy dependencies.

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

# 5. Run one paper-trading cycle against live market data
crypto-bot run --once

# 6. Run continuously (Ctrl-C to stop)
crypto-bot run
```

You can also run it as a module: `python -m crypto_bot run --once`.

## Commands

| Command | Description |
| --- | --- |
| `crypto-bot run [--once] [--mode paper\|live] [--config PATH]` | Run the trading loop. `--once` does a single cycle. |
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

Six strategies ship built-in (list them with `crypto-bot strategies`). They cover both
families — trend-following (buy strength) and mean-reversion (buy weakness) — across the
risk spectrum:

| `name` | Family | Temperament | Key params (defaults) |
| --- | --- | --- | --- |
| `ma_crossover` | Trend-following | Balanced | `fast_period` 12, `slow_period` 26, `ma_type` ema |
| `macd` | Trend / momentum | Balanced | `fast_period` 12, `slow_period` 26, `signal_period` 9 |
| `supertrend` | Trend-following | Balanced / momentum | `period` 10, `multiplier` 3.0 (ATR-based) |
| `breakout` | Trend-following | Aggressive | `lookback` 20 (Donchian channel) |
| `rsi_reversion` | Mean-reversion | Balanced / contrarian | `period` 14, `oversold` 30, `overbought` 70 |
| `bollinger` | Mean-reversion | Conservative | `period` 20, `num_std` 2.0 |

All six are **edge-triggered** (a signal fires once, on the bar the condition flips, not on
every bar after) and long-only. See [docs/STRATEGY_GUIDE.md](docs/STRATEGY_GUIDE.md) for how
each one thinks and when it wins or loses.

### Risk profiles

Risk lives in the *combination* of strategy, timeframe, position sizing, and stops — not any
one knob. Four ready-to-run profiles in [`config/profiles/`](config/profiles/) bundle
sensible combinations so you can compare temperaments without hand-tuning:

| Profile | Strategy | Timeframe | Size / max positions | Stop / take-profit |
| --- | --- | --- | --- | --- |
| [`conservative`](config/profiles/conservative.yaml) | `bollinger` | 1d | 5% / 2 | 5% / 12% |
| [`balanced`](config/profiles/balanced.yaml) | `rsi_reversion` | 4h | 10% / 3 | 6% / 15% |
| [`trend`](config/profiles/trend.yaml) | `supertrend` | 4h | 15% / 3 | 7% / 21% |
| [`aggressive`](config/profiles/aggressive.yaml) | `breakout` | 1h | 20% / 5 | 8% / 30% |

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
  indicators/       # pure-Python SMA / EMA / RSI / stddev / Bollinger / Donchian / MACD / ATR / Supertrend
  strategies/       # Strategy interface, registry, 6 built-in strategies
  risk/             # position sizing, stops, drawdown kill-switch
  exchanges/        # ExchangeAdapter interface + ccxt implementation
  core/             # models, portfolio, brokers (paper/live), engine
config/profiles/    # ready-to-run conservative / balanced / trend / aggressive configs
tests/              # offline unit + engine tests
docs/STRATEGY_GUIDE.md   # beginner-friendly trading-strategy primer
```

## Roadmap

- Backtesting engine over historical OHLCV (the paper broker + engine are close already).
- More strategies (grid, DCA). MACD, Supertrend, RSI mean-reversion, Donchian breakout, and
  Bollinger bands are done.
- `Decimal` money math respecting per-market precision.
- Live position reconciliation from exchange state; partial-fill handling.
- Persistence (SQLite) for trade history and crash recovery; notifications.

## License

MIT.
