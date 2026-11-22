"""Append-only archive of raw Discord signals (issue #308).

Every Discord message a ``DiscordSignalSource`` emits for signal processing
is recorded once (``discord_signal_received``) — parsed or refused, and
including messages from authors outside the trusted allowlist, which are
kept for impostor auditing and marked ``author_trusted: false``. The
startup cursor-priming page is only marked seen and is not archived.
Every per-deployment trading decision on a parsed signal is recorded once
(``discord_signal_decision``). The store exists so the signal bots can be
backtested and meta-labelled later: without it the raw signals only exist in
the log stream, and only from the day recording starts.

Storage is JSON Lines under ``DATA_DIR / "signal_log" /``, one file per UTC
day. JSONL was chosen over the SQLite ``backtests.db`` deliberately: the
archive must never contend with the trading database, must stay trivially
exportable for offline labelling, and "append-only" is literal — a line is
never rewritten. The repo already keeps file-based stores next to the DB
(``DATA_DIR / "backtests"``, ``DATA_DIR / "grid_results"``, signal CSVs under
``DATA_DIR / "signals"``), so a directory of append-only files fits the
existing persistence layout.

Every record carries ``schema_version``; readers of the offline pipeline
must skip lines that do not parse (a crash mid-write can leave a partial
line) and deduplicate on ``discord_message_id`` — a daemon restart can
re-archive recent messages the poll cursor has not yet committed.

The signal path must never block, delay or raise because of the archive:
``record()`` only does a bounded ``put_nowait`` and all serialization and
disk I/O happen on a single daemon writer thread. A full queue drops the
record, counts it and warns; a dead writer counts write errors and warns.
Set ``TP_SIGNAL_ARCHIVE=0`` (or ``false``/``off``/``no``) to disable.
"""

from __future__ import annotations

import json
import math
import os
import queue
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from live_runner.promotion import deployment_signal_processor_name
from shared import constants as _constants
from shared.logging import get_logger

logger = get_logger("signal-archive")

ARCHIVE_DIR_NAME = "signal_log"
ARCHIVE_FILE_PREFIX = "discord_signals-"
ARCHIVE_SUFFIX = ".jsonl"

#: Environment kill switch. Anything but an explicit off value leaves the
#: archive enabled — recording has to start now, so the default is on.
ARCHIVE_ENABLED_ENV = "TP_SIGNAL_ARCHIVE"
_ARCHIVE_DISABLED_VALUES = frozenset({"0", "false", "off", "no"})

#: Bound on queued records. At a few KB per record this caps the backlog at
#: tens of MB; beyond it records are dropped (counted, warned) rather than
#: letting a stalled disk grow memory without bound.
DEFAULT_QUEUE_SIZE = 10_000

#: Items written per flush by the writer thread.
_WRITER_BATCH = 512

_SENTINEL = object()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _archive_enabled_from_env() -> bool:
    return os.environ.get(ARCHIVE_ENABLED_ENV, "").strip().lower() not in _ARCHIVE_DISABLED_VALUES


def archive_dir() -> Path:
    """The archive directory, resolved lazily so ``set_data_dir`` is honoured."""
    return Path(_constants.DATA_DIR) / ARCHIVE_DIR_NAME


