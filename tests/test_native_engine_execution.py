import logging
import math

import pandas as pd
import pytest

from runner.engine import TradeEngine
from runner.engine_pkg.trading_engine import _floor_to_lot_grid
from runner.engines.base import BacktestConfig

# Issue #151: a (lot_step, steps) pair for which `(steps * lot_step) / lot_step`
# is an ulp below `steps`, i.e. a second floor would lose a step.
_NON_IDEMPOTENT_LOT_STEP = 0.01
_NON_IDEMPOTENT_STEPS = 25602


def _engine(params=None, **config_overrides):
    # #84: explicit no-cost declaration for engine fixtures.
    merged_params = {"fees": "zero", "slippage": "none"}
    merged_params.update(params or {})
    config = {
        "strategy_name": "test",
        "symbols": ["EURUSD"],
        "timeframe": "1m",
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
        "initial_capital": 100_000.0,
        "risk_per_trade_pct": 0.01,
        "parameters": merged_params,
    }
    config.update(config_overrides)
    return TradeEngine(BacktestConfig(**config), symbol="EURUSD")


def test_native_engine_reads_native_stop_and_take_profit_aliases():
    engine = _engine()
    row = pd.Series({"sl_price": 1.0950, "tp_price": 1.1100})

    stop_loss, take_profit_levels = engine._extract_levels(row)

    assert stop_loss == 1.0950
    assert take_profit_levels == [{"name": "tp", "price": 1.1100, "fraction": 1.0, "filled": False}]


def test_native_engine_fixed_risk_amount_sizes_from_actual_stop():
    engine = _engine({"fixed_risk_amount": 500.0})

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
    )

    assert quantity == 100.0

def test_native_engine_fixed_risk_amount_honors_mt5_tick_metadata():
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.1,
            "max_lot": 100.0,
            "cap_fixed_risk_by_leverage": False,
        }
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
    )

    assert quantity == 1.0


def test_fixed_risk_leverage_cap_floors_to_lot_step():  # Issue #151
    # Raw lots 5.0; the leverage cap binds at 0.37 units. A broker accepts
    # only the 0.1 grid, so the capped size must be re-floored to 0.3.
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.1,
            "max_lot": 100.0,
        },
        max_leverage=0.00037,  # cap = 100_000 * 0.00037 / 100 = 0.37 units
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )

    # Exact, not approx: the shared decimal floor returns the clean grid
    # value 0.3 -- `0.1 + 0.2`-style float noise and `quantity % 0.1` are
    # both avoided on purpose (#278).
    assert quantity == 0.3


def test_fixed_risk_leverage_cap_below_min_lot_skips_trade():  # Issue #151
    # The cap binds at 0.25 units; floored to the grid that is 0.2, below
    # min_lot 0.5. A broker would reject it, so no trade: size 0, not min_lot.
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.5,
            "max_lot": 100.0,
        },
        max_leverage=0.00025,  # cap = 0.25 units
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == 0.0


def test_fixed_risk_uncapped_path_is_unchanged_by_cap_normalisation():  # Issue #151
    # Same config but a leverage cap that does not bind: the risk-derived
    # size (5.0) is already on the grid and above min_lot, so it passes
    # through unchanged.
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.1,
            "max_lot": 100.0,
        },
        max_leverage=10.0,
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == pytest.approx(5.0)


def test_fixed_risk_uncapped_size_is_not_floored_a_second_time():  # Issue #151
    # A plain `math.floor` of `k * lot_step / lot_step` loses a step for this
    # k: the quotient lands an ulp below k. `_floor_to_lot_grid` (the shared
    # decimal floor, #278) snaps the same fixture back to `k * lot_step` --
    # its 1e-6-of-a-step noise band covers the ulp -- so re-flooring an
    # on-grid uncapped size is idempotent and this
    # test alone does NOT pin the `shares < uncapped_shares` guard. What the
    # guard protects is an uncapped size raised to an off-grid `min_lot` --
    # pinned by `test_fixed_risk_uncapped_size_raised_to_min_lot_is_not_regridded`,
    # which fails if the guard is deleted. This one stays as the on-grid
    # regression pin: an uncapped size is returned bit-exact.
    lot_step = _NON_IDEMPOTENT_LOT_STEP
    k = _NON_IDEMPOTENT_STEPS
    assert math.floor((k * lot_step) / lot_step + 1e-12) != k, "fixture must be non-idempotent"
    engine = _engine(
        {
            # Risk-derived raw size sits half a step above k steps.
            "fixed_risk_amount": k * lot_step + lot_step / 2,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "lot_step": lot_step,
            "min_lot": lot_step,
        },
        initial_capital=1_000_000_000.0,
        max_leverage=10.0,
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=99.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == k * lot_step


def test_fixed_risk_cap_below_min_lot_creates_no_order():  # Issue #151
    # End-to-end: the cap binds below min_lot, so _create_order skips the
    # order entirely rather than opening a sub-minimum position.
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 00:01", "2024-01-01 00:02"],
                utc=True,
            ),
            "open": [100.0, 101.0, 102.0],
            "high": [100.5, 101.5, 102.5],
            "low": [99.5, 100.5, 101.5],
            "close": [100.0, 101.0, 102.0],
            "sl_price": [95.0, float("nan"), float("nan")],
        }
    )

    def strategy(df, params):
        entries = pd.Series([True, False, False], index=df.index)
        exits = pd.Series([False, False, True], index=df.index)
        return entries, exits, {}, []

    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.5,
            "max_lot": 100.0,
        },
        max_leverage=0.00025,  # cap = 0.25 units, below min_lot after flooring
    )

    result = engine.run(frame, strategy)

    assert result.trades == []


def test_fixed_risk_cap_binding_exactly_on_grid_keeps_full_size():  # Issue #151
    # The leverage cap is exactly 0.5 units, which is on the 0.1 grid. The
    # post-cap floor must keep it at 0.5; a float-safety epsilon with the
    # wrong sign would floor the exact quotient 0.5 / 0.1 = 5.0 to 4 steps.
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.1,
            "max_lot": 100.0,
        },
        max_leverage=0.0005,  # cap = 100_000 * 0.0005 / 100 = 0.5 units
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == pytest.approx(0.5)


