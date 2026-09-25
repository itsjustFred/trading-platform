"""Sentry initialization and event hygiene for trading-platform services."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration

import shared.constants  # noqa: F401
from shared.logging import _SENSITIVE_MARKERS, redact_text, redact_value

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_INITIALIZED = False
_INITIALIZED_SERVICE: str | None = None
_REDACTED = "[Filtered]"


class SentryConfigError(ValueError):
    """Raised when Sentry environment configuration is invalid."""


@dataclass(frozen=True)
class SentryConfig:
    """Validated Sentry runtime settings."""

    enabled: bool
    dsn: str
    environment: str
    release: str | None
    sample_rate: float
    traces_sample_rate: float | None
    profiles_sample_rate: float
    send_default_pii: bool
    enable_logs: bool
    debug: bool
    attach_stacktrace: bool

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SentryConfig:
        values = env if env is not None else os.environ
        dsn = _clean(values.get("SENTRY_DSN"))
        enabled_raw = _clean(values.get("SENTRY_ENABLED"))
        enabled = (
            _parse_bool(enabled_raw, name="SENTRY_ENABLED")
            if enabled_raw
            else bool(dsn)
        )
        if enabled and not dsn:
            raise SentryConfigError("SENTRY_DSN is required when SENTRY_ENABLED is true")

        environment = (
            _clean(values.get("SENTRY_ENVIRONMENT"))
            or _clean(values.get("APP_ENV"))
            or _clean(values.get("ENVIRONMENT"))
            or "local"
        )
        release = _clean(values.get("SENTRY_RELEASE")) or _detect_release()

        return cls(
            enabled=enabled,
            dsn=dsn,
            environment=environment,
            release=release,
            sample_rate=_parse_rate(
                values.get("SENTRY_SAMPLE_RATE"),
                name="SENTRY_SAMPLE_RATE",
                default=1.0,
            ),
            traces_sample_rate=_parse_optional_rate(
                values.get("SENTRY_TRACES_SAMPLE_RATE"),
                name="SENTRY_TRACES_SAMPLE_RATE",
            ),
            profiles_sample_rate=_parse_rate(
                values.get("SENTRY_PROFILES_SAMPLE_RATE"),
                name="SENTRY_PROFILES_SAMPLE_RATE",
                default=0.0,
            ),
            send_default_pii=_parse_bool(
                values.get("SENTRY_SEND_DEFAULT_PII"),
                name="SENTRY_SEND_DEFAULT_PII",
                default=False,
            ),
            enable_logs=_parse_bool(
                values.get("SENTRY_ENABLE_LOGS"),
                name="SENTRY_ENABLE_LOGS",
                default=True,
            ),
            debug=_parse_bool(values.get("SENTRY_DEBUG"), name="SENTRY_DEBUG", default=False),
            attach_stacktrace=_parse_bool(
                values.get("SENTRY_ATTACH_STACKTRACE"),
                name="SENTRY_ATTACH_STACKTRACE",
                default=True,
            ),
        )


def initialize_sentry(
    service_name: str,
    *,
    integrations: Sequence[Any] | None = None,
    config: SentryConfig | None = None,
) -> bool:
    """Initialize Sentry once for the current process.

    Returns True when the SDK is active. A missing DSN disables Sentry unless
    SENTRY_ENABLED=true was explicitly configured.
    """

    global _INITIALIZED, _INITIALIZED_SERVICE
    if _INITIALIZED:
        return True

    resolved = config or SentryConfig.from_env()
    if not resolved.enabled:
        return False

    sentry_integrations = [
        LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        *(integrations or ()),
    ]
    sentry_sdk.init(
        dsn=resolved.dsn,
        environment=resolved.environment,
        release=resolved.release,
        sample_rate=resolved.sample_rate,
        traces_sample_rate=resolved.traces_sample_rate,
        profiles_sample_rate=resolved.profiles_sample_rate,
        send_default_pii=resolved.send_default_pii,
        enable_logs=resolved.enable_logs,
        debug=resolved.debug,
        attach_stacktrace=resolved.attach_stacktrace,
        integrations=sentry_integrations,
        before_send=_before_send,
    )
    sentry_sdk.set_tag("service", service_name)
    sentry_sdk.set_context(
        "trading_platform",
        {
            "service": service_name,
            "environment": resolved.environment,
        },
    )
    _INITIALIZED = True
    _INITIALIZED_SERVICE = service_name
    return True


def capture_exception(
    exc: BaseException,
    *,
    service_name: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> str | None:
    """Capture an exception with optional structured context."""

    with sentry_sdk.push_scope() as scope:
        if service_name:
            scope.set_tag("service", service_name)
        if context:
            scope.set_context("error_context", dict(context))
        event_id = sentry_sdk.capture_exception(exc)
    return str(event_id) if event_id is not None else None


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    del hint
    return _redact_value(event)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


def _redact_value(value: Any) -> Any:
    return redact_value(value)


def _redact_string(value: str) -> str:
    return redact_text(value)


def _parse_bool(value: str | None, *, name: str, default: bool | None = None) -> bool:
    if value is None:
        if default is None:
            raise SentryConfigError(f"{name} is required")
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SentryConfigError(f"{name} must be a boolean value")


def _parse_rate(value: str | None, *, name: str, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    parsed = _parse_float(value, name=name)
    if parsed < 0.0 or parsed > 1.0:
        raise SentryConfigError(f"{name} must be between 0.0 and 1.0")
    return parsed


def _parse_optional_rate(value: str | None, *, name: str) -> float | None:
    if value is None or value.strip() == "":
        return None
    return _parse_rate(value, name=name, default=0.0)


def _parse_float(value: str, *, name: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise SentryConfigError(f"{name} must be a number") from exc


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _detect_release() -> str | None:
    package_version = _clean(os.environ.get("TRADING_PLATFORM_VERSION")) or "0.1.0"
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return f"trading-platform@{package_version}"
    git_sha = sha.stdout.strip()
    if sha.returncode == 0 and git_sha:
        return f"trading-platform@{package_version}+{git_sha}"
    return f"trading-platform@{package_version}"


def _reset_for_tests() -> None:
    global _INITIALIZED, _INITIALIZED_SERVICE
    _INITIALIZED = False
    _INITIALIZED_SERVICE = None
