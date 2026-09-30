"""Contract tests for the 16-D2 engine_pkg/adapter split (pre-mortem hardening).

These pin the seams the split introduced:
- PM-2: every key of the `_size_trade` sizing record reaches trade meta
  `risk_math` (viewer contract).
- PM-3: trade/equity timestamps and indicator-point timestamps come from the
  same ISO-UTC formatter.
- PM-4: the `runner.engine` shim exposes the engine package's public surface.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil

import pandas as pd

import runner.engine as engine_shim
import runner.engine_pkg as engine_pkg
from runner.engines.vectorbt_adapter import _size_trade, _SizeRecord

SIZE_RECORD_DROPPED_KEYS = {
    # internal bookkeeping that is intentionally not serialized into risk_math
    "qty",
}
SIZE_RECORD_RENAMED_KEYS = {
    # serialized under a viewer-facing name (see frontend TradeRiskMath)
    "leverage_notional_limit": "max_leverage_notional",
    "cap_notional": "max_position_notional",
}


def test_size_trade_record_keys_reach_risk_math():
    """Every serialized sizing-record key must appear in trade meta risk_math."""
    record_keys = set(_SizeRecord.__annotations__) - SIZE_RECORD_DROPPED_KEYS

    import runner.engines.vectorbt_adapter as vbt

    # Read the *dict literal*, not the text.
    #
    # The previous version sliced the source between `trade_meta["risk_math"]` and
    # `capped_by_max_leverage` and asserted the bare quoted name appears in it. Measured:
    # deleting `max_position_pct` and `max_position_exposure_pct` from the emitted
    # `risk_math` left this test **passing** - because the explanatory comment three lines
    # above them already read `# "max_position_pct" stays for artifact compatibility`, and a
    # quoted name in a comment is indistinguishable from a dict key to a substring match.
    #
    # So this test was already comment-satisfied before anything was deleted. The strongest
    # behavioural probe for the same record, `test_risk_math_records_binding_caps`, also
    # passed with both keys gone, so there was no behavioural twin to catch it either.
    #
    # An AST walk is the fix: a dict key is a node, and a comment is not. It also drops the
    # fragile `+ 200` character window, which depended on how many lines the surrounding
    # keys happened to occupy.
    tree = ast.parse(inspect.getsource(vbt._process_symbol))
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            label = target.slice
            if isinstance(label, ast.Constant) and label.value == "risk_math":
                emitted.update(
                    key.value
                    for key in node.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )

    assert emitted, (
        "no dict literal is assigned to trade_meta[\"risk_math\"] in _process_symbol; "
        "the risk_math payload the viewer contract depends on is gone or was renamed"
    )

    for key in record_keys:
        expected = SIZE_RECORD_RENAMED_KEYS.get(key, key)
        assert expected in emitted, (
            f"sizing record key {key!r} (risk_math name {expected!r}) is not "
            "serialized into risk_math; the viewer contract "
            "(frontend TradeRiskMath) will silently lose it"
        )


def test_engine_timestamps_share_one_formatter():
    """PM-3: equity curve and indicator points must use the same ISO encoding."""
    from runner.engine_pkg import _time as engine_time
    from runner.engine_pkg.result import build_indicator_series

    series = pd.Series([1.0, 2.0], index=pd.RangeIndex(2))
    data = pd.DataFrame(
        {"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"], utc=True)}
    )
    points = build_indicator_series({"sma": series}, data)
    assert points, "indicator series unexpectedly empty"
    indicator_points = points[0].data
    for point, expected_raw in zip(indicator_points, data["timestamp"]):
        assert point["time"] == engine_time.to_iso_utc(expected_raw)


def test_engine_shim_exposes_package_public_surface():
    """PM-4: anything defined public in runner.engine_pkg must resolve through runner.engine.

    Only names *defined* in the module count (its own classes/functions/constants);
    imports and re-exports are intentionally not shimmed.
    """
    missing = []
    # `walk_packages`, not `iter_modules`: the latter is non-recursive, so a
    # subpackage added later would be missed in silence. The engine package has no
    # subpackages today, so this changes nothing now and removes a way for it to
    # become wrong - the same non-recursive-glob shape fixed in df64c93 and 49260b6.
    for module_info in pkgutil.walk_packages(engine_pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"runner.engine_pkg.{module_info.name}")
        public = getattr(module, "__all__", None)
        if public is None:
            public = [
                name
                for name, obj in vars(module).items()
                if not name.startswith("_") and inspect.getmodule(obj) is module
            ]
        for name in public:
            if not hasattr(engine_shim, name):
                missing.append(f"runner.engine_pkg.{module_info.name}.{name}")
    assert not missing, (
        "runner.engine shim is missing public names from the engine package "
        f"(add them to the shim's re-exports): {missing}"
    )


def test_size_trade_returns_typed_record():
    """_size_trade must keep returning the typed record (not a loose dict)."""
    from runner.engines.base import BacktestConfig

    config = BacktestConfig(parameters={"fees": "zero"},
        strategy_name="t",
        symbols=["X"],
        timeframe="1h",
        start_date="2024-01-01",
        end_date="2024-02-01",
        initial_capital=10_000.0,
        # C1: risk-off (0.0) is a hard error — the record type is pinned
        # on an explicitly sized config instead.
        risk_per_trade_pct=0.01,
    )
    record = _size_trade(
        config=config,
        equity=10_000.0,
        entry_price=100.0,
        asset_class="forex",
        stop_loss_series=None,
        atr_series=None,
        entry_idx=0,
        symbol="X",
        trade_counter=1,
    )
    assert set(record) == set(_SizeRecord.__annotations__)
    # C1: the full-equity mode is removed — the record is pinned on the
    # risk_pct path (budget 100 on a 2.0 fallback stop → 50, capped by the
    # 0.2 position cap to 2000/100 = 20).
    assert record["sizing_mode"] == "risk_pct"
    assert record["qty"] == 20.0