def test_fixed_risk_uncapped_size_raised_to_min_lot_is_not_regridded():  # Issue #151
    # Risk-derived size 0.01 is raised to min_lot 0.15 (off the 0.1 grid). No
    # cap binds, so the size must stay 0.15: re-flooring it would give 0.1,
    # below min_lot, and skip a trade the uncapped path always took.
    engine = _engine(
        {
            "fixed_risk_amount": 1.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.15,
            "max_lot": 100.0,
        },
        max_leverage=10.0,
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=99.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == pytest.approx(0.15)


def test_fixed_risk_max_lot_off_grid_is_floored_to_lot_step():  # Issue #151
    # Raw size 500 units is capped by max_lot 99.95, which is off the 0.1
    # grid. The broker never accepts an off-grid size, so the cap resolves
    # to the largest grid value at or below it: 99.9. The leverage cap does
    # not bind here, so this is the max_lot leg alone.
    engine = _engine(
        {
            "fixed_risk_amount": 50_000.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.1,
            "max_lot": 99.95,
        },
        max_leverage=10.0,
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=99.0,
        direction="LONG",
        stop_distance=1.0,
    )

    # Exact, not approx: the shared decimal floor returns the clean grid
    # value 99.9 (#278).
    assert quantity == 99.9


def test_fixed_risk_max_lot_on_grid_is_not_floored_below_itself():  # Issue #151
    # max_lot = k * lot_step is on the grid, but `max_lot / lot_step` lands an
    # ulp below k, so a plain floor would drop a whole step from a limit the
    # broker does allow. The configured max_lot must be kept exactly.
    lot_step = _NON_IDEMPOTENT_LOT_STEP
    k = _NON_IDEMPOTENT_STEPS
    engine = _engine(
        {
            "fixed_risk_amount": 1_000_000.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "lot_step": lot_step,
            "min_lot": lot_step,
            "max_lot": k * lot_step,
        },
        initial_capital=1_000_000_000.0,
        max_leverage=10.0,
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=99.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == pytest.approx(k * lot_step)


def test_fixed_risk_max_lot_below_min_lot_skips_trade(caplog):  # Issue #151
    # Misconfigured limits: max_lot 0.3 is below min_lot 0.5, so no size can
    # satisfy both. Fail closed: size 0 with a warning, never max_lot.
    engine = _engine(
        {
            "fixed_risk_amount": 50_000.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.5,
            "max_lot": 0.3,
        },
        max_leverage=10.0,
    )

    with caplog.at_level(logging.WARNING, logger="runner.engine_pkg.trading_engine"):
        quantity = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=99.0,
            direction="LONG",
            stop_distance=1.0,
        )

    assert quantity == 0.0
    assert any("below min_lot" in r.getMessage() for r in caplog.records)


def test_fixed_risk_cap_below_min_lot_logs_skip(caplog):  # Issue #151
    # A skipped trade must say so: the same cap-below-min_lot case as above,
    # but the skip is logged at WARNING, not silent.
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.5,
            "max_lot": 100.0,
        },
        max_leverage=0.00025,  # cap = 0.25 units, below min_lot after flooring
    )

    with caplog.at_level(logging.WARNING, logger="runner.engine_pkg.trading_engine"):
        quantity = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            direction="LONG",
            stop_distance=1.0,
        )

    assert quantity == 0.0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "below min_lot" in warnings[0].getMessage()


def test_fixed_risk_grid_size_at_min_lot_is_not_skipped():  # Issue #151
    # A size the cap reduced is re-floored to the grid. The shared decimal
    # floor returns the exact grid product -- `3 * 0.3` is 0.9, not
    # 0.8999999999999999 -- so `shares < min_lot` is False and the ulp-snap
    # branch is not needed: the trade happens at 0.9.
    assert _floor_to_lot_grid(0.9, 0.3) == 0.9
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "lot_step": 0.3,
            "min_lot": 0.9,
            "max_lot": 100.0,
        },
        max_leverage=0.0009,  # cap = 100_000 * 0.0009 / 100 = 0.9 units = 3 steps
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == 0.9


def test_fixed_risk_offgrid_min_lot_within_snap_band_is_raised(caplog):  # Issue #151
    # The ulp-snap branch is still reachable, but only through an OFF-GRID
    # `min_lot`: the decimal floor returns the exact grid product 0.9, so
    # `shares < min_lot` needs a `min_lot` a hair above it. Within the
    # 1e-9 relative band the broker's minimum is taken as the size instead
    # of skipping the trade.
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "lot_step": 0.3,
            "min_lot": 0.9000000005,
            "max_lot": 100.0,
        },
        max_leverage=0.0009,  # cap = 100_000 * 0.0009 / 100 = 0.9 units = 3 steps
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == pytest.approx(0.9000000005)
    assert not any("below min_lot" in r.getMessage() for r in caplog.records)


def test_fixed_risk_grid_size_at_min_lot_creates_order():  # Issue #151
    # End-to-end twin of the case above: the capped size lands exactly on
    # min_lot after the decimal grid floor, so a real order must be created
    # at min_lot instead of the trade being skipped with size 0. Flat
    # prices keep the fill-time re-size on the same 100.0 entry as the
    # creation-time sizing.
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 00:01", "2024-01-01 00:02"],
                utc=True,
            ),
            "open": [100.0, 100.0, 100.0],
            "high": [100.5, 100.5, 100.5],
            "low": [99.5, 99.5, 99.5],
            "close": [100.0, 100.0, 100.0],
            "sl_price": [95.0, float("nan"), float("nan")],
        }
    )

    def strategy(df, params):
        entries = pd.Series([True, False, False], index=df.index)
        exits = pd.Series([False, False, True], index=df.index)
        return entries, exits, {}, []

    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "lot_step": 0.3,
            "min_lot": 0.9,
            "max_lot": 100.0,
        },
        max_leverage=0.0009,  # cap = 100_000 * 0.0009 / 100 = 0.9 = 3 steps
    )

    result = engine.run(frame, strategy)

    assert len(result.trades) == 1
    assert result.trades[0].quantity == pytest.approx(0.9)


