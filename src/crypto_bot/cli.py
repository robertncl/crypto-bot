"""Command-line interface.

Commands::

    crypto-bot run [--config PATH] [--mode paper|live] [--once] [--yes-i-understand-live]
    crypto-bot validate-config [--config PATH]
    crypto-bot balance [--config PATH]
    crypto-bot strategies
    crypto-bot version

Paper mode is the default and needs no API keys. Live mode is gated behind an explicit
``--yes-i-understand-live`` flag so real orders are never placed by accident.
"""

from __future__ import annotations

import argparse
import sys

from crypto_bot import __version__
from crypto_bot.config import ConfigError, load_config
from crypto_bot.logging_setup import setup_logging

DEFAULT_CONFIG = "config/config.yaml"


def main(argv: list[str] | None = None) -> int:
    # Load API keys from .env if present (no-op if python-dotenv isn't installed).
    try:
        from dotenv import load_dotenv

        load_dotenv()
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
    run_p.add_argument("--config", default=DEFAULT_CONFIG, help="path to config YAML")
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

    val_p = sub.add_parser("validate-config", help="load and validate the config file")
    val_p.add_argument("--config", default=DEFAULT_CONFIG)
    val_p.set_defaults(func=_cmd_validate)

    bal_p = sub.add_parser("balance", help="show account/paper balance")
    bal_p.add_argument("--config", default=DEFAULT_CONFIG)
    bal_p.set_defaults(func=_cmd_balance)

    strat_p = sub.add_parser("strategies", help="list available strategies")
    strat_p.set_defaults(func=_cmd_strategies)

    ver_p = sub.add_parser("version", help="print version")
    ver_p.set_defaults(func=lambda _a: (print(f"crypto-bot {__version__}"), 0)[1])

    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
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


def _cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(
        f"OK: config valid.\n"
        f"  mode={config.mode}  exchange={config.exchange.name}  "
        f"timeframe={config.timeframe}\n"
        f"  symbols={', '.join(config.symbols)}\n"
        f"  strategy={config.strategy.name} {config.strategy.params}"
    )
    return 0


def _cmd_balance(args: argparse.Namespace) -> int:
    config = load_config(args.config)
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


def _cmd_strategies(_args: argparse.Namespace) -> int:
    from crypto_bot.strategies.registry import available_strategies

    print("available strategies:")
    for name in available_strategies():
        print(f"  - {name}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
