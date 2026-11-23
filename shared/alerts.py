"""Ops alert sink: pushes alert-worthy log records to a webhook, fire-and-forget.

The operator has no active alerting today: problems show up only in the JSON
logs and in Sentry. This module adds one small sink that a :class:`logging.Handler`
feeds from the existing ERROR records (and any record carrying ``data.alert``),
so no call site has to change to reach the channel.

Design constraints, all deliberate for a live-trading process:

- **Never blocks, never raises into the caller.** Records are formatted in
  ``emit`` and handed to a bounded ``queue.Queue`` with ``put_nowait``. A full
  queue drops the alert and counts it; the worker thread does the network I/O.
- **Rate-limited and deduplicated.** An error storm (e.g. a broker outage
  logging the same failure every poll) must not become a webhook storm. The
  same dedup key inside ``OPS_ALERT_DEDUP_SECONDS`` sends once; at most
  ``OPS_ALERT_MAX_PER_MINUTE`` alerts leave per minute, and overflow is
  dropped-and-counted, with the count folded into the next alert that does go
  out.
- **Disabled when unconfigured.** ``OPS_ALERT_WEBHOOK_URL`` unset means no
  sink is installed at all; logging behaviour is exactly what it was before.
  The backend is pluggable so tests (and a future non-Discord channel) inject
  a fake.
- **Configurable filtering.** ``OPS_ALERT_MIN_LEVEL`` raises (or, unusually,
  lowers) the level at which records page - default ``ERROR``, so out of the
  box every ERROR/CRITICAL record alerts. ``OPS_ALERT_EVENTS`` is a
  comma-separated allowlist of event classes - a record's ``data.alert``
  marker or its logger name (exact or dotted-parent prefix) - and when set,
  only matching records page; markers and CRITICAL records always page
  regardless. ``OPS_ALERT_IGNORE`` is a comma-separated denylist of fnmatch
  patterns matched against the message template and the rendered message; it
  mutes even markers and CRITICAL, so it is the explicit "this one routine
  error is noise" switch. All filters live here, so routine errors can be
  silenced without touching call sites; an explicit ``data.alert`` marker
  still bypasses the level floor and the allowlist (it is a deliberate
  operator alert) but not the denylist, which is the explicit mute switch
  and wins over everything.

``install_alert_sink`` attaches the handler to every ``trading.*`` service
logger that exists now and, through a hook in ``shared.logging.get_logger``,
to every one created later. Only the live daemon installs it today; other
entry points are unaffected unless they opt in.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Any, Protocol

#: Discord-compatible webhook URL. Unset/blank disables the sink entirely.
WEBHOOK_URL_ENV = "OPS_ALERT_WEBHOOK_URL"
#: Alerts sent per sliding 60 s window before overflow is dropped.
MAX_PER_MINUTE_ENV = "OPS_ALERT_MAX_PER_MINUTE"
DEFAULT_MAX_PER_MINUTE = 12
#: Identical alerts (same dedup key) are suppressed for this long.
DEDUP_SECONDS_ENV = "OPS_ALERT_DEDUP_SECONDS"
DEFAULT_DEDUP_SECONDS = 300.0
#: Minimum level for level-triggered alerts: name (``ERROR``) or number.
#: Default ERROR, so every ERROR/CRITICAL record alerts out of the box.
MIN_LEVEL_ENV = "OPS_ALERT_MIN_LEVEL"
DEFAULT_MIN_LEVEL = logging.ERROR
#: Comma-separated allowlist of event classes (``data.alert`` marker or logger
#: name). Unset/empty means every alert-worthy record may page.
EVENTS_ENV = "OPS_ALERT_EVENTS"
#: Comma-separated denylist of fnmatch patterns matched against the message
#: template and the rendered message. Mutes even markers and CRITICAL.
#: ``fnmatchcase`` is case-sensitive and a pattern cannot contain a comma
#: (the comma is the entry separator).
IGNORE_ENV = "OPS_ALERT_IGNORE"
#: Bound on queued alerts; a full queue drops and counts.
DEFAULT_QUEUE_SIZE = 256
#: Backend call timeout. Runs on the worker thread, never on a caller.
DEFAULT_BACKEND_TIMEOUT_SECONDS = 5.0
#: Discord message cap is 2000 chars; stay under it.
MAX_MESSAGE_CHARS = 1900

_STOP_POLL_SECONDS = 0.2
_DEDUP_STORE_PRUNE_AT = 1024
#: Flush budget for queued alerts during daemon shutdown.
SHUTDOWN_FLUSH_SECONDS = 2.0

#: ``logging.Handler`` has no ``formatException`` (that lives on
#: ``logging.Formatter``); exception-bearing records must render through one.
_EXC_FORMATTER = logging.Formatter()


def _ops_logger() -> logging.Logger:
    """The ops logger: ``trading.alerts`` via ``shared.logging.get_logger``.

    A bare ``alerts`` logger has no handlers, so its records were dropped
    (warnings reaching stderr only through ``lastResort``, unformatted and
    outside the log file): the effective-filter line at install and the
    backend-failure warning never reached the log. ``get_logger`` gives it
    the file and console handlers. Resolved lazily so ``import
    shared.alerts`` keeps no import-time side effects.
    """
    from shared.logging import get_logger

    return get_logger("alerts")


class AlertBackend(Protocol):
    """Sink target. ``send`` runs on the worker thread; raise to count a failure."""

    def send(self, alert: Mapping[str, Any]) -> None: ...


class DiscordWebhookBackend:
    """POST each alert as a Discord webhook message."""

    def __init__(self, webhook_url: str, *, timeout: float = DEFAULT_BACKEND_TIMEOUT_SECONDS):
        self._webhook_url = webhook_url
        self._timeout = timeout

    def send(self, alert: Mapping[str, Any]) -> None:
        import requests  # lazy: keeps `import shared.alerts` cheap for --help paths

        response = requests.post(
            self._webhook_url,
            # wait=true makes Discord return the created message (or an error)
            # instead of an empty 204, so delivery failures stay observable.
            params={"wait": "true"},
            json={
                "content": format_alert_text(alert),
                # Error text can echo untrusted content; never let it ping the
                # channel (@everyone, @here, role mentions).
                "allowed_mentions": {"parse": []},
            },
            timeout=self._timeout,
        )
        response.raise_for_status()


def format_alert_text(alert: Mapping[str, Any]) -> str:
    """Render an alert payload as the single text body sent to the channel."""
    lines = [
        f"[{alert.get('level', 'ERROR')}] {alert.get('service', 'unknown')} "
        f"{alert.get('location', '')}".rstrip(),
        str(alert.get("message", "")),
    ]
    data = alert.get("data")
    if data:
        try:
            lines.append(f"data: {json.dumps(data, default=str, sort_keys=True)}")
        except (TypeError, ValueError):
            lines.append(f"data: {data!r}")
    if alert.get("dropped"):
        lines.append(f"({alert['dropped']} alert(s) dropped by rate limit or full queue)")
    if alert.get("exception"):
        lines.append(str(alert["exception"]))
    return "\n".join(lines)[:MAX_MESSAGE_CHARS]


class AlertSink:
    """Bounded, rate-limited, deduplicated fire-and-forget alert queue.

    ``submit`` is the only method callers touch: it never blocks and never
    raises. Everything else happens on one daemon worker thread.
    """

    def __init__(
        self,
        backend: AlertBackend,
        *,
        max_per_minute: int = DEFAULT_MAX_PER_MINUTE,
        dedup_seconds: float = DEFAULT_DEDUP_SECONDS,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        rate_window_seconds: float = 60.0,
    ) -> None:
        self._backend = backend
        self._max_per_minute = max(1, int(max_per_minute))
        self._dedup_seconds = max(0.0, float(dedup_seconds))
        self._rate_window_seconds = max(0.001, float(rate_window_seconds))
        self._queue: queue.Queue[tuple[str, Mapping[str, Any]]] = queue.Queue(
            maxsize=max(1, int(queue_size))
        )
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="ops-alert-sink", daemon=True)
        self._sent = 0
        self._dropped = 0
        self._suppressed = 0
        self._failed = 0
        self._dropped_since_last_send = 0
        self._last_sent_by_key: dict[str, float] = {}
        self._send_times: deque[float] = deque()
        self._lock = threading.Lock()
        self._worker.start()

    def submit(self, payload: Mapping[str, Any], *, dedup_key: str) -> None:
        """Enqueue one alert. Drops and counts when the queue is full."""
        try:
            self._queue.put_nowait((dedup_key, payload))
        except queue.Full:
            with self._lock:
                self._dropped += 1
                self._dropped_since_last_send += 1

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "sent": self._sent,
                "dropped": self._dropped,
                "suppressed": self._suppressed,
                "failed": self._failed,
                "queued": self._queue.qsize(),
            }

    def close(self, timeout: float = 5.0) -> None:
        """Stop the worker after draining what is already queued."""
        self._stop.set()
        self._worker.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            try:
                dedup_key, payload = self._queue.get(timeout=_STOP_POLL_SECONDS)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                self._deliver(dedup_key, payload)
            except Exception:
                # A bug in delivery must not kill the worker silently; count it
                # and keep draining so the queue cannot fill behind a dead loop.
                with self._lock:
                    self._failed += 1
            finally:
                self._queue.task_done()
            if self._stop.is_set() and self._queue.empty():
                return

    def _deliver(self, dedup_key: str, payload: Mapping[str, Any]) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._last_sent_by_key.get(dedup_key)
            if last is not None and now - last < self._dedup_seconds:
                self._suppressed += 1
                return
            cutoff = now - self._rate_window_seconds
            while self._send_times and self._send_times[0] <= cutoff:
                self._send_times.popleft()
            if len(self._send_times) >= self._max_per_minute:
                self._dropped += 1
                self._dropped_since_last_send += 1
                return
            dropped_note = self._dropped_since_last_send
        alert = dict(payload)
        if dropped_note:
            alert["dropped"] = dropped_note
        try:
            self._backend.send(alert)
        except Exception as exc:
            # Dedup and rate-limit state are committed only after a successful
            # send, so a failing webhook cannot mute the retry of this alert.
            # Log the exception type (and HTTP status when present), never the
            # exception itself: requests exceptions can embed the webhook URL,
            # whose path segment is the channel secret.
            with self._lock:
                self._failed += 1
            status = getattr(getattr(exc, "response", None), "status_code", None)
            _ops_logger().warning(
                "ops alert backend send failed: %s%s",
                type(exc).__name__,
                f" HTTP {status}" if status is not None else "",
            )
            return
        with self._lock:
            self._sent += 1
            self._send_times.append(now)
            self._last_sent_by_key[dedup_key] = now
            # Subtract only the snapshot taken before the send: drops that
            # arrived while the backend was busy still belong to the next alert.
            self._dropped_since_last_send = max(
                0, self._dropped_since_last_send - dropped_note
            )
            if len(self._last_sent_by_key) > _DEDUP_STORE_PRUNE_AT:
                stale = [
                    key
                    for key, ts in self._last_sent_by_key.items()
                    if now - ts >= self._dedup_seconds
                ]
                for key in stale:
                    del self._last_sent_by_key[key]


def _parse_positive_float(value: str | None, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def create_sink_from_env(
    env: Mapping[str, str] | None = None,
    *,
    backend: AlertBackend | None = None,
) -> AlertSink | None:
    """Build the process sink, or ``None`` when no webhook is configured.

    ``backend`` injects a fake for tests; without it the Discord webhook from
    ``OPS_ALERT_WEBHOOK_URL`` is used. A missing URL disables alerting entirely
    rather than erroring: the daemon must start identically with or without it.
    """
    values = env if env is not None else os.environ
    resolved_backend = backend
    if resolved_backend is None:
        webhook_url = str(values.get(WEBHOOK_URL_ENV) or "").strip()
        if not webhook_url:
            return None
        resolved_backend = DiscordWebhookBackend(webhook_url)
    return AlertSink(
        resolved_backend,
        max_per_minute=_parse_positive_int(values.get(MAX_PER_MINUTE_ENV), DEFAULT_MAX_PER_MINUTE),
        dedup_seconds=_parse_positive_float(values.get(DEDUP_SECONDS_ENV), DEFAULT_DEDUP_SECONDS),
    )


_LEVEL_NAMES = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.CRITICAL,
}


def _parse_min_level(value: str | None, default: int = DEFAULT_MIN_LEVEL) -> int:
    """Level name or number; anything unrecognised falls back to ``default``.

    An invalid value must not silently mute the channel, so the fallback is the
    default (ERROR), never "alert nothing".
    """
    if value is None:
        return default
    text = str(value).strip().upper()
    if not text:
        return default
    if text in _LEVEL_NAMES:
        return _LEVEL_NAMES[text]
    try:
        level = int(text)
    except ValueError:
        return default
    return level if level > 0 else default


def _parse_event_allowlist(value: str | None) -> frozenset[str] | None:
    """Comma-separated event classes; unset or empty means no allowlist."""
    if value is None:
        return None
    entries = frozenset(
        token.strip() for token in str(value).split(",") if token.strip()
    )
    return entries or None


def _alert_filter_from_env(
    values: Mapping[str, str],
) -> tuple[int, frozenset[str] | None, frozenset[str] | None]:
    return (
        _parse_min_level(values.get(MIN_LEVEL_ENV)),
        _parse_event_allowlist(values.get(EVENTS_ENV)),
        _parse_event_allowlist(values.get(IGNORE_ENV)),
    )


class AlertLogHandler(logging.Handler):
    """Routes alert-worthy records into an :class:`AlertSink`.

    Alert-worthy means ``min_level`` or above (default ERROR), or any record
    whose ``data`` carries an ``alert`` key (the explicit operator-alert
    marker used e.g. by ``exit_ledger_mismatch``) - markers bypass the level
    floor because they are deliberate. ``ignored_patterns`` is a denylist of
    fnmatch patterns matched against the message template and the rendered
    message; a match drops the record even when it is a marker or CRITICAL.
    When ``allowed_events`` is set, only records whose ``data.alert`` marker
    or logger name match may page - except markers and CRITICAL records,
    which always page because muting them would be fail-open. A logger-name
    entry matches the logger exactly or as a dotted parent (``trading.x``
    covers ``trading.x.worker`` but not ``trading.xy``). ``emit`` never
    raises and never blocks: the sink queue is bounded and ``put_nowait``
    drops on overflow.
    """

    def __init__(
        self,
        sink: AlertSink,
        *,
        min_level: int = DEFAULT_MIN_LEVEL,
        allowed_events: frozenset[str] | None = None,
        ignored_patterns: frozenset[str] | None = None,
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self._sink = sink
        self._min_level = min_level
        self._allowed_events = allowed_events
        self._ignored_patterns = ignored_patterns

    def _is_ignored(self, record: logging.LogRecord) -> bool:
        if not self._ignored_patterns:
            return False
        template = str(record.msg)
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = template
        return any(
            fnmatch.fnmatchcase(template, pattern)
            or fnmatch.fnmatchcase(rendered, pattern)
            for pattern in self._ignored_patterns
        )

    def _passes_allowlist(
        self, record: logging.LogRecord, alert_name: str | None
    ) -> bool:
        if self._allowed_events is None:
            return True
        # Markers and CRITICAL always page: the allowlist filters routine
        # errors, and muting these two would be a fail-open foot-gun.
        if alert_name is not None or record.levelno >= logging.CRITICAL:
            return True
        if record.name in self._allowed_events:
            return True
        return any(
            record.name.startswith(f"{entry}.") for entry in self._allowed_events
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # This module's own logger must never page: with a lowered
            # OPS_ALERT_MIN_LEVEL its records (the backend-failure warning
            # above, the install-time filter lines) would re-enter the sink,
            # and a failing webhook would enqueue a new alert on every
            # failure - dedup state is only committed on a successful send.
            if record.name == "trading.alerts":
                return
            data = getattr(record, "data", None)
            alert_name = (
                str(data.get("alert")) if isinstance(data, Mapping) and data.get("alert") else None
            )
            if alert_name is None and record.levelno < self._min_level:
                return
            if self._is_ignored(record):
                return
            if not self._passes_allowlist(record, alert_name):
                return
            # Redact before anything leaves the process: record.data and
            # getMessage() are raw, the JSON formatter only redacts at format
            # time, and a webhook is a wider audience than the log file.
            from shared.logging import redact_text, redact_value

            payload = {
                "service": record.name,
                "level": record.levelname,
                "location": f"{record.module}:{record.funcName}:{record.lineno}",
                "message": redact_text(record.getMessage()),
                "data": redact_value(data) if isinstance(data, Mapping) else None,
            }
            if record.exc_info and record.exc_info[0] is not None:
                payload["exception"] = redact_text(
                    _EXC_FORMATTER.formatException(record.exc_info)
                )
            if alert_name:
                payload["alert"] = alert_name
            dedup_key = alert_name or _record_dedup_key(record)
            if isinstance(data, Mapping):
                # The same alert on a different deployment/account/symbol is a
                # different event: fold the stable identifiers into the key so
                # one deployment's storm cannot mute another's first alert.
                scope = {
                    key: data.get(key)
                    for key in ("account_id", "deployment_id", "service_id", "symbol")
                    if data.get(key) is not None
                }
                if scope:
                    dedup_key = (
                        f"{dedup_key}|{json.dumps(scope, sort_keys=True, default=str)}"
                    )
            self._sink.submit(payload, dedup_key=dedup_key)
        except Exception:
            # Alerting must never break the code path that logged the record.
            pass


def _record_dedup_key(record: logging.LogRecord) -> str:
    """Stable key for records without an explicit ``alert`` marker.

    Uses the message *template* (``record.msg``), not the formatted message, so
    the same failure logged with different arguments (ids, quantities) still
    deduplicates instead of paging once per occurrence.
    """
    raw = f"{record.name}|{record.levelno}|{record.msg!r}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


_ALERT_HANDLER: AlertLogHandler | None = None
_ALERT_SINK: AlertSink | None = None


def install_alert_sink(
    *,
    env: Mapping[str, str] | None = None,
    sink: AlertSink | None = None,
) -> AlertSink | None:
    """Create the sink from the environment and attach it to ``trading.*`` loggers.

    Idempotent per process: a second call returns the already-installed sink
    rather than double-attaching the handler. Returns ``None`` when
    ``OPS_ALERT_WEBHOOK_URL`` is unset (alerting disabled).
    """
    global _ALERT_HANDLER, _ALERT_SINK
    if _ALERT_SINK is not None:
        return _ALERT_SINK
    values = env if env is not None else os.environ
    _ALERT_SINK = sink or create_sink_from_env(env=values)
    if _ALERT_SINK is None:
        return None
    min_level, allowed_events, ignored_patterns = _alert_filter_from_env(values)
    _ALERT_HANDLER = AlertLogHandler(
        _ALERT_SINK,
        min_level=min_level,
        allowed_events=allowed_events,
        ignored_patterns=ignored_patterns,
    )
    # The filter decides what pages; a misconfiguration (e.g. an allowlist
    # matching nothing, or a denylist wide enough to mute the known markers)
    # must be visible in the log, not a silently dead or silently mute
    # channel. Logged once per install.
    _ops_logger().info(
        "ops alert filter: min_level=%s allowed_events=%s ignored=%s",
        logging.getLevelName(min_level),
        sorted(allowed_events) if allowed_events else None,
        sorted(ignored_patterns) if ignored_patterns else None,
    )
    if allowed_events is not None:
        _ops_logger().warning(
            "ops alert allowlist %s is set: only matching event classes page "
            "(data.alert markers and CRITICAL records always page)",
            sorted(allowed_events),
        )
    if ignored_patterns is not None:
        _ops_logger().warning(
            "ops alert denylist %s is set: matching records are muted even "
            "when they are data.alert markers or CRITICAL",
            sorted(ignored_patterns),
        )
    _ALERT_HANDLER.setFormatter(logging.Formatter("%(message)s"))
    # Loggers created through shared.logging.get_logger keep LOG_LEVEL on their
    # file/console handlers, so lowering the logger itself does not change what
    # is written to disk or stdout — it only lets `data.alert` markers below
    # LOG_LEVEL reach this handler. Side effect: `logger.debug(...)` in hot
    # loops now builds a LogRecord and walks the handlers (no isEnabledFor
    # guards exist in the repo); output is unchanged, only the record
    # construction cost is added. Foreign `trading.*` loggers keep their own
    # level; ERROR records still alert through them.
    from shared.logging import managed_service_loggers

    managed = set(managed_service_loggers())
    for existing in list(logging.Logger.manager.loggerDict.values()):
        if not (
            isinstance(existing, logging.Logger) and existing.name.startswith("trading.")
        ):
            continue
        if _ALERT_HANDLER not in existing.handlers:
            existing.addHandler(_ALERT_HANDLER)
        if existing in managed:
            existing.setLevel(logging.DEBUG)
    return _ALERT_SINK


def get_alert_handler() -> AlertLogHandler | None:
    """The installed handler, for ``shared.logging.get_logger`` to attach to new loggers."""
    return _ALERT_HANDLER


def close_alert_sink(timeout: float = SHUTDOWN_FLUSH_SECONDS) -> None:
    """Detach the handler and drain/stop the sink.

    Called from daemon shutdown so alerts logged while the process is stopping
    (the most likely ones to matter) are not stranded in the queue. Bounded by
    ``timeout``; the worker is a daemon thread either way.
    """
    global _ALERT_HANDLER, _ALERT_SINK
    handler = _ALERT_HANDLER
    sink = _ALERT_SINK
    if handler is not None:
        # Hold the handler lock across detach and drain: an emit already running
        # on another thread finishes its queue submit before the worker is
        # stopped, so its alert cannot be stranded in a queue nobody drains.
        # Bounded by ``timeout`` via the worker join inside ``sink.close``.
        handler.acquire()
        try:
            for existing in list(logging.Logger.manager.loggerDict.values()):
                if (
                    isinstance(existing, logging.Logger)
                    and handler in existing.handlers
                ):
                    existing.removeHandler(handler)
            if sink is not None:
                sink.close(timeout=timeout)
        finally:
            handler.release()
    elif sink is not None:
        sink.close(timeout=timeout)
    # Restore the configured level on managed loggers that install lowered.
    from shared.logging import LOG_LEVEL, managed_service_loggers

    for managed in managed_service_loggers():
        managed.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    _ALERT_HANDLER = None
    _ALERT_SINK = None


def _reset_for_tests() -> None:
    """Detach and stop everything ``install_alert_sink`` set up."""
    close_alert_sink(timeout=5.0)