def test_fixed_risk_cap_within_snap_band_below_min_lot_without_grid_is_skipped():  # Issue #151
    # #170 follow-up (cubic P2): the min_lot snap-up is a GRID exception. It
    # exists for a grid product that came out of the floor an ulp below
    # `min_lot`; without a `lot_step` there is no grid product, and snapping
    # a capped raw ratio up to `min_lot` would raise the size above the cap
    # that just reduced it. The leverage cap is crafted one ulp below
    # `min_lot` (inside the relative band), so the trade is skipped, not
    # traded at min_lot.
    cap = math.nextafter(0.9, 0.0)
    assert math.isclose(cap, 0.9, rel_tol=1e-9), "fixture relies on the cap being in the band"
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "min_lot": 0.9,
        },
        max_leverage=cap / 1000.0,  # cap = 100_000 * lev / 100 = one ulp below 0.9
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == 0.0


def test_floor_to_lot_grid_never_exceeds_size_by_more_than_the_band():  # Issue #151
    # #170 follow-up (cubic P2): the snap CAN return a value above `size`
    # (the grid multiple a float product already represents), and that is
    # what keeps a cap exact: an on-grid `max_lot` must not floor one whole
    # lot below itself. Pin the documented bound instead of a strict floor:
    # since #278 the shared decimal floor's overshoot is bounded by its noise
    # band (1e-6 of a step, `shared.lot_grid._STEP_NOISE`), far below any
    # broker's quantity resolution, so a capped value is only ever read as
    # the grid multiple it already was. A strict floor would instead cut an
    # on-grid cap by a lot (`25602 * 0.01` -> 256.01, see
    # `test_fixed_risk_max_lot_on_grid_is_not_floored_below_itself`) and drop
    # the ulp-low grid products the min_lot snap-up is built on.
    grid_multiple = _NON_IDEMPOTENT_STEPS * _NON_IDEMPOTENT_LOT_STEP
    size = math.nextafter(grid_multiple, 0.0)  # one ulp below the grid multiple

    floored = _floor_to_lot_grid(size, _NON_IDEMPOTENT_LOT_STEP)

    assert floored == pytest.approx(grid_multiple)
    assert floored - size <= 1e-6 * _NON_IDEMPOTENT_LOT_STEP


def test_fixed_risk_max_lot_without_lot_step_applies_raw():  # Issue #151
    # `max_lot` with no `lot_step` needs no grid floor: the cap applies raw
    # (as before #151), and the floor guard must not divide by a zero step.
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "max_lot": 100.0,
        },
        max_leverage=10.0,
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == pytest.approx(100.0)


@pytest.mark.parametrize("bad_value", [float("inf"), float("nan")])
@pytest.mark.parametrize(
    "key", ["tick_size", "tick_value", "lot_step", "min_lot", "max_lot"]
)
def test_fixed_risk_non_finite_sizing_input_is_refused(key, bad_value):  # Issue #151
    # `inf` crashes the lot-grid floor (`round(inf)`) and `nan` defeats every
    # comparison guard on the sizing path, so a non-finite sizing parameter
    # is a config error: refuse it loudly instead of crashing or leaking a
    # NaN/inf quantity into an order.
    params = {
        "fixed_risk_amount": 500.0,
        "tick_size": 1.0,
        "tick_value": 1.0,
        "lot_step": 0.1,
        "min_lot": 0.1,
        "max_lot": 100.0,
        key: bad_value,
    }
    engine = _engine(params)

    with pytest.raises(ValueError, match=key):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            direction="LONG",
            stop_distance=1.0,
        )


@pytest.mark.parametrize(
    "bad_amount", [float("inf"), float("nan"), float("-inf")]
)
def test_fixed_risk_non_finite_risk_amount_is_refused(bad_amount):  # Issue #151, #170
    # #170: `nan > 0` and `-inf > 0` are both False, so validating only
    # inside the `> 0` gate let a NaN or -inf amount skip the fixed-risk
    # branch and fall through to `risk_per_trade_pct` — a size computed under
    # a different risk rule than the config declares, with no error. Refuse
    # before the gate, for every non-finite spelling.
    engine = _engine({"fixed_risk_amount": bad_amount})

    with pytest.raises(ValueError, match="fixed_risk_amount"):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            direction="LONG",
            stop_distance=1.0,
        )


def test_fixed_risk_negative_risk_amount_is_refused():  # Issue #170
    # A negative amount risks a negative sum; it also compares False against
    # the `> 0` gate, so it too must not fall through to the percentage path.
    engine = _engine({"fixed_risk_amount": -500.0})

    with pytest.raises(ValueError, match="fixed_risk_amount"):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            direction="LONG",
            stop_distance=1.0,
        )


def test_fixed_risk_zero_amount_sizes_like_the_percentage_path():
    # Owner decision: `fixed_risk_amount = 0.0` (and an absent key) is the
    # documented "off" idiom — the fixed-risk branch is skipped and sizing
    # falls through to `risk_per_trade_pct`. The #170 guard must not turn
    # "off" into an error, and "off" must size exactly like "unset".
    engine = _engine({"fixed_risk_amount": 0.0})
    unconfigured = _engine()

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )
    baseline = unconfigured._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == baseline
    assert quantity > 0.0


@pytest.mark.parametrize("bad_value", [0, 0.0, -1.0])
def test_fixed_risk_zero_or_negative_tick_value_is_refused(bad_value):  # Issue #170
    # #170: `tick_value: 0` read as `0 or 1.0` became 1.0, which silently
    # erased the tick-value conversion: on a 0.01-tick instrument the size
    # came out ~100x too large instead of the config error it is.
    engine = _engine({"fixed_risk_amount": 500.0, "tick_value": bad_value})

    with pytest.raises(ValueError, match="tick_value"):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            direction="LONG",
            stop_distance=1.0,
        )


