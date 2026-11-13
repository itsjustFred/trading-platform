"""Broker server clocks: map UTC bar times onto an MT5 broker's server wall clock.

MT5 strategy-tester logic runs on *server time* (``TimeCurrent()``): an EA's
``"16:30"`` entry time, its day boundaries and the D1 bars behind ``iATR(...,
PERIOD_D1, ...)`` are all defined on the broker's clock, not on UTC and not on
any civil time zone the broker happens to be near. Reproducing such a backtest
from UTC data therefore needs the broker clock itself, including the dates on
which it changed its UTC offset.

A clock is a list of :class:`ClockSegment` s, each valid from an UTC instant
until the next segment starts. A segment's server wall time is the wall time of
an IANA *reference zone* plus a fixed number of hours, which expresses the
common broker conventions exactly:

* ``America/New_York`` + 7 h - "New York close = 00:00": UTC+2 in winter, UTC+3
  while the US observes DST (US switch dates, not the EU ones). There is no IANA
  zone with this rule, so it is modelled as New York wall time shifted by 7 h,
  which inherits every US DST date from the tz database rather than restating
  it.
* ``Europe/Athens`` + 0 h - EET/EEST, UTC+2/+3 on EU switch dates.
* ``UTC`` + 2 h - a fixed UTC+2 clock without DST.

A segment also records whether the broker's D1 history in that period contains
Saturday/Sunday bars (``weekend_d1_bars``). On a clock where the week opens at
00:00 Monday server time no weekend bars arise; on a clock that does not move
with the market's DST, the first hour of the week can fall on Sunday server
time, and whether the broker's D1 series then carries a short Sunday bar is a
property of that broker's history, which only the history itself can tell.

The profiles below are *empirical*: they are what the broker's tester history
shows, not what the broker documents. Their evidence is recorded beside each
profile and in
``strategy-comparisons/intraday_volatility_breakout/audit/README.md``.

The conversion produces *naive* server wall-clock timestamps. pandas does not
reliably accept a custom ``tzinfo`` for vectorised conversion, and a broker
clock is not a civil time zone anyway; naive wall time is what MT5 itself
reports (``TimeCurrent()`` carries no zone).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ClockSegment:
    """One stretch of a broker clock.

    ``start_utc`` is the first UTC instant (inclusive) the segment applies to;
    ``None`` means "since the beginning of time". The segment runs until the
    next segment's ``start_utc`` (exclusive).
    """

    start_utc: pd.Timestamp | None
    reference_zone: str
    shift_hours: int
    weekend_d1_bars: bool
    note: str = ""


@dataclass(frozen=True)
class BrokerClock:
    """A broker server clock as an ordered list of :class:`ClockSegment` s."""

    name: str
    segments: tuple[ClockSegment, ...]
    description: str = ""

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError(f"broker clock {self.name!r} has no segments")
        if self.segments[0].start_utc is not None:
            raise ValueError(
                f"broker clock {self.name!r}: the first segment must be open-ended (start_utc=None)"
            )
        starts = [_as_utc(seg.start_utc) for seg in self.segments[1:]]
        if any(s is None for s in starts):
            raise ValueError(
                f"broker clock {self.name!r}: only the first segment may be open-ended"
            )
        if any(later <= earlier for earlier, later in zip(starts, starts[1:])):
            raise ValueError(
                f"broker clock {self.name!r}: segment starts must be strictly increasing"
            )

    def _segment_positions(self, utc: pd.DatetimeIndex) -> np.ndarray:
        if len(self.segments) == 1:
            return np.zeros(len(utc), dtype=int)
        # Both sides in nanoseconds: ``asi8`` is unit-dependent (pandas infers s/us/ns).
        boundaries = pd.DatetimeIndex(
            [_as_utc(seg.start_utc) for seg in self.segments[1:]]
        ).as_unit("ns")
        # side="right": an instant exactly at a boundary belongs to the new segment.
        return np.searchsorted(boundaries.asi8, utc.as_unit("ns").asi8, side="right")

    def offset_hours(self, timestamps) -> np.ndarray:
        """Server-minus-UTC offset in hours for every UTC timestamp."""
        utc = _to_utc_index(timestamps)
        positions = self._segment_positions(utc)
        offsets = np.full(len(utc), np.nan)
        for position, segment in enumerate(self.segments):
            mask = positions == position
            if not mask.any():
                continue
            part = utc[mask]
            if segment.reference_zone.upper() == "UTC":
                base = np.zeros(len(part))
            else:
                local = part.tz_convert(segment.reference_zone).tz_localize(None)
                base = (local - part.tz_localize(None)).total_seconds().to_numpy() / 3600.0
            offsets[mask] = base + segment.shift_hours
        return offsets

    def to_server_time(self, timestamps) -> pd.DatetimeIndex:
        """Naive server wall-clock timestamps for UTC ``timestamps`` (NaT stays NaT)."""
        utc = _to_utc_index(timestamps)
        offsets = self.offset_hours(utc)
        return utc.tz_localize(None) + pd.to_timedelta(offsets, unit="h")

    def weekend_d1_bars(self, timestamps) -> np.ndarray:
        """Whether the broker's D1 history carries weekend bars at each UTC timestamp."""
        utc = _to_utc_index(timestamps)
        flags = np.array([seg.weekend_d1_bars for seg in self.segments], dtype=bool)
        return flags[self._segment_positions(utc)]


