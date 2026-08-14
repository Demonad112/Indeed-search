"""Logging. Loud by default — constraint #5 says no silent failures."""

from __future__ import annotations

import logging
import os
import sys


class _Fmt(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[2;37m",
        "INFO": "\033[0m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    RESET = "\033[0m"

    def __init__(self, color: bool) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-22s %(message)s", "%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        if self.color:
            return f"{self.COLORS.get(record.levelname, '')}{s}{self.RESET}"
        return s


def setup(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Fmt(color=sys.stderr.isatty() and os.environ.get("NO_COLOR") is None))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs every request at INFO; we do our own request logging.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