@pytest.mark.parametrize("bad_value", [0, 0.0, -0.01])
def test_fixed_risk_zero_or_negative_tick_size_is_refused(bad_value):  # Issue #170
    # #170: `tick_size: 0` reads as `0 or 1.0` = 1.0, so the tick-size leg of
    # the conversion vanishes and the same ~100x oversizing is silent.
    engine = _engine({"fixed_risk_amount": 500.0, "tick_size": bad_value})

    with pytest.raises(ValueError, match="tick_size"):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            direction="LONG",
            stop_distance=1.0,
        )


@pytest.mark.parametrize("bad_value", [-0.01, -1.0])
@pytest.mark.parametrize(
    "key", ["lot_step", "min_lot", "max_lot"]
)
def test_fixed_risk_negative_lot_limits_are_refused(key, bad_value):  # Issue #170
    # #170: a negative grid parameter passed the finite check and was then
    # silently dropped by the `> 0` gates, so the broker lot step and the
    # minimum never applied. Zero stays the documented "off" setting; a
    # configured negative value is a config error.
    params = {
        "fixed_risk_amount": 500.0,
        "tick_size": 1.0,
        "tick_value": 1.0,
        "lot_step": 0.1,
        "min_lot": 0.1,
        "max_lot": 100.0,
        key: bad_value,
    }
    engine = _engine(params)

    with pytest.raises(ValueError, match=key):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            direction="LONG",
            stop_distance=1.0,
        )


@pytest.mark.parametrize(
    "key", ["tick_size", "tick_value", "lot_step", "min_lot", "max_lot"]
)
def test_fixed_risk_null_sizing_parameters_are_not_configured(key):  # Issue #170
    # An explicit null is "not configured", not a value: the parameter keeps
    # its default (1.0 for the tick pair, 0.0 = off for the lot grid) and no
    # error is raised. Only a configured, unusable value is refused.
    engine = _engine({"fixed_risk_amount": 500.0, key: None})
    unconfigured = _engine({"fixed_risk_amount": 500.0})

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )
    baseline = unconfigured._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )

    assert quantity == pytest.approx(500.0)
    assert quantity == baseline


def test_fixed_risk_tick_conversion_is_not_silently_dropped():
    # #170: the regression the guard closes. `tick_value: 0` used to size
    # exactly like `tick_value: 1.0` (500.0 units); it must now refuse, so the
    # tick-value conversion cannot vanish without an error.
    dropped = _engine({"fixed_risk_amount": 500.0, "tick_value": 0})

    with pytest.raises(ValueError, match="tick_value"):
        dropped._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            direction="LONG",
            stop_distance=1.0,
        )

    honoured = _engine({"fixed_risk_amount": 500.0, "tick_value": 1.0})

    assert honoured._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    ) == pytest.approx(500.0)


def test_fixed_risk_non_numeric_sizing_parameter_is_refused():  # Issue #170
    # A non-numeric value must name the parameter, not surface a bare
    # `float()` error from deep inside the sizing path.
    engine = _engine({"fixed_risk_amount": 500.0, "lot_step": "wide"})

    with pytest.raises(ValueError, match="lot_step"):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            direction="LONG",
            stop_distance=1.0,
        )


def test_fixed_risk_boolean_sizing_parameters_are_refused():  # Issue #170
    # #170 follow-up: `float(True) == 1.0` and `float(False) == 0.0`, so a
    # boolean was silently accepted as a numeric setting -- `True` a risk
    # amount, `False` a disabled grid. BacktestConfig refuses booleans for
    # its numeric fields; the sizing resolver must do the same, before
    # `float()` ever sees the value.
    base = {"fixed_risk_amount": 500.0, "tick_size": 1.0, "tick_value": 1.0}
    for key in (
        "fixed_risk_amount",
        "Fixed_Risk_Amount",
        "tick_size",
        "tick_value",
        "lot_step",
        "min_lot",
        "max_lot",
    ):
        for bad_bool in (True, False):
            # The camelCase spelling is only read when `fixed_risk_amount`
            # is absent, so the alias case must not also set the canonical
            # key (otherwise the canonical value shadows the boolean).
            params = dict(base)
            if key == "Fixed_Risk_Amount":
                del params["fixed_risk_amount"]
            params[key] = bad_bool
            engine = _engine(params)
            expected = "fixed_risk_amount" if key == "Fixed_Risk_Amount" else key

            with pytest.raises(ValueError, match=expected):
                engine._calculate_position_size(
                    entry_price=100.0,
                    stop_loss_price=95.0,
                    direction="LONG",
                    stop_distance=1.0,
                )


def test_fixed_risk_oversized_sizing_parameter_is_refused():  # Issue #170
    # #170 follow-up: `float(10**400)` raises OverflowError, which escaped
    # the resolver as a bare OverflowError instead of the parameter-specific
    # ValueError every other unusable value gets. BacktestConfig catches
    # OverflowError alongside TypeError/ValueError; the sizing resolver does
    # the same so the message names the parameter.
    base = {"fixed_risk_amount": 500.0, "tick_size": 1.0, "tick_value": 1.0}
    oversized = 10**400
    for key in ("fixed_risk_amount", "tick_size", "tick_value", "lot_step"):
        engine = _engine({**base, key: oversized})

        with pytest.raises(ValueError, match=key):
            engine._calculate_position_size(
                entry_price=100.0,
                stop_loss_price=95.0,
                direction="LONG",
                stop_distance=1.0,
            )


# ---------------------------------------------------------------------------
# Issue #170: the same guard on the stopless path (and on the other inputs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_amount", [float("nan"), float("-inf")])
def test_fixed_risk_bad_amount_is_refused_without_a_stop(bad_amount):  # Issue #170
    # The #151/#170 guard lived behind `stop_loss_price is not None`, so a
    # strategy that never emits a stop reached `risk_per_trade_pct` instead:
    # a backtest traded under a risk rule the config does not declare. Resolve
    # first, so the value is refused on every path.
    engine = _engine({"fixed_risk_amount": bad_amount})

    with pytest.raises(ValueError, match="fixed_risk_amount"):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=None,
            direction="LONG",
        )


