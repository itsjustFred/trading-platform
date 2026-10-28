"""Where a symbol universe came from, and what that means for survivorship bias.

A backtest over "the S&P 500 constituents" is only meaningful if the reader
knows *which* constituents. Today this platform resolves a symbol list once, at
run time, and the artifact records only the resulting tickers: names that were
removed from (or delisted out of) the index before the run are absent from the
whole window, silently. A strategy looks better than it could have been, and
nothing in the artifact says so. The omission is doubly easy to miss because a
delisted ticker cannot be downloaded from today's constituent list at all —
the evidence is missing rather than wrong.

This module records the provenance and reports what the platform can honestly
observe about the resulting bias. Two rules shape every field:

* **The membership fact is recorded, not assumed.** :func:`universe_provenance`
  states who supplied the list, when the membership was determined, how many
  symbols it held, and whether it is point-in-time. This platform ships no
  historical index-membership source (``_PLATFORM_MEMBERSHIP_SOURCES`` is empty
  and stays that way until one is wired in), so a point-in-time claim can only
  ever be *declared* by the operator and cannot be re-derived here. The default
  — a list resolved at run time with no declared membership date — is recorded
  as ``undeclared``, never quietly promoted to ``point_in_time``.
* **Uncomputable is a value, not a gap.** :func:`survivorship_bias_diagnostic`
  reports the counts the OHLCV files can support (symbols with no data at the
  window start, symbols that only start mid-window, symbols whose data stops
  inside the window) and states plainly that the *magnitude* of survivorship
  bias is not computable here, because the missing members are, by
  construction, the symbols that are not in the universe. Anything that cannot
  be computed is ``None`` and named in ``undefined_metrics`` (the convention
  :mod:`runner.stats` already uses), never estimated.

Both functions are pure over their inputs; :func:`observe_symbol_coverage` is
the only part that touches the filesystem, and it returns a plain record per
symbol so the diagnostic can be tested without any data on disk.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

__all__ = [
    "MEMBERSHIP_CURRENT",
    "MEMBERSHIP_POINT_IN_TIME",
    "MEMBERSHIP_UNDECLARED",
    "PLATFORM_MEMBERSHIP_SOURCES",
    "PROVENANCE_VERSION",
    "UNIVERSE_PROVENANCE_KEY",
    "SymbolCoverage",
    "observe_symbol_coverage",
    "read_universe_provenance",
    "survivorship_bias_diagnostic",
    "universe_provenance",
    "unknown_universe_provenance",
]

#: Where the provenance lives inside a persisted ``BacktestResult``'s
#: ``meta.data``, next to ``execution_context``.
UNIVERSE_PROVENANCE_KEY = "universe_provenance"

#: Bumped whenever the provenance payload changes shape, so a reader can tell
#: an old artifact from a new one instead of reading missing keys as facts.
PROVENANCE_VERSION = 1

#: Membership was determined as of a declared date and is a point-in-time list.
MEMBERSHIP_POINT_IN_TIME = "point_in_time"
#: Membership is a current snapshot, i.e. today's list applied to past history.
MEMBERSHIP_CURRENT = "current"
#: The run does not say when the membership was determined. Not the same claim
#: as either of the above, and never rendered as one.
MEMBERSHIP_UNDECLARED = "undeclared"

MEMBERSHIP_BASES = (MEMBERSHIP_POINT_IN_TIME, MEMBERSHIP_CURRENT, MEMBERSHIP_UNDECLARED)

#: The membership sources this platform can resolve ITSELF. Empty on purpose:
#: nothing in ``data_tools/downloaders`` (yfinance, alpaca, dukascopy,
#: histdata, jforex, huggingface, dolthub) returns index membership as of a
#: past date, so a point-in-time universe can only be supplied by the operator.
#: While this is empty, no run can claim a membership fact it did not declare.
PLATFORM_MEMBERSHIP_SOURCES: tuple[str, ...] = ()

#: Default provider string: the list came from the run's own ``--symbols``
#: argument, which says nothing about when its membership was determined.
DEFAULT_UNIVERSE_PROVIDER = "cli:--symbols"

_MAX_LISTED_SYMBOLS = 20


def _as_timestamp(value: Any) -> pd.Timestamp | None:
    """Parse a timestamp, or None when the value cannot be read.

    None is the honest answer for a field a reader must not see invented: an
    unparseable as-of date makes the membership fact unknown, not "today".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed)


