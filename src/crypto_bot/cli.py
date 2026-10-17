"""Command-line interface.

Commands::

    crypto-bot run [--config PATH | --bot NAME] [--mode paper|live] [--once]
                   [--yes-i-understand-live]
    crypto-bot backtest [--config PATH | --bot NAME] [--days N]
    crypto-bot bots [--max-risk N] [--details]
    crypto-bot compare [--days N] [--max-risk N]
    crypto-bot validate-config [--config PATH | --bot NAME]
    crypto-bot balance [--config PATH | --bot NAME]
    crypto-bot strategies
    crypto-bot version

Paper mode is the default and needs no API keys. Live mode is gated behind an explicit
``--yes-i-understand-live`` flag so real orders are never placed by accident.
``backtest`` replays the configured strategy over historical candles and prints
performance metrics (return vs buy-and-hold, Sharpe/Sortino, drawdown, trade stats).
``bots`` lists the ready-made bots in ``config/profiles`` by risk level, ``--bot NAME``
runs one of them, and ``compare`` backtests them all on the same history.
"""

from __future__ import annotations

import argparse
import sys
import time

from crypto_bot import __version__
from crypto_bot.catalog import PROFILES_DIR, bot_path, load_catalog
from crypto_bot.config import RISK_LEVELS, ConfigError, load_config
from crypto_bot.logging_setup import setup_logging

DEFAULT_CONFIG = "config/config.yaml"


def main(argv: list[str] | None = None) -> int:
    # Load API keys from .env if present (no-op if python-dotenv isn't installed). Search
    # from the working directory — where config/config.yaml is resolved too — not from
    # wherever the package is installed, so a stray .env above site-packages can never
    # supply credentials.
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass

    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crypto-bot", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="run the trading loop")
    _add_config_args(run_p)
    run_p.add_argument(
        "--mode", choices=["paper", "live"], help="override the mode set in config"
    )
    run_p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    run_p.add_argument(
        "--yes-i-understand-live",
        action="store_true",
        help="required acknowledgement to place REAL orders in live mode",
    )
    run_p.set_defaults(func=_cmd_run)

    bt_p = sub.add_parser("backtest", help="replay the strategy over historical candles")
    _add_config_args(bt_p)
    bt_p.add_argument(
        "--days", type=float, default=90.0, help="how much history to test (default 90)"
    )
    bt_p.set_defaults(func=_cmd_backtest)

    val_p = sub.add_parser("validate-config", help="load and validate the config file")
    _add_config_args(val_p)
    val_p.set_defaults(func=_cmd_validate)

    bal_p = sub.add_parser("balance", help="show account/paper balance")
    _add_config_args(bal_p)
    bal_p.set_defaults(func=_cmd_balance)

    bots_p = sub.add_parser("bots", help="list the ready-made bots by risk level")
    _add_catalog_args(bots_p)
    bots_p.add_argument(
        "--details", action="store_true", help="also show each bot's fit and main risk"
    )
    bots_p.set_defaults(func=_cmd_bots)

    cmp_p = sub.add_parser("compare", help="backtest every ready-made bot on the same history")
    _add_catalog_args(cmp_p)
    cmp_p.add_argument(
        "--days", type=float, default=180.0, help="how much history to test (default 180)"
    )
    cmp_p.set_defaults(func=_cmd_compare)

    strat_p = sub.add_parser("strategies", help="list available strategies")
    strat_p.set_defaults(func=_cmd_strategies)

    ver_p = sub.add_parser("version", help="print version")
    ver_p.set_defaults(func=lambda _a: (print(f"crypto-bot {__version__}"), 0)[1])

    return parser


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--config", help=f"path to config YAML (default {DEFAULT_CONFIG})")
    group.add_argument(
        "--bot", metavar="NAME", help="use a ready-made bot (see `crypto-bot bots`)"
    )


def _add_catalog_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dir", default=PROFILES_DIR, help=f"bot directory ({PROFILES_DIR})")
    parser.add_argument(
        "--max-risk", type=int, choices=sorted(RISK_LEVELS), metavar="N",
        help="only bots at or below this risk level (1-5)",
    )


def _config_path(args: argparse.Namespace) -> str:
    if getattr(args, "bot", None):
        return str(bot_path(args.bot))
    return args.config or DEFAULT_CONFIG


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(_config_path(args))
    if args.mode:
        config.mode = args.mode
    setup_logging(config.logging.level, config.logging.file)

    if config.is_live and not args.yes_i_understand_live:
        print(
            "Refusing to start in LIVE mode without acknowledgement.\n"
            "Live mode places REAL orders with REAL funds. If you have tested in paper "
            "mode and funded API keys are configured, re-run with "
            "--yes-i-understand-live.",
            file=sys.stderr,
        )
        return 3

    # Imported here so `validate-config`/`strategies` work without ccxt installed.
    from crypto_bot.core.engine import build_engine

    engine = build_engine(config)
    try:
        if args.once:
            engine.run_once()
        else:
            engine.run()
    finally:
        engine.exchange.close()
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    config = load_config(_config_path(args))
    config.mode = "paper"  # a backtest must never touch live order endpoints
    setup_logging(config.logging.level, config.logging.file)

    if args.days <= 0:
        print("--days must be positive", file=sys.stderr)
        return 2

    # Imported here so `validate-config`/`strategies` work without ccxt installed.
    from crypto_bot.backtest import Backtester, fetch_history
    from crypto_bot.exchanges.factory import build_exchange

    exchange = build_exchange(config.exchange, require_credentials=False)
    since_ms = int((time.time() - args.days * 86_400) * 1000)
    candles_by_symbol = {}
    try:
        for symbol in config.symbols:
            candles = fetch_history(exchange, symbol, config.timeframe, since_ms)
            if not candles:
                print(f"no history returned for {symbol}; aborting", file=sys.stderr)
                return 1
            print(f"fetched {len(candles)} {config.timeframe} candles for {symbol}")
            candles_by_symbol[symbol] = candles
    finally:
        exchange.close()

    result = Backtester(config).run(candles_by_symbol)
    print()
    print(result.format_report())
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(_config_path(args))
    print(
        f"OK: config valid.\n"
        f"  mode={config.mode}  exchange={config.exchange.name}  "
        f"timeframe={config.timeframe}\n"
        f"  symbols={', '.join(config.symbols)}\n"
        f"  strategy={config.strategy.name} {config.strategy.params}"
    )
    return 0


