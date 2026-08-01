"""crypto_bot.logging_setup: console + optional file handler, idempotent setup."""

from __future__ import annotations

import logging

import pytest

from crypto_bot.logging_setup import LOGGER_NAME, setup_logging


@pytest.fixture(autouse=True)
def _clean_logger():
    # setup_logging mutates the process-wide logger named LOGGER_NAME. pytest's own
    # log-capture handler attaches to it between this fixture's setup and the test
    # body running, so `logger.handlers` isn't reliably empty at that point — every
    # test below clears it again as its first statement, immediately before the call
    # under test. This fixture's job is just to leave the logger as it found it
    # (level/propagate included) once the test is done.
    logger = logging.getLogger(LOGGER_NAME)
    level = logger.level
    propagate = logger.propagate
    yield
    logger.handlers.clear()
    logger.setLevel(level)
    logger.propagate = propagate


def _clean():
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    return logger


def test_setup_logging_returns_the_named_logger():
    _clean()
    logger = setup_logging("INFO")
    assert logger.name == LOGGER_NAME
    assert logger.level == logging.INFO


def test_setup_logging_adds_a_console_handler_and_disables_propagation():
    _clean()
    logger = setup_logging("DEBUG")
    assert logger.propagate is False
    assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)


def test_setup_logging_level_is_case_insensitive():
    _clean()
    logger = setup_logging("warning")
    assert logger.level == logging.WARNING


def test_setup_logging_is_idempotent_and_does_not_stack_handlers():
    _clean()
    first = setup_logging("INFO")
    n_handlers = len(first.handlers)
    second = setup_logging("DEBUG")  # a second call, even with a different level
    assert second is first
    assert len(second.handlers) == n_handlers  # no duplicate handlers added


def test_setup_logging_adds_a_file_handler_when_given_a_path(tmp_path):
    _clean()
    log_file = tmp_path / "nested" / "bot.log"
    logger = setup_logging("INFO", str(log_file))
    assert any(isinstance(h, logging.FileHandler) for h in logger.handlers)
    assert log_file.parent.is_dir()  # parent directories created on demand

    logger.info("hello")
    for h in logger.handlers:
        h.flush()
    assert "hello" in log_file.read_text()
