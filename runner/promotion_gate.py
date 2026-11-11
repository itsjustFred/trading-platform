"""SHA256-bound promotion gate: ``experimental`` versus ``production`` (#165).

A real-money account only accepts code that a named human reviewer promoted to
``production``. A promotion records three pieces of evidence and is bound to

* the SHA256 of the promoted file (a strategy file, or a signal bot's processor
  module), and
* the SHA256 of the canonicalised parameter set the deployment trades with.

Change one byte of the file or one bound parameter and the code is
``experimental`` again. There are only those two stages; paper/demo accounts
may run experimental code.

Required evidence (maintainer decision on #165):

1. **Out of sample** - an optimization result with a PASSED overfitting verdict
   from #164 (``three_way.overfitting.passed is True``), a minimum length of the
   untouched test segment and a minimum trade count in it.
2. **Paper trading** - a minimum duration and a minimum trade count.
3. **Parity** - cross-engine or reference parity within a tolerance.

The ledger lives next to the promoted file (``<file>.promoted.json``, in git),
following :mod:`runner.approval_gate`, whose hashing and atomic-write helpers
are reused here. One ledger can hold several promotions of the same file, one
per parameter set.

Everything fails closed. A missing file, a missing, unreadable or malformed
ledger, an unknown field shape, evidence that no longer meets the *current*
thresholds: all of them mean ``experimental``. The evidence is re-validated on
every check, so a ledger edited by hand to say something the checks would have
refused does not pass either.

This module only knows files, parameters and ledgers. What a live deployment
hashes and which parameters it binds is decided in
:mod:`live_runner.promotion`, which is also the single enforcement function.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from runner.approval_gate import _atomic_write_json, _relative_name, _sha256_of

LEDGER_SUFFIX = ".promoted.json"
LEDGER_VERSION = 1

STAGE_EXPERIMENTAL = "experimental"
STAGE_PRODUCTION = "production"

PromotionKind = Literal["strategy", "signal_bot"]
PROMOTION_KINDS: tuple[str, ...] = ("strategy", "signal_bot")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
#: Optional repository file overriding the threshold defaults. It is read on
#: every check; if it exists but is malformed, nothing is ``production``.
THRESHOLDS_FILE = PROJECT_ROOT / "promotion_thresholds.json"

#: Tolerated clock skew when rejecting evidence dated in the future.
_FUTURE_SKEW = timedelta(minutes=5)


class StrategyNotPromotedError(RuntimeError):
    """A real-money account was pointed at code that is not ``production``."""


class InvalidPromotionError(ValueError):
    """A promotion was refused: evidence, reviewer, parameters or thresholds are invalid."""


# --------------------------------------------------------------------------- #
# Small strict readers. A ledger is hand-editable JSON, so nothing is coerced:
# a field of the wrong type is a malformed ledger, not a value to interpret.
# --------------------------------------------------------------------------- #


def _require_text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidPromotionError(f"'{key}' must be a non-empty string")
    return value.strip()


def _require_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPromotionError(f"'{key}' must be an integer")
    return value


def _require_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidPromotionError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise InvalidPromotionError(f"{label} must be finite")
    return number


def _require_sha256(data: Mapping[str, Any], key: str) -> str:
    value = _require_text(data, key).lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise InvalidPromotionError(f"'{key}' must be a SHA256 hex digest")
    return value


def _parse_instant(value: Any, label: str) -> datetime:
    """Parse an ISO date or timestamp. A value without an offset is UTC."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidPromotionError(f"{label} must be an ISO date or timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidPromotionError(f"{label} is not a valid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _duration_days(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 86400.0


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #


def _canonical_value(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidPromotionError(f"parameter {path} is not a finite number")
        # 10 and 10.0 are the same parameter value; YAML, JSON and Python do
        # not agree on which one a config round-trips to.
        return int(value) if value.is_integer() else value
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidPromotionError(f"parameter {path} has a non-string key: {key!r}")
            canonical[key] = _canonical_value(item, f"{path}.{key}")
        return canonical
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise InvalidPromotionError(
        f"parameter {path} has unsupported type {type(value).__name__}; "
        "only JSON values can be bound to a promotion"
    )


def canonical_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """The parameter set in the one form that is hashed.

    Key order is irrelevant, integral floats collapse to ints, tuples are
    lists. Anything that is not plain JSON (NaN, objects, non-string keys) is
    refused rather than stringified, because a lossy rendering could make two
    different parameter sets hash alike.
    """
    if not isinstance(parameters, Mapping):
        raise InvalidPromotionError("parameters must be a mapping")
    return _canonical_value(parameters, "parameters")


def parameters_hash(parameters: Mapping[str, Any]) -> str:
    """SHA256 of the canonical JSON of ``parameters``."""
    text = json.dumps(
        canonical_parameters(parameters),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PromotionThresholds:
    """Minimum evidence for a promotion. Defaults confirmed by the maintainer (#165)."""

    #: Minimum length of the untouched out-of-sample test segment, in days.
    min_test_segment_days: int = 180
    #: Minimum number of trades in that test segment.
    min_test_trade_count: int = 50
    #: Minimum paper/demo trading duration, in days.
    min_paper_trading_days: int = 60
    #: Minimum number of paper/demo trades.
    min_paper_trade_count: int = 30
    #: Largest tolerated relative deviation in any parity metric (0.02 = 2 %).
    max_parity_deviation: float = 0.02

    def __post_init__(self) -> None:
        for name in (
            "min_test_segment_days",
            "min_test_trade_count",
            "min_paper_trading_days",
            "min_paper_trade_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise InvalidPromotionError(f"threshold {name} must be an integer >= 1")
        deviation = self.max_parity_deviation
        if (
            isinstance(deviation, bool)
            or not isinstance(deviation, (int, float))
            or not math.isfinite(deviation)
            or deviation < 0
        ):
            raise InvalidPromotionError("threshold max_parity_deviation must be a number >= 0")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Any) -> "PromotionThresholds":
        if not isinstance(data, Mapping):
            raise InvalidPromotionError("promotion thresholds must be a JSON object")
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            # A typo would otherwise silently leave the default in force while
            # the operator believes the threshold was changed.
            raise InvalidPromotionError(f"unknown promotion threshold(s): {', '.join(unknown)}")
        return cls(**dict(data))


def load_promotion_thresholds(path: Path | str | None = None) -> PromotionThresholds:
    """Thresholds in force: the defaults, overridden by ``promotion_thresholds.json``.

    The same thresholds apply when a promotion is recorded and every time it is
    checked. A file that exists but cannot be read is an error, never a silent
    fallback to the defaults.
    """
    source = Path(path) if path is not None else THRESHOLDS_FILE
    if not source.exists():
        return PromotionThresholds()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidPromotionError(f"promotion thresholds file is unreadable: {source}") from exc
    return PromotionThresholds.from_mapping(data)


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OutOfSampleEvidence:
    """The untouched test segment of a three-way optimization result (#164)."""

    optimization_id: str
    result_file: str
    result_sha256: str
    strategy_file: str
    #: Hash of the result's ``effective_best_parameters``.
    parameters_sha256: str
    test_start: str
    test_end: str
    test_trade_count: int
    overfitting_passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Any) -> "OutOfSampleEvidence":
        if not isinstance(data, Mapping):
            raise InvalidPromotionError("out-of-sample evidence must be an object")
        passed = data.get("overfitting_passed")
        if not isinstance(passed, bool):
            raise InvalidPromotionError("'overfitting_passed' must be a boolean")
        return cls(
            optimization_id=_require_text(data, "optimization_id"),
            result_file=_require_text(data, "result_file"),
            result_sha256=_require_sha256(data, "result_sha256"),
            strategy_file=_require_text(data, "strategy_file"),
            parameters_sha256=_require_sha256(data, "parameters_sha256"),
            test_start=_require_text(data, "test_start"),
            test_end=_require_text(data, "test_end"),
            test_trade_count=_require_int(data, "test_trade_count"),
            overfitting_passed=passed,
        )

    def problems(
        self,
        thresholds: PromotionThresholds,
        *,
        kind: str,
        file_name: str,
        parameters_sha256: str,
    ) -> list[str]:
        found: list[str] = []
        if self.overfitting_passed is not True:
            found.append("out of sample: the overfitting verdict did not pass")
        try:
            days = _duration_days(
                _parse_instant(self.test_start, "test_start"),
                _parse_instant(self.test_end, "test_end"),
            )
        except InvalidPromotionError as exc:
            found.append(f"out of sample: {exc}")
        else:
            if days < thresholds.min_test_segment_days:
                found.append(
                    f"out of sample: test segment is {days:.1f} days, "
                    f"required {thresholds.min_test_segment_days}"
                )
        if self.test_trade_count < thresholds.min_test_trade_count:
            found.append(
                f"out of sample: {self.test_trade_count} trades in the test segment, "
                f"required {thresholds.min_test_trade_count}"
            )
        if kind == "strategy":
            # The optimizer runs strategy files, so for a strategy the result
            # can and must be about this file and these parameters. A signal
            # bot's processor is not what the optimizer ran; see the runbook.
            if self.strategy_file != file_name:
                found.append(
                    f"out of sample: the result is for '{self.strategy_file}', "
                    f"not for '{file_name}'"
                )
            if self.parameters_sha256 != parameters_sha256:
                found.append(
                    "out of sample: the result's effective_best_parameters are not "
                    "the parameter set being promoted"
                )
        return found


@dataclass(frozen=True)
class PaperTradingEvidence:
    """A paper/demo trading run, backed by a report file."""

    account_id: str
    broker: str
    start: str
    end: str
    trade_count: int
    report_file: str
    report_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Any) -> "PaperTradingEvidence":
        if not isinstance(data, Mapping):
            raise InvalidPromotionError("paper-trading evidence must be an object")
        return cls(
            account_id=_require_text(data, "account_id"),
            broker=_require_text(data, "broker"),
            start=_require_text(data, "start"),
            end=_require_text(data, "end"),
            trade_count=_require_int(data, "trade_count"),
            report_file=_require_text(data, "report_file"),
            report_sha256=_require_sha256(data, "report_sha256"),
        )

    def problems(self, thresholds: PromotionThresholds, *, now: datetime) -> list[str]:
        found: list[str] = []
        try:
            start = _parse_instant(self.start, "start")
            end = _parse_instant(self.end, "end")
        except InvalidPromotionError as exc:
            found.append(f"paper trading: {exc}")
        else:
            if end > now + _FUTURE_SKEW:
                found.append("paper trading: the run ends in the future")
            days = _duration_days(start, end)
            if days < thresholds.min_paper_trading_days:
                found.append(
                    f"paper trading: ran {days:.1f} days, "
                    f"required {thresholds.min_paper_trading_days}"
                )
        if self.trade_count < thresholds.min_paper_trade_count:
            found.append(
                f"paper trading: {self.trade_count} trades, "
                f"required {thresholds.min_paper_trade_count}"
            )
        return found


@dataclass(frozen=True)
class ParityEvidence:
    """Cross-engine or reference parity: measured relative deviation per metric."""

    reference: str
    #: metric name -> relative deviation from the reference (0.02 = 2 %).
    deviations: Mapping[str, float]
    report_file: str
    report_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "deviations": dict(self.deviations),
            "report_file": self.report_file,
            "report_sha256": self.report_sha256,
        }

    @classmethod
    def from_mapping(cls, data: Any) -> "ParityEvidence":
        if not isinstance(data, Mapping):
            raise InvalidPromotionError("parity evidence must be an object")
        raw = data.get("deviations")
        if not isinstance(raw, Mapping) or not raw:
            raise InvalidPromotionError("'deviations' must name at least one compared metric")
        deviations: dict[str, float] = {}
        for metric, value in raw.items():
            if not isinstance(metric, str) or not metric.strip():
                raise InvalidPromotionError("parity metric names must be non-empty strings")
            deviations[metric] = _require_number(value, f"parity deviation '{metric}'")
        return cls(
            reference=_require_text(data, "reference"),
            deviations=deviations,
            report_file=_require_text(data, "report_file"),
            report_sha256=_require_sha256(data, "report_sha256"),
        )

    def problems(self, thresholds: PromotionThresholds) -> list[str]:
        found: list[str] = []
        if not self.deviations:
            found.append("parity: no metric was compared")
        for metric, deviation in sorted(self.deviations.items()):
            if abs(deviation) > thresholds.max_parity_deviation:
                found.append(
                    f"parity: {metric} deviates by {abs(deviation):.2%}, "
                    f"tolerance {thresholds.max_parity_deviation:.2%}"
                )
        return found


