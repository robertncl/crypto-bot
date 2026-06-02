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
- **Pluggable strategies** — a simple `Strategy` interface plus a registry. Ships with a
  moving-average (MA) crossover strategy.
- **Risk management** — fractional position sizing, max-open-positions cap, per-position
  stop-loss / take-profit, and a portfolio drawdown kill-switch.
- **Dependency-light core** — indicators, strategies, risk, and the paper engine are pure
  Python and unit-tested without any network or heavy dependencies.

## How it works

```
            ┌────────────┐     candles      ┌───────────────┐  signal  ┌─────────────┐
 exchange ─▶│  Exchange  │ ───────────────▶ │   Strategy    │ ───────▶ │    Risk     │
 (ccxt)     │  Adapter   │                  │ (MA crossover)│          │  Manager    │
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
# 1. Install (editable, with dev tools)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

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

## Testing

```bash
pytest            # 39 tests, no network or ccxt required
ruff check .      # lint
```

The core (indicators, strategies, risk, portfolio, paper broker, engine) is tested against an
in-memory fake exchange, so the suite is fast and offline.

## Project layout

```
src/crypto_bot/
  cli.py            # command-line interface
  config.py         # YAML + env config loading & validation
  indicators/       # pure-Python SMA / EMA / RSI
  strategies/       # Strategy interface, registry, MA crossover
  risk/             # position sizing, stops, drawdown kill-switch
  exchanges/        # ExchangeAdapter interface + ccxt implementation
  core/             # models, portfolio, brokers (paper/live), engine
tests/              # offline unit + engine tests
docs/STRATEGY_GUIDE.md   # beginner-friendly trading-strategy primer
```

## Roadmap

- Backtesting engine over historical OHLCV (the paper broker + engine are close already).
- More strategies (RSI mean-reversion, grid, breakout, DCA).
- `Decimal` money math respecting per-market precision.
- Live position reconciliation from exchange state; partial-fill handling.
- Persistence (SQLite) for trade history and crash recovery; notifications.

## License

MIT.
