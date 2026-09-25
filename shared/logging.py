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
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

from shared.constants import LOG_DIR

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
_REDACTED = "[Filtered]"
_SENSITIVE_MARKERS = (
    "api_key",
    "apikey",
    "app_secret",
    "authorization",
    "broker_login",
    "cookie",
    "credential",
    "dsn",
    "github_token",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session",
    "token",
    "webhook",
)
_SENSITIVE_KEY_PARTS = (
    "apikey",
    "appsecret",
    "authorization",
    "brokerlogin",
    "cookie",
    "credential",
    "dsn",
    "githubtoken",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "session",
    "token",
    "webhook",
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?:api[_-]?key|app[_-]?secret|broker[_-]?login|credential|"
    r"github[_-]?token|password|passwd|private[_-]?key|refresh[_-]?token|"
    r"secret|session[_-]?id|webhook|(?:^|[^a-z])token(?:[^a-z]|$))"
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:access_token|accessToken|api_key|apiKey|apikey|broker_login|"
    r"brokerLogin|credential|dsn|key|password|refresh_token|refreshToken|secret|"
    r"session|sessionId|token)=)([^&#\s]+)"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?(?:access_token|accessToken|api_key|apiKey|apikey|app_secret|"
    r"appSecret|broker_login|brokerLogin|cookie|credential|dsn|github_token|"
    r"githubToken|password|private_key|privateKey|refresh_token|refreshToken|secret|"
    r"session|sessionId|token|webhook)[\"']?\s*[:=]\s*[\"']?)([^\"',;&\s}]+)"
)
_SENSITIVE_QUOTED_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:access_token|accessToken|api_key|apiKey|apikey|"
    r"app_secret|appSecret|broker_login|brokerLogin|cookie|credential|dsn|"
    r"github_token|githubToken|password|private_key|privateKey|refresh_token|"
    r"refreshToken|secret|session|sessionId|token|webhook)[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(bearer|basic)\s+[^\s,;]+"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_PRIVATE_KEY_RE = re.compile(r"(?is)-----BEGIN [^-]+-----.*?-----END [^-]+-----")
_TOKEN_VALUE_RES = (
    re.compile(r"(?i)\b(?:sk|pk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)

_loggers: dict[str, logging.Logger] = {}


def _is_sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).strip().lower())
    return any(marker in normalized for marker in _SENSITIVE_KEY_PARTS)


def redact_text(value: object) -> str:
    try:
        raw = str(value)
    except Exception:
        return "[Unserializable]"
    redacted = _AUTHORIZATION_RE.sub(r"\1\2 [Filtered]", raw)
    redacted = _BEARER_TOKEN_RE.sub("Bearer [Filtered]", redacted)
    redacted = _SENSITIVE_QUERY_RE.sub(r"\1[Filtered]", redacted)
    redacted = _SENSITIVE_QUOTED_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{_REDACTED}{match.group('quote')}"
        ),
        redacted,
    )
    redacted = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1[Filtered]", redacted)
    redacted = _PRIVATE_KEY_RE.sub("[Filtered private key]", redacted)
    for pattern in _TOKEN_VALUE_RES:
        redacted = pattern.sub("[Filtered]", redacted)
    return redacted


def redact_value(value: Any) -> Any:
    return _redact_value(value, set())


def _redact_value(value: Any, seen: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return _REDACTED
        seen.add(identity)
        try:
            redacted: dict[Any, Any] = {}
            for key, child in value.items():
                if _is_sensitive_key(key):
                    redacted[key] = _REDACTED
                    continue
                if (
                    isinstance(child, str)
                    and _SENSITIVE_VALUE_RE.search(child)
                    and not any(separator in child for separator in ("=", ":", "?"))
                ):
                    redacted[key] = _REDACTED
                else:
                    redacted[key] = _redact_value(child, seen)
            return redacted
        finally:
            seen.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in seen:
            return _REDACTED
        seen.add(identity)
        try:
            return [_redact_value(child, seen) for child in value]
        finally:
            seen.remove(identity)
    if isinstance(value, tuple):
        identity = id(value)
        if identity in seen:
            return _REDACTED
        seen.add(identity)
        try:
            return tuple(_redact_value(child, seen) for child in value)
        finally:
            seen.remove(identity)
    if isinstance(value, set):
        identity = id(value)
        if identity in seen:
            return _REDACTED
        seen.add(identity)
        try:
            return {_redact_value(child, seen) for child in value}
        finally:
            seen.remove(identity)
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (bytes, bytearray)):
        return redact_text(bytes(value).decode("utf-8", errors="replace"))
    if isinstance(value, BaseException):
        return redact_text(value)
    return "[Unserializable]"


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
            "message": redact_text(record.getMessage()),
        }
        data = getattr(record, "data", None)
        if data is not None:
            entry["data"] = redact_value(data)
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = redact_text(self.formatException(record.exc_info))
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
