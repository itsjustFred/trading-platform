"""Tests for shared/alerts.py: the ops alert sink and its logging handler.

The backend is always a fake here; no test touches the network or a real
webhook. The sink's guarantees under test: never blocks, never raises,
rate-limits, deduplicates, drops-and-counts on overflow.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from typing import Any

import pytest

from shared import alerts
from shared.alerts import (
    AlertLogHandler,
    AlertSink,
    DiscordWebhookBackend,
    create_sink_from_env,
    format_alert_text,
    install_alert_sink,
)


class FakeBackend:
    """Collects alerts; can be gated to simulate a slow/hung webhook."""

    def __init__(
        self,
        *,
        gate: threading.Event | None = None,
        started: threading.Event | None = None,
    ) -> None:
        self.sent: list[dict[str, Any]] = []
        self.gate = gate
        self.started = started
        self._lock = threading.Lock()

    def send(self, alert: Mapping[str, Any]) -> None:
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=10)
        with self._lock:
            self.sent.append(dict(alert))

    def wait_for(self, count: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.sent) >= count:
                return
            time.sleep(0.01)
        raise AssertionError(f"expected {count} alerts, got {len(self.sent)}")


def _sink(backend: FakeBackend, **kwargs: Any) -> AlertSink:
    kwargs.setdefault("dedup_seconds", 0.0)
    return AlertSink(backend, **kwargs)


def _wait_stats(sink: AlertSink, predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stats = sink.stats()
        if predicate(stats):
            return stats
        time.sleep(0.01)
    raise AssertionError(f"stats never satisfied predicate: {sink.stats()}")


@pytest.fixture(autouse=True)
def _clean_alert_state():
    alerts._reset_for_tests()
    yield
    alerts._reset_for_tests()


class TestSinkDelivery:
    def test_submit_delivers_payload_to_backend(self):
        backend = FakeBackend()
        sink = _sink(backend)
        try:
            sink.submit({"message": "boom", "level": "ERROR"}, dedup_key="k1")
            backend.wait_for(1)
            assert backend.sent[0]["message"] == "boom"
            assert sink.stats()["sent"] == 1
        finally:
            sink.close()

    def test_backend_failure_is_counted_not_raised(self):
        class Exploding:
            def send(self, alert):
                raise RuntimeError("webhook down")

        sink = _sink(Exploding())
        try:
            sink.submit({"message": "x"}, dedup_key="k")
            stats = _wait_stats(sink, lambda s: s["failed"] == 1)
            assert stats["sent"] == 0
        finally:
            sink.close()

    def test_dedup_suppresses_same_key_within_window(self):
        backend = FakeBackend()
        sink = AlertSink(backend, dedup_seconds=60.0)
        try:
            for _ in range(5):
                sink.submit({"message": "same"}, dedup_key="dup")
            stats = _wait_stats(sink, lambda s: s["sent"] + s["suppressed"] == 5)
            assert stats["sent"] == 1
            assert stats["suppressed"] == 4
        finally:
            sink.close()

    def test_different_keys_are_not_deduplicated(self):
        backend = FakeBackend()
        sink = _sink(backend)
        try:
            sink.submit({"message": "a"}, dedup_key="k1")
            sink.submit({"message": "b"}, dedup_key="k2")
            backend.wait_for(2)
        finally:
            sink.close()

    def test_rate_limit_drops_and_counts_overflow(self):
        backend = FakeBackend()
        sink = AlertSink(backend, max_per_minute=2, dedup_seconds=0.0)
        try:
            for i in range(6):
                sink.submit({"message": f"m{i}"}, dedup_key=f"k{i}")
            stats = _wait_stats(sink, lambda s: s["sent"] + s["dropped"] == 6)
            assert stats["sent"] == 2
            assert stats["dropped"] == 4
        finally:
            sink.close()

    def test_full_queue_drops_without_blocking_or_raising(self):
        gate = threading.Event()
        backend = FakeBackend(gate=gate)
        sink = AlertSink(backend, queue_size=4, dedup_seconds=0.0)
        try:
            # Worker takes at most one item while gated; 10 submits against a
            # 4-slot queue must drop the excess without blocking the caller.
            for i in range(10):
                sink.submit({"message": f"m{i}"}, dedup_key=f"k{i}")
            stats = sink.stats()
            assert stats["dropped"] > 0
            assert stats["queued"] <= 4
        finally:
            gate.set()
            sink.close()

    def test_dropped_count_is_folded_into_next_alert(self):
        backend = FakeBackend()
        sink = AlertSink(backend, max_per_minute=1, dedup_seconds=0.0, rate_window_seconds=0.2)
        try:
            sink.submit({"message": "first"}, dedup_key="a")
            sink.submit({"message": "dropped1"}, dedup_key="b")
            sink.submit({"message": "dropped2"}, dedup_key="c")
            backend.wait_for(1)
            stats = _wait_stats(sink, lambda s: s["sent"] + s["dropped"] == 3)
            assert stats["sent"] == 1
            assert stats["dropped"] == 2
            # After the rate window slides, the next alert reports the drops.
            time.sleep(0.3)
            sink.submit({"message": "after window"}, dedup_key="d")
            backend.wait_for(2)
            assert backend.sent[1].get("dropped") == 2
        finally:
            sink.close()

    def test_failed_send_does_not_consume_dedup_or_rate_slot(self):
        """A failed webhook attempt must not mute the retry of the same alert."""

        class FlakyBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self._fail_next = True

            def send(self, alert: Mapping[str, Any]) -> None:
                if self._fail_next:
                    self._fail_next = False
                    raise RuntimeError("webhook down")
                super().send(alert)

        backend = FlakyBackend()
        # One send per minute: if the failed attempt consumed the slot or the
        # dedup key, the identical retry below could never go out.
        sink = AlertSink(backend, max_per_minute=1, dedup_seconds=60.0)
        try:
            sink.submit({"message": "same"}, dedup_key="k1")
            sink.submit({"message": "same"}, dedup_key="k1")
            stats = _wait_stats(sink, lambda s: s["sent"] == 1)
            assert stats["failed"] == 1
            assert backend.sent[0]["message"] == "same"
        finally:
            sink.close()

    def test_drops_during_send_are_not_cleared_by_success(self):
        """Drops counted while the backend was busy belong to the next alert."""
        gate = threading.Event()
        started = threading.Event()
        backend = FakeBackend(gate=gate, started=started)
        sink = AlertSink(backend, queue_size=2, dedup_seconds=0.0)
        try:
            sink.submit({"message": "in-flight"}, dedup_key="a")
            assert started.wait(timeout=5), "worker never entered send()"
            # The worker reserved dropped_note=0 and is blocked inside send();
            # fill the 2-slot queue and overflow the rest while it is busy.
            for i in range(6):
                sink.submit({"message": f"q{i}"}, dedup_key=f"q{i}")
            gate.set()
            backend.wait_for(3)
            # "in-flight" reserved dropped_note=0 before sending, so the four
            # queue-overflow drops must surface on the next delivered alert.
            _wait_stats(sink, lambda s: s["sent"] == 3)
            assert backend.sent[1].get("dropped") == 4
        finally:
            gate.set()
            sink.close()


class TestLogHandler:
    @pytest.fixture(autouse=True)
    def _detach_handlers(self):
        """Never leave an AlertLogHandler behind on a global logger."""
        created: list[logging.Logger] = []
        self._created_loggers = created
        yield
        for logger in created:
            for handler in list(logger.handlers):
                if isinstance(handler, AlertLogHandler):
                    logger.removeHandler(handler)

    def _logger(self, name: str) -> logging.Logger:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        self._created_loggers.append(logger)
        return logger

    def test_error_record_reaches_sink(self):
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(sink)
        logger = self._logger("trading.test-alerts-error")
        logger.addHandler(handler)
        try:
            logger.error("broker exploded", extra={"data": {"symbol": "EURUSD"}})
            backend.wait_for(1)
            alert = backend.sent[0]
            assert alert["level"] == "ERROR"
            assert alert["message"] == "broker exploded"
            assert alert["data"]["symbol"] == "EURUSD"
        finally:
            sink.close()

    def test_info_record_is_not_alerted(self):
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(sink)
        logger = self._logger("trading.test-alerts-info")
        logger.addHandler(handler)
        try:
            logger.info("routine heartbeat")
            logger.warning("still routine")
            time.sleep(0.2)
            assert backend.sent == []
        finally:
            sink.close()

    def test_alert_marker_routes_below_error(self):
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(sink)
        logger = self._logger("trading.test-alerts-marker")
        logger.addHandler(handler)
        try:
            logger.warning(
                "operator note",
                extra={"data": {"alert": "custom_marker", "k": 1}},
            )
            backend.wait_for(1)
            assert backend.sent[0]["alert"] == "custom_marker"
        finally:
            sink.close()

    def test_dedup_key_uses_template_not_formatted_message(self):
        """Same failure with different args must dedup, not page per occurrence."""
        backend = FakeBackend()
        sink = AlertSink(backend, dedup_seconds=60.0)
        handler = AlertLogHandler(sink)
        logger = self._logger("trading.test-alerts-templ")
        logger.addHandler(handler)
        try:
            for i in range(4):
                logger.error("fill rejected for id=%s", i)
            stats = _wait_stats(sink, lambda s: s["sent"] + s["suppressed"] == 4)
            assert stats["sent"] == 1
            assert stats["suppressed"] == 3
        finally:
            sink.close()

    def test_sensitive_data_is_redacted_before_send(self):
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(sink)
        logger = self._logger("trading.test-alerts-redact")
        logger.addHandler(handler)
        try:
            logger.error(
                "auth failed for token=abc123secret",
                extra={"data": {"api_key": "sk-live-123", "symbol": "XAUUSD"}},
            )
            backend.wait_for(1)
            alert = backend.sent[0]
            assert "abc123secret" not in alert["message"]
            assert alert["data"]["api_key"] == "[Filtered]"
            assert alert["data"]["symbol"] == "XAUUSD"
        finally:
            sink.close()

    def test_emit_never_raises_on_broken_record(self):
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(sink)
        record = logging.LogRecord(
            "trading.test-alerts-broken",
            logging.ERROR,
            __file__,
            1,
            "msg %s",
            ("arg",),
            None,
        )
        record.data = {"bad": object()}  # not a Mapping value json can fail on -> still fine
        handler.emit(record)  # must not raise
        sink.close()

    def test_exception_record_delivers_traceback(self):
        """logger.exception records must alert, not die on formatException."""
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(sink)
        logger = self._logger("trading.test-alerts-exc")
        logger.addHandler(handler)
        try:
            try:
                raise ValueError("kaput")
            except ValueError:
                logger.exception("it broke")
            backend.wait_for(1)
            alert = backend.sent[0]
            assert alert["message"] == "it broke"
            assert "ValueError" in alert["exception"]
            assert "kaput" in alert["exception"]
            assert "ValueError" in format_alert_text(alert)
        finally:
            sink.close()

    def test_plain_error_dedup_is_scoped_by_data_identifiers(self):
        """Same message template on different symbols must not mute each other."""
        backend = FakeBackend()
        sink = AlertSink(backend, dedup_seconds=60.0)
        handler = AlertLogHandler(sink)
        logger = self._logger("trading.test-alerts-scope")
        logger.addHandler(handler)
        try:
            logger.error("fill rejected", extra={"data": {"symbol": "EURUSD"}})
            logger.error("fill rejected", extra={"data": {"symbol": "XAUUSD"}})
            backend.wait_for(2)
        finally:
            sink.close()


class TestFiltering:
    """OPS_ALERT_MIN_LEVEL / OPS_ALERT_EVENTS: routine errors can be filtered
    without touching call sites. Default stays "every ERROR pages"."""

    def test_min_level_filters_below_threshold(self):
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(sink, min_level=logging.CRITICAL)
        logger = logging.getLogger("trading.test-alerts-minlevel")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            logger.error("routine error")
            logger.critical("real emergency")
            backend.wait_for(1)
            time.sleep(0.2)
            assert [a["level"] for a in backend.sent] == ["CRITICAL"]
        finally:
            logger.removeHandler(handler)
            sink.close()

    def test_alert_marker_bypasses_the_level_floor(self):
        """A deliberate ``data.alert`` marker is not a routine error: it pages
        even when MIN_LEVEL is raised above the record's own level."""
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(sink, min_level=logging.CRITICAL)
        logger = logging.getLogger("trading.test-alerts-marker-floor")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            logger.warning(
                "deliberate marker", extra={"data": {"alert": "m1"}}
            )
            backend.wait_for(1)
            assert backend.sent[0]["alert"] == "m1"
        finally:
            logger.removeHandler(handler)
            sink.close()

    def test_events_allowlist_matches_marker_or_logger_name(self):
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(
            sink, allowed_events=frozenset({"exit_ledger_mismatch"})
        )
        marked = logging.getLogger("trading.test-alerts-allow-marked")
        plain = logging.getLogger("trading.test-alerts-allow-plain")
        for logger in (marked, plain):
            logger.handlers.clear()
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            logger.addHandler(handler)
        try:
            plain.error("routine error")
            marked.error(
                "ledger gap", extra={"data": {"alert": "exit_ledger_mismatch"}}
            )
            backend.wait_for(1)
            time.sleep(0.2)
            assert [a.get("alert") for a in backend.sent] == [
                "exit_ledger_mismatch"
            ]
        finally:
            for logger in (marked, plain):
                logger.removeHandler(handler)
            sink.close()

    def test_events_allowlist_matches_the_logger_name(self):
        """Listing a logger name allows every alert-worthy record it emits."""
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(
            sink, allowed_events=frozenset({"trading.test-alerts-allow-logger"})
        )
        allowed = logging.getLogger("trading.test-alerts-allow-logger")
        other = logging.getLogger("trading.test-alerts-allow-other")
        for logger in (allowed, other):
            logger.handlers.clear()
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            logger.addHandler(handler)
        try:
            other.error("filtered out")
            allowed.error("pages")
            backend.wait_for(1)
            time.sleep(0.2)
            assert [a["message"] for a in backend.sent] == ["pages"]
        finally:
            for logger in (allowed, other):
                logger.removeHandler(handler)
            sink.close()

    def test_ignore_denylist_drops_matching_template(self):
        """OPS_ALERT_IGNORE mutes one noisy routine ERROR without a call-site
        change: fnmatch against the message template (and rendered message)."""
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(
            sink, ignored_patterns=frozenset({"poll failed for *", "*heartbeat*"})
        )
        logger = logging.getLogger("trading.test-alerts-ignore")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            logger.error("poll failed for %s", "EURUSD")
            logger.error("broker heartbeat %s", "ok")
            logger.error("real failure")
            backend.wait_for(1)
            time.sleep(0.2)
            assert [a["message"] for a in backend.sent] == ["real failure"]
        finally:
            logger.removeHandler(handler)
            sink.close()

    def test_ignore_denylist_applies_to_markers_and_critical(self):
        """The denylist is an explicit mute: it wins over markers and CRITICAL,
        unlike the allowlist which never filters those."""
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(
            sink, ignored_patterns=frozenset({"known flap*"})
        )
        logger = logging.getLogger("trading.test-alerts-ignore-marker")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            logger.critical("known flap")
            logger.error(
                "known flap marker",
                extra={"data": {"alert": "flap"}},
            )
            logger.error("not ignored", extra={"data": {"alert": "flap"}})
            backend.wait_for(1)
            time.sleep(0.2)
            assert [a.get("message") for a in backend.sent] == ["not ignored"]
        finally:
            logger.removeHandler(handler)
            sink.close()

    def test_ignore_denylist_matches_template_and_rendered_separately(self):
        """Each denylist arm is load-bearing: ``"poll failed for %s"`` can
        only match the message *template* (the rendered text has the argument
        substituted), and ``"*EURUSD*"`` can only match the *rendered*
        message. Dropping either ``fnmatchcase`` call must fail here."""
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(
            sink,
            ignored_patterns=frozenset({"poll failed for %s", "*EURUSD*"}),
        )
        logger = logging.getLogger("trading.test-alerts-ignore-arms")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            # Template arm only: rendered is "poll failed for GBPUSD", which
            # matches neither pattern.
            logger.error("poll failed for %s", "GBPUSD")
            # Rendered arm only: the template "quote stale for %s" matches
            # neither pattern; the rendered text contains EURUSD.
            logger.error("quote stale for %s", "EURUSD")
            logger.error("real failure")
            backend.wait_for(1)
            time.sleep(0.2)
            assert [a["message"] for a in backend.sent] == ["real failure"]
        finally:
            logger.removeHandler(handler)
            sink.close()

    def test_the_alerts_logger_itself_never_pages(self):
        """``trading.alerts`` carries this module's own operational warnings
        (backend send failed, the install-time filter lines). With a lowered
        ``OPS_ALERT_MIN_LEVEL`` they would otherwise re-enter the sink - and
        a failing webhook would enqueue a new alert on every failure, since
        dedup state is committed only on a successful send."""
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(sink, min_level=logging.WARNING)
        logger = logging.getLogger("trading.alerts")
        logger.addHandler(handler)
        try:
            logger.warning("ops alert backend send failed: RuntimeError")
            time.sleep(0.3)
            assert backend.sent == []
            assert sink.stats()["sent"] == 0
        finally:
            logger.removeHandler(handler)
            sink.close()

    def test_allowlist_uses_logger_hierarchy_prefix(self):
        """An allowlist entry for a parent logger covers its children, matching
        logging's dotted-name hierarchy."""
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(
            sink, allowed_events=frozenset({"trading.test-alerts-hier"})
        )
        child = logging.getLogger("trading.test-alerts-hier.worker")
        sibling = logging.getLogger("trading.test-alerts-hierx")
        for logger in (child, sibling):
            logger.handlers.clear()
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            logger.addHandler(handler)
        try:
            sibling.error("filtered out")
            child.error("pages via parent entry")
            backend.wait_for(1)
            time.sleep(0.2)
            assert [a["message"] for a in backend.sent] == [
                "pages via parent entry"
            ]
        finally:
            for logger in (child, sibling):
                logger.removeHandler(handler)
            sink.close()

    def test_allowlist_never_filters_markers_or_critical(self):
        """Markers and CRITICAL always page: the allowlist exists to filter
        routine errors, and muting those two would be a fail-open foot-gun."""
        backend = FakeBackend()
        sink = _sink(backend)
        handler = AlertLogHandler(
            sink, allowed_events=frozenset({"something-else"})
        )
        logger = logging.getLogger("trading.test-alerts-allow-bypass")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
        try:
            logger.error("routine error")
            logger.critical("emergency")
            logger.error(
                "marker below error",
                extra={"data": {"alert": "exit_ledger_mismatch"}},
            )
            backend.wait_for(2)
            time.sleep(0.2)
            assert sorted(a.get("alert") or a["level"] for a in backend.sent) == [
                "CRITICAL",
                "exit_ledger_mismatch",
            ]
        finally:
            logger.removeHandler(handler)
            sink.close()

    def test_ignore_env_is_parsed_like_the_allowlist(self):
        assert alerts._parse_event_allowlist("a, b ,,c ") == frozenset(
            {"a", "b", "c"}
        )

    def test_install_reads_ignore_from_env(self):
        backend = FakeBackend()
        noisy = logging.getLogger("trading.test-alerts-env-noisy")
        quiet = logging.getLogger("trading.test-alerts-env-quiet")
        for logger in (noisy, quiet):
            logger.handlers.clear()
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
        sink = install_alert_sink(
            sink=_sink(backend),
            env={"OPS_ALERT_IGNORE": "routine *"},
        )
        assert sink is not None
        try:
            noisy.error("routine poll failed")
            quiet.error("real failure")
            backend.wait_for(1)
            time.sleep(0.2)
            assert [a["message"] for a in backend.sent] == ["real failure"]
        finally:
            alerts._reset_for_tests()

    def test_install_logs_the_effective_filter(self):
        """A misconfigured filter must be visible in the log, not silent. The
        lines go through ``trading.alerts`` - the logger ``get_logger`` gives
        the file and console handlers - so the assertion attaches a capture
        handler to that real logger. caplog on the root would pass even when
        the records are dropped in production, which is exactly the defect
        this pins: a bare ``alerts`` logger has no handlers at all."""
        backend = FakeBackend()
        ops_logger = logging.getLogger("trading.alerts")
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        capture = _Capture()
        ops_logger.addHandler(capture)
        try:
            sink = install_alert_sink(
                sink=_sink(backend),
                env={
                    "OPS_ALERT_MIN_LEVEL": "CRITICAL",
                    "OPS_ALERT_EVENTS": "trading.live-runner",
                    "OPS_ALERT_IGNORE": "flap*",
                },
            )
            assert sink is not None
        finally:
            ops_logger.removeHandler(capture)
            alerts._reset_for_tests()
        messages = [r.getMessage() for r in records]
        assert any("min_level" in m and "CRITICAL" in m for m in messages)
        assert any("trading.live-runner" in m for m in messages)
        assert any("flap*" in m for m in messages)
        # The allowlist and the denylist being set are themselves warned
        # about: either can mute the whole channel, so neither may install
        # silently.
        assert any(r.levelno >= logging.WARNING for r in records)
        assert any("denylist" in m for m in messages)

    def test_invalid_min_level_falls_back_to_error(self):
        assert alerts._parse_min_level("bogus") == logging.ERROR
        assert alerts._parse_min_level("") == logging.ERROR
        assert alerts._parse_min_level("critical") == logging.CRITICAL
        assert alerts._parse_min_level("30") == logging.WARNING
        assert alerts._parse_min_level(None) == logging.ERROR

    def test_empty_events_value_means_no_allowlist(self):
        assert alerts._parse_event_allowlist(None) is None
        assert alerts._parse_event_allowlist("") is None
        assert alerts._parse_event_allowlist(" , ") is None
        assert alerts._parse_event_allowlist(" a, b ,,c ") == frozenset(
            {"a", "b", "c"}
        )

    def test_install_reads_the_filter_from_env(self):
        backend = FakeBackend()
        # Loggers must exist before install: the handler attach scans the
        # loggerDict once, it does not retro-fit later getLogger() calls.
        denied = logging.getLogger("trading.test-alerts-env-denied")
        allowed = logging.getLogger("trading.test-alerts-env-allowed")
        for logger in (denied, allowed):
            logger.handlers.clear()
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
        sink = install_alert_sink(
            sink=_sink(backend),
            env={
                "OPS_ALERT_MIN_LEVEL": "CRITICAL",
                "OPS_ALERT_EVENTS": "trading.test-alerts-env-allowed",
            },
        )
        assert sink is not None
        try:
            denied.error("filtered by allowlist")
            allowed.error("filtered by level? no - ERROR < CRITICAL")
            allowed.critical("pages")
            backend.wait_for(1)
            time.sleep(0.2)
            assert [a["message"] for a in backend.sent] == ["pages"]
        finally:
            alerts._reset_for_tests()


