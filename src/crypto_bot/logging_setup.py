"""Centralized logging configuration."""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER_NAME = "crypto_bot"


def setup_logging(level: str = "INFO", file: str | None = None) -> logging.Logger:
    """Configure and return the bot's logger. Idempotent across calls."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())

    # Avoid stacking duplicate handlers if called more than once.
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    if file:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger
