"""Tests for the live internal strategy runner (issue #348).

Fake bar feeds + fake adapters only: nothing here touches a broker, the
network, or the repo's ``data/`` directory.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from live_runner.internal_strategy_runner import InternalStrategyRunner
from risk.models import TradeSignal
from shared.models import LiveDeploymentConfig

# Monday 10:00 ET — squarely inside the NYSE regular session, so equity
# symbols resolve to trading bars under the venue calendar.
BASE = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


def _bars(count: int = 30, *, last_open: datetime = BASE, step_seconds: int = 60) -> pd.DataFrame:
    """``count`` 1m bars ending with the bar that opens at ``last_open``."""
    opens = [last_open - timedelta(seconds=step_seconds * (count - 1 - i)) for i in range(count)]
    return pd.DataFrame(
        {
            "timestamp": [o.isoformat() for o in opens],
            "open": [100.0 + i * 0.1 for i in range(count)],
            "high": [101.0 + i * 0.1 for i in range(count)],
            "low": [99.0 + i * 0.1 for i in range(count)],
            "close": [100.5 + i * 0.1 for i in range(count)],
            "volume": [1000.0] * count,
        }
    )


class FakeAdapter:
    """Minimal BaseLiveAdapter stand-in: bars only, no broker."""

    demo = True

    def __init__(self, frame: pd.DataFrame | None = None, *, serve_bars: bool = True) -> None:
        self.frame = frame if frame is not None else _bars()
        self.serve_bars = serve_bars
        self.calls: list[tuple[str, str, int]] = []
        self.working_ids: set[str] | None = None
        self.positions: list[dict[str, Any]] = []
        self.positions_error: Exception | None = None

    async def get_market_data(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        self.calls.append((symbol, timeframe, limit))
        return self.frame.tail(limit).reset_index(drop=True)

    def supports_market_data_timeframe(self, timeframe: str) -> bool:
        return self.serve_bars

    async def working_order_ids(self) -> set[str]:
        if self.working_ids is None:
            raise RuntimeError("working orders endpoint unavailable")
        return set(self.working_ids)

    async def get_positions(self) -> list[dict[str, Any]]:
        if self.positions_error is not None:
            raise self.positions_error
        return list(self.positions)


class NoBarsAdapter(FakeAdapter):
    async def get_market_data(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        raise RuntimeError("feed down")


def _deployment(
    *,
    status: str = "ACTIVE",
    source: str = "internal",
    source_type: str = "internal",
    symbols: list[str] | None = None,
    timeframe: str = "1m",
    settings: dict[str, Any] | None = None,
    signal_config: dict[str, Any] | None = None,
    opted_in: bool = True,
) -> LiveDeploymentConfig:
    merged_settings = {"internal_runner_enabled": opted_in, **(settings or {})}
    return LiveDeploymentConfig(
        id=uuid.uuid4(),
        strategy_name="fake_strategy",
        account_id="acct-1",
        symbols=symbols if symbols is not None else ["AAPL"],
        timeframe=timeframe,
        status=status,  # type: ignore[arg-type]
        signal_source_type=source_type,  # type: ignore[arg-type]
        signal_source=source,  # type: ignore[arg-type]
        settings=merged_settings,
        signal_config=signal_config or {},
    )


def _strategy_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake_strategy.py"
    path.write_text(body)
    return path


def _target_for(path: Path) -> Any:
    return SimpleNamespace(path=path, root=path.parent, parameters={})


def _module_with(fn: Any) -> Any:
    return SimpleNamespace(generate_signals=fn)


def _runner(
    submit: Any,
    *,
    path: Path,
    module: Any,
    **kwargs: Any,
) -> InternalStrategyRunner:
    return InternalStrategyRunner(
        submit_signal=submit,
        load_target=lambda name: _target_for(path),
        load_module=lambda p, strategies_dir=None: module,
        **kwargs,
    )


def _collect() -> tuple[list[TradeSignal], Any]:
    sent: list[TradeSignal] = []

    async def submit(adapter: Any, deployment: LiveDeploymentConfig, signal: TradeSignal) -> str:
        sent.append(signal)
        return "submitted"

    return sent, submit


def _entry_strategy(
    df: pd.DataFrame, params: dict[str, Any]
) -> tuple[Any, Any, dict[str, Any], list]:
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[-1] = True
    indicators = {
        "sl_price": pd.Series(95.0, index=df.index),
        "tp_price": pd.Series(110.0, index=df.index),
        "direction": pd.Series("LONG", index=df.index),
    }
    return entries, exits, indicators, []


META = 'STRATEGY_META = {"parameters": {"fast": 2}}\n'


def test_entry_on_newly_closed_bar_is_submitted(tmp_path: Path) -> None:
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    deployment = _deployment()
    # The bar opening at BASE closes at BASE+60s; evaluate 10s into the next bar.
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([deployment], {"acct-1": FakeAdapter()}, now=now))

    assert len(sent) == 1
    signal = sent[0]
    assert signal.action == "buy"
    assert signal.symbol == "AAPL"
    assert signal.stop_loss == 95.0
    assert signal.take_profit == 110.0
    assert signal.metadata["source"] == "internal"
    assert signal.metadata["internal_direction"] == "LONG"
    assert signal.metadata["signal_timestamp"] == (BASE + timedelta(seconds=60)).isoformat()
    assert signal.metadata["idempotency_key"]


def test_same_bar_is_not_resubmitted_within_a_process(tmp_path: Path) -> None:
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    deployment = _deployment()
    adapter = FakeAdapter()
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([deployment], {"acct-1": adapter}, now=now))
    asyncio.run(runner.run_once([deployment], {"acct-1": adapter}, now=now))

    assert len(sent) == 1


def test_restart_reemits_identical_idempotency_key(tmp_path: Path) -> None:
    """A fresh runner (restart) re-evaluates the same closed bar and produces
    the same producer-stable key, so the adapter's save_live_signal dedupe
    returns the existing row instead of a second broker order."""
    path = _strategy_file(tmp_path, META)
    sent_a, submit_a = _collect()
    sent_b, submit_b = _collect()
    deployment = _deployment()
    now = BASE + timedelta(seconds=70)

    asyncio.run(
        _runner(submit_a, path=path, module=_module_with(_entry_strategy)).run_once(
            [deployment], {"acct-1": FakeAdapter()}, now=now
        )
    )
    asyncio.run(
        _runner(submit_b, path=path, module=_module_with(_entry_strategy)).run_once(
            [deployment], {"acct-1": FakeAdapter()}, now=now
        )
    )

    assert len(sent_a) == len(sent_b) == 1
    assert sent_a[0].metadata["idempotency_key"] == sent_b[0].metadata["idempotency_key"]


def test_signal_exit_becomes_reduce_only_exit_intent(tmp_path: Path) -> None:
    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        exits.iloc[-1] = True
        return entries, exits, {}, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([_deployment()], {"acct-1": FakeAdapter()}, now=now))

    assert len(sent) == 1
    signal = sent[0]
    assert signal.metadata["signal_intent"] == "exit"
    assert signal.metadata["exit_fraction"] == 1.0
    assert signal.metadata["exit_only"] is True
    assert "internal_direction" not in signal.metadata
    assert len(signal.metadata["idempotency_key"]) == 32


def test_stale_bar_is_never_submitted(tmp_path: Path) -> None:
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    # Bar closed at BASE+60s; evaluating 200s later is past the 60s+30s grace.
    now = BASE + timedelta(seconds=260)

    asyncio.run(runner.run_once([_deployment()], {"acct-1": FakeAdapter()}, now=now))

    assert sent == []


def test_forming_bar_is_not_evaluated(tmp_path: Path) -> None:
    """A bar whose close is still in the future must not produce signals."""
    frame = _bars(count=30, last_open=BASE)
    forming = pd.DataFrame(
        {
            "timestamp": [(BASE + timedelta(seconds=60)).isoformat()],
            "open": [110.0],
            "high": [111.0],
            "low": [109.0],
            "close": [110.5],
            "volume": [500.0],
        }
    )
    feed = pd.concat([frame, forming], ignore_index=True)
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    now = BASE + timedelta(seconds=70)  # forming bar closes at BASE+120s

    asyncio.run(runner.run_once([_deployment()], {"acct-1": FakeAdapter(feed)}, now=now))

    assert len(sent) == 1
    # The signal is for the BASE bar (close BASE+60s), not the forming one.
    assert sent[0].entry_price == pytest.approx(100.5 + 29 * 0.1)
    assert sent[0].metadata["signal_timestamp"] == (BASE + timedelta(seconds=60)).isoformat()


def test_no_closed_bars_means_no_evaluation(tmp_path: Path) -> None:
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    # Every bar in the feed is still forming.
    future = _bars(count=5, last_open=BASE + timedelta(hours=1))
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([_deployment()], {"acct-1": FakeAdapter(future)}, now=now))

    assert sent == []


def test_adapter_without_market_data_fails_closed(tmp_path: Path) -> None:
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    adapter = FakeAdapter(serve_bars=False)

    asyncio.run(
        runner.run_once(
            [_deployment()], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert sent == []
    assert adapter.calls == []


def test_market_data_failure_is_contained(tmp_path: Path) -> None:
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))

    asyncio.run(
        runner.run_once(
            [_deployment()], {"acct-1": NoBarsAdapter()}, now=BASE + timedelta(seconds=70)
        )
    )

    assert sent == []


def test_inactive_and_discord_deployments_are_skipped(tmp_path: Path) -> None:
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    now = BASE + timedelta(seconds=70)
    deployments = [
        _deployment(status="PAUSED"),
        _deployment(status="STOPPED"),
        _deployment(source="discord", source_type="discord"),
    ]

    asyncio.run(runner.run_once(deployments, {"acct-1": FakeAdapter()}, now=now))

    assert sent == []


def test_multi_take_profit_entry_fails_closed(tmp_path: Path) -> None:
    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[-1] = True
        indicators = {
            "sl_price": pd.Series(95.0, index=df.index),
            "tp1": pd.Series(105.0, index=df.index),
            "tp2": pd.Series(115.0, index=df.index),
            "direction": pd.Series("LONG", index=df.index),
        }
        return entries, exits, indicators, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([_deployment()], {"acct-1": FakeAdapter()}, now=now))

    assert sent == []


def test_short_entry_is_emitted_for_the_daemon_gate(tmp_path: Path) -> None:
    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[-1] = True
        indicators = {
            "sl_price": pd.Series(105.0, index=df.index),
            "tp_price": pd.Series(90.0, index=df.index),
            "direction": pd.Series("SHORT", index=df.index),
        }
        return entries, exits, indicators, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([_deployment()], {"acct-1": FakeAdapter()}, now=now))

    assert len(sent) == 1
    assert sent[0].action == "sell"
    assert sent[0].metadata["internal_direction"] == "SHORT"


def test_second_closed_bar_is_processed_after_first(tmp_path: Path) -> None:
    """Catch-up within the freshness window: a second bar closing while the
    runner is alive produces its own signal on the next pass."""
    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[-1] = True  # signal on whatever the newest closed bar is
        indicators = {
            "sl_price": pd.Series(95.0, index=df.index),
            "tp_price": pd.Series(110.0, index=df.index),
            "direction": pd.Series("LONG", index=df.index),
        }
        return entries, exits, indicators, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))
    deployment = _deployment()

    # First pass: bar opening BASE is newest closed.
    asyncio.run(
        runner.run_once([deployment], {"acct-1": FakeAdapter()}, now=BASE + timedelta(seconds=70))
    )
    # Second pass: feed now includes the next bar (open BASE+60, close BASE+120).
    later_feed = _bars(count=31, last_open=BASE + timedelta(seconds=60))
    asyncio.run(
        runner.run_once(
            [deployment],
            {"acct-1": FakeAdapter(later_feed)},
            now=BASE + timedelta(seconds=130),
        )
    )

    assert len(sent) == 2
    assert sent[0].metadata["signal_timestamp"] != sent[1].metadata["signal_timestamp"]
    assert sent[0].metadata["idempotency_key"] != sent[1].metadata["idempotency_key"]


# ── daemon submit callback: chokepoint routing + short gate ──────────────


def _daemon_signal(
    *,
    action: str = "buy",
    metadata: dict[str, Any] | None = None,
) -> TradeSignal:
    return TradeSignal(
        action=action,  # type: ignore[arg-type]
        symbol="AAPL",
        entry_price=100.5,
        stop_loss=95.0,
        take_profit=110.0,
        timeframe="1m",
        metadata={
            "source": "internal",
            "signal_timestamp": (BASE + timedelta(seconds=60)).isoformat(),
            "idempotency_key": "a" * 32,
            **(metadata or {}),
        },
    )


def _patched_prepare(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    import live_runner.daemon as daemon

    calls: list[dict[str, Any]] = []

    async def fake_prepare(
        account_id: str,
        adapter: Any,
        deployment: LiveDeploymentConfig,
        normalized_signal: TradeSignal,
        live_signal: Any,
        *,
        is_exit: bool,
    ) -> None:
        calls.append(
            {
                "is_exit": is_exit,
                "action": live_signal.action,
                "idempotency_key": live_signal.metadata.get("idempotency_key"),
                "source": live_signal.metadata.get("source"),
            }
        )

    monkeypatch.setattr(daemon, "_prepare_claim_and_submit_signal", fake_prepare)
    return calls


def test_daemon_submit_routes_entry_through_claim_and_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import live_runner.daemon as daemon

    calls = _patched_prepare(monkeypatch)
    deployment = _deployment()
    signal = _daemon_signal(metadata={"internal_direction": "LONG"})

    asyncio.run(daemon._submit_internal_strategy_signal(FakeAdapter(), deployment, signal))

    assert calls == [
        {"is_exit": False, "action": "buy", "idempotency_key": "a" * 32, "source": "internal"}
    ]


def test_daemon_submit_routes_exit_with_exit_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import live_runner.daemon as daemon
    from live_runner.exit_signals import FULL_EXIT, exit_intent_metadata

    calls = _patched_prepare(monkeypatch)
    deployment = _deployment()
    metadata = {
        "internal_direction": "LONG",
        **exit_intent_metadata(FULL_EXIT),
    }
    signal = _daemon_signal(action="sell", metadata=metadata)

    asyncio.run(daemon._submit_internal_strategy_signal(FakeAdapter(), deployment, signal))

    assert len(calls) == 1
    assert calls[0]["is_exit"] is True


def test_daemon_submit_skips_short_entry_on_long_only_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import live_runner.daemon as daemon

    calls = _patched_prepare(monkeypatch)
    deployment = _deployment()  # allow_short defaults to False
    signal = _daemon_signal(action="sell", metadata={"internal_direction": "SHORT"})

    asyncio.run(daemon._submit_internal_strategy_signal(FakeAdapter(), deployment, signal))

    assert calls == []


def test_daemon_submit_allows_short_entry_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import live_runner.daemon as daemon

    calls = _patched_prepare(monkeypatch)
    deployment = _deployment(signal_config={"allow_short": True})
    signal = _daemon_signal(action="sell", metadata={"internal_direction": "SHORT"})

    asyncio.run(daemon._submit_internal_strategy_signal(FakeAdapter(), deployment, signal))

    assert len(calls) == 1
    assert calls[0]["is_exit"] is False
    assert calls[0]["action"] == "sell"


# ── review follow-ups: risk policy, freshness, caps, config refusal ──────


def _indicators(**columns: Any) -> dict[str, Any]:
    return {name: pd.Series(values, index=range(30)) for name, values in columns.items()}


def test_entry_without_take_profit_gets_unbounded_policy(tmp_path: Path) -> None:
    """No TP level: the strategy's exits signal is the exit mechanism, so the
    entry is emitted with the unbounded-TP policy (engine parity)."""

    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[-1] = True
        indicators = _indicators(
            sl_price=[95.0] * 30, direction=["LONG"] * 30
        )
        return entries, exits, indicators, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))

    asyncio.run(
        runner.run_once(
            [_deployment()], {"acct-1": FakeAdapter()}, now=BASE + timedelta(seconds=70)
        )
    )

    assert len(sent) == 1
    assert sent[0].take_profit is None
    assert sent[0].stop_loss == 95.0
    assert (
        sent[0].metadata["take_profit_policy"]
        == "unbounded_exit_on_opposite_signal"
    )


def test_entry_without_stop_loss_gets_fallback_broker_stop(tmp_path: Path) -> None:
    """Maintainer decision (a): no strategy stop -> a broker stop at the
    engine's fallback distance (fallback_stop_pct of the entry price)."""

    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[-1] = True
        indicators = _indicators(tp_price=[110.0] * 30, direction=["LONG"] * 30)
        return entries, exits, indicators, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([_deployment()], {"acct-1": FakeAdapter()}, now=now))

    assert len(sent) == 1
    signal = sent[0]
    # Last bar close = 100.5 + 29*0.1 = 103.4; fallback stop = close * 0.98.
    expected_stop = 103.4 * 0.98
    assert signal.stop_loss == pytest.approx(expected_stop)
    assert signal.take_profit == 110.0
    assert signal.metadata["internal_stop_source"] == "fallback_pct"