class TestInstall:
    def test_disabled_without_webhook_env(self):
        assert create_sink_from_env(env={}) is None
        assert install_alert_sink(env={}) is None

    def test_enabled_with_webhook_env_uses_discord_backend(self, monkeypatch):
        sent = []

        class FakeResponse:
            def raise_for_status(self):
                return None

        monkeypatch.setattr(
            "requests.post",
            lambda url, json=None, timeout=None, params=None: (
                sent.append({"url": url, "json": json, "params": params}) or FakeResponse()
            ),
        )
        sink = create_sink_from_env(env={"OPS_ALERT_WEBHOOK_URL": "https://discord.example/hook"})
        assert sink is not None
        try:
            sink.submit({"message": "hi", "level": "ERROR"}, dedup_key="k")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not sent:
                time.sleep(0.01)
            assert sent and sent[0]["url"] == "https://discord.example/hook"
            assert "hi" in sent[0]["json"]["content"]
        finally:
            sink.close()

    def test_install_attaches_handler_to_existing_and_new_loggers(self):
        backend = FakeBackend()
        # A logger that already exists when install runs is picked up by the
        # loggerDict scan inside install_alert_sink.
        pre = logging.getLogger("trading.test-alerts-preinstall")
        pre.setLevel(logging.DEBUG)
        pre.propagate = False
        sink = install_alert_sink(sink=_sink(backend))
        assert sink is not None
        assert any(isinstance(h, AlertLogHandler) for h in pre.handlers)
        try:
            # A service logger created after install gets the handler through
            # the get_logger hook.
            from shared.logging import get_logger

            post = get_logger("test-alerts-postinstall")
            post.error("post-install failure")
            backend.wait_for(1)
            assert backend.sent[0]["message"] == "post-install failure"
        finally:
            alerts._reset_for_tests()

    def test_install_is_idempotent(self):
        backend = FakeBackend()
        first = install_alert_sink(sink=_sink(backend))
        # No sink argument: a second candidate must not be constructed (its
        # worker thread would leak, since the first sink wins).
        second = install_alert_sink()
        assert first is second

    def test_install_lowers_managed_logger_level_and_close_restores(self):
        """LOG_LEVEL moves to the output handlers so alert= markers always route."""
        import shared.logging as shared_logging

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(shared_logging, "LOG_LEVEL", "ERROR")
        try:
            from shared.logging import get_logger

            service = get_logger("test-alerts-level")
            assert service.level == logging.ERROR
            output_levels = {
                h.level for h in service.handlers if not isinstance(h, AlertLogHandler)
            }
            assert output_levels == {logging.ERROR}

            backend = FakeBackend()
            sink = install_alert_sink(sink=_sink(backend))
            assert sink is not None
            assert service.level == logging.DEBUG
            # A WARNING record with an alert marker must reach the sink even
            # though LOG_LEVEL=ERROR keeps it out of the file/console handlers.
            service.warning("marker below log level", extra={"data": {"alert": "m1"}})
            backend.wait_for(1)
            assert backend.sent[0]["alert"] == "m1"

            alerts.close_alert_sink()
            assert service.level == logging.ERROR
        finally:
            monkeypatch.undo()
            alerts._reset_for_tests()

    def test_close_alert_sink_drains_queued_alerts(self):
        backend = FakeBackend()
        sink = install_alert_sink(sink=_sink(backend))
        assert sink is not None
        sink.submit({"message": "shutdown error", "level": "ERROR"}, dedup_key="k")
        alerts.close_alert_sink(timeout=5.0)
        backend.wait_for(1)
        assert backend.sent[0]["message"] == "shutdown error"

    def test_daemon_startup_failure_still_delivers_one_alert(
        self, monkeypatch, tmp_path
    ):
        """A crash before the main loop must page and must not leak the sink."""
        import asyncio

        import live_runner.daemon as daemon_module

        backend = FakeBackend()
        assert install_alert_sink(sink=_sink(backend)) is not None

        def _boom(*args, **kwargs):
            raise RuntimeError("accounts database is on fire")

        monkeypatch.setattr(
            daemon_module, "_discover_and_register_discord_services", _boom
        )
        with pytest.raises(RuntimeError, match="accounts database is on fire"):
            asyncio.run(
                daemon_module.run_daemon(accounts_env=tmp_path / "accounts.env")
            )
        backend.wait_for(1)
        time.sleep(0.2)
        assert len(backend.sent) == 1
        assert backend.sent[0]["message"] == "Live daemon failed to start"
        assert "RuntimeError" in backend.sent[0]["exception"]
        # run_daemon's finally closed the sink; nothing stays installed.
        assert alerts._ALERT_SINK is None
        assert alerts._ALERT_HANDLER is None


