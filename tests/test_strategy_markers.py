"""Unit tests for shared.strategy_markers (issue #264).

The helper must reproduce the exact marker dicts the per-strategy tails
emitted: same keys in the same order, UTC ISO times, per-group caps, and an
empty list when the frame has no ``timestamp`` column.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd

from shared.strategy_markers import entry_exit_markers, signal_markers


def _df(rows: int = 10) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
        }
    )


def test_entry_exit_markers_shape_and_order():
    df = _df()
    entries = pd.Series([True] * 3 + [False] * 7, index=df.index)
    exits = pd.Series([False] * 4 + [True] * 3 + [False] * 3, index=df.index)

    drawings = entry_exit_markers(df, entries, exits)

    # Serialized marker dicts must preserve the exact key order the inline
    # tails emitted (the viewer contract), so compare the key list itself.
    assert all(
        list(d) == ["type", "time", "position", "shape", "color", "text"]
        for d in drawings
    )
    assert drawings == [
        {
            "type": "marker",
            "time": "2024-01-01T00:00:00+00:00",
            "position": "belowBar",
            "shape": "arrowUp",
            "color": "#22c55e",
            "text": "Entry",
        },
        {
            "type": "marker",
            "time": "2024-01-01T01:00:00+00:00",
            "position": "belowBar",
            "shape": "arrowUp",
            "color": "#22c55e",
            "text": "Entry",
        },
        {
            "type": "marker",
            "time": "2024-01-01T02:00:00+00:00",
            "position": "belowBar",
            "shape": "arrowUp",
            "color": "#22c55e",
            "text": "Entry",
        },
        {
            "type": "marker",
            "time": "2024-01-01T04:00:00+00:00",
            "position": "aboveBar",
            "shape": "arrowDown",
            "color": "#ef4444",
            "text": "Exit",
        },
        {
            "type": "marker",
            "time": "2024-01-01T05:00:00+00:00",
            "position": "aboveBar",
            "shape": "arrowDown",
            "color": "#ef4444",
            "text": "Exit",
        },
        {
            "type": "marker",
            "time": "2024-01-01T06:00:00+00:00",
            "position": "aboveBar",
            "shape": "arrowDown",
            "color": "#ef4444",
            "text": "Exit",
        },
    ]


def test_entry_exit_markers_respects_limit_and_text():
    df = _df(50)
    entries = pd.Series(True, index=df.index)
    exits = pd.Series(False, index=df.index)

    drawings = entry_exit_markers(df, entries, exits, entry_text="Long", limit=5)

    assert len(drawings) == 5
    assert all(d["text"] == "Long" for d in drawings)
    assert drawings[-1]["time"] == "2024-01-01T04:00:00+00:00"


def test_signal_markers_multiple_groups_preserve_order():
    df = _df()
    mask = pd.Series([True, False] * 5, index=df.index)

    drawings = signal_markers(
        df,
        [
            (mask, "belowBar", "triangleUp", "#22c55e", "Long Signal", 30),
            (mask, "aboveBar", "triangleDown", "#ef4444", "Short Signal", 2),
        ],
    )

    assert [d["text"] for d in drawings] == ["Long Signal"] * 5 + ["Short Signal"] * 2
    assert drawings[5]["shape"] == "triangleDown"


def test_signal_markers_empty_without_timestamp_column():
    df = _df().drop(columns=["timestamp"])
    mask = pd.Series(True, index=df.index)

    assert signal_markers(df, [(mask, "belowBar", "arrowUp", "#fff", "x", 30)]) == []
    assert entry_exit_markers(df, mask, mask) == []


def test_signal_markers_drops_unparseable_timestamps():
    df = _df(4)
    df["timestamp"] = df["timestamp"].astype(str)
    df.loc[df.index[1], "timestamp"] = "not-a-date"
    mask = pd.Series(True, index=df.index)

    drawings = signal_markers(
        df, [(mask, "belowBar", "arrowUp", "#fff", "x", 30)]
    )

    assert [d["time"] for d in drawings] == [
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
        "2024-01-01T03:00:00+00:00",
    ]


def test_signal_markers_subset_parse_skips_unparseable_masked_rows():
    """``parse="subset"``: an unparseable timestamp ON a masked row is
    skipped and does not consume a ``limit`` slot (the pre-refactor tail's
    ``.dropna()`` before ``iloc[:limit]``)."""
    df = _df(5)
    df["timestamp"] = df["timestamp"].astype(str)
    df.loc[df.index[0], "timestamp"] = "garbage"  # masked AND unparseable
    mask = pd.Series(True, index=df.index)

    drawings = signal_markers(
        df,
        [(mask, "belowBar", "arrowUp", "#fff", "x", 2)],
        parse="subset",
    )

    # Without dropna the NaT row would emit a "NaT" marker and eat a slot.
    assert [d["time"] for d in drawings] == [
        "2024-01-01T01:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    ]


def test_signal_markers_column_parse_skips_unparseable_masked_rows():
    """``parse="column"``: same contract — a coerced-NaT masked row is
    skipped and does not count against ``limit``."""
    df = _df(5)
    df["timestamp"] = df["timestamp"].astype(str)
    df.loc[df.index[0], "timestamp"] = "garbage"
    mask = pd.Series(True, index=df.index)

    drawings = signal_markers(
        df,
        [(mask, "belowBar", "arrowUp", "#fff", "x", 2)],
        parse="column",
    )

    assert [d["time"] for d in drawings] == [
        "2024-01-01T01:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
    ]


def test_signal_markers_accepts_non_utc_input():
    df = _df(2)
    df["timestamp"] = [
        "2024-01-01 09:30-05:00",
        "2024-01-01 16:00-05:00",
    ]
    mask = pd.Series(True, index=df.index)

    drawings = signal_markers(
        df, [(mask, "belowBar", "arrowUp", "#fff", "x", 30)]
    )

    assert drawings[0]["time"] == "2024-01-01T14:30:00+00:00"
    assert drawings[1]["time"] == "2024-01-01T21:00:00+00:00"


def test_entry_exit_markers_subset_parse_survives_mixed_formats():
    """The pre-refactor entry/exit tails parsed only each mask's own rows,
    so pandas inferred the datetime format from the signal rows alone. A
    string column whose FIRST row uses a different format must not poison
    the parse of the ISO signal rows (whole-column parse coerces them all
    to NaT and silently drops every marker)."""
    df = _df(6)
    df["timestamp"] = [
        "01.01.2024 00:00",  # odd format on a non-signal row
        "2024-01-01T01:00:00Z",
        "2024-01-01T02:00:00Z",
        "2024-01-01T03:00:00Z",
        "2024-01-01T04:00:00Z",
        "2024-01-01T05:00:00Z",
    ]
    entries = pd.Series([False, True, False, False, True, False], index=df.index)
    exits = pd.Series([False, False, False, True, False, False], index=df.index)

    drawings = entry_exit_markers(df, entries, exits)

    assert [d["text"] for d in drawings] == ["Entry", "Entry", "Exit"]
    assert drawings[0]["time"] == "2024-01-01T01:00:00+00:00"
    assert drawings[2]["time"] == "2024-01-01T03:00:00+00:00"


def test_signal_markers_rejects_unknown_parse_mode():
    df = _df()
    mask = pd.Series(True, index=df.index)

    try:
        signal_markers(df, [(mask, "belowBar", "arrowUp", "#fff", "x", 30)], parse="bogus")
    except ValueError as exc:
        assert "parse" in str(exc)
    else:  # pragma: no cover - assertion path
        raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# Call-site contract: every strategy's marker arguments, pinned statically
# ---------------------------------------------------------------------------
#
# The characterisation fixtures pin marker OUTPUT on fixed frames, but a
# frame that never reaches a cap or a group cannot kill a mutant that
# changes the call-site constant (e.g. ``limit=200`` -> ``30`` or
# ``"Short Signal"`` -> ``"Short"``). These expectations pin the exact
# arguments each strategy passes to the shared helpers, so such mutants
# fail even when no fixture frame exercises them.

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# strategy -> ("entry_exit", entry_text, exit_text, limit)
_ENTRY_EXIT_EXPECTED = {
    name: ("Entry", "Exit", 30)
    for name in (
        "cross_asset_momentum",
        "ema_crossoiver_conversion_test",
        "gold_momentum",
        "intraday_volatility_breakout",
        "pine_superosc_vumanchu",
        "pine_supertrend_dema",
        "post_vix_dip_buy",
        "risk_parity",
        "sector_rotation",
        "short_term_reversal",
        "stat_arb_pairs",
        "trend_following",
        "volatility_breakout",
    )
}
_ENTRY_EXIT_EXPECTED.update(
    {
        "us100_scalper": ("Scalp Long", "Exit", 200),
        "mean_reversion_rsi_bb": ("Gap fade", "Exit", 200),
    }
)

# strategy -> [(position, shape, color, text, limit), ...]
_SIGNAL_EXPECTED = {
    "channel_breakout": [
        ("belowBar", "arrowUp", "#22c55e", "Long", 30),
        ("aboveBar", "arrowDown", "#f97316", "Short", 30),
        ("aboveBar", "arrowDown", "#ef4444", "Exit", 30),
    ],
    "qg_30min_es_breakout_strategy": [
        ("belowBar", "triangleUp", "#22c55e", "Long Signal", 200),
        ("aboveBar", "triangleDown", "#ef4444", "Short Signal", 200),
        ("aboveBar", "arrowDown", "#f59e0b", "Exit", 200),
    ],
}


def _marker_call(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"entry_exit_markers", "signal_markers"}
        ):
            return node.func.id, node
    return None, None


def _const(node):
    assert isinstance(node, ast.Constant), f"expected a literal, got {ast.dump(node)}"
    return node.value


def test_every_marker_call_site_matches_the_pinned_contract():
    seen: set[str] = set()
    for strategy_dir in sorted((_PROJECT_ROOT / "strategies").iterdir()):
        path = strategy_dir / f"{strategy_dir.name}.py"
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        if "strategy_markers" not in source:
            continue
        name = strategy_dir.name
        seen.add(name)
        helper, call = _marker_call(path)
        assert helper is not None, f"{name}: no marker helper call found"

        if helper == "entry_exit_markers":
            expected = _ENTRY_EXIT_EXPECTED.get(name)
            assert expected is not None, f"{name}: unexpected entry_exit_markers call"
            kwargs = {kw.arg: kw.value for kw in call.keywords}
            entry_text = _const(kwargs["entry_text"]) if "entry_text" in kwargs else "Entry"
            exit_text = _const(kwargs["exit_text"]) if "exit_text" in kwargs else "Exit"
            limit = _const(kwargs["limit"]) if "limit" in kwargs else 30
            assert (entry_text, exit_text, limit) == expected, (
                f"{name}: entry_exit_markers args changed: "
                f"{(entry_text, exit_text, limit)!r} != {expected!r}"
            )
        else:
            expected = _SIGNAL_EXPECTED.get(name)
            assert expected is not None, f"{name}: unexpected signal_markers call"
            groups_node = call.args[1]
            assert isinstance(groups_node, ast.List)
            groups = []
            for group in groups_node.elts:
                assert isinstance(group, ast.Tuple) and len(group.elts) == 6
                # elt 0 is the mask expression (covered by the
                # characterisation fixtures); pin the five constants.
                groups.append(tuple(_const(e) for e in group.elts[1:]))
            assert groups == expected, f"{name}: signal_markers groups changed"

    assert seen == set(_ENTRY_EXIT_EXPECTED) | set(_SIGNAL_EXPECTED), (
        f"marker call-site coverage drifted: {sorted(seen)}"
    )