def test_entry_without_stop_or_tp_exits_on_strategy_signals(tmp_path: Path) -> None:
    """Maintainer decision (b): no TP is allowed — the position exits on the
    strategy's exits signal under the unbounded-TP policy, and the fallback
    stop still protects it."""

    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[-1] = True
        exits.iloc[-2] = True
        indicators = _indicators(direction=["LONG"] * 30)
        return entries, exits, indicators, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))
    frame = _bars(count=30, last_open=BASE)
    adapter = FakeAdapter(frame)
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([_deployment()], {"acct-1": adapter}, now=now))

    # Only the newest closed bar is actionable on first evaluation: an
    # entry with no TP and no strategy stop.
    assert len(sent) == 1
    signal = sent[0]
    assert signal.stop_loss == pytest.approx(103.4 * 0.98)
    assert signal.take_profit is None
    assert (
        signal.metadata["take_profit_policy"]
        == "unbounded_exit_on_opposite_signal"
    )


def test_short_entry_without_stop_gets_fallback_stop_above_entry(
    tmp_path: Path,
) -> None:
    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[-1] = True
        indicators = _indicators(direction=["SHORT"] * 30)
        return entries, exits, indicators, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))
    deployment = _deployment(signal_config={"allow_short": True})
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([deployment], {"acct-1": FakeAdapter()}, now=now))

    assert len(sent) == 1
    assert sent[0].action == "sell"
    assert sent[0].stop_loss == pytest.approx(103.4 * 1.02)