class TestFormatting:
    def test_format_includes_level_service_location_and_data(self):
        text = format_alert_text(
            {
                "level": "ERROR",
                "service": "trading.live-runner",
                "location": "daemon:run:10",
                "message": "boom",
                "data": {"symbol": "EURUSD"},
            }
        )
        assert "[ERROR] trading.live-runner daemon:run:10" in text
        assert "boom" in text
        assert "EURUSD" in text

    def test_format_truncates_to_discord_limit(self):
        text = format_alert_text({"message": "x" * 5000})
        assert len(text) <= alerts.MAX_MESSAGE_CHARS

    def test_discord_backend_payload_shape(self, monkeypatch):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

        def fake_post(url, json=None, timeout=None, params=None):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            captured["params"] = params
            return FakeResponse()

        monkeypatch.setattr("requests.post", fake_post)
        backend = DiscordWebhookBackend("https://discord.example/hook")
        backend.send({"message": "alert body", "level": "ERROR"})
        assert captured["url"] == "https://discord.example/hook"
        assert captured["params"] == {"wait": "true"}
        assert captured["json"] == {
            "content": format_alert_text({"message": "alert body", "level": "ERROR"}),
            "allowed_mentions": {"parse": []},
        }
        assert captured["timeout"] == alerts.DEFAULT_BACKEND_TIMEOUT_SECONDS