def _record_file_for(record: Mapping[str, Any]) -> Path:
    """Daily file for a record, keyed on its ``logged_at`` day (UTC)."""
    day = ""
    logged_at = record.get("logged_at")
    if isinstance(logged_at, str) and logged_at:
        try:
            day = datetime.fromisoformat(logged_at.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            day = ""
    if not day:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return archive_dir() / f"{ARCHIVE_FILE_PREFIX}{day}{ARCHIVE_SUFFIX}"


class SignalArchive:
    """Bounded queue plus one writer thread appending JSONL records to disk.

    Every public method is total: nothing here raises into the caller, because
    the caller is the live signal path.
    """

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False
        # Counters the signal path and ops can inspect. Plain ints: a lost
        # increment under a race is cosmetic, a lock here would not be.
        self.written = 0
        self.dropped = 0
        self.write_errors = 0
        self.enqueue_errors = 0

    # -- producer side -----------------------------------------------------

    def record(self, record: Mapping[str, Any]) -> None:
        """Enqueue one record. Never blocks, never raises."""
        if not self.enabled:
            return
        try:
            # The closed-check and the put are atomic with close()'s flag
            # flip: a record can never land behind a point the writer has
            # already decided is the end of the stream.
            with self._state_lock:
                if self._closed:
                    return
                self._queue.put_nowait(dict(record))
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                logger.warning(
                    "Signal archive queue is full; dropping records "
                    "(the archive must never delay the signal path)",
                    extra={"data": {"dropped_total": self.dropped}},
                )
            return
        except Exception as exc:  # pragma: no cover - defensive
            self.enqueue_errors += 1
            logger.warning(
                "Signal archive could not enqueue a record",
                extra={"data": {"error": str(exc)}},
            )
            return
        self._ensure_writer()

    def _ensure_writer(self) -> None:
        with self._state_lock:
            if self._closed or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(
                target=self._writer_loop,
                name="signal-archive-writer",
                daemon=True,
            )
            self._thread.start()

    # -- consumer side -----------------------------------------------------

    def _writer_loop(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=0.5)
            except queue.Empty:
                # close() drops its sentinel when the queue is full, so the
                # sentinel alone cannot end the loop: once closed and drained
                # the writer exits on its own.
                if self._closed and self._queue.empty():
                    return
                continue
            batch = [first]
            while len(batch) < _WRITER_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._write_batch(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()
            if _SENTINEL in batch:
                # Drain whatever producers managed to enqueue before close.
                # The sentinel is the last item close() can place, but a
                # record() that passed its _closed check just before close()
                # set it can still land behind the sentinel — write those too.
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        if item is not _SENTINEL:
                            self._write_batch([item])
                    finally:
                        self._queue.task_done()
                return
            if self._closed and self._queue.empty():
                return

    def _write_batch(self, batch: list[Any]) -> None:
        records = [item for item in batch if item is not _SENTINEL]
        if not records:
            return
        by_day: dict[str, list[str]] = {}
        for record in records:
            try:
                line = json.dumps(record, default=str, separators=(",", ":"))
            except Exception as exc:
                self.write_errors += 1
                logger.warning(
                    "Signal archive could not serialize a record",
                    extra={"data": {"error": str(exc)}},
                )
                continue
            by_day.setdefault(_record_file_for(record).name, []).append(line)
        for name, lines in by_day.items():
            path = archive_dir() / name
            # One payload, one os.write per file per batch on an O_APPEND fd.
            # A short write is a write error, not a retry: a full disk or
            # signal can still leave a partial last line, which readers skip.
            payload = "".join(f"{line}\n" for line in lines).encode("utf-8")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(
                    path,
                    os.O_WRONLY
                    | os.O_APPEND
                    | os.O_CREAT
                    | getattr(os, "O_BINARY", 0),
                    0o644,
                )
                try:
                    written = os.write(fd, payload)
                    if written != len(payload):
                        raise OSError(
                            "short write on signal archive file: "
                            f"{written}/{len(payload)} bytes"
                        )
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.written += len(lines)
            except Exception as exc:
                self.write_errors += len(lines)
                logger.warning(
                    "Signal archive could not write records",
                    extra={"data": {"path": str(path), "error": str(exc)}},
                )

    # -- lifecycle ---------------------------------------------------------

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until every enqueued record has been written. Test/ops helper."""
        deadline = threading.Event()
        def _wait() -> None:
            self._queue.join()
            deadline.set()
        waiter = threading.Thread(target=_wait, name="signal-archive-flush", daemon=True)
        waiter.start()
        waiter.join(timeout)
        return deadline.is_set()

    def close(self, timeout: float = 5.0) -> None:
        """Stop accepting records and let the writer drain, best effort."""
        # The sentinel goes out under the same lock record() checks, so it
        # always lands behind every accepted record; when the queue is full
        # it is dropped and the writer exits on `_closed and queue.empty()`.
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            try:
                self._queue.put_nowait(_SENTINEL)
            except queue.Full:
                pass
        if thread is not None:
            thread.join(timeout)

    def stats(self) -> dict[str, int]:
        return {
            "written": self.written,
            "dropped": self.dropped,
            "write_errors": self.write_errors,
            "enqueue_errors": self.enqueue_errors,
            "queued": self._queue.qsize(),
        }


_ARCHIVE: SignalArchive | None = None
_ARCHIVE_LOCK = threading.Lock()


def get_signal_archive() -> SignalArchive:
    """The process-wide archive. Constructed lazily on first use."""
    global _ARCHIVE
    with _ARCHIVE_LOCK:
        if _ARCHIVE is None:
            _ARCHIVE = SignalArchive(enabled=_archive_enabled_from_env())
        return _ARCHIVE


def reset_signal_archive() -> None:
    """Drop the singleton (test isolation). Waits briefly for the old writer."""
    global _ARCHIVE
    with _ARCHIVE_LOCK:
        old = _ARCHIVE
        _ARCHIVE = None
    if old is not None:
        old.close(timeout=2.0)


# -- record builders ---------------------------------------------------------


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def received_record(
    *,
    bot_name: str,
    account_id: str | None,
    service_id: str | None,
    message: Mapping[str, Any],
    channel_id: str,
    parsed: Any | None,
    refusal: tuple[str, str | None] | None,
    author_trusted: bool | None = None,
) -> dict[str, Any]:
    """One ``discord_signal_received`` record for an emitted Discord message.

    ``parsed`` is the ``TradeSignal`` the parser produced (``None`` when it
    refused); ``refusal`` is the ``(reason, detail)`` the parser reported.
    Everything is read defensively: this runs on the signal path and must
    never raise.
    """
    author = message.get("author") if isinstance(message.get("author"), Mapping) else {}
    author = author or {}
    logged_at = utcnow_iso()
    created_at = message.get("timestamp")
    content = str(message.get("content") or "")

    parsed_fields: dict[str, Any] | None = None
    signal_type = None
    is_exit = False
    if parsed is not None:
        metadata = getattr(parsed, "metadata", None) or {}
        signal_type = metadata.get("discord_signal_type")
        is_exit = signal_type in {"exit", "partial_exit"}
        parsed_fields = {
            "action": getattr(parsed, "action", None),
            "symbol": getattr(parsed, "symbol", None),
            "entry_price": _finite(getattr(parsed, "entry_price", None)),
            "stop_loss": _finite(getattr(parsed, "stop_loss", None)),
            "take_profit": _finite(getattr(parsed, "take_profit", None)),
            "timeframe": getattr(parsed, "timeframe", None),
            "signal_type": signal_type,
        }

    # Cheap receipt-time features: clock features of the signal itself and
    # shape features of the parsed fields. Market features need OHLCV, which
    # only exists later on the deployment path (see market_features_from_frame).
    clock_source = created_at if isinstance(created_at, str) and created_at else logged_at
    hour_utc: int | None = None
    dow_utc: int | None = None
    try:
        moment = datetime.fromisoformat(str(clock_source).replace("Z", "+00:00"))
        hour_utc = moment.hour
        dow_utc = moment.weekday()
    except (ValueError, AttributeError):
        pass
    features: dict[str, Any] = {
        "hour_utc": hour_utc,
        "day_of_week_utc": dow_utc,
        "content_length": len(content),
        "has_explicit_stop_loss": parsed_fields is not None
        and parsed_fields.get("stop_loss") is not None,
        "has_explicit_take_profit": parsed_fields is not None
        and parsed_fields.get("take_profit") is not None,
        "is_exit_signal": is_exit,
    }

    return {
        "type": "discord_signal_received",
        "schema_version": 1,
        "logged_at": logged_at,
        "message_created_at": created_at if isinstance(created_at, str) else None,
        "bot_name": str(bot_name or ""),
        "account_id": account_id,
        "service_id": service_id,
        "discord_message_id": str(message.get("id") or ""),
        "discord_channel_id": str(
            message.get("channel_id") or channel_id or ""
        ),
        "discord_author_id": str(author.get("id") or ""),
        "author_username": str(author.get("username") or "") or None,
        "author_is_bot": bool(author.get("bot")),
        # False marks a message kept for impostor auditing: it is archived
        # like every other polled message but was refused by the
        # trusted-author allowlist before parsing.
        "author_trusted": author_trusted,
        "discord_message_type": message.get("type"),
        "content": content,
        "parse_status": "parsed" if parsed is not None else "rejected",
        "refusal_reason": refusal[0] if refusal else None,
        "refusal_detail": refusal[1] if refusal else None,
        "signal": parsed_fields,
        "features": features,
    }


def _signal_age_seconds(metadata: Mapping[str, Any]) -> float | None:
    raw = str(metadata.get("received_at") or "").strip()
    if not raw:
        return None
    try:
        received = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - received).total_seconds()


class SignalDecisionRecorder:
    """Accumulates one deployment's outcome for one signal, writes one record.

    Exactly one terminal call (``traded``/``refused``/``ignored``/``unknown``)
    produces the record; later calls are ignored so a path that records and
    then falls through cannot double-write.
    """

    def __init__(
        self,
        *,
        trade_signal: Any,
        account_id: str | None,
        deployment: Any | None,
        archive: SignalArchive | None = None,
    ) -> None:
        self._archive = archive if archive is not None else get_signal_archive()
        self._signal = trade_signal
        self._account_id = account_id
        self._deployment = deployment
        self._features: dict[str, Any] = {}
        self._done = False
        self._lock = threading.Lock()

    def set_features(self, features: Mapping[str, Any] | None) -> None:
        """Merge cheaply computed signal-time features into the record."""
        if not features:
            return
        with self._lock:
            for key, value in features.items():
                if value is not None:
                    self._features[str(key)] = value

    def traded(self, *, detail: str | None = None, **extra: Any) -> None:
        self._finish("traded", None, detail, extra)

    def refused(self, reason: str, *, detail: str | None = None, **extra: Any) -> None:
        self._finish("refused", reason, detail, extra)

    def ignored(self, reason: str, *, detail: str | None = None, **extra: Any) -> None:
        self._finish("ignored", reason, detail, extra)

    def unknown(self, reason: str, *, detail: str | None = None, **extra: Any) -> None:
        self._finish("unknown", reason, detail, extra)

    def _finish(
        self, outcome: str, reason: str | None, detail: str | None, extra: Mapping[str, Any]
    ) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
            features = dict(self._features)
        try:
            self._archive.record(self._build_record(outcome, reason, detail, features, extra))
        except Exception as exc:  # pragma: no cover - record() is already total
            logger.warning(
                "Signal archive decision record failed",
                extra={"data": {"error": str(exc)}},
            )

    def _build_record(
        self,
        outcome: str,
        reason: str | None,
        detail: str | None,
        features: Mapping[str, Any],
        extra: Mapping[str, Any],
    ) -> dict[str, Any]:
        signal = self._signal
        metadata = dict(getattr(signal, "metadata", None) or {})
        deployment = self._deployment
        signal_processor = (
            deployment_signal_processor_name(deployment) or None
            if deployment is not None
            else None
        )
        return {
            "type": "discord_signal_decision",
            "schema_version": 1,
            "logged_at": utcnow_iso(),
            "discord_message_id": metadata.get("discord_message_id"),
            "discord_channel_id": metadata.get("discord_channel_id"),
            "discord_author_id": metadata.get("discord_author_id"),
            "discord_bot_name": metadata.get("discord_bot_name") or metadata.get("source"),
            "account_id": self._account_id,
            "deployment_id": (
                str(getattr(deployment, "id", "") or "") or None
                if deployment is not None
                else None
            ),
            "strategy_name": getattr(deployment, "strategy_name", None),
            "signal_processor": signal_processor,
            "signal_kind": metadata.get("discord_signal_type") or "entry",
            "symbol": getattr(signal, "symbol", None),
            "action": getattr(signal, "action", None),
            "entry_price": _finite(getattr(signal, "entry_price", None)),
            "stop_loss": _finite(getattr(signal, "stop_loss", None)),
            "take_profit": _finite(getattr(signal, "take_profit", None)),
            "timeframe": getattr(signal, "timeframe", None),
            "outcome": outcome,
            "reason": reason,
            "detail": detail,
            "signal_age_seconds": _signal_age_seconds(metadata),
            "features": dict(features),
            **dict(extra),
        }


def record_signal_decision(
    trade_signal: Any,
    *,
    account_id: str | None,
    deployment: Any | None,
    outcome: str,
    reason: str | None = None,
    detail: str | None = None,
    features: Mapping[str, Any] | None = None,
    archive: SignalArchive | None = None,
) -> None:
    """Write a single terminal decision record (dispatch-gate refusals)."""
    recorder = SignalDecisionRecorder(
        trade_signal=trade_signal,
        account_id=account_id,
        deployment=deployment,
        archive=archive,
    )
    recorder.set_features(features)
    if outcome == "traded":
        recorder.traded(detail=detail)
    elif outcome == "ignored":
        recorder.ignored(reason or "ignored", detail=detail)
    elif outcome == "unknown":
        recorder.unknown(reason or "unknown", detail=detail)
    else:
        recorder.refused(reason or "refused", detail=detail)


# -- cheap market features ---------------------------------------------------

_ATR_SPAN = 14
_SMA_FAST = 20
_SMA_SLOW = 50


def market_features_from_frame(frame: pd.DataFrame | None) -> dict[str, Any]:
    """Market features computable from an OHLCV frame already in hand.

    Every value is derived from the frame's own bars — no network, no broker
    calls — so this is safe to run on the signal path. It never raises: any
    failure yields ``{}`` and the caller simply records fewer features.

    Deliberately v0: ATR(14) and its percentile rank inside the frame, distances
    to SMA20/SMA50 and to a volume-weighted average price (only when a
    ``volume`` column exists), the latest bar's gap, and a coarse trend regime.
    Follow-ups the issue names but this cannot do without extra data: bid/ask
    spread (needs quotes), a session-anchored VWAP, and multi-window
    percentiles.
    """
    features: dict[str, Any] = {}
    try:
        if frame is None or getattr(frame, "empty", True):
            return features
        required = {"open", "high", "low", "close"}
        columns = {str(column).strip().lower() for column in frame.columns}
        if not required.issubset(columns):
            return features
        work = frame.copy()
        work.columns = [str(column).strip().lower() for column in work.columns]
        for column in ("open", "high", "low", "close"):
            work[column] = pd.to_numeric(work[column], errors="coerce")
        work = work.dropna(subset=["open", "high", "low", "close"])
        if len(work) < 2:
            return features

        high = work["high"].astype(float)
        low = work["low"].astype(float)
        close = work["close"].astype(float)
        prev_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        # First bar has no previous close; seed its TR with high - low so
        # atr_14 exists on short frames. The ewm(span=14, adjust=False)
        # smoothing matches the convention the signal processors use.
        true_range = true_range.fillna(high - low)
        atr = true_range.ewm(span=_ATR_SPAN, adjust=False).mean()

        features["bars"] = int(len(work))
        features["close"] = _finite(close.iloc[-1])
        atr_last = _finite(atr.iloc[-1])
        features["atr_14"] = atr_last
        atr_history = atr.dropna()
        if len(atr_history) >= 2 and atr_last is not None:
            features["atr_14_pct_rank"] = _finite(
                float((atr_history <= atr_last).mean())
            )

        for span, key in ((_SMA_FAST, "sma_20"), (_SMA_SLOW, "sma_50")):
            if len(work) >= span:
                sma = close.rolling(span).mean().iloc[-1]
                sma_value = _finite(sma)
                features[key] = sma_value
                if sma_value:
                    features[f"dist_{key}_pct"] = _finite(
                        (close.iloc[-1] - sma_value) / sma_value * 100.0
                    )
        dist_sma50 = features.get("dist_sma_50_pct")
        if dist_sma50 is not None:
            features["trend_regime"] = (
                "above_sma50" if dist_sma50 > 0 else "below_sma50"
            )

        if "volume" in work.columns:
            volume = pd.to_numeric(work["volume"], errors="coerce").fillna(0.0)
            typical = (high + low + close) / 3.0
            cum_vol = volume.cumsum()
            last_vol = float(cum_vol.iloc[-1])
            if last_vol > 0:
                vwap = float((typical * volume).cumsum().iloc[-1] / last_vol)
                vwap = _finite(vwap)
                if vwap:
                    features["vwap"] = vwap
                    features["dist_vwap_pct"] = _finite(
                        (close.iloc[-1] - vwap) / vwap * 100.0
                    )

        open_last = _finite(work["open"].iloc[-1])
        prev_close_last = _finite(close.iloc[-2])
        if open_last is not None and prev_close_last:
            features["gap_pct"] = _finite(
                (open_last - prev_close_last) / prev_close_last * 100.0
            )

        index = getattr(work, "index", None)
        if index is not None and len(index):
            last_label = index[-1]
            if isinstance(last_label, pd.Timestamp):
                features["last_bar_time"] = last_label.isoformat()
            else:
                features["last_bar_time"] = str(last_label)
    except Exception as exc:  # pragma: no cover - defensive; features are best-effort
        logger.debug(
            "Market feature computation failed",
            extra={"data": {"error": str(exc)}},
        )
        return {}
    return features