def test_partial_single_take_profit_is_refused(tmp_path: Path) -> None:
    """tp_pct < 1 on a single TP level is a partial exit the live bracket
    cannot express — refused like a multi-level ladder."""

    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[-1] = True
        indicators = _indicators(
            sl_price=[95.0] * 30,
            tp_price=[110.0] * 30,
            tp_pct=[0.5] * 30,
            direction=["LONG"] * 30,
        )
        return entries, exits, indicators, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))

    asyncio.run(
        runner.run_once(
            [_deployment()], {"acct-1": FakeAdapter()}, now=BASE + timedelta(seconds=70)
        )
    )

    assert sent == []


def test_sizing_only_stop_fields_are_refused(tmp_path: Path) -> None:
    """sizing_sl_price/stop_distance are engine sizing hints, never live
    stops — an entry carrying them cannot be sized like the engine, refused."""

    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[-1] = True
        indicators = _indicators(
            sl_price=[95.0] * 30,
            tp_price=[110.0] * 30,
            stop_distance=[5.0] * 30,
            direction=["LONG"] * 30,
        )
        return entries, exits, indicators, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))

    asyncio.run(
        runner.run_once(
            [_deployment()], {"acct-1": FakeAdapter()}, now=BASE + timedelta(seconds=70)
        )
    )

    assert sent == []