@dataclass(frozen=True)
class PromotionEvidence:
    """The complete evidence set. All three parts are mandatory by construction."""

    out_of_sample: OutOfSampleEvidence
    paper_trading: PaperTradingEvidence
    parity: ParityEvidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "out_of_sample": self.out_of_sample.to_dict(),
            "paper_trading": self.paper_trading.to_dict(),
            "parity": self.parity.to_dict(),
        }

    @classmethod
    def from_mapping(cls, data: Any) -> "PromotionEvidence":
        if not isinstance(data, Mapping):
            raise InvalidPromotionError("evidence must be an object")
        missing = [
            name for name in ("out_of_sample", "paper_trading", "parity") if name not in data
        ]
        if missing:
            raise InvalidPromotionError(f"evidence is incomplete, missing: {', '.join(missing)}")
        return cls(
            out_of_sample=OutOfSampleEvidence.from_mapping(data["out_of_sample"]),
            paper_trading=PaperTradingEvidence.from_mapping(data["paper_trading"]),
            parity=ParityEvidence.from_mapping(data["parity"]),
        )

    def problems(
        self,
        thresholds: PromotionThresholds,
        *,
        kind: str,
        file_name: str,
        parameters_sha256: str,
        now: datetime,
    ) -> list[str]:
        return [
            *self.out_of_sample.problems(
                thresholds, kind=kind, file_name=file_name, parameters_sha256=parameters_sha256
            ),
            *self.paper_trading.problems(thresholds, now=now),
            *self.parity.problems(thresholds),
        ]


