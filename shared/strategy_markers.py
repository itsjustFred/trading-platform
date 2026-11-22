"""Shared builder for strategy chart-marker (``drawings``) tails.

Strategies return ``(entries, exits, indicators_dict, drawings)``; the
viewer renders each drawing dict as a chart marker. Every strategy that
emits entry/exit markers needs the same tail: UTC-parse the ``timestamp``
column at the signal bars and append capped ``{"type": "marker", ...}``
dicts. That tail used to be copy-pasted across the strategy files
(issue #264 / SonarCloud duplicated-blocks); it now lives here.

Pure refactor: the produced dicts keep the exact key order and values the
inline tails produced, so serialized backtest output is unchanged.

Timestamp parsing deliberately keeps BOTH legacy semantics: the
entry/exit tails parsed only each mask's own rows
(``df.loc[mask[mask].index, "timestamp"]``) while the multi-group tails
(channel_breakout, qg) parsed the whole column first. ``parse="subset"``
reproduces the former — pandas infers the datetime format from the
group's rows alone, so a mixed-format column whose first row is not a
signal row cannot poison the parse — and ``parse="column"`` the latter.
"""

from __future__ import annotations

from typing import Iterable, Literal

import pandas as pd

# A marker group is ``(mask, position, shape, color, text, limit)`` where
# ``mask`` is a boolean Series aligned to ``df.index`` and ``limit`` caps how
# many of the earliest matching bars get a marker.
MarkerGroup = tuple[pd.Series, str, str, str, str, int]

MarkerParse = Literal["column", "subset"]


def _marker(position: str, timestamp: pd.Timestamp, shape: str, color: str, text: str) -> dict:
    return {
        "type": "marker",
        "time": timestamp.isoformat(),
        "position": position,
        "shape": shape,
        "color": color,
        "text": text,
    }


def signal_markers(
    df: pd.DataFrame,
    groups: Iterable[MarkerGroup],
    *,
    parse: MarkerParse = "column",
) -> list[dict]:
    """Build marker drawings for boolean signal masks.

    Returns ``[]`` when ``df`` has no ``timestamp`` column (the same guard
    every strategy tail applied). Each group's markers are emitted in frame
    order, capped at ``limit`` per group, in the order the groups are given.

    ``parse`` selects the timestamp parse scope: ``"column"`` parses the
    whole ``timestamp`` column once (the channel_breakout/qg tails), while
    ``"subset"`` parses only each group's masked rows — the pre-refactor
    entry/exit tails, which keeps mixed-format string columns working when
    the odd rows are not signal rows.
    """
    if parse not in ("column", "subset"):
        raise ValueError(f"parse must be 'column' or 'subset', got {parse!r}")
    if "timestamp" not in df.columns:
        return []
    drawings: list[dict] = []
    if parse == "subset":
        for mask, position, shape, color, text, limit in groups:
            times = pd.to_datetime(
                df.loc[mask[mask].index, "timestamp"], utc=True, errors="coerce"
            ).dropna()
            drawings.extend(
                _marker(position, t, shape, color, text) for t in times.iloc[:limit]
            )
        return drawings
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for mask, position, shape, color, text, limit in groups:
        drawings.extend(
            _marker(position, t, shape, color, text)
            for t in ts.loc[mask].dropna().iloc[:limit]
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
    Timestamps are parsed per group on the masked rows (``parse="subset"``),
    matching the tails these strategies carried before the extraction.
    """
    return signal_markers(
        df,
        [
            (entries, "belowBar", "arrowUp", "#22c55e", entry_text, limit),
            (exits, "aboveBar", "arrowDown", "#ef4444", exit_text, limit),
        ],
        parse="subset",
    )