def test_daily_equity_timeframe_is_allowed_and_fills_at_next_open(
    tmp_path: Path,
) -> None:
    """Maintainer decision (d): daily+ bars on session-bound symbols are
    allowed — positions may be held over the close, and the signal becomes
    actionable at the next session open."""
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    # Daily bars stamped at midnight ET (00:00 ET = 05:00 UTC in January).
    daily_open = datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc)
    adapter = FakeAdapter(_bars(count=30, last_open=daily_open, step_seconds=86_400))
    deployment = _deployment(timeframe="1d")
    # Tuesday 09:31:10 ET — just after the session open following Monday's
    # daily bar, inside its actionable window.
    now = datetime(2026, 1, 6, 14, 31, 10, tzinfo=timezone.utc)

    asyncio.run(runner.run_once([deployment], {"acct-1": adapter}, now=now))

    assert len(sent) == 1
    assert sent[0].symbol == "AAPL"


def test_daily_equity_is_not_evaluated_while_market_is_closed(
    tmp_path: Path,
) -> None:
    """Same deployment evaluated mid-session-close gap: no fetch, no signal."""
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    daily_open = datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc)
    adapter = FakeAdapter(_bars(count=30, last_open=daily_open, step_seconds=86_400))
    deployment = _deployment(timeframe="1d")
    # Monday 21:30 ET = Tuesday 02:30 UTC — venue closed.
    now = datetime(2026, 1, 6, 2, 30, tzinfo=timezone.utc)

    asyncio.run(runner.run_once([deployment], {"acct-1": adapter}, now=now))

    assert sent == []
    assert adapter.calls == []


def test_intraday_bar_outside_session_produces_no_signal(tmp_path: Path) -> None:
    """A 1h equity bar whose open is outside the regular session is not a
    trading bar — the venue calendar excludes it."""
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    # Hourly bars stamped 21:00-01:00 UTC = 16:00-20:00 ET: every open is at
    # or after the 16:00 ET close — outside the regular session.
    evening = datetime(2026, 1, 6, 1, 0, tzinfo=timezone.utc)
    adapter = FakeAdapter(_bars(count=5, last_open=evening, step_seconds=3600))
    deployment = _deployment(timeframe="1h")
    # Tuesday 10:00 ET = 15:00 UTC, market open.
    now = datetime(2026, 1, 6, 15, 0, tzinfo=timezone.utc)

    asyncio.run(runner.run_once([deployment], {"acct-1": adapter}, now=now))

    assert sent == []


def test_deployment_without_opt_in_is_inert(tmp_path: Path) -> None:
    """Maintainer decision (e): ACTIVE internal deployments need the explicit
    per-deployment opt-in flag; without it nothing is fetched or submitted."""
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    adapter = FakeAdapter()
    deployment = _deployment(opted_in=False)
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([deployment], {"acct-1": adapter}, now=now))

    assert sent == []
    assert adapter.calls == []
    assert (str(deployment.id), "opt_in") in runner._reported


