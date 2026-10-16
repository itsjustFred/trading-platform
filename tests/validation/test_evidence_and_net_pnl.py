"""The validator's evidence layer: what it trusts, and what it must refuse to certify without.

    Why this file exists

    2026-10-15, second batch of `validation/`'s nineteen unexamined money functions. Three mutations,
    against the full suite:

        M27  an inconsistent net_pnl is trusted instead of recomputed   SURVIVED - 5,015 passed
        M28  the report's own verdict is taken without any field check  SURVIVED
        M29  deltas with no expected/actual evidence -> "complete", passing   SURVIVED

    **M29 is the important one.** It turns the validator's evidence check inside out: a report whose
    field deltas carry no numbers at all is reported as *complete* and *passing*, rather than as
    `empty`. A report that contains no evidence is not a report that found nothing wrong; it is a
    report that checked nothing, and the difference is the whole job.

    **M28** is the same failure one step up: `passed` taken from the report alone discards every field
    delta, so a report that claims success is believed even when one of its own comparisons failed.

    **M27** removes a deliberate guard with a stated reason. `_net_realized_pnl` only trusts an
    explicit `net_pnl` when it agrees with the exit PnLs minus fees; a trade that violates that
    invariant has fee-inclusive exit PnLs *and* a fees field, so trusting it double-counts the fees.
    The code logs a warning in exactly that case - and a warning nobody reads is not a control.

    All three tests assert on the function directly rather than through the rendered report, because
    the failure modes are about what the report *claims*, and a rendered report can look identical
    either way once an empty delta list has been summarised.
"""

from __future__ import annotations

import pytest

from shared.models import Trade, TradeExit
from validation.reporting.json_report import _extract_risk_deltas, _risk_evidence
from validation.risk.comparison import (
    FieldComparisonResult,
    RiskComparisonReport,
    RiskComparisonResult,
)
from validation.stats.calculator import _net_realized_pnl


def _exit(pnl: float, qty: float = 1.0) -> TradeExit:
    return TradeExit(time="2024-01-02T00:00:00Z", price=100.0, quantity=qty, pnl=pnl)


def _trade(**overrides) -> Trade:
    fields = {
        "id": 1,
        "symbol": "X",
        "direction": "LONG",
        "entry_time": "2024-01-01T00:00:00Z",
        "entry_price": 100.0,
        "quantity": 1.0,
        "exits": [_exit(100.0)],
        "pnl": 100.0,
    }
    fields.update(overrides)
    return Trade(**fields)


# --- M27: an inconsistent net_pnl must be recomputed, not trusted ----------------


def test_a_consistent_net_pnl_is_trusted() -> None:
    """The fast path still works: exits 100, fees 10, so net 90 is exactly consistent."""
    trade = _trade(exits=[_exit(100.0)], fees=10.0, net_pnl=90.0)
    assert _net_realized_pnl(trade) == pytest.approx(90.0)


def test_an_inconsistent_net_pnl_is_recomputed_not_trusted(caplog) -> None:
    """Exit PnLs are gross by contract, so 90 gross - 10 fees is a true net of 80.

    `net_pnl` claims 90. Trusting it would overstate this trade by exactly the fees, and a validator
    that overstates net PnL is overstating every aggregate built on top of it. The function logs a
    warning when it disagrees - and a warning nobody reads is not a control, so the log line is
    asserted as well.
    """
    trade = _trade(exits=[_exit(90.0)], pnl=90.0, fees=10.0, net_pnl=90.0)
    result = _net_realized_pnl(trade)
    assert result == pytest.approx(80.0)
    assert any("inconsistent" in r.getMessage() for r in caplog.records), (
        "the inconsistency is logged as a warning and nothing reads warnings; asserting it keeps the "
        "signal from being quietly removed alongside the guard"
    )


def test_no_net_pnl_means_gross_minus_fees() -> None:
    trade = _trade(exits=[_exit(100.0)], fees=10.0, net_pnl=None)
    assert _net_realized_pnl(trade) == pytest.approx(90.0)


# --- M28/M29: what counts as evidence, and who decides pass ----------------------


def _report(*, passed: bool, field_results: dict[str, FieldComparisonResult]) -> RiskComparisonReport:
    return RiskComparisonReport(
        trade_results=[
            RiskComparisonResult(passed=passed, field_results=field_results),
        ],
        summary={},
        passed=passed,
        explanation="",
    )


def test_a_failing_field_delta_fails_the_evidence_even_when_the_report_claims_success() -> None:
    """M28. The report's own verdict is not evidence about its own contents."""
    report = _report(
        passed=True,
        field_results={
            "position_size": FieldComparisonResult(
                field="position_size", expected=1.0, actual=2.0,
                tolerance=(0.01, 0.0), passed=False, message="position size drift",
            )
        },
    )
    _, has_evidence, passed, _ = _risk_evidence(report)
    assert has_evidence
    assert not passed, (
        "a field delta that failed must fail the evidence, even when the report says it passed"
    )


def test_evidence_with_no_numbers_is_empty_not_complete() -> None:
    """M29. No expected and no actual means nothing was compared."""
    report = _report(
        passed=True,
        field_results={
            "position_size": FieldComparisonResult(
                field="position_size", expected=None, actual=None,
                tolerance=(0.01, 0.0), passed=True, message="not compared",
            )
        },
    )
    deltas = _extract_risk_deltas(report)
    assert deltas, "the delta is still reported, so the reader can see it exists"
    assert all(d.expected is None and d.actual is None for d in deltas)

    _, has_evidence, passed, status = _risk_evidence(report)
    assert not has_evidence, "deltas carrying no figures are not evidence"
    assert not passed
    assert status == "empty"


def test_a_missing_report_is_not_evidence() -> None:
    _, has_evidence, passed, status = _risk_evidence(None)
    assert not has_evidence
    assert not passed
    assert status == "missing"
