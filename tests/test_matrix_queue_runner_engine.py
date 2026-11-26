"""Matrix queue runner engine default and vectorbt-only exclusions (#318).

The queue must default to the native engine and refuse — loudly, per strategy —
to queue strategies whose signal output only the frozen vectorbt adapter can
execute (``entry_stop`` stop-entry fills were dropped per the #318 decision).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(
        f"scripts_{name.replace('.py', '')}", str(path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def queue_runner(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MATRIX_ENGINE", raising=False)
    module = _load_script("matrix_queue_runner.py")
    matrix_dir = tmp_path / "matrix"
    monkeypatch.setattr(module, "MATRIX_DIR", matrix_dir)
    monkeypatch.setattr(module, "STATE_PATH", matrix_dir / "queue_state.json")
    monkeypatch.setattr(module, "LOCK_PATH", matrix_dir / "queue.lock")
    return module


def test_matrix_queue_runner_defaults_to_native_engine(queue_runner) -> None:
    assert queue_runner.ENGINE == "native"


def test_matrix_queue_runner_honors_engine_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MATRIX_ENGINE", "vectorbt")
    module = _load_script("matrix_queue_runner.py")
    monkeypatch.setattr(module, "MATRIX_DIR", tmp_path / "matrix")
    assert module.ENGINE == "vectorbt"


def test_native_engine_excludes_entry_stop_strategies(queue_runner) -> None:
    """us100_scalper emits ``entry_stop``; native cannot honour stop-entry fills."""
    excluded = queue_runner.engine_excluded_strategies()

    assert "us100_scalper" in excluded
    assert "entry_stop" in excluded["us100_scalper"]


def test_native_engine_does_not_exclude_native_compatible_strategies(queue_runner) -> None:
    """Strategies using only shared signal keys stay queued.

    ``intraday_volatility_breakout`` declares ``engine: native`` and only has an
    ``entry_stop_distance`` *variable* — it emits ``stop_distance``, which the
    native engine executes — so the AST key scan must not false-positive on it.
    """
    excluded = queue_runner.engine_excluded_strategies()

    assert "intraday_volatility_breakout" not in excluded
    assert "example_sma" not in excluded


def test_engine_exclusion_is_disabled_for_vectorbt(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MATRIX_ENGINE", "vectorbt")
    module = _load_script("matrix_queue_runner.py")
    monkeypatch.setattr(module, "MATRIX_DIR", tmp_path / "matrix")

    assert module.engine_excluded_strategies() == {}


@pytest.mark.parametrize("engine", ["native", "lean", "pynecore"])
def test_engine_exclusion_fires_for_every_non_vectorbt_engine(
    monkeypatch, tmp_path: Path, engine: str
) -> None:
    """Only vectorbt executes ``entry_stop``; lean/pynecore must not queue it either."""
    monkeypatch.setenv("MATRIX_ENGINE", engine)
    module = _load_script("matrix_queue_runner.py")
    monkeypatch.setattr(module, "MATRIX_DIR", tmp_path / "matrix")

    excluded = module.engine_excluded_strategies()

    assert "us100_scalper" in excluded
    assert engine in excluded["us100_scalper"]


def test_engine_exclusion_covers_atr_fallback_without_stop(queue_runner, tmp_path: Path) -> None:
    """A strategy emitting ``atr*`` with no stop key sizes/stops differently per engine."""
    strategies_dir = tmp_path / "strategies"
    strategy_dir = strategies_dir / "atr_fallback_strat"
    strategy_dir.mkdir(parents=True)
    (strategy_dir / "atr_fallback_strat.py").write_text(
        "def generate_signals(df, params):\n"
        "    indicators = {'atr': 1.0, 'take_profit': 2.0}\n"
        "    return None, None, indicators\n",
        encoding="utf-8",
    )

    excluded = queue_runner.engine_excluded_strategies(strategies_dir)

    assert "atr_fallback_strat" in excluded
    assert "atr" in excluded["atr_fallback_strat"]
    assert "1xATR" in excluded["atr_fallback_strat"]


def test_excluded_strategies_are_never_picked(queue_runner, monkeypatch) -> None:
    """An excluded slug gets status 'excluded' and is not returned as pending."""
    monkeypatch.setattr(
        queue_runner, "strategy_slugs", lambda: ["demo", "us100_scalper"]
    )
    monkeypatch.setattr(
        queue_runner,
        "engine_excluded_strategies",
        lambda: {"us100_scalper": "emits vectorbt-only signal key(s) ['entry_stop']"},
    )

    state = {"strategies": []}
    queue_runner.refresh_strategy_states(state)

    by_slug = {row["strategy"]: row for row in state["strategies"]}
    assert by_slug["us100_scalper"]["status"] == "excluded"
    assert "entry_stop" in by_slug["us100_scalper"]["error"]
    assert by_slug["demo"]["status"] == "pending"
    assert queue_runner.next_pending_strategy(state) == "demo"


def test_render_state_shows_excluded_reason(queue_runner, monkeypatch) -> None:
    monkeypatch.setattr(
        queue_runner, "strategy_slugs", lambda: ["us100_scalper"]
    )
    monkeypatch.setattr(
        queue_runner,
        "engine_excluded_strategies",
        lambda: {"us100_scalper": "emits vectorbt-only signal key(s) ['entry_stop']"},
    )

    state = {"strategies": []}
    queue_runner.refresh_strategy_states(state)
    rendered = queue_runner.render_state(state)

    assert "excluded `1`" in rendered
    assert "us100_scalper" in rendered
    assert "entry_stop" in rendered
