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

BASE = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)  # Monday


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

    async def get_market_data(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        self.calls.append((symbol, timeframe, limit))
        return self.frame.tail(limit).reset_index(drop=True)

    def supports_market_data_timeframe(self, timeframe: str) -> bool:
        return self.serve_bars


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
) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=uuid.uuid4(),
        strategy_name="fake_strategy",
        account_id="acct-1",
        symbols=symbols if symbols is not None else ["AAPL"],
        timeframe=timeframe,
        status=status,  # type: ignore[arg-type]
        signal_source_type=source_type,  # type: ignore[arg-type]
        signal_source=source,  # type: ignore[arg-type]
        settings=settings or {},
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

    async def submit(adapter: Any, deployment: LiveDeploymentConfig, signal: TradeSignal) -> None:
        sent.append(signal)

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


def test_entry_without_stop_loss_is_refused(tmp_path: Path) -> None:
    """No SL: the engine only uses a fallback stop for sizing; until the
    maintainer decides whether live may run broker-stopless, the runner
    fails closed with one ERROR per deployment."""

    def strategy(df: pd.DataFrame, params: dict[str, Any]):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[-1] = True
        indicators = _indicators(tp_price=[110.0] * 30, direction=["LONG"] * 30)
        return entries, exits, indicators, []

    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(strategy))
    deployment = _deployment()
    now = BASE + timedelta(seconds=70)

    asyncio.run(runner.run_once([deployment], {"acct-1": FakeAdapter()}, now=now))
    asyncio.run(runner.run_once([deployment], {"acct-1": FakeAdapter()}, now=now))

    assert sent == []
    assert (str(deployment.id), "no_stop") in runner._reported


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


def test_daily_equity_timeframe_is_refused(tmp_path: Path) -> None:
    """Session-crossing daily bars on equities close while the quote feed is
    shut — the deployment is refused before any fetch."""
    path = _strategy_file(tmp_path, META)
    sent, submit = _collect()
    runner = _runner(submit, path=path, module=_module_with(_entry_strategy))
    adapter = FakeAdapter(_bars(count=30, last_open=BASE, step_seconds=86_400))
    deployment = _deployment(timeframe="1d")

    asyncio.run(
        runner.run_once(
            [deployment], {"acct-1": adapter}, now=BASE + timedelta(seconds=86_410)
        )
    )

    assert sent == []
    assert adapter.calls == []
    assert (str(deployment.id), "session_bars:1d") in runner._reported


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
        "list_live_deployment_net_filled_qty_by_symbol",
        lambda deployment_id: {"AAPL": 3.0},
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
        "list_live_deployment_net_filled_qty_by_symbol",
        lambda deployment_id: {
            "AAA": 1.0, "BBB": 1.0, "CCC": 1.0, "DDD": 1.0, "EEE": 1.0,
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