def test_daily_crypto_timeframe_is_allowed(tmp_path: Path) -> None:
    """Crypto pairs trade 24/7, so the session-bound refusal does not apply."""
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    adapter = FakeAdapter(_bars(count=30, last_open=BASE, step_seconds=86_400))
    deployment = _deployment(timeframe="1d", symbols=["BTC/USD"])

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=86_410)
        )
    )

    assert len(sent) == 1
    assert sent[0].symbol == "BTC/USD"


def test_staleness_window_is_grace_plus_feed_lag_not_a_full_bar(tmp_path: Path) -> None:
    """A 4h signal 200s after close was inside the old bar+grace window but is
    now stale: the actionable window is grace + min(120s, 10% of bar)."""
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    adapter = FakeAdapter(_bars(count=30, last_open=BASE, step_seconds=14_400))
    deployment = _deployment(timeframe="4h")
    # Bar opened at BASE closes at BASE+4h; evaluated 200s later.
    now = BASE + timedelta(seconds=14_400 + 200)

    asyncio.run(runner.run_once([deployment], {"acct-1": adapter}, now=now))

    assert sent == []


def test_no_market_data_refetch_until_next_bar_closes(tmp_path: Path) -> None:
    """After processing a bar the runner sleeps until the *next* bar closes —
    it must not re-fetch 1000 bars every 15s tick."""
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    adapter = FakeAdapter()
    deployment = _deployment()
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([deployment], {"acct-1": adapter}, now=now))
    asyncio.run(runner.run_once([deployment], {"acct-1": adapter}, now=now))

    assert len(sent) == 1
    assert len(adapter.calls) == 1


def test_open_position_blocks_pyramiding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import live_runner.internal_strategy_runner as runner_mod

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_open_entry_counts_by_symbol",
        lambda deployment_id, working_order_ids=None, broker_held_symbols=None: {"AAPL": 1},
    )

    asyncio.run(
        runner.run_once(
            [_deployment()], {"acct-1": FakeAdapter()}, now=BASE + timedelta(seconds=70)
        )
    )

    assert sent == []


def test_max_positions_cap_uses_deployment_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import live_runner.internal_strategy_runner as runner_mod

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_open_entry_counts_by_symbol",
        lambda deployment_id, working_order_ids=None, broker_held_symbols=None: {
            "AAA": 1, "BBB": 1, "CCC": 1, "DDD": 1, "EEE": 1,
        },
    )

    asyncio.run(
        runner.run_once(
            [_deployment(symbols=["NVDA"])],
            {"acct-1": FakeAdapter()},
            now=BASE + timedelta(seconds=70),
        )
    )

    assert sent == []


def test_max_open_per_symbol_above_one_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Maintainer decision (c), revised 2026-11-22: the per-symbol cap is
    configurable, but N>1 is not supported end-to-end yet — the deployment
    is refused with a clear error rather than silently faking it."""
    import live_runner.internal_strategy_runner as runner_mod

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_open_entry_counts_by_symbol",
        lambda deployment_id, working_order_ids=None, broker_held_symbols=None: {"AAPL": 1},
    )
    adapter = FakeAdapter()
    deployment = _deployment(settings={"internal_max_open_per_symbol": 2})

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert sent == []
    # Entries are disabled, but the deployment is still evaluated so exits
    # keep flowing — the market-data fetch proves evaluation ran.
    assert adapter.calls != []
    assert (str(deployment.id), "open_position_cap") in runner._reported


def test_max_open_per_symbol_above_engine_maximum_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any value other than 1 fails closed — including values that would
    clamp under the engine maximum."""
    import live_runner.internal_strategy_runner as runner_mod

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_open_entry_counts_by_symbol",
        lambda deployment_id, working_order_ids=None, broker_held_symbols=None: {"AAPL": 5},
    )
    adapter = FakeAdapter()
    deployment = _deployment(settings={"internal_max_open_per_symbol": 99})

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert sent == []
    assert adapter.calls != []
    assert (str(deployment.id), "open_position_cap") in runner._reported


def test_max_open_per_symbol_invalid_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    deployment = _deployment(settings={"internal_max_open_per_symbol": "lots"})
    adapter = FakeAdapter()

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert sent == []
    assert adapter.calls != []
    assert (str(deployment.id), "open_position_cap") in runner._reported


