"""Shared builder for strategy chart-marker (``drawings``) tails.

Strategies return ``(entries, exits, indicators_dict, drawings)``; the
viewer renders each drawing dict as a chart marker. Every strategy that
emits entry/exit markers needs the same tail: UTC-parse the ``timestamp``
column at the signal bars and append capped ``{"type": "marker", ...}``
dicts. That tail used to be copy-pasted across the strategy files
(issue #264 / SonarCloud duplicated-blocks); it now lives here.

Pure refactor: the produced dicts keep the exact key order and values the
inline tails produced, so serialized backtest output is unchanged.
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd

# A marker group is ``(mask, position, shape, color, text, limit)`` where
# ``mask`` is a boolean Series aligned to ``df.index`` and ``limit`` caps how
# many of the earliest matching bars get a marker.
MarkerGroup = tuple[pd.Series, str, str, str, str, int]


def signal_markers(df: pd.DataFrame, groups: Iterable[MarkerGroup]) -> list[dict]:
    """Build marker drawings for boolean signal masks.

    Returns ``[]`` when ``df`` has no ``timestamp`` column (the same guard
    every strategy tail applied). Each group's markers are emitted in frame
    order, capped at ``limit`` per group, in the order the groups are given.
    """
    if "timestamp" not in df.columns:
        return []
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    drawings: list[dict] = []
    for mask, position, shape, color, text, limit in groups:
        for t in ts.loc[mask].dropna().iloc[:limit]:
            drawings.append(
                {
                    "type": "marker",
                    "time": t.isoformat(),
                    "position": position,
                    "shape": shape,
                    "color": color,
                    "text": text,
                }
            )
    return drawings


def entry_exit_markers(
    df: pd.DataFrame,
    entries: pd.Series,
    exits: pd.Series,
    *,
    entry_text: str = "Entry",
    exit_text: str = "Exit",
    limit: int = 30,
) -> list[dict]:
    """Markers for the common two-group entry/exit tail.

    Entries render as a green ``arrowUp`` below the bar, exits as a red
    ``arrowDown`` above it — the style every single-direction strategy used.
    """
    return signal_markers(
        df,
        [
            (entries, "belowBar", "arrowUp", "#22c55e", entry_text, limit),
            (exits, "aboveBar", "arrowDown", "#ef4444", exit_text, limit),
        ],
    )