def test_fixed_risk_negative_amount_is_refused_without_a_stop():  # Issue #170
    # A negative amount is refused too, on the stopless path as well: it
    # compares False against the `> 0` gate, so before the hoist it silently
    # selected the percentage branch.
    engine = _engine({"fixed_risk_amount": -500.0})

    with pytest.raises(ValueError, match="fixed_risk_amount"):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=None,
            direction="LONG",
        )


def test_fixed_risk_bad_amount_is_refused_end_to_end_without_a_stop():  # Issue #170
    # End-to-end: a stopless strategy with a NaN risk amount must fail loudly,
    # not run to completion on the percentage rule. Flat prices keep the
    # fill-time re-size on the same entry as the creation-time sizing.
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 00:01", "2024-01-01 00:02"],
                utc=True,
            ),
            "open": [100.0, 100.0, 100.0],
            "high": [100.5, 100.5, 100.5],
            "low": [99.5, 99.5, 99.5],
            "close": [100.0, 100.0, 100.0],
        }
    )

    def strategy(df, params):
        entries = pd.Series([True, False, False], index=df.index)
        exits = pd.Series([False, False, True], index=df.index)
        return entries, exits, {}, []

    engine = _engine({"fixed_risk_amount": float("nan")})

    with pytest.raises(ValueError, match="fixed_risk_amount"):
        engine.run(frame, strategy)


@pytest.mark.parametrize(
    "order_params",
    [{}, {"fixed_risk_amount": 0.0}, {"Fixed_Risk_Amount": 0.0}],
)
def test_fixed_risk_zero_and_absent_amount_stay_off_without_a_stop(order_params):
    # #170: `0.0` (and an absent/explicitly-null key) is still the documented
    # "off" idiom, stop or no stop: the fixed-risk branch is skipped and the
    # percentage rule sizes the trade. Hoisting the resolution must not turn
    # "off" into a config error.
    engine = _engine(order_params)
    unconfigured = _engine()

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=None,
        direction="LONG",
    )
    baseline = unconfigured._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=None,
        direction="LONG",
    )

    assert quantity == baseline
    assert quantity > 0.0


def test_fixed_risk_tick_pair_is_refused_when_the_stop_distance_is_zero():
    # #170: the same ordering check for the other sizing inputs. A stop exactly
    # at the entry leaves no risk distance, so the conversion cannot be
    # computed at all -- and which error you get must not depend on the bar
    # data: resolving the tick pair with its branch, instead of inside its
    # innermost gate, names the real config defect (`tick_value: 0`) rather
    # than the symptom ("entry_price and stop_loss must differ").
    engine = _engine({"fixed_risk_amount": 500.0, "tick_value": 0.0})

    with pytest.raises(ValueError, match="tick_value"):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=100.0,
            direction="LONG",
        )


def test_fixed_risk_negative_lot_grid_is_refused_when_the_stop_distance_is_zero():
    # Same check on the broker grid: the `> 0` gates used to discard a
    # negative `lot_step`, which must not depend on the stop distance either.
    engine = _engine({"fixed_risk_amount": 500.0, "lot_step": -0.1})

    with pytest.raises(ValueError, match="lot_step"):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=100.0,
            direction="LONG",
        )


def test_fixed_risk_zero_stop_distance_is_refused_not_silently_resized():
    # Preserved behaviour, pinned: with a stop exactly at the entry there is
    # no risk distance to convert into shares, and the engine refuses instead
    # of falling through and sizing under a different risk rule. Only the
    # unusable-value refusal moved above the distance gate; this refusal did
    # not change.
    engine = _engine({"fixed_risk_amount": 500.0})

    with pytest.raises(ValueError, match="must differ"):
        engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=100.0,
            direction="LONG",
        )


def test_fixed_risk_max_lot_below_lot_step_skips_trade_with_a_warning(caplog):
    # `0 < max_lot < lot_step`: the grid floor of the cap is 0, so no size can
    # be traded. With `min_lot == 0` the min_lot skip above never fires, so the
    # zero used to be returned silently -- log the drop like the min_lot skip.
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.0,
            "max_lot": 0.05,
        },
        initial_capital=1_000_000.0,
        max_leverage=10.0,
    )

    with caplog.at_level(logging.WARNING, logger="runner.engine_pkg.trading_engine"):
        quantity = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            direction="LONG",
            stop_distance=1.0,
        )

    assert quantity == 0.0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "zero after caps" in warnings[0].getMessage()


def _fixed_risk_run_frame(stop_price=None):
    """Three flat bars: one entry on the first, one time exit on the last."""
    data = {
        "timestamp": pd.to_datetime(
            ["2024-01-01 00:00", "2024-01-01 00:01", "2024-01-01 00:02"],
            utc=True,
        ),
        "open": [100.0, 100.0, 100.0],
        "high": [100.5, 100.5, 100.5],
        "low": [99.5, 99.5, 99.5],
        "close": [100.0, 100.0, 100.0],
    }
    if stop_price is not None:
        data["sl_price"] = [stop_price, float("nan"), float("nan")]
    return pd.DataFrame(data)


def _entry_then_exit_strategy(df, params):
    entries = pd.Series([True, False, False], index=df.index)
    exits = pd.Series([False, False, True], index=df.index)
    return entries, exits, {}, []


@pytest.mark.parametrize(
    "order_params", [{}, {"fixed_risk_amount": 0.0}, {"Fixed_Risk_Amount": 0.0}]
)
def test_fixed_risk_zero_and_absent_amount_orders_stay_equity_proportional(order_params):
    # #170 / A-M4 (D5): the order's `equity_proportional_sizing` flag drives
    # the shared-equity replay. `fixed_risk_amount: 0.0` is "off" -- the order
    # is percentage-sized, so the replay must re-scale it exactly like an
    # absent key. A flag that only asked whether the key existed stopped the
    # multi-symbol replay from re-scaling a percentage-sized position.
    engine = _engine(order_params)

    result = engine.run(_fixed_risk_run_frame(95.0), _entry_then_exit_strategy)

    assert len(result.trades) == 1
    assert result.trades[0].meta["equity_proportional_sizing"] is True


