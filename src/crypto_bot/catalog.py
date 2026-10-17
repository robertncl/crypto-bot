"""The bot catalog: ready-made bot profiles, discoverable by risk level.

A *bot* is an ordinary config file (strategy + timeframe + sizing + stops) that also
carries a ``bot:`` metadata section (:class:`~crypto_bot.config.BotProfile`) naming its
category, risk level and return potential. The shipped ones live in ``config/profiles/``
and are run by file name: ``crypto-bot run --bot grid``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from crypto_bot.config import BotConfig, BotProfile, ConfigError, load_config

PROFILES_DIR = "config/profiles"

# Bot ids are file names: keep them to a safe slug so `--bot` can never name a path.
_BOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


@dataclass(frozen=True)
class CatalogEntry:
    bot_id: str  # file stem, as passed to --bot
    path: Path
    config: BotConfig

    @property
    def profile(self) -> BotProfile:
        assert self.config.bot is not None  # load_catalog only keeps profiled configs
        return self.config.bot


def load_catalog(
    directory: str | Path = PROFILES_DIR, *, max_risk: int | None = None
) -> tuple[list[CatalogEntry], list[tuple[Path, str]]]:
    """Every config in ``directory`` with ``bot:`` metadata, lowest risk first.

    Returns ``(entries, problems)``: files that fail to load are reported in
    ``problems`` rather than raised, so one broken profile does not hide the rest.
    Configs without a ``bot:`` section are plain configs, not catalog bots, and are
    skipped silently.
    """
    entries: list[CatalogEntry] = []
    problems: list[tuple[Path, str]] = []
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            config = load_config(path)
        except ConfigError as exc:
            problems.append((path, str(exc)))
            continue
        if config.bot is None:
            continue
        if max_risk is not None and config.bot.risk_level > max_risk:
            continue
        entries.append(CatalogEntry(bot_id=path.stem, path=path, config=config))
    entries.sort(key=lambda e: (e.profile.risk_level, e.bot_id))
    return entries, problems


def bot_path(bot_id: str, directory: str | Path = PROFILES_DIR) -> Path:
    """Resolve a ``--bot`` name to its profile file, or raise a helpful ConfigError."""
    if not _BOT_ID.fullmatch(bot_id):
        raise ConfigError(f"invalid bot name {bot_id!r}: use a name from `crypto-bot bots`")
    path = Path(directory) / f"{bot_id}.yaml"
    if not path.is_file():
        available = sorted(p.stem for p in Path(directory).glob("*.yaml"))
        raise ConfigError(
            f"unknown bot {bot_id!r}; available: {', '.join(available) or '(none)'}"
        )
    return path