def _as_date_text(value: Any) -> str | None:
    timestamp = _as_timestamp(value)
    return None if timestamp is None else timestamp.date().isoformat()


def _normalize_symbols(symbols: Iterable[Any]) -> list[str]:
    """Unique, upper-cased symbols in first-seen order."""
    normalized: list[str] = []
    for raw_symbol in symbols or []:
        symbol = str(raw_symbol or "").strip().upper()
        if symbol and symbol not in normalized:
            normalized.append(symbol)
    return normalized


def _symbols_digest(symbols: Sequence[str]) -> str:
    """A stable digest of the universe, so the count cannot be read apart from
    the list it describes."""
    encoded = json.dumps(sorted(symbols), separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _capped(symbols: Sequence[str]) -> list[str]:
    listed = list(symbols[:_MAX_LISTED_SYMBOLS])
    if len(symbols) > _MAX_LISTED_SYMBOLS:
        listed.append(f"… +{len(symbols) - _MAX_LISTED_SYMBOLS} more")
    return listed


def universe_provenance(
    *,
    symbols: Iterable[Any],
    provider: str = DEFAULT_UNIVERSE_PROVIDER,
    as_of: Any = None,
    membership: str = MEMBERSHIP_UNDECLARED,
    point_in_time_source: str = "",
    resolved_at: Any = None,
    window_start: Any = None,
    window_end: Any = None,
) -> dict[str, Any]:
    """Record how a symbol universe was determined, and when.

    ``provider`` names where the list came from (by default the run's own
    ``--symbols`` argument, which is all this platform can say on its own).
    ``as_of`` is the date the membership was determined; ``resolved_at`` is
    when this run resolved the list (the run timestamp, unless given
    explicitly). ``membership`` is one of :data:`MEMBERSHIP_BASES`.

    Fails closed on two claims that cannot be true rather than on a bias:

    * declaring ``point_in_time`` without a readable ``as_of`` — a point-in-time
      claim with no date is unfalsifiable, and the whole point of the field is
      that a reader can check it;
    * declaring ``point_in_time`` with an ``as_of`` later than ``window_end`` —
      membership known only *after* the history it is applied to is
      look-ahead, which is a worse failure than survivorship bias because it is
      silent.

    Everything else is recorded, including the absence of a fact: a run with no
    declared membership date reports ``membership_basis="undeclared"`` and
    ``is_point_in_time=None`` instead of defaulting to the reassuring answer.
    """
    basis = str(membership or "").strip().lower()
    if basis not in MEMBERSHIP_BASES:
        raise ValueError(
            f"membership must be one of {MEMBERSHIP_BASES}, got {membership!r}"
        )

    as_of_ts = _as_timestamp(as_of)
    if as_of is not None and as_of_ts is None:
        raise ValueError(f"Universe as_of date is not a readable date: {as_of!r}")
    window_start_ts = _as_timestamp(window_start)
    window_end_ts = _as_timestamp(window_end)
    if window_start_ts is None and window_start is not None:
        raise ValueError(f"Backtest window start is not a readable date: {window_start!r}")
    if window_end_ts is None and window_end is not None:
        raise ValueError(f"Backtest window end is not a readable date: {window_end!r}")

    declared_source = str(point_in_time_source or "").strip()
    if basis == MEMBERSHIP_POINT_IN_TIME:
        if as_of_ts is None:
            raise ValueError(
                "membership='point_in_time' requires an as-of date: a "
                "point-in-time claim without a date cannot be checked by a reader."
            )
        if window_end_ts is not None and as_of_ts > window_end_ts:
            raise ValueError(
                f"membership='point_in_time' with as_of={as_of_ts.date().isoformat()} "
                f"after the backtest window end "
                f"({window_end_ts.date().isoformat()}) is look-ahead: membership "
                "known only after the history it is applied to cannot inform it."
            )
    if basis != MEMBERSHIP_POINT_IN_TIME and declared_source:
        # A source may only be claimed for the basis it can actually support.
        raise ValueError(
            f"point_in_time_source={declared_source!r} was given with "
            f"membership={basis!r}; declare membership='point_in_time' as well, or "
            "drop the source."
        )

    normalized = _normalize_symbols(symbols)
    resolved = _as_timestamp(resolved_at)
    undefined_metrics: list[str] = []
    if basis == MEMBERSHIP_UNDECLARED:
        # Two facts a reader would otherwise have to assume.
        undefined_metrics.extend(["as_of", "membership_basis"])
    if basis == MEMBERSHIP_POINT_IN_TIME:
        # A static list is point-in-time, but it does not model membership
        # *changes* inside the window.
        undefined_metrics.append("daily_membership")

    return {
        "version": PROVENANCE_VERSION,
        "provider": str(provider or "").strip() or DEFAULT_UNIVERSE_PROVIDER,
        "resolved_at": None if resolved is None else resolved.isoformat(),
        "as_of": None if as_of_ts is None else as_of_ts.date().isoformat(),
        "as_of_declared": as_of_ts is not None,
        "symbol_count": len(normalized),
        "symbols_sha256": _symbols_digest(normalized),
        "membership_basis": basis,
        "is_point_in_time": (
            True
            if basis == MEMBERSHIP_POINT_IN_TIME
            else (False if basis == MEMBERSHIP_CURRENT else None)
        ),
        "point_in_time_source": declared_source,
        "platform_membership_sources": list(PLATFORM_MEMBERSHIP_SOURCES),
        "window": {
            "start": None if window_start_ts is None else window_start_ts.isoformat(),
            "end": None if window_end_ts is None else window_end_ts.isoformat(),
        },
        "symbols": _capped(normalized),
        "disclosure": _disclosure(
            basis=basis,
            provider=str(provider or "").strip() or DEFAULT_UNIVERSE_PROVIDER,
            as_of=_as_date_text(as_of),
            resolved_at=None if resolved is None else resolved.isoformat(),
            point_in_time_source=declared_source,
        ),
        "undefined_metrics": sorted(undefined_metrics),
    }


def _disclosure(
    *,
    basis: str,
    provider: str,
    as_of: str | None,
    resolved_at: str | None,
    point_in_time_source: str,
) -> str:
    """The plain-language disclosure a reader needs before trusting a result.

    Deliberately not a boolean: "we used today's list" and "nobody said which
    list" are different facts with the same consequence, and a reader can only
    weigh the result if the artifact distinguishes them.
    """
    if basis == MEMBERSHIP_POINT_IN_TIME:
        source = point_in_time_source or "an undeclared source"
        return (
            f"The universe is a point-in-time membership snapshot as of {as_of}, "
            f"supplied by {source}, and is used unchanged for the whole window. "
            "Membership changes inside the window are NOT modelled: names added "
            f"after {as_of} are absent from the entire backtest, and names that "
            f"were members on {as_of} but removed later are present throughout — "
            "the mirror image of survivorship bias. This platform ships no "
            "historical index-membership source, so the snapshot cannot be "
            "re-derived or verified here; the claim rests entirely on the "
            "declared source."
        )
    if basis == MEMBERSHIP_CURRENT:
        return (
            f"The universe is the membership as of {as_of}, supplied by {provider}, "
            "applied to a window that starts earlier. Names removed from, or "
            f"delisted out of, that universe before {as_of} are absent from the "
            "entire backtest window, so the result is biased upward. The "
            "magnitude is not computable here: this platform ships no historical "
            "index-membership source, so the excluded members cannot be "
            "enumerated."
        )
    return (
        f"The symbol list was resolved from {provider} at {resolved_at or 'run time'}"
        " and the run makes no statement about when its membership was "
        "determined. This platform ships no historical (point-in-time) index-"
        "membership source. If the list came from a current constituents "
        "snapshot, every name removed from or delisted before "
        f"{as_of or resolved_at or 'the run'} is missing from the whole window "
        "and the result is biased upward; if it is a hand-picked list, "
        "membership was constant and the point-in-time concern does not apply. "
        "Which of the two is true is unknown, and is recorded as unknown rather "
        "than assumed."
    )


def read_universe_provenance(meta: Any) -> dict[str, Any] | None:
    """Read a persisted result's universe provenance, or None when it has none.

    None means "this artifact predates provenance recording", which callers
    report as unknown — never as a clean bill of health.
    """
    data = getattr(meta, "data", None)
    if not isinstance(data, Mapping):
        return None
    provenance = data.get(UNIVERSE_PROVENANCE_KEY)
    return dict(provenance) if isinstance(provenance, Mapping) else None


def unknown_universe_provenance(reason: str) -> dict[str, Any]:
    """The provenance of a run that did not record one.

    Used by readers (the optimizer) for legacy artifacts. The symbol count and
    window are deliberately absent rather than guessed: this shape asserts
    "not recorded", and a reader can tell it apart from a real record.
    """
    return {
        "version": None,
        "provider": "",
        "resolved_at": None,
        "as_of": None,
        "as_of_declared": False,
        "symbol_count": None,
        "symbols_sha256": "",
        "membership_basis": MEMBERSHIP_UNDECLARED,
        "is_point_in_time": None,
        "point_in_time_source": "",
        "platform_membership_sources": list(PLATFORM_MEMBERSHIP_SOURCES),
        "window": {"start": None, "end": None},
        "symbols": [],
        "disclosure": (
            "This run recorded no universe provenance, so nothing is known about "
            f"how or when its symbol list was determined ({reason}). Treat the "
            "membership as unknown, not as point-in-time."
        ),
        "undefined_metrics": ["as_of", "membership_basis", "symbol_count"],
    }


@dataclass(frozen=True)
class SymbolCoverage:
    """What the OHLCV files say about one symbol's presence in time.

    ``first_bar``/``last_bar`` are the bounds of the stored frame, which is the
    only listing evidence this platform has. They are *not* listing dates: a
    missing download and a late IPO are indistinguishable here, and a reader
    must not treat these as either.
    """

    symbol: str
    first_bar: pd.Timestamp | None
    last_bar: pd.Timestamp | None
    path: str = ""

    @property
    def observed(self) -> bool:
        return self.first_bar is not None and self.last_bar is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "first_bar": None if self.first_bar is None else self.first_bar.isoformat(),
            "last_bar": None if self.last_bar is None else self.last_bar.isoformat(),
            "path": self.path,
        }


