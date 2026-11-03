"""Entry point for the frozen engine: the same `vigil` command, without a Python installation."""

from __future__ import annotations

import multiprocessing

from vigil.cli.main import app

if __name__ == "__main__":
    multiprocessing.freeze_support()  # a frozen child process must not re-run the whole program
    app()
