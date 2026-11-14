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
- **Pluggable strategies** — a simple `Strategy` interface plus a registry. Ships with eleven:
  trend/momentum **MA crossover**, **Donchian breakout**, **MACD**, and volatility-adaptive
  **Supertrend**; mean-reverting **RSI** and **Bollinger-band**; a **grid** bot that
  harvests oscillation; scheduled **DCA / Auto-Invest** accumulation; an ADX-driven
  **regime-switching ensemble**; and two perpetual-swap strategies (long/short trend and
  funding-rate positioning).
- **Bot catalog** — nine ready-made bots, each rated for **risk level (1–5)** and **return
  potential**, from a capital-preserving mean-reverter to a long/short perp trend bot.
  `crypto-bot bots` lists them, `--bot NAME` runs one, and `crypto-bot compare` backtests
  them all on the same history so you can see the risk/return trade-off in real numbers.
- **Backtesting** — `crypto-bot backtest` replays any strategy/config over historical
  candles **through the exact same engine used live** and reports return vs buy-and-hold,
  CAGR, Sharpe/Sortino, max drawdown, win rate, and profit factor (fees and slippage
  included, trade PnL net of fees).
- **Risk management** — fractional or **risk-based position sizing**, max-open-positions cap,
  stop-loss / trailing stop / take-profit as fixed percentages or **scaled to volatility
  (ATR)**, a **trend filter** that keeps long-only bots out of downtrends, optional
  averaging-in with a per-symbol cap, and a drawdown kill-switch that can re-arm after a
  cooldown.
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

By default the strategy is re-evaluated on every poll, including on the candle that is
still forming. With `signal_on_close: true` it decides **once per completed candle**, which
is what a backtest does, so live behaviour matches backtested results. Protective stops
still check the latest price on every poll. A bot started mid-candle waits for the next
candle to complete before its first decision. The ready-made signal bots all use it.

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
| `crypto-bot run [--once] [--mode paper\|live] [--config PATH \| --bot NAME]` | Run the trading loop. `--once` does a single cycle. |
| `crypto-bot backtest [--days N] [--config PATH \| --bot NAME]` | Replay the configured strategy over the last N days of history and print performance metrics. |
| `crypto-bot bots [--max-risk N] [--details]` | List the ready-made bots, lowest risk first, with category and return potential. |
| `crypto-bot compare [--days N] [--max-risk N]` | Backtest every ready-made bot on the same history and rank return, drawdown and Sharpe side by side. |
| `crypto-bot validate-config [--config PATH \| --bot NAME]` | Load and validate the config file. |
| `crypto-bot balance [--config PATH \| --bot NAME]` | Show paper starting cash, or live exchange balances. |
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
signal_on_close: false      # true = decide once per completed candle (as a backtest does)
strategy:
  name: ma_crossover
  params: { fast_period: 12, slow_period: 26, ma_type: ema }
risk:
  position_pct: 0.10        # fraction of equity per new position
  max_open_positions: 3
  stop_loss_pct: 0.05       # 0 to disable
  take_profit_pct: 0.15     # 0 to disable
  max_drawdown_pct: 0.25    # halt new entries past this drawdown (0 to disable)
  # Volatility-aware options (all off by default):
  atr_stop_mult: 0.0        # stop-loss N ATRs from entry, instead of / as well as a fixed %
  atr_trailing_mult: 0.0    # trailing stop N ATRs behind the best price
  risk_per_trade_pct: 0.0   # size so a stop-out costs this share of equity (needs atr_stop_mult)
  trend_filter_period: 0    # only buy above (short below) the N-candle moving average
  drawdown_cooldown_days: 0 # re-arm the kill-switch after N days (0 = halt until restart)
paper:
  starting_cash: 10000.0
  quote_currency: USDT      # in paper mode every symbol must quote in this
  fee_rate: 0.001
  slippage_pct: 0.0005