def test_engine_position_cap_counts_same_pass_submits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundle-39 blocker 4: open_entries is updated after each submit, so a
    pass cannot exceed TRADE_ENGINE_MAX_POSITIONS within itself."""
    import live_runner.internal_strategy_runner as runner_mod

    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        # Signal on every symbol: the runner evaluates per symbol, and the
        # strategy sees the same frame shape for each.
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[-1] = True
        indicators = _indicators(
            sl_price=[95.0] * len(df),
            tp_price=[110.0] * len(df),
            direction=["LONG"] * len(df),
        )
        return entries, exits, indicators, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))
    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_open_entry_counts_by_symbol",
        lambda deployment_id, working_order_ids=None, broker_held_symbols=None: {
            "H1": 1, "H2": 1, "H3": 1, "H4": 1,
        },
    )
    symbols = ["N1", "N2", "N3"]
    deployment = _deployment(symbols=symbols)

    asyncio.run(
        runner.run_once(
            [deployment],
            {"acct-1": FakeAdapter()},
            now=BASE + timedelta(seconds=70),
        )
    )

    # 4 open + engine cap 5 -> exactly one of the three new entries submits.
    assert len(sent) == 1


def test_strategy_parameters_are_refused_at_load(tmp_path: Path) -> None:
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    deployment = _deployment(settings={"strategy_parameters": {"fast": 9}})
    adapter = FakeAdapter()

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert sent == []
    assert adapter.calls == []
    assert (str(deployment.id), "deployment_config") in runner._reported


def test_signal_processor_config_is_refused(tmp_path: Path) -> None:
    """Processors are a Discord-pipeline concept; the internal path never runs
    one, so a deployment carrying processor config fails closed."""
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    deployment = _deployment(signal_config={"signal_processor": "rsi_reversal"})

    asyncio.run(
        runner.run_once([deployment], {"acct-1": FakeAdapter()}, now=BASE + timedelta(seconds=70))
    )

    assert sent == []


def test_nan_close_row_is_skipped(tmp_path: Path) -> None:
    """A NaN close on the signal bar must not become an order price."""
    frame = _bars(count=30, last_open=BASE)
    frame.loc[frame.index[-1], "close"] = float("nan")
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))

    asyncio.run(
        runner.run_once(
            [_deployment()], {"acct-1": FakeAdapter(frame)}, now=BASE + timedelta(seconds=70)
        )
    )

    assert sent == []


def test_consecutive_submit_failures_escalate_once(tmp_path: Path) -> None:
    """Three consecutive submission failures produce one escalation marker;
    the counter resets on success."""
    path = _strategy_file(tmp_path, META)
    deployment = _deployment()

    async def failing_submit(adapter: Any, dep: LiveDeploymentConfig, signal: TradeSignal) -> None:
        raise RuntimeError("broker down")

    runner = _runner(failing_submit, path=path, module=_module_with(_entry_strategy))
    for offset in (70, 130, 190):
        feed = _bars(count=30 + offset // 60, last_open=BASE + timedelta(seconds=offset - 10))
        asyncio.run(
            runner.run_once(
                [deployment], {"acct-1": FakeAdapter(feed)}, now=BASE + timedelta(seconds=offset)
            )
        )

    assert runner._submit_failures[(str(deployment.id), "AAPL")] == 3
    assert (str(deployment.id), "submit_failures:AAPL") in runner._reported


# ── Bundle 47: session overlap, entries-disabled exits, outcome escalation ──


def test_first_hour_bar_overlapping_session_open_is_evaluated(
    tmp_path: Path,
) -> None:
    """A 1h bar stamped 09:00 ET opens before the 09:30 NYSE open but its
    09:30-10:00 portion is regular-hours data. Session *overlap*, not bar
    open inside the session, decides actionability — the bar must trade."""
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    # Hourly bars; the newest opens 09:00 ET = 14:00 UTC and closes 10:00 ET.
    last_open = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
    adapter = FakeAdapter(
        _bars(count=30, last_open=last_open, step_seconds=3600)
    )
    deployment = _deployment(timeframe="1h")
    # 10:01:30 ET — just after the bar's 10:00 ET close, inside the window.
    now = datetime(2026, 1, 5, 15, 1, 30, tzinfo=timezone.utc)

    asyncio.run(runner.run_once([deployment], {"acct-1": adapter}, now=now))

    assert len(sent) == 1
    assert sent[0].symbol == "AAPL"


def test_session_bound_frame_keeps_extended_hours_bars(tmp_path: Path) -> None:
    """Session logic gates actionability, not indicator input: the strategy
    sees every nominally closed feed bar — including pre-market bars the
    venue calendar would never open a position on — matching the unfiltered
    backtest dataset. A still-forming bar stays excluded."""
    seen: dict[str, pd.DataFrame] = {}

    def recording_strategy(df: pd.DataFrame, params: dict[str, Any]):
        seen["df"] = df.copy()
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        return entries, exits, {}, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(recording_strategy))
    # Hourly bars; the feed's newest bar opens 10:00 ET and is still forming.
    last_open = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
    adapter = FakeAdapter(
        _bars(count=31, last_open=last_open, step_seconds=3600)
    )
    deployment = _deployment(timeframe="1h")
    # 10:30 ET: the 09:00-10:00 ET bar is the newest closed one.
    now = datetime(2026, 1, 5, 15, 30, tzinfo=timezone.utc)

    asyncio.run(runner.run_once([deployment], {"acct-1": adapter}, now=now))

    frame = seen["df"]
    stamps = [pd.Timestamp(ts) for ts in frame["timestamp"]]
    # The still-forming 10:00 ET bar is not in the strategy's input.
    assert stamps[-1] == pd.Timestamp("2026-01-05T14:00:00+00:00")
    # Pre-market bars (e.g. 04:00 ET = 09:00 UTC) are preserved for
    # indicator parity with the backtest dataset.
    assert pd.Timestamp("2026-01-05T09:00:00+00:00") in stamps
    assert sent == []


def _exit_and_entry_strategy(df: pd.DataFrame, params: dict[str, Any]):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[-1] = True
    exits.iloc[-1] = True
    indicators = _indicators(
        sl_price=[95.0] * len(df),
        tp_price=[110.0] * len(df),
        direction=["LONG"] * len(df),
    )
    return entries, exits, indicators, []


def test_working_unfilled_entry_counts_against_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unfilled entry still working at the broker counts toward the cap;
    the adapter's working-order ids are what the ledger filters on."""
    import live_runner.internal_strategy_runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_unfilled_entry_order_refs",
        lambda deployment_id: {"bo-1"},
    )
    seen_working: list[set[str]] = []

    def fake_counts(deployment_id, working_order_ids=None, broker_held_symbols=None):
        seen_working.append(set(working_order_ids or ()))
        return {"AAPL": 1}

    monkeypatch.setattr(
        runner_mod, "list_live_deployment_open_entry_counts_by_symbol", fake_counts
    )
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    adapter = FakeAdapter()
    adapter.working_ids = {"bo-1"}
    deployment = _deployment()

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert seen_working == [{"bo-1"}]
    assert sent == []


