# Crypto Trading Strategy — A Beginner's Guide

This guide explains, in plain language, what a trading strategy is and how the ideas map to
**this bot**. No prior trading knowledge is assumed.

> ⚠️ **Please read.** Nothing here is financial advice. Crypto is volatile and most beginners
> *lose* money. The single most important sentence in this guide: **trade in `paper` mode
> until you genuinely understand what the bot is doing — and only ever risk money you can
> afford to lose entirely.**

---

## 1. The absolute basics

**An exchange** (Binance, Bybit, Coinbase) is a marketplace where buyers and sellers meet.

**Spot vs. futures.** *Spot* means you buy the actual coin (you own 0.1 BTC). *Futures/margin*
let you trade with borrowed money (leverage) — much riskier and **out of scope** for this
starter bot, which is spot, long-only (it buys, then sells what it bought; it never shorts).

**A trading pair** like `BTC/USDT` means "price of BTC, measured in USDT." The first symbol
is the **base** (what you're buying), the second is the **quote** (what you pay with).

**Order types:**
- **Market order** — "buy/sell right now at whatever the current price is." Fast, but you pay
  the *spread* and may get *slippage* (a slightly worse price). This bot uses market orders.
- **Limit order** — "buy/sell only at this price or better." You control price, but it might
  never fill.

**The spread** is the gap between the best buy price (*bid*) and best sell price (*ask*).
**Slippage** is when a market order fills at a worse average price than you expected. The bot
*simulates* both in paper mode via `paper.fee_rate` and `paper.slippage_pct`, so paper results
aren't unrealistically perfect.

**Candles & timeframes.** Price history is summarized into *candlesticks*. Each candle covers
a fixed *timeframe* (1m, 1h, 1d, …) and records four prices — **O**pen, **H**igh, **L**ow,
**C**lose (OHLC) — plus volume. A "1h candle" is one hour of trading squeezed into one bar.
Strategies read a list of candles and decide what to do. In this bot that's the `timeframe`
setting, and each strategy needs a minimum number of candles to "warm up" before it can act.

---

## 2. What *is* a trading strategy?

A strategy is just **a rule that turns price data into a decision: buy, sell, or do nothing.**

That's it. A good strategy is:
- **Objective** — the same data always produces the same decision (no emotion, no "I have a
  feeling"). Computers are excellent at this; humans are not.
- **Testable** — you can replay it over past data (*backtesting*) and on live data without real
  money (*paper trading*) to see if it would have worked.
- **Paired with risk rules** — *when* to buy is only half the job. *How much* to buy and *when
  to cut a loss* matter more (see §5).

In this codebase a strategy implements one method, `generate(candles) -> Signal`, returning
`BUY`, `SELL`, or `HOLD`. It never places orders or decides size itself — that separation
keeps strategies easy to test and swap.

---

## 3. The two big families of strategy

Almost every strategy is a flavor of one of these:

| Family | Core belief | Buys when… | Works best in… | Fails in… |
| --- | --- | --- | --- | --- |
| **Trend-following** | "A move in motion tends to continue." | price is *rising* | strong trends | choppy, sideways markets (gets whipsawed) |
| **Mean-reversion** | "Prices snap back to an average." | price is *unusually low* | calm, range-bound markets | strong trends (keeps catching a falling knife) |

No strategy works in all conditions. That's normal. The goal isn't to be right every
time — it's to **win more on winners than you lose on losers, over many trades.**

---

## 4. The first strategy to learn: Moving-Average (MA) Crossover

The bot ships with four strategies (see §6), but start here: this is the classic beginner
**trend-following** strategy, and a great one to learn on because you can see it on a chart.

**A moving average (MA)** is just the average price over the last *N* candles, recalculated
each candle. It smooths out the noise so you can see the underlying direction. A *fast* MA
(few candles, e.g. 12) hugs the price closely; a *slow* MA (many candles, e.g. 26) reacts
slowly.

**The rule:**
- When the **fast MA crosses *above* the slow MA** → recent prices are pulling up faster than
  the longer trend → **BUY** (a "golden cross").
- When the **fast MA crosses *below* the slow MA** → momentum is fading → **SELL** (a "death
  cross").

```
price
  │                      ╭─╮          fast MA ─── crosses ABOVE slow MA  → BUY ▲
  │             ╭───╮   ╱   ╲        ╱
  │      ╭─────╯     ╲ ╱     ╰──────╯   ← slow MA (smooth, slow to turn)
  │ ────╯            ╳  ← crossover point
  │                 ╱ ╲
  └────────────────────────────────────────▶ time
```

**Why two MAs instead of one?** A single MA gives constant noisy signals. Requiring one MA to
*cross* another filters for a meaningful shift in momentum, and makes the signal
**edge-triggered** — it fires once, on the bar the cross happens, not on every bar afterward.

**Its weakness:** in a sideways, choppy market the two MAs tangle together and you get repeated
false signals ("whipsaw"), each costing a little in fees and slippage. That's exactly why risk
management (§5) exists.

**In this bot** (`strategy` section of the config):
```yaml
strategy:
  name: ma_crossover
  params:
    fast_period: 12     # fast MA over 12 candles
    slow_period: 26     # slow MA over 26 candles  (must be > fast)
    ma_type: ema        # ema reacts faster to recent prices; sma weights all candles equally
```
- **SMA** (simple) averages the last N closes equally.
- **EMA** (exponential) weights recent candles more, so it turns faster — more responsive, but
  also more false signals. Both are built in; switch with `ma_type`.

Try changing the periods and timeframe in **paper mode** and watch how the behavior changes.

---

## 5. Risk management — the part that actually decides if you survive

Beginners obsess over *entry* signals. Professionals obsess over *risk*. You can be right less
than half the time and still make money if your losses are small and your wins are larger.

**Position sizing — don't bet the farm on one trade.** Risk a *fixed small fraction* of your
account per position, so no single trade can wreck you. This bot allocates a set percentage of
your equity to each new position:
```yaml
risk:
  position_pct: 0.10        # put at most 10% of equity into any one position
  max_open_positions: 3     # never hold more than 3 positions at once (caps total exposure)
```

**Stop-loss — decide your exit *before* you're losing.** A stop-loss automatically sells if the
price falls a set amount below your entry, capping the damage of any one bad trade. This is the
most important risk tool there is.
```yaml
  stop_loss_pct: 0.05       # auto-sell if a position drops 5% below entry
```

**Take-profit — lock in gains.** The mirror image: sell once you're up a set amount.
```yaml
  take_profit_pct: 0.15     # auto-sell if a position rises 15% above entry
```
Notice `take_profit (15%)` is larger than `stop_loss (5%)` here — a 3:1 *reward-to-risk* ratio.
That means you can be wrong twice for every time you're right and still come out ahead. Aiming
for reward ≥ risk is a core discipline.

**Drawdown kill-switch — live to trade another day.** *Drawdown* is how far your account has
fallen from its peak. If a strategy is clearly failing, you want it to *stop*, not keep digging.
```yaml
  max_drawdown_pct: 0.25    # if equity falls 25% below its high-water mark, stop opening trades
```

**Diversification.** Trading several uncorrelated pairs spreads risk — but in crypto, most coins
move together, so don't assume 5 coins = 5x safer.

> Rule of thumb beginners ignore at their peril: **a 50% loss requires a 100% gain to recover.**
> Protecting capital beats chasing gains.

---

## 6. The other strategies this bot ships — and choosing a risk profile

You don't have to stop at MA crossover. The bot ships **four** strategies (run
`crypto-bot strategies` to list them) — two from each family in §3:

**Trend-following (buy strength):**
- **MA crossover** (`ma_crossover`) — the worked example in §4. *Balanced.*
- **Breakout** (`breakout`) — buys when price punches *above* its highest high of the last N
  candles (a "Donchian channel"), betting a fresh move has begun; sells on a new N-bar low. It
  rides big trends hard but gets chopped up in sideways markets, which makes it the most
  *aggressive* of the four — give it a wide stop and a generous take-profit.

**Mean-reversion (buy weakness):**
- **RSI reversion** (`rsi_reversion`) — RSI is a 0–100 "overbought/oversold" gauge. This buys
  as RSI *climbs back above* the oversold line (e.g. up through 30) — waiting for the bounce to
  actually start instead of catching a falling knife — and sells as it rolls back below
  overbought (70). *Balanced, contrarian.*
- **Bollinger bands** (`bollinger`) — wraps a moving average in an envelope that widens when the
  market is volatile and tightens when it's calm. It buys when price is stretched *below* the
  lower band and sells when stretched *above* the upper one. Because it demands a genuine
  statistical stretch (2 standard deviations by default) before acting, it trades rarely on calm
  majors — the most *conservative* of the four.

### Your "risk profile" is the whole recipe, not one setting

Conservative vs. aggressive isn't a single dial — it's the *combination* of which strategy, on
what timeframe, with how much size and how tight a stop. A slow Bollinger strategy on the daily
chart risking 5% per trade is cautious; a 1-hour breakout risking 20% is not. The bot bundles
three ready-made recipes in [`config/profiles/`](../config/profiles/):

| Profile | Strategy | Timeframe | Position size | Max positions | Stop / take-profit |
| --- | --- | --- | --- | --- | --- |
| `conservative.yaml` | Bollinger | 1d | 5% | 2 | 5% / 12% |
| `balanced.yaml` | RSI reversion | 4h | 10% | 3 | 6% / 15% |
| `aggressive.yaml` | Breakout | 1h | 20% | 5 | 8% / 30% |

Run one straight away — all three are **paper** mode, so nothing is at risk:

```bash
crypto-bot run --once --config config/profiles/conservative.yaml
```

Then read the logs, change **one** thing, and run it again. These are training wheels to learn
from — *not* advice about what will make money. There is no "best" profile; aggressive simply
means bigger swings in both directions.

### Still just ideas (good next exercises)

- **Dollar-Cost Averaging (DCA)** — buy a fixed amount on a schedule regardless of price.
  Boring, low-effort, historically hard to beat for long-term holders, and the lowest-stress
  approach of all. (Heads-up: the starter holds at most one position per symbol and doesn't
  *average in*, so true DCA needs a small extension to the risk manager — a nice project.)
- **Grid trading** — place a ladder of buy/sell orders across a price range to profit from
  oscillation. Great in sideways markets, dangerous in strong trends.

---

## 7. Backtest and paper-trade — *always*, before real money

1. **Backtest** — replay the strategy over historical candles to see how it *would* have done.
   Cheap and fast, but beware **overfitting**: if you tweak parameters until they look perfect
   on past data, they usually fail on new data. Past performance ≠ future results.
2. **Paper trade** — run on *live* market data with fake money (this bot's default `paper`
   mode). This catches problems backtests miss: fees, slippage, latency, and your own behavior.
3. **Testnet** — many exchanges offer a sandbox with fake funds but the *real* order system
   (`sandbox: true` in config). The last rehearsal before going live.
4. **Live, tiny** — start with an amount you'd be fine losing entirely.

Skipping straight to step 4 is the most common — and most expensive — beginner mistake.

---

## 8. Common beginner mistakes

- **Trading real money before paper trading.** Don't.
- **No stop-loss.** One bad trade erases dozens of good ones.
- **Position sizing too big.** "This one's a sure thing" is how accounts go to zero.
- **Overfitting the backtest.** Perfect on history, broke in reality.
- **Revenge trading / emotion.** A bot's main advantage is *not* feeling FOMO or panic — don't
  override it manually on a whim.
- **Ignoring fees & slippage.** A strategy that trades constantly can bleed out on costs even if
  each signal looks fine. This bot models both so paper results stay honest.
- **Leverage.** Amplifies losses as much as gains. Avoid until you truly know what you're doing.

---

## 9. A sensible first configuration

A conservative starting point for learning — slow timeframe (less noise, fewer fees), small
position size, protective stops, on a couple of major pairs, in **paper** mode:

```yaml
mode: paper
exchange: { name: binance, sandbox: false }
symbols: [BTC/USDT, ETH/USDT]
timeframe: 4h               # higher timeframe = fewer, higher-quality signals
poll_seconds: 60
strategy:
  name: ma_crossover
  params: { fast_period: 12, slow_period: 26, ma_type: ema }
risk:
  position_pct: 0.10
  max_open_positions: 2
  stop_loss_pct: 0.05
  take_profit_pct: 0.15
  max_drawdown_pct: 0.25
paper:
  starting_cash: 10000
  quote_currency: USDT
  fee_rate: 0.001
  slippage_pct: 0.0005
```

Run it, read the logs each cycle, and change **one thing at a time** so you can tell what each
setting actually does.

---

## 10. Mini-glossary

| Term | Meaning |
| --- | --- |
| **Base / Quote** | In `BTC/USDT`, BTC is base (bought), USDT is quote (paid). |
| **Spot** | Owning the actual coin (vs. leveraged futures). |
| **Candle / OHLC** | One timeframe of price: Open, High, Low, Close (+ volume). |
| **Spread** | Gap between best buy (bid) and best sell (ask) price. |
| **Slippage** | Getting a worse fill price than expected on a market order. |
| **MA (SMA/EMA)** | Moving average; the smoothed average price over N candles. |
| **Long / Short** | Betting price goes up (long) / down (short). This bot is long-only. |
| **Position** | An open holding in a symbol. |
| **Stop-loss / Take-profit** | Auto-exit on a set loss / gain. |
| **Drawdown** | % drop from your account's peak value. |
| **Equity** | Total account value = cash + current value of open positions. |
| **Backtest / Paper trade** | Test on past data / on live data with fake money. |
| **Reward-to-risk** | Expected gain vs. amount risked per trade (aim ≥ 1, ideally ≥ 2). |
| **Whipsaw** | Repeated false signals in a choppy market. |

---

## 11. Where to learn more

- **ccxt docs** — how the bot talks to exchanges: <https://docs.ccxt.com>
- **Investopedia** — solid plain-English definitions for any term above.
- Read the bot's own code: `src/crypto_bot/strategies/ma_crossover.py` is ~60 lines and is the
  best way to see a strategy turn candles into a decision.

**The one habit that matters most: paper trade first, size small, and always use a stop-loss.**
