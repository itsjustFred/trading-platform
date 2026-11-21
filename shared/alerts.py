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

``install_alert_sink`` attaches the handler to every ``trading.*`` service
logger that exists now and, through a hook in ``shared.logging.get_logger``,
to every one created later. Only the live daemon installs it today; other
entry points are unaffected unless they opt in.
"""

from __future__ import annotations

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
#: Bound on queued alerts; a full queue drops and counts.
DEFAULT_QUEUE_SIZE = 256
#: Backend call timeout. Runs on the worker thread, never on a caller.
DEFAULT_BACKEND_TIMEOUT_SECONDS = 5.0
#: Discord message cap is 2000 chars; stay under it.
MAX_MESSAGE_CHARS = 1900

_STOP_POLL_SECONDS = 0.2
_DEDUP_STORE_PRUNE_AT = 1024

_logger = logging.getLogger("alerts")


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
            json={"content": format_alert_text(alert)},
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
            self._send_times.append(now)
            self._last_sent_by_key[dedup_key] = now
            if len(self._last_sent_by_key) > _DEDUP_STORE_PRUNE_AT:
                stale = [
                    key
                    for key, ts in self._last_sent_by_key.items()
                    if now - ts >= self._dedup_seconds
                ]
                for key in stale:
                    del self._last_sent_by_key[key]
        alert = dict(payload)
        if dropped_note:
            alert["dropped"] = dropped_note
        try:
            self._backend.send(alert)
        except Exception as exc:
            with self._lock:
                self._failed += 1
            _logger.warning("ops alert backend send failed: %r", exc)
            return
        with self._lock:
            self._sent += 1
            self._dropped_since_last_send = 0


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


class AlertLogHandler(logging.Handler):
    """Routes alert-worthy records into an :class:`AlertSink`.

    Alert-worthy means ERROR or CRITICAL, or any record whose ``data`` carries
    an ``alert`` key (the explicit operator-alert marker used e.g. by
    ``exit_ledger_mismatch``). ``emit`` never raises and never blocks: the sink
    queue is bounded and ``put_nowait`` drops on overflow.
    """

    def __init__(self, sink: AlertSink) -> None:
        super().__init__(level=logging.NOTSET)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = getattr(record, "data", None)
            alert_name = (
                str(data.get("alert")) if isinstance(data, Mapping) and data.get("alert") else None
            )
            if record.levelno < logging.ERROR and alert_name is None:
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
                payload["exception"] = redact_text(self.formatException(record.exc_info))
            if alert_name:
                payload["alert"] = alert_name
            dedup_key = alert_name or _record_dedup_key(record)
            if alert_name and isinstance(data, Mapping):
                # The same alert on a different deployment/account/symbol is a
                # different event: fold the stable identifiers into the key so
                # one deployment's storm cannot mute another's first alert.
                scope = {
                    key: data.get(key)
                    for key in ("account_id", "deployment_id", "service_id", "symbol")
                    if data.get(key) is not None
                }
                if scope:
                    dedup_key = f"{alert_name}|{json.dumps(scope, sort_keys=True, default=str)}"
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
    _ALERT_SINK = sink or create_sink_from_env(env=env)
    if _ALERT_SINK is None:
        return None
    _ALERT_HANDLER = AlertLogHandler(_ALERT_SINK)
    _ALERT_HANDLER.setFormatter(logging.Formatter("%(message)s"))
    for existing in list(logging.Logger.manager.loggerDict.values()):
        if (
            isinstance(existing, logging.Logger)
            and existing.name.startswith("trading.")
            and _ALERT_HANDLER not in existing.handlers
        ):
            existing.addHandler(_ALERT_HANDLER)
    return _ALERT_SINK


def get_alert_handler() -> AlertLogHandler | None:
    """The installed handler, for ``shared.logging.get_logger`` to attach to new loggers."""
    return _ALERT_HANDLER


def _reset_for_tests() -> None:
    """Detach and stop everything ``install_alert_sink`` set up."""
    global _ALERT_HANDLER, _ALERT_SINK
    if _ALERT_HANDLER is not None:
        for existing in list(logging.Logger.manager.loggerDict.values()):
            if isinstance(existing, logging.Logger) and _ALERT_HANDLER in existing.handlers:
                existing.removeHandler(_ALERT_HANDLER)
    if _ALERT_SINK is not None:
        _ALERT_SINK.close()
    _ALERT_HANDLER = None
    _ALERT_SINK = None
