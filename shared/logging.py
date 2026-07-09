"""
Centralized logging module for trading-platform services.

Provides structured JSON logging with file rotation and stdout output.

Usage:
    from shared.logging import get_logger
    logger = get_logger("viewer")
    logger.info("Backtest loaded", extra={"data": {"backtest_id": "abc", "trade_count": 42}})

Configuration via environment variables:
    LOG_LEVEL   — DEBUG, INFO, WARNING, ERROR, CRITICAL (default: INFO)
    LOG_DIR     — Directory for log files (default: ~/logs)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from shared.constants import LOG_DIR

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 10 * 1024 * 1024  # 10 MB
BACKUP_COUNT = 5

_loggers: dict[str, logging.Logger] = {}


class JsonFormatter(logging.Formatter):
    """Formats log records as structured JSON."""

    def __init__(self, service: str = ""):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            LOG_TIMESTAMP_FORMAT
        )
        module_name = record.name or record.module or "<unknown>"
        function_name = record.funcName or "<unknown>"
        line_number = record.lineno if isinstance(record.lineno, int) else 0
        entry = {
            "timestamp": timestamp,
            "level": record.levelname,
            "module": module_name,
            "function": function_name,
            "line": line_number,
            "message": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if data is not None:
            entry["data"] = data
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def get_logger(service: str) -> logging.Logger:
    """
    Create or return a logger for the given service.

    Each service gets:
    - A RotatingFileHandler writing JSON to ~/logs/<service>.log
    - A StreamHandler writing JSON to stdout
    """
    if service in _loggers:
        return _loggers[service]

    logger = logging.getLogger(f"trading.{service}")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    logger.propagate = False

    formatter = JsonFormatter()

    # File handler with rotation
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_DIR / f"{service}.log",
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Stdout handler
    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    _loggers[service] = logger
    return logger