def observe_symbol_coverage(
    symbols: Iterable[Any],
    *,
    timeframe: str,
    data_dir: str | Path = "",
    data_source: str = "",
) -> dict[str, SymbolCoverage | None]:
    """Read each symbol's stored bar bounds, or None when they cannot be read.

    Reads only parquet metadata (row-group statistics, with a boundary-row
    fallback already implemented in :mod:`data_tools.auto_download`), so this
    stays cheap for a full index-sized universe. A symbol with no frame, an
    unreadable frame, or a source filter that matches nothing is reported as
    ``None`` — "unknown", never "listed later".
    """
    from data_tools.auto_download import ohlcv_timestamp_bounds
    from runner.engines import contract

    observations: dict[str, SymbolCoverage | None] = {}
    for symbol in _normalize_symbols(symbols):
        try:
            selected, _candidates = contract.select_ohlcv_path(
                _data_root(data_dir), symbol, timeframe, data_source=data_source
            )
        except (ValueError, OSError):
            observations[symbol] = None
            continue
        if selected is None:
            observations[symbol] = None
            continue
        bounds = ohlcv_timestamp_bounds(selected)
        if bounds is None:
            observations[symbol] = None
            continue
        first_bar, last_bar = bounds
        observations[symbol] = SymbolCoverage(
            symbol=symbol,
            first_bar=first_bar,
            last_bar=last_bar,
            path=str(selected),
        )
    return observations