def out_of_sample_evidence_from_result(result_path: Path | str) -> OutOfSampleEvidence:
    """Read the out-of-sample evidence out of an optimization result file.

    Nothing here is typed in by the promoter: the verdict, the test window and
    the trade count are taken from the file the optimizer wrote (#164), and the
    file's SHA256 is recorded so the ledger names exactly one result.
    """
    path = Path(result_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidPromotionError(f"optimization result is unreadable: {path}") from exc
    if not isinstance(payload, Mapping):
        raise InvalidPromotionError(f"optimization result is not a JSON object: {path}")

    three_way = payload.get("three_way")
    overfitting = three_way.get("overfitting") if isinstance(three_way, Mapping) else None
    if not isinstance(overfitting, Mapping) or not isinstance(overfitting.get("passed"), bool):
        raise InvalidPromotionError(
            "optimization result carries no three-way overfitting verdict "
            "(three_way.overfitting.passed); re-run the optimization with the "
            "three-way split"
        )
    test_period = payload.get("final_test_period")
    test_stats = payload.get("final_test_stats")
    if not isinstance(test_period, Mapping) or not isinstance(test_stats, Mapping):
        raise InvalidPromotionError(
            "optimization result has no untouched test segment "
            "(final_test_period / final_test_stats)"
        )
    trades = test_stats.get("total_trades")
    if isinstance(trades, float) and trades.is_integer():
        trades = int(trades)
    if isinstance(trades, bool) or not isinstance(trades, int):
        raise InvalidPromotionError("optimization result has no final_test_stats.total_trades")
    strategy = payload.get("strategy")
    strategy_path = str(strategy.get("path") or "") if isinstance(strategy, Mapping) else ""
    if not strategy_path.strip():
        raise InvalidPromotionError("optimization result does not name its strategy file")
    best_parameters = payload.get("effective_best_parameters")
    if not isinstance(best_parameters, Mapping):
        raise InvalidPromotionError("optimization result has no effective_best_parameters")

    return OutOfSampleEvidence.from_mapping(
        {
            "optimization_id": str(payload.get("id") or path.stem),
            "result_file": path.name,
            "result_sha256": _sha256_of(path),
            # Only the file name: results record an absolute path of the
            # machine that ran them. Both separators are handled by hand so a
            # result written on Windows reads the same on POSIX.
            "strategy_file": strategy_path.replace("\\", "/").rstrip("/").split("/")[-1],
            "parameters_sha256": parameters_hash(best_parameters),
            "test_start": str(test_period.get("start") or ""),
            "test_end": str(test_period.get("end") or ""),
            "test_trade_count": trades,
            "overfitting_passed": overfitting["passed"],
        }
    )


def report_reference(report_path: Path | str) -> tuple[str, str]:
    """``(file name, sha256)`` of an evidence report, which has to exist."""
    path = Path(report_path)
    if not path.is_file():
        raise InvalidPromotionError(f"evidence report not found: {path}")
    return path.name, _sha256_of(path)


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


def ledger_path_for(path: Path | str) -> Path:
    """Ledger path for ``path``: the file name plus ``LEDGER_SUFFIX``."""
    target = Path(path)
    return target.with_name(target.name + LEDGER_SUFFIX)


def _valid_reviewer(value: Any) -> str:
    reviewer = value.strip() if isinstance(value, str) else ""
    if not reviewer or reviewer.casefold() == "unspecified":
        raise InvalidPromotionError("promoted_by must name the human reviewer")
    return reviewer


def _utc_now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _read_ledger_records(ledger: Path) -> list[Mapping[str, Any]]:
    """Promotion records of a ledger. Raises ``InvalidPromotionError`` if malformed."""
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidPromotionError("the promotion ledger is unreadable") from exc
    if not isinstance(data, Mapping):
        raise InvalidPromotionError("the promotion ledger is not a JSON object")
    if data.get("version") != LEDGER_VERSION:
        raise InvalidPromotionError("the promotion ledger has an unsupported version")
    records = data.get("promotions")
    if not isinstance(records, list) or not all(isinstance(item, Mapping) for item in records):
        raise InvalidPromotionError("the promotion ledger has no list of promotions")
    return records


def _record_problem(
    record: Mapping[str, Any],
    *,
    path: Path,
    kind: str,
    file_sha256: str,
    parameters_sha256: str,
    root: Path | str | None,
    thresholds: PromotionThresholds,
    now: datetime,
) -> str | None:
    """Why ``record`` does not make this file with these parameters production."""
    if record.get("parameters_sha256") != parameters_sha256:
        return "the parameter set differs from the promoted one"
    if record.get("file_sha256") != file_sha256:
        return "the file changed after it was promoted"
    if record.get("kind") != kind:
        return "the promotion was recorded for a different kind of target"
    # A ledger copied next to another file never legitimises that file.
    if record.get("file") != _relative_name(path, root):
        return "the ledger names a different file"
    try:
        _valid_reviewer(record.get("promoted_by"))
        promoted_at = _parse_instant(record.get("promoted_at"), "promoted_at")
        evidence = PromotionEvidence.from_mapping(record.get("evidence"))
    except InvalidPromotionError as exc:
        return f"the promotion record is malformed: {exc}"
    if promoted_at > now + _FUTURE_SKEW:
        return "the promotion record is dated in the future"
    problems = evidence.problems(
        thresholds,
        kind=kind,
        file_name=path.name,
        parameters_sha256=parameters_sha256,
        now=now,
    )
    if problems:
        return "the recorded evidence does not meet the thresholds: " + "; ".join(problems)
    return None


@dataclass(frozen=True)
class PromotionStatus:
    stage: str
    reason: str
    record: Mapping[str, Any] | None = None

    @property
    def is_production(self) -> bool:
        return self.stage == STAGE_PRODUCTION and self.record is not None


def _experimental(reason: str) -> PromotionStatus:
    return PromotionStatus(stage=STAGE_EXPERIMENTAL, reason=reason)


def promotion_status(
    path: Path | str,
    parameters: Mapping[str, Any],
    *,
    kind: str,
    root: Path | str | None = None,
    thresholds: PromotionThresholds | None = None,
    now: datetime | None = None,
) -> PromotionStatus:
    """``production`` only for a complete, current, matching promotion.

    File and ledger are re-read on every call. This never raises for bad input:
    every doubt is reported as ``experimental`` with the reason.
    """
    target = Path(path)
    if kind not in PROMOTION_KINDS:
        return _experimental(f"unknown promotion kind {kind!r}")
    try:
        if not target.is_file():
            return _experimental(f"the file does not exist: {target.name}")
        file_sha256 = _sha256_of(target)
    except OSError as exc:
        return _experimental(f"the file is unreadable ({type(exc).__name__})")
    try:
        bound_parameters_sha256 = parameters_hash(parameters)
        active_thresholds = thresholds or load_promotion_thresholds()
    except InvalidPromotionError as exc:
        return _experimental(str(exc))

    ledger = ledger_path_for(target)
    if not ledger.exists():
        return _experimental("no promotion ledger exists")
    try:
        records = _read_ledger_records(ledger)
    except InvalidPromotionError as exc:
        return _experimental(str(exc))

    reasons: list[str] = []
    for record in records:
        problem = _record_problem(
            record,
            path=target,
            kind=kind,
            file_sha256=file_sha256,
            parameters_sha256=bound_parameters_sha256,
            root=root,
            thresholds=active_thresholds,
            now=_utc_now(now),
        )
        if problem is None:
            return PromotionStatus(stage=STAGE_PRODUCTION, reason="promoted", record=record)
        reasons.append(problem)
    if not reasons:
        return _experimental("the promotion ledger holds no promotion")
    # Name the most specific reason: a record for these parameters that failed
    # says more than "another parameter set was promoted".
    specific = [item for item in reasons if not item.startswith("the parameter set differs")]
    return _experimental((specific or reasons)[0])


def require_promoted(
    path: Path | str,
    parameters: Mapping[str, Any],
    *,
    kind: str,
    root: Path | str | None = None,
    thresholds: PromotionThresholds | None = None,
    now: datetime | None = None,
) -> Mapping[str, Any]:
    """Return the matching promotion record or raise ``StrategyNotPromotedError``."""
    status = promotion_status(
        path, parameters, kind=kind, root=root, thresholds=thresholds, now=now
    )
    if not status.is_production or status.record is None:
        raise StrategyNotPromotedError(
            f"{Path(path).name} is {STAGE_EXPERIMENTAL}, not promoted to "
            f"{STAGE_PRODUCTION}: {status.reason}"
        )
    return status.record


def promote(
    path: Path | str,
    *,
    kind: str,
    parameters: Mapping[str, Any],
    evidence: PromotionEvidence,
    promoted_by: str,
    root: Path | str | None = None,
    thresholds: PromotionThresholds | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a promotion of the current content of ``path`` with ``parameters``.

    Refuses (``InvalidPromotionError``) unless the reviewer is named and all
    three pieces of evidence are present and meet the thresholds in force. A
    promotion of the same parameter set replaces the earlier one; promotions of
    a previous version of the file are dropped, they can never match again.
    """
    target = Path(path)
    if kind not in PROMOTION_KINDS:
        raise InvalidPromotionError(f"unknown promotion kind {kind!r}")
    if not target.is_file():
        raise InvalidPromotionError(f"cannot promote a missing file: {target}")
    reviewer = _valid_reviewer(promoted_by)
    if not isinstance(evidence, PromotionEvidence):
        raise InvalidPromotionError("evidence must be a complete PromotionEvidence")
    active_thresholds = thresholds or load_promotion_thresholds()
    moment = _utc_now(now)

    file_sha256 = _sha256_of(target)
    bound_parameters = canonical_parameters(parameters)
    bound_parameters_sha256 = parameters_hash(bound_parameters)
    # Round-trip through the ledger form so exactly what is written is checked.
    recorded_evidence = PromotionEvidence.from_mapping(evidence.to_dict())
    problems = recorded_evidence.problems(
        active_thresholds,
        kind=kind,
        file_name=target.name,
        parameters_sha256=bound_parameters_sha256,
        now=moment,
    )
    if problems:
        raise InvalidPromotionError("promotion refused: " + "; ".join(problems))

    record: dict[str, Any] = {
        "kind": kind,
        "file": _relative_name(target, root),
        "file_sha256": file_sha256,
        "parameters": bound_parameters,
        "parameters_sha256": bound_parameters_sha256,
        "promoted_by": reviewer,
        "promoted_at": moment.isoformat(),
        # Informational: the thresholds in force when this was recorded. The
        # check always applies the thresholds in force at check time.
        "thresholds": active_thresholds.to_dict(),
        "evidence": recorded_evidence.to_dict(),
    }

    ledger = ledger_path_for(target)
    kept: list[Mapping[str, Any]] = []
    if ledger.exists():
        try:
            existing = _read_ledger_records(ledger)
        except InvalidPromotionError:
            # A malformed ledger promotes nothing, so nothing is lost by
            # replacing it; refusing would leave the reviewer stuck.
            existing = []
        kept = [
            item
            for item in existing
            if item.get("file_sha256") == file_sha256
            and item.get("parameters_sha256") != bound_parameters_sha256
        ]
    _atomic_write_json(ledger, {"version": LEDGER_VERSION, "promotions": [*kept, record]})
    return record
