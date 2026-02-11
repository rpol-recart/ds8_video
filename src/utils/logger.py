"""Structured JSON logging setup."""

import logging
import sys
from pythonjsonlogger import jsonlogger


def setup_logging(level: str = "INFO"):
    """Configure root logger with JSON output."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level"},
    )
    handler.setFormatter(formatter)

    root.handlers.clear()
    root.addHandler(handler)