def _data_root(data_dir: str | Path) -> Path:
    """The OHLCV root, resolved the same way the engines resolve it."""
    from shared.constants import DATA_DIR

    text = str(data_dir or "").strip()
    return Path(text) if text else Path(DATA_DIR)


def survivorship_bias_diagnostic(
    *,
    symbols: Iterable[Any],
    coverage: Mapping[str, SymbolCoverage | None] | None,
    window_start: Any,
    window_end: Any,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Report what the stored data shows about a non-point-in-time universe.

    Three observable facts, each of which is a *symptom* of membership drift
    rather than a measurement of it:

    ``n_no_data_at_window_start``
        symbols whose first stored bar is after the window opened — there is no
        data covering the start of the window, so they could not have been
        traded then, whatever the universe claims. Each one is listed with the
        size of its gap (``lag_days``): a few days is an ordinary data start,
        a couple of years is a name that joined the universe later, and a bare
        count cannot tell those apart.
    ``n_started_mid_window`` / ``n_no_data_anywhere_in_window``
        the two halves of that set — symbols that did appear inside the window,
        and symbols with no bar in it at all.
    ``n_symbols_with_data_ending_before_window_end``
        symbols whose data stops before the window closed. A delisting and an
        unfinished download look identical here; this is reported as what it
        is, a data boundary.

    The direction of the bias follows the recorded membership basis (see
    :func:`universe_provenance`) and is reported as ``"unknown"`` when the run
    did not declare one. The *magnitude* is always ``None``: the symbols that
    carry the bias are the ones missing from the universe, so no computation
    over the present universe can bound it. ``bias_magnitude_status`` says so in
    words, and ``undefined_metrics`` names every quantity that stays unknown.
    """
    universe = _normalize_symbols(symbols)
    start_ts = _as_timestamp(window_start)
    end_ts = _as_timestamp(window_end)
    observed = dict(coverage or {})

    basis = str((provenance or {}).get("membership_basis") or MEMBERSHIP_UNDECLARED)
    if basis not in MEMBERSHIP_BASES:
        basis = MEMBERSHIP_UNDECLARED

    no_data_at_start: list[dict[str, Any]] = []
    started_mid_window: list[str] = []
    no_data_anywhere: list[str] = []
    ended_early: list[str] = []
    unobserved: list[str] = []
    with_data_at_start: list[str] = []
    for symbol in universe:
        record = observed.get(symbol)
        first_bar = getattr(record, "first_bar", None)
        last_bar = getattr(record, "last_bar", None)
        if first_bar is None or last_bar is None or start_ts is None or end_ts is None:
            unobserved.append(symbol)
            continue
        if first_bar > start_ts:
            lag_days = round((first_bar - start_ts).total_seconds() / 86400.0, 4)
            no_data_at_start.append(
                {"symbol": symbol, "first_bar": first_bar.isoformat(), "lag_days": lag_days}
            )
            if first_bar <= end_ts:
                started_mid_window.append(symbol)
            else:
                no_data_anywhere.append(symbol)
        else:
            with_data_at_start.append(symbol)
        if last_bar < end_ts:
            ended_early.append(symbol)

    denominator = len(universe)
    n_observed = denominator - len(unobserved)
    basis_known = basis != MEMBERSHIP_UNDECLARED
    if basis == MEMBERSHIP_CURRENT:
        bias_direction = "upward"
    elif basis == MEMBERSHIP_POINT_IN_TIME:
        bias_direction = "none_expected_from_membership"
    else:
        bias_direction = "unknown"

    undefined_metrics = [
        # A membership list fixed at one date cannot say who was a member on any
        # other date, so the true in-window membership is always unobserved.
        "in_window_membership",
        "listing_dates",
        "delisting_dates",
        "survivorship_bias_magnitude",
    ]
    if not basis_known:
        undefined_metrics.append("membership_basis")
    if unobserved:
        undefined_metrics.append("coverage_for_some_symbols")

    return {
        "n_symbols": denominator,
        "n_symbols_observed": n_observed,
        "n_symbols_unobserved": len(unobserved),
        "unobserved_symbols": _capped(sorted(unobserved)),
        "n_no_data_at_window_start": len(no_data_at_start),
        "no_data_at_window_start": no_data_at_start[:_MAX_LISTED_SYMBOLS],
        "n_max_symbols_listed_truncated": max(
            0, len(no_data_at_start) - _MAX_LISTED_SYMBOLS
        ),
        "max_first_bar_lag_days": max(
            (entry["lag_days"] for entry in no_data_at_start), default=None
        ),
        "n_started_mid_window": len(started_mid_window),
        "started_mid_window_symbols": _capped(sorted(started_mid_window)),
        "n_no_data_anywhere_in_window": len(no_data_anywhere),
        "no_data_anywhere_in_window_symbols": _capped(sorted(no_data_anywhere)),
        "n_symbols_with_data_ending_before_window_end": len(ended_early),
        "symbols_with_data_ending_before_window_end": _capped(sorted(ended_early)),
        "n_with_data_at_window_start": len(with_data_at_start),
        "fraction_no_data_at_window_start": (
            _ratio(len(no_data_at_start), n_observed) if n_observed else None
        ),
        "fraction_data_ending_before_window_end": (
            _ratio(len(ended_early), n_observed) if n_observed else None
        ),
        "membership_basis": basis,
        "is_point_in_time": (provenance or {}).get("is_point_in_time"),
        "bias_direction": bias_direction,
        "bias_magnitude": None,
        "bias_magnitude_status": "not_computable",
        "statement": _statement(
            basis=basis,
            n_symbols=denominator,
            n_absent=len(no_data_at_start),
            n_started=len(started_mid_window),
            n_nowhere=len(no_data_anywhere),
            n_ended=len(ended_early),
            n_unobserved=len(unobserved),
            n_observed=n_observed,
        ),
        "detectable": [
            "symbols in the universe with no data covering the start of the "
            "window, and how late their first bar is",
            "symbols that only appear after the window opened",
            "symbols with no bar in the window at all",
            "symbols whose stored data stops before the window closes",
        ],
        "not_detectable": [
            "symbols that were members of the index but are absent from this "
            "universe (never observable: the artifact cannot list what it does "
            "not contain, and they cannot be downloaded from a current list)",
            "listing and delisting dates, so a late first bar cannot be told "
            "apart from a missing download",
            "whether a data boundary is a delisting or an unfinished download",
            "the size of the resulting performance distortion",
        ],
        "caveat": (
            "fraction_no_data_at_window_start measures how far the recorded "
            "universe is from a point-in-time list, and lag_days separates an "
            "ordinary data start from a late arrival. Neither is an estimate of "
            "the bias: the symbols that drive the bias are the ones missing "
            "from the universe, and no fraction of what is present can bound "
            "them."
        ),
        "window": {
            "start": None if start_ts is None else start_ts.isoformat(),
            "end": None if end_ts is None else end_ts.isoformat(),
        },
        "undefined_metrics": sorted(set(undefined_metrics)),
    }


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    value = float(numerator) / float(denominator)
    return value if math.isfinite(value) else 0.0


def _statement(
    *,
    basis: str,
    n_symbols: int,
    n_absent: int,
    n_started: int,
    n_nowhere: int,
    n_ended: int,
    n_unobserved: int,
    n_observed: int,
) -> str:
    if not n_symbols:
        return "No symbols in the universe, so there is nothing to diagnose."
    parts = [
        f"Of {n_symbols} symbols, {n_observed} have readable OHLCV coverage and "
        f"{n_unobserved} do not.",
        f"{n_absent} have no data covering the start of the window: {n_started} "
        f"of them appear later inside it, {n_nowhere} have no bar in it at all.",
        f"{n_ended} have stored data that stops before the window closes.",
    ]
    if basis == MEMBERSHIP_CURRENT:
        parts.append(
            "The universe is a current membership snapshot, so members removed "
            "or delisted before that snapshot are absent from the entire window; "
            "the direction of the resulting bias is upward and its size is not "
            "computable from the data present."
        )
    elif basis == MEMBERSHIP_POINT_IN_TIME:
        parts.append(
            "The universe was declared point-in-time, so membership itself is not "
            "the source of bias; note that a list fixed at one date still cannot "
            "model membership changes inside the window."
        )
    else:
        parts.append(
            "The run declared no membership date, so the direction of the bias is "
            "unknown: if the list is a current snapshot the bias is upward, and "
            "if it is a fixed hand-picked list it does not arise. Size is not "
            "computable either way."
        )
    return " ".join(parts)