def test_fixed_risk_amount_orders_are_not_equity_proportional():
    # A configured, used amount is absolute-$: the replay must keep the
    # quantity (exposure is still summed, PnL still credited).
    engine = _engine({"fixed_risk_amount": 500.0})

    result = engine.run(_fixed_risk_run_frame(95.0), _entry_then_exit_strategy)

    assert len(result.trades) == 1
    assert result.trades[0].meta["equity_proportional_sizing"] is False


def test_fixed_risk_stop_distance_only_orders_are_not_equity_proportional():
    # #170 follow-up (code review should-fix, cross-layer marker): a strategy
    # that emits a `stop_distance` column but no stop column has no stop at
    # creation, so the order is percentage-sized and the D5 replay flag
    # starts True. At fill the stop is synthesized from `stop_distance` and
    # the SAME order is re-sized by the absolute-$ fixed-risk branch, so the
    # flag must follow the branch that sized the final quantity -- a True
    # flag on an absolute-$ position makes the shared-equity replay re-scale
    # it (Nx the configured risk on a multi-symbol run). Flat prices keep
    # the fill-time re-size on the same 100.0 entry as creation.
    frame = _fixed_risk_run_frame()
    frame["stop_distance"] = [1.0, float("nan"), float("nan")]

    engine = _engine({"fixed_risk_amount": 500.0})

    result = engine.run(frame, _entry_then_exit_strategy)

    assert len(result.trades) == 1
    # 500 / 1.0 distance * (tick_size 1.0 / tick_value 1.0): the fixed-risk
    # branch sized it -- the percentage rule would not produce this size.
    assert result.trades[0].quantity == pytest.approx(500.0)
    assert result.trades[0].meta["equity_proportional_sizing"] is False


def test_fixed_risk_zero_amount_order_is_not_equity_proportional_without_a_stop():
    # Stopless `fixed_risk_amount: 0.0` cannot use fixed-risk sizing at all
    # (there is no risk distance), so the order is percentage-sized and stays
    # proportional.
    engine = _engine({"fixed_risk_amount": 0.0})

    result = engine.run(_fixed_risk_run_frame(), _entry_then_exit_strategy)

    assert len(result.trades) == 1
    assert result.trades[0].meta["equity_proportional_sizing"] is True


def test_fixed_risk_exposure_cap_floors_to_lot_step():  # Issue #151
    # `cap_fixed_risk_by_leverage: false` + a binding
    # `max_position_exposure_pct` is the OTHER arm of the cap, and it is a raw
    # equity/price ratio too -- its size has to be re-floored to the grid as
    # well, and an uncapped size still must not be.
    engine = _engine(
        {
            "fixed_risk_amount": 100.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "cap_fixed_risk_by_leverage": False,
        },
        initial_capital=100_000.0,
        max_position_exposure_pct=0.00049,  # cap = 100_000 * 0.00049 / 100 = 0.49
    )

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    )

    # 0.49 is off the 0.1 grid -- a size no broker accepts.
    assert quantity == pytest.approx(0.4)

    on_grid = _engine(
        {
            "fixed_risk_amount": 100.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "cap_fixed_risk_by_leverage": False,
        },
        initial_capital=100_000.0,
        max_position_exposure_pct=0.0005,  # cap = 0.5, already on the grid
    )

    assert on_grid._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=1.0,
    ) == pytest.approx(0.5)


def test_fixed_risk_max_lot_below_min_lot_logs_the_uncapped_size(caplog):  # Issue #151
    # The skip must report the size the config asked for, not the size after
    # the broker band: with `max_lot < min_lot` the value captured before the
    # leverage/exposure cap is already the truncated `max_lot` grid value, so
    # the WARNING carries the pre-`max_lot` size as well.
    engine = _engine(
        {
            "fixed_risk_amount": 500.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "lot_step": 0.1,
            "min_lot": 0.5,
            "max_lot": 0.3,
        },
        initial_capital=100_000.0,
        max_leverage=10.0,  # never binds here: the leverage cap is 10,000 units
    )

    with caplog.at_level(logging.WARNING, logger="runner.engine_pkg.trading_engine"):
        quantity = engine._calculate_position_size(
            entry_price=100.0,
            stop_loss_price=95.0,
            direction="LONG",
            stop_distance=1.0,
        )

    assert quantity == 0.0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    # The risk-derived 500 / 1 = 500 units, not the 0.3 max_lot floor.
    assert "size before caps 500" in message
    assert "before the leverage/exposure cap 0.3" in message


def test_fixed_risk_prefers_explicit_stop_distance():
    # MT5 parity (strategy-test-audit): lots derive from the strategy's stop
    # distance (e.g. ATR-based), not from |entry - stop|, so fill-price noise
    # never leaks into sizing.
    engine = _engine({"fixed_risk_amount": 500.0})

    quantity = engine._calculate_position_size(
        entry_price=100.7,  # noisy fill, must not matter
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=5.0,
    )

    assert quantity == 100.0


def test_fixed_risk_falls_back_to_entry_minus_stop_without_distance():
    engine = _engine({"fixed_risk_amount": 500.0})

    quantity = engine._calculate_position_size(
        entry_price=100.0,
        stop_loss_price=95.0,
        direction="LONG",
        stop_distance=None,
    )

    assert quantity == 100.0


def test_bar_spread_defaults_to_zero_without_column():
    engine = _engine()
    row = pd.Series({"open": 100.0})

    assert engine._bar_spread(row) == 0.0


def test_bar_spread_reads_configured_price_column():
    engine = _engine({"spread_price_col": "spread_price"})
    row = pd.Series({"open": 100.0, "spread_price": 0.25})

    assert engine._bar_spread(row) == 0.25


def test_bar_spread_treats_nan_as_zero():
    import math

    engine = _engine({"spread_price_col": "spread_price"})
    row = pd.Series({"open": 100.0, "spread_price": float("nan")})

    assert engine._bar_spread(row) == 0.0
    assert not math.isnan(engine._bar_spread(row))


def _spread_frame():
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 00:01", "2024-01-01 00:02"], utc=True
            ),
            "open": [100.0, 101.0, 102.0],
            "high": [100.5, 101.5, 102.5],
            "low": [99.5, 100.5, 101.5],
            "close": [100.2, 101.2, 102.2],
            "spread_price": [0.2, 0.2, 0.2],
        }
    )