def _cmd_balance(args: argparse.Namespace) -> int:
    config = load_config(_config_path(args))
    setup_logging(config.logging.level, None)
    if not config.is_live:
        print(
            f"PAPER balance: {config.paper.starting_cash:.2f} "
            f"{config.paper.quote_currency} (simulated starting cash)"
        )
        return 0

    from crypto_bot.exchanges.factory import build_exchange

    exchange = build_exchange(config.exchange, require_credentials=True)
    try:
        balances = exchange.fetch_balance()
    finally:
        exchange.close()
    if not balances:
        print("no non-zero balances")
        return 0
    print(f"LIVE balances on {config.exchange.name}:")
    for currency, amount in sorted(balances.items()):
        print(f"  {currency:>8}: {amount}")
    return 0


def _cmd_bots(args: argparse.Namespace) -> int:
    entries, problems = load_catalog(args.dir, max_risk=args.max_risk)
    for path, error in problems:
        print(f"skipping {path}: {error}", file=sys.stderr)
    if not entries:
        print(f"no bots found in {args.dir}", file=sys.stderr)
        return 1

    print(f"Ready-made bots in {args.dir}, lowest risk first:\n")
    rows = [
        (
            f"{e.profile.risk_level} {e.profile.risk_label}",
            e.bot_id,
            e.profile.name,
            e.profile.category,
            e.profile.return_potential,
            f"{e.config.strategy.name} {e.config.timeframe}",
        )
        for e in entries
    ]
    _print_table(("RISK", "BOT", "NAME", "CATEGORY", "RETURN POTENTIAL", "STRATEGY"), rows)
    if args.details:
        for e in entries:
            print(f"\n{e.bot_id} - {e.profile.name}")
            for label, text in (
                ("what", e.profile.summary),
                ("best for", e.profile.best_for),
                ("main risk", e.profile.main_risk),
            ):
                if text:
                    print(f"  {label:<11}{text}")
    print(
        "\nRisk and return potential describe each recipe (exposure, stops, timeframe), not"
        "\na forecast. Measure them: `crypto-bot compare`. Try one: `crypto-bot backtest"
        " --bot NAME`."
    )
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    if args.days <= 0:
        print("--days must be positive", file=sys.stderr)
        return 2
    entries, problems = load_catalog(args.dir, max_risk=args.max_risk)
    for path, error in problems:
        print(f"skipping {path}: {error}", file=sys.stderr)
    if not entries:
        print(f"no bots found in {args.dir}", file=sys.stderr)
        return 1

    from crypto_bot.backtest.compare import compare_bots
    from crypto_bot.exchanges.factory import build_exchange

    setup_logging("WARNING", None)
    since_ms = int((time.time() - args.days * 86_400) * 1000)
    print(f"Backtesting {len(entries)} bots over the last {args.days:g} days...")
    results = compare_bots(
        entries,
        since_ms,
        lambda cfg: build_exchange(cfg, require_credentials=False),
        on_fetch=lambda symbol, tf, n: print(f"  fetched {n} {tf} candles for {symbol}"),
    )

    rows = []
    for item in results:
        p = item.entry.profile
        head = (f"{p.risk_level} {p.risk_label}", item.entry.bot_id, p.return_potential)
        if item.result is None:
            rows.append((*head, "error", "", "", "", ""))
            continue
        r = item.result
        rows.append((
            *head,
            f"{r.total_return_pct:+.1%}",
            f"{r.buy_hold_return_pct:+.1%}",
            f"{r.max_drawdown_pct:.1%}",
            "inf" if r.sharpe == float("inf") else f"{r.sharpe:.2f}",
            str(r.num_trades),
        ))
    print()
    _print_table(
        ("RISK", "BOT", "EXPECTED", "RETURN", "BUY&HOLD", "MAX DD", "SHARPE", "TRADES"),
        rows,
        right_from=3,
    )
    for item in results:
        if item.error:
            print(f"  {item.entry.bot_id}: {item.error}")
    print(
        "\nSame history for every bot; fees and slippage included. TRADES counts closed round"
        "\ntrips (an accumulator that never sells shows 0). One period's results are"
        "\nnot a forecast — re-run over different --days windows before trusting a ranking."
    )
    return 0


def _print_table(headers: tuple[str, ...], rows: list[tuple], right_from: int = 99) -> None:
    widths = [max(len(str(c)) for c in col) for col in zip(headers, *rows, strict=True)]

    def fmt(cells: tuple) -> str:
        return "  ".join(
            str(c).rjust(w) if i >= right_from else str(c).ljust(w)
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ).rstrip()

    print(fmt(headers))
    for row in rows:
        print(fmt(row))


def _cmd_strategies(_args: argparse.Namespace) -> int:
    from crypto_bot.strategies.registry import available_strategies

    print("available strategies:")
    for name in available_strategies():
        print(f"  - {name}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
