"""`python -m crypto_bot`: the package's ``__main__.py`` entry point.

Its body only runs under ``if __name__ == "__main__"``, which a normal import (as
every other test does) never triggers. Covered two ways: ``runpy`` executes it
in-process under that name, and a subprocess proves the real ``python -m`` invocation
works end to end.
"""

from __future__ import annotations

import runpy
import subprocess
import sys

import pytest

from crypto_bot import __version__


def test_main_module_runs_in_process_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["crypto-bot", "version"])
    # runpy warns (and may misbehave) if the module is already in sys.modules, which
    # the import test below leaves it in. Drop it so this runs cleanly and
    # deterministically whatever order the two tests happen to execute in.
    monkeypatch.delitem(sys.modules, "crypto_bot.__main__", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("crypto_bot", run_name="__main__")

    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_importing_the_module_does_not_run_the_cli():
    # The other side of the `if __name__ == "__main__"` guard: importing the module
    # must be side-effect free, so nothing here starts trading on a plain import.
    import importlib

    module = importlib.import_module("crypto_bot.__main__")

    assert module.main is not None


def test_python_dash_m_crypto_bot_runs_and_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "crypto_bot", "version"],
        capture_output=True,
        text=True,
        cwd="src",
        timeout=30,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout
