"""Acceptance tests for #131: trade matching is a global assignment, not greedy.

The greedy matcher let an earlier reference take the actual trade that is the
only feasible partner of a later reference, reporting one missing and one
extra although a complete 1:1 matching exists. The matcher must first
maximise the number of matched pairs, then minimise the total cost, and the
result must not depend on the input order.
"""

from __future__ import annotations

from shared.models import Trade, TradeExit
from validation.comparison.matcher import TradeMatcher


def _trade(trade_id: int, entry: str, exit_: str, *, price: float = 100.0, qty: float = 1.0,
           direction: str = "LONG", symbol: str = "EURUSD") -> Trade:
    return Trade(
        id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_time=entry,
        entry_price=price,
        quantity=qty,
        exits=[TradeExit(time=exit_, price=price, quantity=qty)],
        pnl=0.0,
    )


# ref_a is feasible with both actuals and cheaper with act_shared;
# ref_b is feasible only with act_shared (act_only_a is 110 s away from it).
REF_A = _trade(1, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z")
REF_B = _trade(2, "2024-01-02T10:01:00Z", "2024-01-02T11:01:00Z")
ACT_SHARED = _trade(101, "2024-01-02T10:00:30Z", "2024-01-02T11:00:30Z")
ACT_ONLY_A = _trade(102, "2024-01-02T09:59:10Z", "2024-01-02T10:59:10Z")
TOLERANCE = {"time_tolerance_seconds": 60}


def _pairs(result) -> set[tuple[int, int]]:
    return {(ref.id, act.id) for ref, act, _key in result.matched}


def test_global_assignment_matches_both_where_greedy_leaves_one_missing() -> None:
    result = TradeMatcher.match([REF_A, REF_B], [ACT_SHARED, ACT_ONLY_A], **TOLERANCE)

    assert _pairs(result) == {(1, 102), (2, 101)}
    assert result.missing == []
    assert result.extra == []


def test_result_is_independent_of_reference_order() -> None:
    forward = TradeMatcher.match([REF_A, REF_B], [ACT_SHARED, ACT_ONLY_A], **TOLERANCE)
    reversed_refs = TradeMatcher.match([REF_B, REF_A], [ACT_SHARED, ACT_ONLY_A], **TOLERANCE)

    assert _pairs(forward) == _pairs(reversed_refs)


def test_result_is_independent_of_actual_order() -> None:
    forward = TradeMatcher.match([REF_A, REF_B], [ACT_SHARED, ACT_ONLY_A], **TOLERANCE)
    reversed_acts = TradeMatcher.match([REF_A, REF_B], [ACT_ONLY_A, ACT_SHARED], **TOLERANCE)

    assert _pairs(forward) == _pairs(reversed_acts)


def test_lowest_total_cost_wins_when_cardinality_is_equal() -> None:
    """Two refs, two actuals, both cross-pairings feasible: pick the cheaper total."""
    ref_1 = _trade(1, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z")
    ref_2 = _trade(2, "2024-01-02T10:00:40Z", "2024-01-02T11:00:40Z")
    act_1 = _trade(101, "2024-01-02T10:00:05Z", "2024-01-02T11:00:05Z")
    act_2 = _trade(102, "2024-01-02T10:00:35Z", "2024-01-02T11:00:35Z")

    for refs in ([ref_1, ref_2], [ref_2, ref_1]):
        for acts in ([act_1, act_2], [act_2, act_1]):
            result = TradeMatcher.match(refs, acts, **TOLERANCE)
            assert _pairs(result) == {(1, 101), (2, 102)}


def test_solver_sentinel_fill_is_never_reported_as_a_match() -> None:
    """A component whose maximum matching is smaller than its dimensions.

    ``ref_b``/``ref_c`` can only pair with ``act_x``, so at most two of the
    three pairs can be matched even though three refs and three actuals exist.
    The rectangular solver is forced to fill the leftover row and column with
    its big-M sentinel; reporting that sentinel would invent a match between
    an infeasible pair.
    """
    ref_a = _trade(1, "2024-01-02T10:00:00Z", "2024-01-02T10:01:00Z")
    ref_b = _trade(2, "2024-01-02T10:00:00Z", "2024-01-02T10:00:00Z")
    ref_c = _trade(3, "2024-01-02T10:00:00Z", "2024-01-02T10:00:00Z")
    act_x = _trade(101, "2024-01-02T10:00:00Z", "2024-01-02T10:00:00Z")
    act_y = _trade(102, "2024-01-02T10:00:00Z", "2024-01-02T10:01:30Z")
    act_z = _trade(103, "2024-01-02T10:00:00Z", "2024-01-02T10:02:00Z")

    result = TradeMatcher.match([ref_a, ref_b, ref_c], [act_x, act_y, act_z], **TOLERANCE)

    pairs = _pairs(result)
    assert len(result.matched) == 2
    # ref_a's cheapest feasible partner is act_y; one of ref_b/ref_c takes act_x.
    assert (1, 102) in pairs
    assert len(pairs & {(2, 101), (3, 101)}) == 1
    assert len(pairs) == 2
    assert len(result.missing) == 1
    assert [t.id for t in result.extra] == [103]


def test_content_identical_duplicates_pair_by_id_in_every_input_order() -> None:
    """Exact duplicates still pair by id, so the pairing cannot depend on order."""
    ref_1 = _trade(1, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z")
    ref_2 = _trade(2, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z")
    act_101 = _trade(101, "2024-01-02T10:00:05Z", "2024-01-02T11:00:05Z")
    act_102 = _trade(102, "2024-01-02T10:00:05Z", "2024-01-02T11:00:05Z")

    for refs in ([ref_1, ref_2], [ref_2, ref_1]):
        for acts in ([act_101, act_102], [act_102, act_101]):
            result = TradeMatcher.match(refs, acts, **TOLERANCE)
            assert _pairs(result) == {(1, 101), (2, 102)}


def test_infeasible_pairs_are_never_matched() -> None:
    ref = _trade(1, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z")
    wrong_direction = _trade(101, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z", direction="SHORT")
    wrong_symbol = _trade(102, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z", symbol="GBPUSD")
    too_far = _trade(103, "2024-01-02T10:05:00Z", "2024-01-02T11:00:00Z")

    result = TradeMatcher.match([ref], [wrong_direction, wrong_symbol, too_far], **TOLERANCE)

    assert result.matched == []
    assert [t.id for t in result.missing] == [1]
    assert sorted(t.id for t in result.extra) == [101, 102, 103]


def test_exact_ties_are_deterministic_by_canonical_order() -> None:
    """An exact tie resolves by canonical order, not by the input position."""
    ref = _trade(1, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z")
    twin_102 = _trade(102, "2024-01-02T10:00:10Z", "2024-01-02T11:00:10Z")
    twin_101 = _trade(101, "2024-01-02T10:00:10Z", "2024-01-02T11:00:10Z")

    result = TradeMatcher.match([ref], [twin_102, twin_101], **TOLERANCE)

    assert _pairs(result) == {(1, 101)}
    assert [t.id for t in result.extra] == [102]


def test_matched_list_keeps_reference_order_and_missing_and_extra_keep_input_order() -> None:
    refs = [REF_A, REF_B, _trade(3, "2024-03-01T10:00:00Z", "2024-03-01T11:00:00Z")]
    acts = [ACT_SHARED, _trade(103, "2024-04-01T10:00:00Z", "2024-04-01T11:00:00Z"), ACT_ONLY_A]

    result = TradeMatcher.match(refs, acts, **TOLERANCE)

    assert [ref.id for ref, _act, _key in result.matched] == [1, 2]
    assert [t.id for t in result.missing] == [3]
    assert [t.id for t in result.extra] == [103]


def test_more_references_than_actuals_matches_only_the_actuals() -> None:
    """Two refs compete for one actual: one match, one missing, no extra.

    This is the ``ref_count > act_count`` direction of the objective, where
    the rectangular solver must leave one reference unassigned.  Which of the
    two content-identical refs wins an exact cost tie is solver-dependent, so
    only the cardinality and the identity of the matched actual are pinned.
    """
    ref_a = _trade(1, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z")
    ref_b = _trade(2, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z")
    shared = _trade(101, "2024-01-02T10:00:10Z", "2024-01-02T11:00:10Z")

    for refs in ([ref_a, ref_b], [ref_b, ref_a]):
        result = TradeMatcher.match(refs, [shared], **TOLERANCE)

        assert len(result.matched) == 1
        assert len(result.missing) == 1
        assert result.extra == []
        assert result.matched[0][1].id == 101
        assert sorted([result.matched[0][0].id, result.missing[0].id]) == [1, 2]


def test_huge_weights_take_the_rescale_path_and_keep_the_global_optimum() -> None:
    """Each cost is finite but their sum overflows, so big-M is rescaled.

    With ``time_weight=1e308`` the three feasible costs are ~5e307, ~8.3e307
    and ~5e307: finite one by one, ``inf`` once summed. The rescaled solve
    must still find the same global assignment as the unscaled case.
    """
    for refs in ([REF_A, REF_B], [REF_B, REF_A]):
        for acts in ([ACT_SHARED, ACT_ONLY_A], [ACT_ONLY_A, ACT_SHARED]):
            result = TradeMatcher.match(refs, acts, time_weight=1.0e308, **TOLERANCE)
            assert _pairs(result) == {(1, 102), (2, 101)}
            assert result.missing == []
            assert result.extra == []


def test_huge_weights_never_hand_inf_to_the_solver(monkeypatch) -> None:
    """The rescale branch, not scipy's tolerance for ``inf``, keeps the matrix finite."""
    import numpy as np

    import validation.comparison.matcher as matcher_module

    matrices: list = []
    real_solver = matcher_module.linear_sum_assignment

    def spy(matrix):
        matrices.append(np.array(matrix, copy=True))
        return real_solver(matrix)

    monkeypatch.setattr(matcher_module, "linear_sum_assignment", spy)
    TradeMatcher.match([REF_A, REF_B], [ACT_SHARED, ACT_ONLY_A], time_weight=1.0e308, **TOLERANCE)

    assert matrices, "the dense solver was not called"
    assert all(np.isfinite(m).all() for m in matrices)


def test_nonfinite_price_keeps_order_independence() -> None:
    """NaN-price trades never match (their cost is non-finite) in any input order.

    The canonical-key clamp of non-finite prices only orders such trades; since
    they can never be matched, the outcome is the same with or without it.
    """
    nan_ref_1 = _trade(1, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z", price=float("nan"))
    nan_ref_2 = _trade(2, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z", price=float("nan"))
    act_1 = _trade(101, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z")
    act_2 = _trade(102, "2024-01-02T10:00:00Z", "2024-01-02T11:00:00Z")

    outcomes = set()
    for refs in ([nan_ref_1, nan_ref_2], [nan_ref_2, nan_ref_1]):
        for acts in ([act_1, act_2], [act_2, act_1]):
            result = TradeMatcher.match(refs, acts, **TOLERANCE)
            outcomes.add(
                (
                    frozenset(_pairs(result)),
                    frozenset(t.id for t in result.missing),
                    frozenset(t.id for t in result.extra),
                )
            )
    assert len(outcomes) == 1


def test_empty_inputs() -> None:
    assert TradeMatcher.match([], []).matched == []
    only_ref = TradeMatcher.match([REF_A], [], **TOLERANCE)
    assert [t.id for t in only_ref.missing] == [1]
    only_act = TradeMatcher.match([], [ACT_SHARED], **TOLERANCE)
    assert [t.id for t in only_act.extra] == [101]
