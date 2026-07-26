# Trading derivatives (perpetual swaps)

By default this bot is **long-only spot**: a BUY opens a position, a SELL closes it, and a
SELL with nothing held does nothing. That is a safe default, but it means half of every
market is untradable — in a downtrend the best outcome available is *flat*.

Enabling the `derivatives` config section changes that. It adds the two things that
actually distinguish a perpetual swap from spot:

1. **Shorting** — a SELL with nothing held opens a short position.
2. **Funding** — the periodic payment between longs and shorts that tethers the perp to
   spot.

## Enabling it

```yaml
symbols:
  - BTC/USDT:USDT        # a PERP symbol, not the BTC/USDT spot pair

derivatives:
  allow_shorts: true
  funding_interval_hours: 8
  funding_rate: 0.0001   # fallback per-interval rate for paper/backtest
```

Note the symbol format. Perp markets on ccxt carry a settlement suffix
(`BTC/USDT:USDT`); the plain `BTC/USDT` is the spot pair and **cannot be shorted**.
Setting `allow_shorts: true` against a spot symbol will produce short signals your venue
will reject in live mode.

## What changes when shorts are on

**Every existing strategy becomes long/short.** They already emit SELL — previously that
was only an exit, now it opens a short. No strategy code changed. So `supertrend`,
`macd`, `ma_crossover`, `bollinger` and the rest all become two-sided systems for free.

The engine **stop-and-reverses**: a signal opposing an open position closes it and opens
the other side, as two separate orders so the ledger stays unambiguous.

Protective exits mirror correctly. A short's stop-loss triggers when price *rises*, its
take-profit when price *falls*, and its trailing stop ratchets from the lowest price seen
rather than the highest.

## Accounting model

Opening a position posts `amount × entry_price` of cash as margin and returns it on close:

```
equity = cash + Σ (margin + unrealized_pnl)
```

For a long this is algebraically identical to the old spot marking
(`cash + Σ amount × price`), which is why turning derivatives on does not disturb any
existing spot behaviour or backtest result.

## Funding

Positive funding means longs pay shorts (the usual state, since perps tend to trade above
spot in a bull market); negative reverses it. Each settlement moves
`notional × rate` per position. The engine settles once per
`funding_interval_hours`, catching up whole intervals if the timeframe is coarser than
the funding period.

In **live** mode the real venue rate is fetched per symbol. In **paper/backtest** mode
there is no funding endpoint, so the configured `funding_rate` constant is used. Treat
backtested funding as a first-order approximation: real funding varies continuously and
spikes exactly when positioning is most crowded, which a constant cannot capture.

`funding_rate` is a **per-interval** rate, not annualized. The config loader rejects
anything beyond ±1% per interval, because pasting in an annualized figure would quietly
drain the account over a long backtest.

## Derivatives-native strategies

### `trend_ls` — filtered long/short breakout

A Donchian breakout with the two filters that most improve it, made symmetric:

- **ADX filter** — only trade breaks while the market is genuinely trending, which is the
  cheapest defence against a breakout system's worst enemy (range-bound chop).
- **EMA regime filter** — only long above the slow EMA, only short below it.

```yaml
strategy:
  name: trend_ls
  params: { lookback: 20, adx_period: 14, adx_threshold: 20, trend_period: 100 }
```

Fewer trades than plain `breakout` — that is the point, since a breakout system bleeds on
false starts. Degrades gracefully to long-only if shorts are off.

### `funding_bias` — funding-rate positioning

The one edge that exists *only* on derivatives. Funding is simultaneously a crowding
gauge (it only goes sharply positive because leveraged longs are queueing up) and a cash
flow (the unpopular side gets paid to wait). So it shorts extreme positive funding and
longs extreme negative funding, collecting carry while fading crowded positioning.

```yaml
strategy:
  name: funding_bias
  params: { enter_apr: 0.20, trend_period: 100 }
```

`enter_apr` is an **annualized** threshold (0.20 = 20%/yr) because that is the scale
humans reason about funding on. `trend_period` guards against fading a trend that keeps
running; set it to 0 to trade funding alone.

Two caveats stated plainly:

- Real funding arbitrage is **delta-neutral** — short perp against long spot, collecting
  carry with price risk hedged out. This bot holds one directional position per symbol, so
  this is a *directional* trade with a funding tailwind: a weaker, more volatile cousin of
  the real carry trade.
- It needs a funding rate to produce any signal at all. Against a venue with no funding
  endpoint and no configured `funding_rate`, it holds forever.

## What is deliberately NOT implemented

**Leverage and liquidation.** The margin model has the hook for it (`margin = notional /
leverage`) but leverage is not exposed, because modelling it honestly requires modelling
liquidation too. Without a liquidation engine a backtest will happily carry a leveraged
position through a drawdown that would have been force-closed in reality, turning a total
loss into a winning trade. A leverage knob without liquidation is not a feature, it is a
way to produce confidently wrong backtests.

**Delta-neutral / multi-leg positions.** The portfolio holds one position per symbol, so
a hedged spot-vs-perp carry trade cannot be expressed.

**Funding-rate history in backtests.** Backtests use a constant fallback rate rather than
the real historical funding series.