```

See [`config/config.example.yaml`](config/config.example.yaml) for the fully-commented version.

### Strategies

Eleven strategies ship built-in (list them with `crypto-bot strategies`). They cover both
families — trend-following (buy strength) and mean-reversion (buy weakness) — plus a grid
trader, a scheduled accumulator, a regime-switching ensemble that combines the two
families, and two derivatives-oriented strategies for perpetual swaps:

| `name` | Family | Temperament | Key params (defaults) |
| --- | --- | --- | --- |
| `ma_crossover` | Trend-following | Balanced | `fast_period` 12, `slow_period` 26, `ma_type` ema |
| `macd` | Trend / momentum | Balanced | `fast_period` 12, `slow_period` 26, `signal_period` 9 |
| `supertrend` | Trend-following | Balanced / momentum | `period` 10, `multiplier` 3.0 (ATR-based) |
| `breakout` | Trend-following | Aggressive | `lookback` 20 (Donchian channel), `exit_lookback` (shorter exit channel) |
| `rsi_reversion` | Mean-reversion | Balanced / contrarian | `period` 14, `oversold` 30, `overbought` 70, `exit_rsi` (early exit level) |
| `bollinger` | Mean-reversion | Conservative | `period` 20, `num_std` 2.0, `exit_on_mean` (exit at the average) |
| `grid` | Grid / range harvesting | Steady in ranges | `spacing_pct` 0.02 (2% between lines) |
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

`grid` is the classic exchange **grid bot**. It lays geometric price lines `spacing_pct`
apart: each fall through a line buys one tranche, and each rise through a line sells that
share of the stack (a partial, *reduce-only* exit that never opens a short). Buys and sells
sit a full step apart, so a price wobbling around one line doesn't churn fees. While it holds
nothing, the grid trails price upward. It needs `risk.allow_averaging_in: true`, with
`risk.max_position_pct` capping the stack.

`trend_ls` and `funding_bias` target **perpetual swaps**. Enable `derivatives.allow_shorts`
and every strategy above becomes long/short (they already emit SELL — it just stops being
only an exit), so a downtrend becomes tradable rather than something to sit out.
`funding_bias` trades the funding rate itself, which is the one signal that has no spot
equivalent. See [docs/DERIVATIVES.md](docs/DERIVATIVES.md) — including what is deliberately
*not* implemented (leverage, liquidation, delta-neutral legs) and why.

See [docs/STRATEGY_GUIDE.md](docs/STRATEGY_GUIDE.md) for how
each one thinks and when it wins or loses.

### Bot catalog: risk levels and return potential

A *bot* is a ready-to-run recipe: a strategy, a timeframe, position sizing and stops,
bundled in [`config/profiles/`](config/profiles/) with a `bot:` section that rates it. Risk
lives in the *combination*, not in any one knob, so each bot is rated on the whole recipe:

| Level | Rubric |
| --- | --- |
| 1 very low | ≤ 15% of equity deployed, stops on, slow timeframe |
| 2 low | ≤ 35% deployed, stops on |
| 3 moderate | ≤ 60% deployed |
| 4 high | up to fully deployed, or no stop-loss |
| 5 very high | fully deployed *and* two-sided (long/short on perpetual swaps) |

| Risk | Bot (`--bot`) | Category | Return potential | Strategy · timeframe | Best for |
| --- | --- | --- | --- | --- | --- |
| 1 very low | `conservative` — Capital Preserver | mean-reversion | low | `bollinger` · 1d | Calm, rising majors; learning with little at stake |
| 2 low | `adaptive` — Regime Switcher | adaptive | moderate | `regime` · 4h | Markets that alternate trend and range |
| 2 low | `balanced` — Balanced Trend | trend | moderate | `ma_crossover` · 1d | Sustained up-moves lasting weeks |
| 3 moderate | `grid` — Grid Trader | grid | moderate | `grid` · 4h | Sideways-to-rising, oscillating markets |
| 3 moderate | `momentum` — Momentum Swing | momentum | high | `macd` · 1d | Sustained swings |
| 3 moderate | `trend` — Trend Rider | trend | high | `supertrend` · 1d | Strong, persistent up-trends |
| 4 high | `aggressive` — Breakout Hunter | breakout | very high | `breakout` · 4h | Volatile, trending markets |
| 4 high | `dca` — Auto-Invest Accumulator | accumulation | market | `dca` · 1d | Long-horizon accumulation |
| 5 very high | `perp-trend` — Perp Long/Short Trend | long-short | very high | `trend_ls` · 4h | Strong trends either way, incl. bear markets |

"Return potential" describes how much upside the recipe *can* capture. It is not a
forecast, and higher potential always comes with deeper drawdowns. `market` means the bot
tracks the assets it holds: DCA has little timing risk but full market risk, because it never
sells, which is why it rates 4.

```bash
crypto-bot bots --details            # the catalog, with each bot's fit and main risk
crypto-bot backtest --bot grid --days 180
crypto-bot compare --days 365        # every bot, same history, side by side
crypto-bot run --once --bot conservative
```

#### How the bots were built, and what the evidence shows

The recipes were rebuilt in November 2026 from a study of Binance history from January 2021.
The last 16 months (July 2025 onward) were set aside as a **holdout** that no design
decision was allowed to see; the recipes were fixed on the earlier 4.5 years and then run
on the holdout once. What the study found, and what changed:

| Finding (2021 to mid-2025) | Change |
| --- | --- |
| Fixed-percent stops and trailing stops sat inside crypto's normal swings. One bot's 8% trailing stop alone turned a strongly profitable recipe into a losing one | Stops are scaled to volatility: 3 × ATR from entry. No fixed trailing stops |
| Take-profit capped the trend bots. One took +21% in a year its coins rose over 30× | No take-profit on trend, momentum or breakout bots: the strategy's own exit ends the trade |
| Fast timeframes paid the edge away. The 1h breakout bot spent more on fees than it earned | Trend, momentum and balanced bots trade daily candles; breakout and perp-trend trade 4h |
| Long-only bots kept buying through the 2022 bear market | Long-only bots only buy above the 200-candle moving average |
| RSI mean-reversion showed no edge after costs in any configuration tested | `balanced` now trades a daily moving-average crossover |
| The kill-switch never re-armed: one trip ended trading for good | It re-arms after a 30-day pause |

Results, with fees and slippage included. "Old" is the previous recipe run through the
same backtester:

| Bot | Design period: old → new return | New max drawdown | Holdout: old → new return | New max drawdown |
| --- | ---: | ---: | ---: | ---: |
| conservative | −0.8% → +5.0% | 2.7% | −1.8% → +0.1% | 1.5% |
| adaptive | +1.6% → +16.2% | 8.8% | −8.0% → −0.8% | 3.7% |
| balanced | +0.8% → +76.8% | 18.2% | −13.0% → +5.0% | 7.9% |
| grid | −22.2% → +20.6% | 19.7% | −22.9% → −5.8% | 17.3% |
| momentum | +6.3% → +91.7% | 20.4% | −19.2% → −8.3% | 14.9% |
| trend | +10.6% → +146.9% | 25.2% | −8.6% → +4.9% | 8.7% |
| aggressive | +132.1% → +854.7% | 36.6% | −14.5% → +12.5% | 31.0% |
| dca (unchanged) | +173.7% | 77.0% | −26.7% | 61.2% |
| perp-trend | −35.3% → +1,145.6% | 52.4% | −6.1% → +49.8% | 24.0% |

Design period: 2021-01 to 2025-06. Holdout: 2025-07 to 2026-11, during which simply holding
the same coins lost 21–31%.

Read these with care:

- **The holdout is the honest test.** Every rebuilt bot did better than its old recipe on
  data it was not fitted to, on both return and drawdown. Five of eight made money in a
  falling market; `grid`, `momentum` and `adaptive` still lost.
- **The design-period returns are large because 2021–2024 was a strong period for crypto.**
  Holding the same coins made 250% to over 3,000% across it. The bots captured part of that
  with far smaller drawdowns; they did not beat buy-and-hold in the bull years.
- **The daily bots trade rarely.** `trend` made 25 trades in 4.5 years and 5 in the holdout.
  That is too few to be statistically sure of anything.
- **`conservative` and `adaptive` have a small edge at best**, and mean-reversion in general
  was weak in this data.
- **Past performance does not predict future results.** Run `crypto-bot compare` over
  several `--days` windows yourself, and paper-trade before risking money.

The backtester itself was corrected as part of this work, which changes results for any
config: stops now trigger inside a candle at their own price (as the live bot's polling
does) instead of only at the close, entries are sized against available cash, and trade
statistics count short trades and exclude orders the portfolio rejected.

All bots are `paper` mode — validate any of them with zero funds at risk before considering
live. They're starting points to learn from, **not** recommendations; change one thing at a
time and watch what it does. To add your own, copy a profile, edit it, and give it a `bot:`
section; `crypto-bot bots` will pick it up.

### API keys

For live mode, set per-exchange variables in `.env` (see `.env.example`):
`BINANCE_API_KEY` / `BINANCE_API_SECRET`, `BYBIT_API_KEY` / `BYBIT_API_SECRET`,
`COINBASE_API_KEY` / `COINBASE_API_SECRET` (+ `COINBASE_API_PASSWORD` if your key needs it).
`.env` is read from the directory you run the bot in (or the nearest parent that has one).

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
`build_engine` to reconcile positions. Each live order is confirmed before it is booked: venues
that only acknowledge an order (Bybit, Coinbase) are polled until it fills, a still-working
order is cancelled after 10 s, and whatever actually executed (including a partial fill) is
what the bot books. For perpetual futures, set the exchange's leverage to 1× for every symbol
you trade. The bot refuses to start live otherwise (see
[docs/DERIVATIVES.md](docs/DERIVATIVES.md)).

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
pytest            # offline: no network or exchange keys required
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
  strategies/       # Strategy interface, registry, 11 built-in strategies
  catalog.py        # bot catalog: discover config/profiles bots by risk level
  risk/             # sizing (fixed or risk-based), % and ATR stops, trend filter, kill-switch
  backtest/         # replay engine, metrics (Sharpe, drawdown, trades), multi-bot compare
  exchanges/        # ExchangeAdapter interface + ccxt implementation
  core/             # models, portfolio, brokers (paper/live), engine
config/profiles/    # the bot catalog: 9 ready-to-run bots rated by risk level and return potential
tests/              # offline unit + engine tests
docs/STRATEGY_GUIDE.md   # beginner-friendly trading-strategy primer
```

## Roadmap

- More strategies (fixed-quote DCA; bounded grids with fixed price ranges). Grid trading,
  the regime ensemble, DCA/Auto-Invest, MACD, Supertrend, RSI mean-reversion, Donchian
  breakout, and Bollinger bands are done, as are the backtester and the bot catalog.
- A funding-carry bot, once backtests can replay historical funding rates (the
  `funding_bias` strategy works live, but a backtest only sees the configured fallback rate).
- Walk-forward / out-of-sample splits and parameter sweeps built into the backtester
  (the 2026 bot rebuild did this by hand with a design/holdout split).
- `Decimal` money math respecting per-market precision.
- Live position reconciliation from exchange state (pre-existing holdings, restarts).
- Persistence (SQLite) for trade history and crash recovery; notifications.

## License

MIT.