def _spread_strategy(df, params):
    entries = pd.Series([True, False, False], index=df.index)
    exits = pd.Series([False, False, True], index=df.index)
    direction = pd.Series(["LONG", None, None], index=df.index, dtype=object)
    return entries, exits, {"direction": direction}, []


def test_market_long_pays_spread_short_hits_bid():
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={"fees": "zero", "slippage": "none", "spread_price_col": "spread_price"},
        ),
        symbol="T",
    )
    result = engine.run(_spread_frame(), _spread_strategy)
    assert len(result.trades) == 1
    trade = result.trades[0]
    # LONG market order created on bar 0 fills at bar 1 open + spread.
    assert trade.entry_price == 101.0 + 0.2


def test_signal_exit_fill_open_uses_bar_open():
    from runner.engine import TradeEngine as _TE

    engine = _TE(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            signal_exit_fill="open",
            parameters={"fees": "zero", "slippage": "none"},
        ),
        symbol="T",
    )
    result = engine.run(_spread_frame(), _spread_strategy)
    assert len(result.trades) == 1
    # Signal exit on bar 2 fills at bar 2 open (not close 102.2).
    assert result.trades[0].exits[0].price == 102.0


def test_signal_exit_fill_close_is_default():
    from runner.engine import TradeEngine as _TE

    engine = _TE(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={"fees": "zero", "slippage": "none"},
        ),
        symbol="T",
    )
    result = engine.run(_spread_frame(), _spread_strategy)
    assert len(result.trades) == 1
    assert result.trades[0].exits[0].price == 102.2


def _short_spread_strategy(df, params):
    entries = pd.Series([True, False, False], index=df.index)
    exits = pd.Series([False, False, True], index=df.index)
    direction = pd.Series(["SHORT", None, None], index=df.index, dtype=object)
    return entries, exits, {"direction": direction}, []


def test_signal_exit_fill_open_short_pays_spread():
    # MT5 parity: closing a SHORT buys the ask (open + spread).
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            signal_exit_fill="open",
            parameters={"fees": "zero", "slippage": "none", "spread_price_col": "spread_price"},
        ),
        symbol="T",
    )
    result = engine.run(_spread_frame(), _short_spread_strategy)
    assert len(result.trades) == 1
    trade = result.trades[0]
    # SHORT entry hits the bid; signal exit fills at open + spread (ask).
    assert trade.entry_price == 101.0
    assert trade.exits[0].price == 102.0 + 0.2


def test_warmup_bars_feed_indicators_but_create_no_trades():
    # Warmup (strategy-test-audit): bars before start_date warm indicators;
    # entries AND exits on them are ignored, the equity curve starts at start.
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01 00:00",
                    "2024-01-01 00:01",
                    "2024-01-01 00:02",
                    "2024-01-01 00:03",
                ],
                utc=True,
            ),
            "open": [100.0, 101.0, 102.0, 103.0],
            "high": [100.5, 101.5, 102.5, 103.5],
            "low": [99.5, 100.5, 101.5, 102.5],
            "close": [100.2, 101.2, 102.2, 103.2],
        }
    )

    def warmup_strategy(df, params):
        entries = pd.Series([True, False, True, False], index=df.index)
        exits = pd.Series([False, True, False, True], index=df.index)
        return entries, exits, {}, []

    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01 00:02",
            end_date="2024-01-01 00:03",
            initial_capital=100_000.0,
            warmup_start="2024-01-01 00:00",
            parameters={"fees": "zero", "slippage": "none"},
        ),
        symbol="T",
    )
    result = engine.run(frame, warmup_strategy)
    # Only the 00:02 entry -> 00:03 exit survives (market fills next-open);
    # warmup bars trade nothing.
    assert len(result.trades) == 1
    assert result.trades[0].entry_time.startswith("2024-01-01T00:03")


def _ask_stop_frame():
    # SHORT with stop at 101. Bid high never breaches; ask high does on bar 2.
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 00:01", "2024-01-01 00:02"], utc=True
            ),
            "open": [100.0, 100.0, 100.0],
            "high": [100.5, 100.5, 100.5],
            "low": [99.5, 99.5, 99.5],
            "close": [100.0, 100.0, 100.0],
            "ask_high": [100.5, 100.5, 101.5],
            "ask_low": [99.5, 99.5, 99.5],
        }
    )


def _ask_stop_strategy(df, params):
    entries = pd.Series([True, False, False], index=df.index)
    exits = pd.Series([False, False, False], index=df.index)
    direction = pd.Series(["SHORT", None, None], index=df.index, dtype=object)
    sl = pd.Series([101.0, float("nan"), float("nan")], index=df.index)
    return entries, exits, {"direction": direction, "sl_price": sl}, []


def test_short_stop_uses_ask_high_when_enabled():
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={"fees": "zero", "slippage": "none", "ask_ohlc": True},
        ),
        symbol="T",
    )
    result = engine.run(_ask_stop_frame(), _ask_stop_strategy)
    assert len(result.trades) == 1
    assert result.trades[0].exits[0].reason == "stop_loss"
    assert result.trades[0].exits[0].price == 101.0


def test_short_stop_stays_bid_based_by_default():
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={"fees": "zero", "slippage": "none"},
        ),
        symbol="T",
    )
    result = engine.run(_ask_stop_frame(), _ask_stop_strategy)
    # Bid high (100.5) never reaches the 101 stop -> no exit, position open
    # until end_of_data.
    assert len(result.trades) == 1
    assert result.trades[0].exits[0].reason == "end_of_data"


def _fill_bar_stop_frame():
    # LONG stop 95: bar 1 (the fill bar) already trades through it.
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01 00:00",
                    "2024-01-01 00:01",
                    "2024-01-01 00:02",
                    "2024-01-01 00:03",
                ],
                utc=True,
            ),
            "open": [100.0, 100.0, 100.0, 100.0],
            "high": [101.0, 101.0, 101.0, 101.0],
            "low": [99.0, 90.0, 99.0, 99.0],
            "close": [100.0, 100.0, 100.0, 100.0],
            "volume": [1000.0, 1000.0, 1000.0, 1000.0],
        }
    )