def test_dead_unfilled_entry_does_not_block_reentry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canceled/expired unfilled order is absent from the broker's working
    list, so the ledger counts nothing and the next entry submits."""
    import live_runner.internal_strategy_runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_unfilled_entry_order_refs",
        lambda deployment_id: {"bo-dead"},
    )
    seen_working: list[set[str]] = []

    def fake_counts(deployment_id, working_order_ids=None, broker_held_symbols=None):
        seen_working.append(set(working_order_ids or ()))
        return {}

    monkeypatch.setattr(
        runner_mod, "list_live_deployment_open_entry_counts_by_symbol", fake_counts
    )
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    adapter = FakeAdapter()
    adapter.working_ids = set()
    deployment = _deployment()

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert seen_working == [set()]
    assert len(sent) == 1


def test_filled_but_unbooked_entry_counts_via_broker_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundle-50 blocker: an entry that is no longer working but whose fill
    is not yet booked must still count against the cap — the broker's
    non-zero position is the proof. Without it the N=1 cap would be
    exceeded on the next bar."""
    import live_runner.internal_strategy_runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_unfilled_entry_order_refs",
        lambda deployment_id: {"bo-filled"},
    )
    seen_held: list[set[str] | None] = []

    def fake_counts(deployment_id, working_order_ids=None, broker_held_symbols=None):
        seen_held.append(
            set(broker_held_symbols) if broker_held_symbols is not None else None
        )
        return {"AAPL": 1}

    monkeypatch.setattr(
        runner_mod, "list_live_deployment_open_entry_counts_by_symbol", fake_counts
    )
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    adapter = FakeAdapter()
    adapter.working_ids = set()  # filled orders are not working
    adapter.positions = [{"symbol": "AAPL", "qty": 5.0}]
    deployment = _deployment()

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert seen_held == [{"AAPL"}]
    assert sent == []


def test_broker_held_symbols_keeps_the_brokers_crypto_spelling(
    tmp_path: Path,
) -> None:
    """Bundle-51 blocker: Alpaca reports crypto positions as ``BTCUSD``; the
    runner hands that spelling to the ledger unchanged — the canonical
    ``crypto_pair_key`` match lives in
    ``list_live_deployment_open_entry_counts_by_symbol`` (pinned in
    test_open_entry_counts.py), so ``BTC/USD`` entries still match."""
    path = _strategy_file(tmp_path, META)
    _sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    adapter = FakeAdapter()
    adapter.positions = [{"symbol": "BTCUSD", "qty": 0.5}]
    deployment = _deployment()

    held = asyncio.run(runner._broker_held_symbols(adapter, deployment))

    assert held == {"BTCUSD"}


def test_non_dict_position_row_disables_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Should-fix: a position row that is not a dict is unreadable, not flat —
    entries fail closed, exits still flow."""
    import live_runner.internal_strategy_runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_unfilled_entry_order_refs",
        lambda deployment_id: {"bo-1"},
    )
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_exit_and_entry_strategy))
    adapter = FakeAdapter()
    adapter.working_ids = set()
    adapter.positions = [{"symbol": "AAPL", "qty": 1.0}, "not-a-dict"]
    deployment = _deployment()

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert len(sent) == 1
    assert sent[0].metadata.get("exit_only") is True
    assert (str(deployment.id), "broker_positions") in runner._reported


def test_none_positions_disable_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Should-fix: ``get_positions`` returning ``None`` is "unproven", not
    "flat" — entries fail closed."""
    import live_runner.internal_strategy_runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_unfilled_entry_order_refs",
        lambda deployment_id: {"bo-1"},
    )
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_exit_and_entry_strategy))
    adapter = FakeAdapter()
    adapter.working_ids = set()

    async def _none_positions():
        return None

    adapter.get_positions = _none_positions
    deployment = _deployment()

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert len(sent) == 1
    assert sent[0].metadata.get("exit_only") is True
    assert (str(deployment.id), "broker_positions") in runner._reported


