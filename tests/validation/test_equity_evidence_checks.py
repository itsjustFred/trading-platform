"""Two readiness and evidence checks that were satisfied by anything non-null.

    Why this file exists

    2026-10-15, third batch of `validation/`'s unexamined money functions. Two mutations, both
    surviving the full suite at 5,021 tests:

        M31  `_check_equity_evidence`: any non-null status counts as available    SURVIVED
        M32  `_equity_curve_from_evidence`: points with more than 5 fields accepted SURVIVED

    **M31 is the third instance of one failure mode**, and that is what makes it worth a card of its
    own rather than a backlog line:

        af68e42   a NaN risk figure made every check that read it agree
        8001cf9   deltas carrying no evidence reported as complete and passing
        8001cf9   the report's verdict taken without checking any field delta
        now       a status of "missing" or "incomplete" counted as evidence *present*

    Every one of these is the validator agreeing with something it has not actually read. None of them
    produces a wrong number. All of them produce a green tick, which is the output a reader acts on.
    That is the shape to watch for in this codebase: not bad arithmetic, but assent.

    **M32** is smaller and is the mirror image — the check that refuses malformed input stops
    refusing it, so bad evidence flows into an equity curve instead of raising.

    The fourth mutation in this batch, `_resolve_stop_level` never resolving a stop from a canonical
    field or a legacy key, was killed by 10 existing tests and needed nothing here.
"""

from __future__ import annotations

import pytest

from validation.importers.quantconnect_result import QuantConnectResultImporter
from validation.readiness.validator import ReadinessValidator


# --- M31: "a status exists" is not "the evidence exists" ------------------------


@pytest.mark.parametrize("status", ["missing", "incomplete", "partial", "unavailable", "", "?"])
def test_a_non_available_equity_status_fails_readiness(status: str) -> None:
    """The check must read the status, not merely notice that someone supplied one."""
    recommendations: list[str] = []
    checks = ReadinessValidator._check_equity_evidence(status, recommendations)
    assert len(checks) == 1
    check = checks[0]
    assert check.passed is False, (
        f"status {status!r} is not 'available', so readiness must not be certified on it"
    )
    assert check.severity == "error"
    assert recommendations, "a failing check must tell the reader what to provide"


def test_available_equity_evidence_passes() -> None:
    checks = ReadinessValidator._check_equity_evidence("available", [])
    assert [c.passed for c in checks] == [True]


def test_an_absent_status_produces_no_check_at_all() -> None:
    """`None` means the caller did not run the evidence step, which is not the same as failing it."""
    assert ReadinessValidator._check_equity_evidence(None, []) == []


# --- M32: malformed equity points are refused, not absorbed ---------------------


def _evidence(*points: object) -> dict:
    return {"points": [list(p) for p in points]}


def test_a_well_formed_equity_point_is_accepted() -> None:
    curve = QuantConnectResultImporter._equity_curve_from_evidence(
        _evidence([1_700_000_000, 0, 0, 0, 10_000.0])
    )
    assert len(curve) == 1
    assert curve[0].equity == pytest.approx(10_000.0)
    assert curve[0].time.endswith("Z")


@pytest.mark.parametrize(
    "point",
    [
        [1_700_000_000, 0, 0, 0],          # too few
        [1_700_000_000, 0, 0, 0, 1.0, 2],  # too many - M32
        [],                                 # empty
    ],
)
def test_a_malformed_equity_point_is_refused(point: list) -> None:
    with pytest.raises(ValueError, match="malformed"):
        QuantConnectResultImporter._equity_curve_from_evidence(_evidence(point))


def test_a_point_that_is_not_a_list_is_refused() -> None:
    with pytest.raises(ValueError, match="malformed"):
        QuantConnectResultImporter._equity_curve_from_evidence({"points": ["nope"]})  # type: ignore[dict-item]


def test_an_out_of_range_timestamp_is_refused() -> None:
    """Refused with its own message, so the reader learns which of the two problems it was."""
    with pytest.raises(ValueError, match="timestamp is out of range"):
        QuantConnectResultImporter._equity_curve_from_evidence(
            _evidence([10**30, 0, 0, 0, 10_000.0])
        )


def test_equity_is_read_from_the_last_field() -> None:
    """The equity value is element 4; reading a different one would plot the wrong quantity."""
    curve = QuantConnectResultImporter._equity_curve_from_evidence(
        _evidence([1_700_000_000, 111.0, 222.0, 333.0, 10_000.0])
    )
    assert curve[0].equity == pytest.approx(10_000.0)
    assert curve[0].equity not in (pytest.approx(111.0), pytest.approx(222.0), pytest.approx(333.0))