def _fill_bar_stop_strategy(df, params):
    entries = pd.Series([True, False, False, False], index=df.index)
    exits = pd.Series([False, False, False, False], index=df.index)
    direction = pd.Series(["LONG", None, None, None], index=df.index, dtype=object)
    stop = pd.Series([95.0, float("nan"), float("nan"), float("nan")], index=df.index)
    return entries, exits, {"direction": direction, "stop_loss": stop}, []


def test_fill_bar_stop_exits_on_fill_bar():
    # A-H1: the market order fills at bar 1 open and bar 1 already trades
    # through the fresh stop — the scoped fill-bar recheck must exit on bar
    # 1 itself instead of leaking a full extra bar of phantom exposure.
    engine = _engine()
    result = engine.run(_fill_bar_stop_frame(), _fill_bar_stop_strategy)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.holding_bars == 0
    assert trade.exits[0].reason == "stop_loss"
    assert trade.exits[0].price == 95.0
    assert trade.exits[0].time.startswith("2024-01-01T00:01")


def _short_spread_stop_frame():
    # SHORT filled at bar 1 open (100); bar 1 high touches stop 105.
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 00:01", "2024-01-01 00:02"], utc=True
            ),
            "open": [100.0, 100.0, 100.0],
            "high": [100.5, 106.0, 100.5],
            "low": [99.5, 99.5, 99.5],
            "close": [100.0, 100.0, 100.0],
            "volume": [1000.0, 1000.0, 1000.0],
            "spread_price": [0.5, 0.5, 0.5],
        }
    )


def _short_spread_stop_strategy(df, params):
    entries = pd.Series([True, False, False], index=df.index)
    exits = pd.Series([False, False, False], index=df.index)
    direction = pd.Series(["SHORT", None, None], index=df.index, dtype=object)
    stop = pd.Series([105.0, float("nan"), float("nan")], index=df.index)
    return entries, exits, {"direction": direction, "stop_loss": stop}, []


def test_short_stop_fill_adds_spread():
    # A-H7: SHORT stop fills buy the ask (stop level + spread), LONG stops
    # sell the bid (level as-is).
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={"fees": "zero", "slippage": "none", "spread_price_col": "spread_price"},
        ),
        symbol="T",
    )
    result = engine.run(_short_spread_stop_frame(), _short_spread_stop_strategy)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == 100.0
    assert trade.exits[0].reason == "stop_loss"
    assert trade.exits[0].price == 105.0 + 0.5


def test_explicit_fees_reduce_net_pnl():
    # A-L1: fee_rate (fraction of notional per side) + fee_flat_per_trade
    # must come out of net_pnl instead of silently staying 0.0.
    engine = TradeEngine(
        BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="test",
            symbols=["T"],
            timeframe="1m",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            parameters={
                "fee_rate": 0.01,
                "fee_flat_per_trade": 3.0,
                "slippage": "none",
            },
        ),
        symbol="T",
    )
    result = engine.run(_spread_frame(), _spread_strategy)
    assert len(result.trades) == 1
    trade = result.trades[0]
    qty = trade.quantity
    expected_fees = (trade.entry_price * qty + trade.exits[0].price * qty) * 0.01 + 3.0
    assert trade.fees > 0
    assert trade.fees == pytest.approx(expected_fees)
    assert trade.net_pnl == trade.pnl - trade.fees


def test_native_engine_orders_take_profit_levels_numerically():
    # Regression (#154): string sorting put tp10/tp11 before tp2.
    engine = _engine()
    # Insert out of order so the result cannot come from column order.
    columns = ["tp10", "tp2", "tp11", "tp1", "tp9", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8"]
    row = pd.Series({name: 100.0 + int(name[2:]) for name in columns})

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == [f"tp{i}" for i in range(1, 12)]
    assert [level["price"] for level in take_profit_levels] == [100.0 + i for i in range(1, 12)]


def test_native_engine_maps_pct_fractions_to_numerically_ordered_levels():
    engine = _engine()
    data = {f"tp{i}": 100.0 + i for i in range(1, 12)}
    # Fractions are keyed by level name, so they must follow their level
    # through the numeric re-ordering (tp2_pct -> tp2, tp10_pct -> tp10).
    data["tp2_pct"] = 0.5
    data["tp10_pct"] = 0.1
    row = pd.Series(data)

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == [f"tp{i}" for i in range(1, 12)]
    fractions = {level["name"]: level["fraction"] for level in take_profit_levels}
    default_fraction = 1.0 / 11
    assert fractions["tp2"] == pytest.approx(0.5)
    assert fractions["tp10"] == pytest.approx(0.1)
    for name in ("tp1", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8", "tp9", "tp11"):
        assert fractions[name] == pytest.approx(default_fraction)


def test_native_engine_puts_plain_take_profit_before_numbered_levels():
    engine = _engine()
    row = pd.Series(
        {"tp2": 102.0, "tp10": 110.0, "tp_pct": 0.25, "tp1": 101.0, "take_profit": 99.0}
    )

    _, take_profit_levels = engine._extract_levels(row)

    assert [level["name"] for level in take_profit_levels] == ["tp", "tp1", "tp2", "tp10"]
    assert take_profit_levels[0]["price"] == 99.0
    assert take_profit_levels[0]["fraction"] == pytest.approx(0.25)


def test_native_engine_ignores_non_decimal_tp_suffix_columns():
    # Regression guard: str.isdigit() accepts superscripts such as "²", which
    # int() rejects. Such columns must be ignored, not crash the sort key.
    engine = _engine()
    row = pd.Series({"tp1": 101.0, "tp²": 102.0, "tp2": 103.0, "tp²_pct": 0.9})

    stop_loss, take_profit_levels = engine._extract_levels(row)

    assert stop_loss is None
    assert [level["name"] for level in take_profit_levels] == ["tp1", "tp2"]
    assert [level["price"] for level in take_profit_levels] == [101.0, 103.0]
    assert [level["fraction"] for level in take_profit_levels] == [
        pytest.approx(0.5),
        pytest.approx(0.5),
    ]