def test_unreadable_positions_disable_entries_but_exits_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the broker's position list cannot be read, a not-working unfilled
    entry cannot be told from a filled one: entries fail closed, exits still
    go out."""
    import live_runner.internal_strategy_runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_unfilled_entry_order_refs",
        lambda deployment_id: {"bo-1"},
    )
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_exit_and_entry_strategy))
    adapter = FakeAdapter()
    adapter.working_ids = set()
    adapter.positions_error = RuntimeError("positions endpoint down")
    deployment = _deployment()

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert len(sent) == 1
    assert sent[0].metadata.get("exit_only") is True
    assert (str(deployment.id), "broker_positions") in runner._reported


def test_unreadable_unfilled_refs_still_evaluates_exits_and_later_deployments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundle-50 blocker: the unfilled-entry refs read is a ledger read like
    the counts read — when it raises for one deployment, that deployment's
    exits still submit AND the next deployment in the snapshot is still
    evaluated (previously the exception left run_once entirely)."""
    import live_runner.internal_strategy_runner as runner_mod

    first, second = _deployment(), _deployment()

    def refs(deployment_id):
        if deployment_id == str(first.id):
            raise RuntimeError("ledger locked")
        return set()

    monkeypatch.setattr(
        runner_mod, "list_live_deployment_unfilled_entry_order_refs", refs
    )
    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_open_entry_counts_by_symbol",
        lambda deployment_id, working_order_ids=None, broker_held_symbols=None: {},
    )
    path = _strategy_file(tmp_path, META)
    sent: list[tuple[str, TradeSignal]] = []

    async def submit(adapter, deployment, signal):
        sent.append((str(deployment.id), signal))
        return "submitted"

    runner = _runner(submit, path=path, module=_module_with(_exit_and_entry_strategy))
    adapter = FakeAdapter()

    asyncio.run(
        runner.run_once(
            [first, second], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    first_signals = [signal for dep_id, signal in sent if dep_id == str(first.id)]
    second_signals = [signal for dep_id, signal in sent if dep_id == str(second.id)]
    # First deployment: entries disabled, but its exit still went out.
    assert len(first_signals) == 1
    assert first_signals[0].metadata.get("exit_only") is True
    assert (str(first.id), "ledger") in runner._reported
    # Second deployment: evaluated normally — exit AND entry.
    assert len(second_signals) == 2
    assert any(not s.metadata.get("exit_only") for s in second_signals)


def test_unreadable_working_orders_disable_entries_but_exits_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the broker's working-order list cannot be read, unfilled entries
    cannot be told from dead ones: entries fail closed, exits still go out."""
    import live_runner.internal_strategy_runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_unfilled_entry_order_refs",
        lambda deployment_id: {"bo-1"},
    )
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_exit_and_entry_strategy))
    adapter = FakeAdapter()  # working_ids=None -> working_order_ids raises
    deployment = _deployment()

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=70)
        )
    )

    assert len(sent) == 1
    assert sent[0].metadata.get("exit_only") is True
    assert (str(deployment.id), "working_orders") in runner._reported


def test_unreadable_ledger_disables_entries_but_exits_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable entry-count ledger must not suppress exits: entries are
    disabled for the pass, the exit intent still submits."""
    import live_runner.internal_strategy_runner as runner_mod

    def _boom(deployment_id, working_order_ids=None, broker_held_symbols=None):
        raise RuntimeError("ledger locked")

    monkeypatch.setattr(
        runner_mod, "list_live_deployment_open_entry_counts_by_symbol", _boom
    )
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_exit_and_entry_strategy))
    deployment = _deployment()

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": FakeAdapter()}, now=BASE + timedelta(seconds=70)
        )
    )

    assert len(sent) == 1
    assert sent[0].metadata.get("exit_only") is True
    assert (str(deployment.id), "ledger") in runner._reported


def test_cap_refusal_still_allows_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap the runner cannot honor disables entries only — an exit signal
    on the same bar still reaches the submit chokepoint."""
    import live_runner.internal_strategy_runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "list_live_deployment_open_entry_counts_by_symbol",
        lambda deployment_id, working_order_ids=None, broker_held_symbols=None: {},
    )
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_exit_and_entry_strategy))
    deployment = _deployment(settings={"internal_max_open_per_symbol": 2})

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": FakeAdapter()}, now=BASE + timedelta(seconds=70)
        )
    )

    assert len(sent) == 1
    assert sent[0].metadata.get("exit_only") is True
    assert (str(deployment.id), "open_position_cap") in runner._reported


def test_refused_submit_outcome_counts_toward_escalation(tmp_path: Path) -> None:
    """A normal refusal is not an exception, but the signal still never
    reached the broker — three in a row must escalate like failures do."""
    path = _strategy_file(tmp_path, META)
    deployment = _deployment()

    async def refusing_submit(
        adapter: Any, dep: LiveDeploymentConfig, signal: TradeSignal
    ) -> str:
        return "refused"

    runner = _runner(refusing_submit, path=path, module=_module_with(_entry_strategy))
    for offset in (70, 130, 190):
        feed = _bars(count=30 + offset // 60, last_open=BASE + timedelta(seconds=offset - 10))
        asyncio.run(
            runner.run_once(
                [deployment], {"acct-1": FakeAdapter(feed)}, now=BASE + timedelta(seconds=offset)
            )
        )

    assert runner._submit_failures[(str(deployment.id), "AAPL")] == 3
    assert (str(deployment.id), "submit_failures:AAPL") in runner._reported


def test_same_bar_entry_and_exit_have_distinct_idempotency_keys(
    tmp_path: Path,
) -> None:
    """An exit and an entry emitted on the same bar must never collide on
    the producer-stable idempotency key — the ``kind`` component differs."""
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_exit_and_entry_strategy))
    deployment = _deployment()

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": FakeAdapter()}, now=BASE + timedelta(seconds=70)
        )
    )

    assert len(sent) == 2
    keys = {signal.metadata["idempotency_key"] for signal in sent}
    assert len(keys) == 2


def test_run_daemon_creates_and_cancels_internal_strategy_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run_daemon`` must own the internal strategy loop like every other
    long-running task: created with the deployment snapshot and cancelled
    (awaited) on shutdown."""
    import live_runner.daemon as daemon

    events: list[str] = []

    class _Scheduler:
        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(0.01)
        raise RuntimeError("sync boom")

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _internal_loop(*_args: Any, **_kwargs: Any) -> None:
        events.append("started")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("cancelled")
            raise

    monkeypatch.setattr(
        daemon, "_discover_and_register_discord_services", lambda *_a, **_k: []
    )
    monkeypatch.setattr(daemon, "_recover_failed_services", lambda: [])
    monkeypatch.setattr(daemon, "ServiceScheduler", lambda: _Scheduler())
    monkeypatch.setattr(daemon, "LiveTrailingStopManager", lambda: object())
    monkeypatch.setattr(daemon, "sync_deployments_loop", _boom)
    monkeypatch.setattr(daemon, "sync_services_loop", _noop)
    monkeypatch.setattr(daemon, "_poll_balances", _noop)
    monkeypatch.setattr(daemon, "run_trailing_stops_loop", _noop)
    monkeypatch.setattr(daemon, "_backfill_commissions_loop", _noop)
    monkeypatch.setattr(daemon, "run_broker_reconciliation_loop", _noop)
    monkeypatch.setattr(daemon, "run_internal_strategy_loop", _internal_loop)

    with pytest.raises(RuntimeError, match="sync boom"):
        asyncio.run(daemon.run_daemon(accounts_env=tmp_path / "accounts.env"))

    assert events == ["started", "cancelled"]
