"""`python -m crypto_bot`: the package's ``__main__.py`` guard only executes under
``if __name__ == "__main__"``, which a normal import (as every other test does)
never triggers — a subprocess is the only way to actually exercise it."""

from __future__ import annotations

import subprocess
import sys

from crypto_bot import __version__


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