def _as_utc(value: pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _to_utc_index(timestamps) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True, errors="coerce"))
    # Nanosecond resolution so that ``asi8`` compares like for like with the segment bounds.
    return index.as_unit("ns")


def _utc(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

#: The canonical "New York close" clock: UTC+2/+3 on US DST dates, no exceptions.
NY_CLOSE = BrokerClock(
    name="ny_close",
    description=(
        "America/New_York + 7 h (UTC+2 / UTC+3 on US DST dates); 00:00 server = 17:00 New York."
    ),
    segments=(ClockSegment(None, "America/New_York", 7, weekend_d1_bars=True),),
)

# Dukascopy-demo-mt5-1 strategy-tester history, derived from the IntradayVolatilityBreakout
# tester logs 2018-01..2026-06 (EURUSD, USATECH.IDX, USA500.IDX): the EA logs the M1 open at
# 16:30 server time every day, which pins the server-minus-UTC offset against UTC M1 data, and
# its logged daily ATR pins the D1 bar boundaries (including whether weekend bars exist).
#
# * Legacy history (until the weekend of 2023-12-02/03): forex bars on Europe/Athens, index CFD
#   bars on a fixed UTC+2 (no DST at all; 0 contradicting days out of ~1,100 decided days per
#   index); the D1 series has no Sunday bars even where Athens time puts the first hour of the
#   week on Sunday 23:00 (US-DST-only weeks in March / late October).
# * Transition (until the weekend of 2024-04-06/07): fixed UTC+2 for every symbol - the week of
#   2024-04-01 is still UTC+2 although both the EU and the US were on DST; Sunday 23:00 bars do
#   exist (2024-03-10/17/24/31).
# * Current history: New York close (UTC+2/+3 on US dates), except that the autumn-2024 switch
#   came a week late: 2024-11-04..08 is still UTC+3 (that week's Friday 16:00-17:00 New York
#   hour therefore lands on Saturday 00:00 server time, and the D1 history has that Saturday
#   bar).
#
# Instants inside closed-market weekends are not observable; the boundaries below sit on the
# weekend in which the change was observed (Saturday 00:00 UTC), and the 2024 late switch on
# the same wall-clock instant as the US rule, one week later. Any boundary between
# 2023-10-30 and 2024-03-10 is observationally equivalent for the legacy -> transition step;
# 2023-12-02 is where the broker's history starts to match the Dukascopy M1 feed tick-exactly.
_DUKASCOPY_TRANSITION_START = _utc("2023-12-02 00:00")
_DUKASCOPY_CURRENT_START = _utc("2024-04-06 00:00")
_DUKASCOPY_2024_LATE_SWITCH_START = _utc("2024-11-03 06:00")  # US fall-back instant (02:00 EDT)
_DUKASCOPY_2024_LATE_SWITCH_END = _utc("2024-11-10 07:00")  # one week later, 02:00 EST


def _dukascopy_segments(legacy_zone: str, legacy_shift: int) -> tuple[ClockSegment, ...]:
    return (
        ClockSegment(None, legacy_zone, legacy_shift, weekend_d1_bars=False, note="legacy history"),
        ClockSegment(
            _DUKASCOPY_TRANSITION_START,
            "UTC",
            2,
            weekend_d1_bars=True,
            note="transition: fixed UTC+2",
        ),
        ClockSegment(
            _DUKASCOPY_CURRENT_START,
            "America/New_York",
            7,
            weekend_d1_bars=True,
            note="New York close",
        ),
        ClockSegment(
            _DUKASCOPY_2024_LATE_SWITCH_START,
            "UTC",
            3,
            weekend_d1_bars=True,
            note="autumn-2024 switch one week late",
        ),
        ClockSegment(
            _DUKASCOPY_2024_LATE_SWITCH_END,
            "America/New_York",
            7,
            weekend_d1_bars=True,
            note="New York close",
        ),
    )


DUKASCOPY_MT5_FOREX = BrokerClock(
    name="dukascopy_mt5_forex",
    description="Dukascopy-demo MT5 tester history clock, forex (empirical, see docstring).",
    segments=_dukascopy_segments("Europe/Athens", 0),
)

DUKASCOPY_MT5_INDEX = BrokerClock(
    name="dukascopy_mt5_index",
    description="Dukascopy-demo MT5 tester history clock, index CFDs (empirical, see docstring).",
    segments=_dukascopy_segments("UTC", 2),
)

BROKER_CLOCKS: dict[str, BrokerClock] = {
    clock.name: clock for clock in (NY_CLOSE, DUKASCOPY_MT5_FOREX, DUKASCOPY_MT5_INDEX)
}


def resolve_broker_clock(name: object) -> BrokerClock | None:
    """Return the named clock, ``None`` for an empty name; unknown names raise ``ValueError``."""
    key = str(name or "").strip().lower()
    if not key:
        return None
    try:
        return BROKER_CLOCKS[key]
    except KeyError:
        raise ValueError(
            f"Unknown broker_clock {name!r}; known: {', '.join(sorted(BROKER_CLOCKS))}"
        ) from None
