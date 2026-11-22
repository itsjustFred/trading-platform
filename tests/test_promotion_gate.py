"""Promotion gate (#165): ledger, evidence, binding and the one enforcement function.

Enforcement-point tests (preflight, daemon, viewer) live in
``tests/live_runner/test_promotion_enforcement.py`` and reuse the helpers here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from live_runner import promotion as live_promotion
from live_runner.promotion import (
    deployment_promotion_target,
    enforce_promotion_gate,
    is_real_money,
)
from runner.promotion_cli import record_promotion
from runner.promotion_gate import (
    LEDGER_VERSION,
    InvalidPromotionError,
    PaperTradingEvidence,
    ParityEvidence,
    PromotionEvidence,
    PromotionThresholds,
    StrategyNotPromotedError,
    canonical_parameters,
    ledger_path_for,
    load_promotion_thresholds,
    out_of_sample_evidence_from_result,
    parameters_hash,
    promote,
    promotion_status,
    recorded_evidence_path,
    require_promoted,
)
from shared.models import LiveDeploymentConfig
from tests.conftest import TEST_DATA_DIR

STRATEGY_NAME = "alpha"
DEFAULTS = {"fast": 10, "slow": 30, "mode": "trend"}
STRATEGY_SOURCE = (
    "STRATEGY_META = {\n"
    "    'name': 'Alpha',\n"
    "    'parameters': {'fast': 10, 'slow': 30, 'mode': 'trend'},\n"
    "}\n"
    "\n"
    "def generate_signals(data, params):\n"
    "    return data\n"
)
#: Where the evidence files of a promotion are written by :func:`good_evidence`.
EVIDENCE_SUBDIR = "evidence"


# --------------------------------------------------------------------------- #
# Shared builders (also imported by the enforcement-point tests)
# --------------------------------------------------------------------------- #


def make_strategy(root: Path, name: str = STRATEGY_NAME, source: str = STRATEGY_SOURCE) -> Path:
    path = root / name / f"{name}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def make_signal_bots_root(root: Path) -> Path:
    """A throwaway copy of the signal bot layout (processor + config) for ramjet."""
    bot_dir = root / "ramjet"
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "processor.py").write_text(
        "class RamJetSignalProcessor:\n    pass\n", encoding="utf-8"
    )
    (bot_dir / "config.yaml").write_text("ramjet:\n  min_rr: 1.5\n", encoding="utf-8")
    return root


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


#: Evidence root for the callers that have no ``tmp_path`` of their own. It lives in the
#: suite's throwaway ``DATA_DIR`` (``tests/conftest.py``), which is per-process and
#: removed at exit, so a test never writes outside ``tmp_path`` or ``DATA_DIR``.
_EVIDENCE_SCRATCH_ROOT = TEST_DATA_DIR / "promotion-evidence"


def _evidence_scratch_dir() -> Path:
    """A unique directory for the evidence files of one promotion.

    The ledger records the path and the hash of every evidence file, so evidence
    whose files do not exist cannot be recorded or re-checked. Callers that
    tamper with the files pass a directory under ``tmp_path`` instead; this
    scratch tree serves the ones that have none and goes with the session data
    directory.
    """
    directory = _EVIDENCE_SCRATCH_ROOT / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def good_evidence(
    *,
    strategy_file: str = f"{STRATEGY_NAME}.py",
    parameters: dict[str, Any] | None = None,
    now: datetime | None = None,
    overfitting_passed: bool = True,
    test_days: int = 200,
    test_trades: int = 60,
    paper_days: int = 70,
    paper_trades: int = 40,
    deviations: dict[str, float] | None = None,
    evidence_dir: Path | None = None,
) -> PromotionEvidence:
    """Evidence whose files exist, so the ledger can record and re-hash them (#260)."""
    moment = now or datetime.now(timezone.utc)
    directory = evidence_dir if evidence_dir is not None else _evidence_scratch_dir()
    directory.mkdir(parents=True, exist_ok=True)
    bound = DEFAULTS if parameters is None else parameters
    test_end = moment - timedelta(days=100)
    # The payload shape the optimizer writes (#164), so the out-of-sample part
    # is read off the file exactly as the CLI reads it, hash and path included.
    result_path = directory / "opt-1.json"
    result_path.write_text(
        json.dumps(
            {
                "id": "opt-1",
                "strategy": {"path": f"/work/strategies/{strategy_file}"},
                "effective_best_parameters": bound,
                "final_test_period": {
                    "start": (test_end - timedelta(days=test_days)).isoformat(),
                    "end": test_end.isoformat(),
                },
                "final_test_stats": {"total_trades": test_trades},
                "three_way": {"overfitting": {"passed": overfitting_passed}},
            }
        ),
        encoding="utf-8",
    )
    paper_report = directory / "paper.csv"
    paper_report.write_text("trade_id,pnl\n1,0.4\n", encoding="utf-8")
    parity_report = directory / "parity.json"
    parity_report.write_text(json.dumps({"total_return": 0.01}), encoding="utf-8")
    return PromotionEvidence(
        out_of_sample=out_of_sample_evidence_from_result(result_path),
        paper_trading=PaperTradingEvidence(
            account_id="paper-1",
            broker="ctrader",
            start=(moment - timedelta(days=paper_days + 1)).isoformat(),
            end=(moment - timedelta(days=1)).isoformat(),
            trade_count=paper_trades,
            report_file=paper_report.name,
            report_path=recorded_evidence_path(paper_report),
            report_sha256=_sha256(paper_report),
        ),
        parity=ParityEvidence(
            reference="lean",
            deviations={"total_return": 0.01} if deviations is None else deviations,
            report_file=parity_report.name,
            report_path=recorded_evidence_path(parity_report),
            report_sha256=_sha256(parity_report),
        ),
    )


def promote_strategy_file(
    strategies_dir: Path,
    name: str = STRATEGY_NAME,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """Promote one strategy file, its evidence in a directory of its own.

    The default is per kind (``evidence/strategy``), so promoting a strategy and
    a signal bot in the same test cannot have one overwrite the other's files.
    """
    return promote(
        strategies_dir / name / f"{name}.py",
        kind="strategy",
        parameters=DEFAULTS,
        evidence=good_evidence(
            strategy_file=f"{name}.py",
            evidence_dir=evidence_dir
            or strategies_dir.parent / EVIDENCE_SUBDIR / "strategy",
        ),
        promoted_by="Fred (reviewer)",
        root=strategies_dir,
    )


def strategy_deployment(name: str = STRATEGY_NAME, **overrides: Any) -> LiveDeploymentConfig:
    values: dict[str, Any] = {
        "id": "11111111-1111-1111-1111-111111111111",
        "strategy_name": name,
        "account_id": "acc-1",
        "symbols": ["EURUSD"],
        "timeframe": "1h",
        "status": "ACTIVE",
        "signal_source": "internal",
        "signal_config": {},
        "settings": {"risk_per_trade_pct": 0.01},
    }
    values.update(overrides)
    return LiveDeploymentConfig(**values)


def ramjet_deployment(processor_config: dict[str, Any] | None = None) -> LiveDeploymentConfig:
    signal_config: dict[str, Any] = {"channel_id": "1", "signal_processor": "ramjet"}
    if processor_config is not None:
        signal_config["signal_processor_config"] = processor_config
    return strategy_deployment(
        "ramjet", signal_source="discord", signal_source_type="discord", signal_config=signal_config
    )


def promote_ramjet_bot(evidence_dir: Path | None = None) -> Any:
    """Promote the ramjet signal bot of the ``bots_root`` tree (no processor overrides)."""
    target = deployment_promotion_target(ramjet_deployment())
    promote(
        target.path,
        kind=target.kind,
        parameters=target.parameters,
        evidence=good_evidence(
            strategy_file="ramjet_strategy.py",
            evidence_dir=evidence_dir
            or target.root.parent / EVIDENCE_SUBDIR / "signal-bot",
        ),
        promoted_by="Fred (reviewer)",
        root=target.root,
    )
    return target


def use_strategies_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway strategy tree with STRATEGY_NAME, used by the enforcement."""
    root = tmp_path / "strategies"
    make_strategy(root)
    # The enforcement resolves strategies against the repository tree; point it here.
    monkeypatch.setattr(live_promotion, "DEFAULT_STRATEGIES_DIR", root)
    return root


@pytest.fixture
def strategies_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return use_strategies_dir(tmp_path, monkeypatch)


@pytest.fixture
def bots_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import signal_bots.registry as registry

    root = make_signal_bots_root(tmp_path / "signal_bots")
    monkeypatch.setattr(registry, "_ROOT", root)
    # The throwaway bot tree sits in a throwaway repository: imports resolve there.
    monkeypatch.setattr(live_promotion, "REPOSITORY_ROOT", tmp_path)
    return root


def _status(strategies_dir: Path, parameters: dict[str, Any] | None = None):
    return promotion_status(
        strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
        DEFAULTS if parameters is None else parameters,
        kind="strategy",
        root=strategies_dir,
    )


# --------------------------------------------------------------------------- #
# Parameters and thresholds
# --------------------------------------------------------------------------- #


def test_canonical_parameters_ignore_key_order_and_integral_floats() -> None:
    assert parameters_hash({"a": 1, "b": [1.0, 2]}) == parameters_hash({"b": [1, 2.0], "a": 1.0})
    assert parameters_hash({"a": 1}) != parameters_hash({"a": 1.5})


@pytest.mark.parametrize("bad", [{"a": float("nan")}, {"a": object()}, {1: "x"}])
def test_canonical_parameters_refuse_what_cannot_be_hashed_faithfully(bad: dict) -> None:
    with pytest.raises(InvalidPromotionError):
        canonical_parameters(bad)


def test_threshold_defaults_are_the_maintainer_decision() -> None:
    assert PromotionThresholds().to_dict() == {
        "min_test_segment_days": 180,
        "min_test_trade_count": 50,
        "min_paper_trading_days": 60,
        "min_paper_trade_count": 30,
        "max_parity_deviation": 0.02,
    }


def test_thresholds_file_overrides_and_rejects_typos(tmp_path: Path) -> None:
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({"min_paper_trade_count": 100}), encoding="utf-8")
    assert load_promotion_thresholds(path).min_paper_trade_count == 100
    path.write_text(json.dumps({"min_paper_trades": 100}), encoding="utf-8")
    with pytest.raises(InvalidPromotionError, match="unknown"):
        load_promotion_thresholds(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(InvalidPromotionError):
        load_promotion_thresholds(path)
    assert load_promotion_thresholds(tmp_path / "absent.json") == PromotionThresholds()


def test_a_malformed_thresholds_file_makes_nothing_production(
    strategies_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import runner.promotion_gate as gate

    promote_strategy_file(strategies_dir)
    broken = tmp_path / "promotion_thresholds.json"
    broken.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(gate, "THRESHOLDS_FILE", broken)
    status = _status(strategies_dir)
    assert not status.is_production
    assert "thresholds" in status.reason


# --------------------------------------------------------------------------- #
# Recording a promotion
# --------------------------------------------------------------------------- #


def test_promotion_with_complete_evidence_is_production(strategies_dir: Path) -> None:
    record = promote_strategy_file(strategies_dir)

    assert record["file"] == f"{STRATEGY_NAME}/{STRATEGY_NAME}.py"
    assert record["promoted_by"] == "Fred (reviewer)"
    assert record["parameters"] == DEFAULTS
    ledger = ledger_path_for(strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py")
    assert ledger.name == f"{STRATEGY_NAME}.py.promoted.json"
    assert json.loads(ledger.read_text(encoding="utf-8"))["version"] == LEDGER_VERSION
    status = _status(strategies_dir)
    assert status.is_production, status.reason


@pytest.mark.parametrize("reviewer", ["", "   ", "unspecified", "Unspecified"])
def test_promotion_requires_a_named_reviewer(strategies_dir: Path, reviewer: str) -> None:
    evidence = good_evidence()
    with pytest.raises(InvalidPromotionError, match="reviewer"):
        promote(
            strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
            kind="strategy",
            parameters=DEFAULTS,
            evidence=evidence,
            promoted_by=reviewer,
            root=strategies_dir,
        )


@pytest.mark.parametrize(
    "evidence_kwargs, expected",
    [
        ({"overfitting_passed": False}, "overfitting verdict"),
        ({"test_days": 179}, "test segment is"),
        ({"test_trades": 49}, "49 trades in the test segment"),
        ({"paper_days": 59}, "paper trading: ran"),
        ({"paper_trades": 29}, "paper trading: 29 trades"),
        ({"deviations": {"total_return": 0.021}}, "parity: total_return"),
        # Parity is a tolerance on the size of the gap, in either direction.
        ({"deviations": {"total_return": -0.021}}, "parity: total_return"),
        ({"strategy_file": "other.py"}, "not for"),
        ({"parameters": {**DEFAULTS, "fast": 11}}, "effective_best_parameters"),
    ],
)
def test_promotion_refuses_incomplete_or_insufficient_evidence(
    strategies_dir: Path, evidence_kwargs: dict[str, Any], expected: str
) -> None:
    evidence = good_evidence(**evidence_kwargs)
    with pytest.raises(InvalidPromotionError, match=expected):
        promote(
            strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
            kind="strategy",
            parameters=DEFAULTS,
            evidence=evidence,
            promoted_by="Fred",
            root=strategies_dir,
        )
    assert not ledger_path_for(strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py").exists()


def test_thresholds_are_inclusive_at_the_boundary(strategies_dir: Path) -> None:
    promote(
        strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
        kind="strategy",
        parameters=DEFAULTS,
        evidence=good_evidence(
            test_days=180, test_trades=50, paper_days=60, paper_trades=30,
            deviations={"total_return": 0.02, "sharpe": -0.02},
        ),
        promoted_by="Fred",
        root=strategies_dir,
    )
    assert _status(strategies_dir).is_production


def test_paper_run_ending_in_the_future_is_refused(strategies_dir: Path) -> None:
    now = datetime.now(timezone.utc)
    evidence = good_evidence(now=now + timedelta(days=10))
    with pytest.raises(InvalidPromotionError, match="future"):
        promote(
            strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
            kind="strategy",
            parameters=DEFAULTS,
            evidence=evidence,
            promoted_by="Fred",
            root=strategies_dir,
            now=now,
        )


def test_out_of_sample_segment_ending_in_the_future_is_refused(strategies_dir: Path) -> None:
    """A test window that has not happened yet cannot have produced its trades."""
    now = datetime.now(timezone.utc)
    evidence = good_evidence(now=now)
    future_oos = replace(
        evidence.out_of_sample, test_end=(now + timedelta(days=10)).isoformat()
    )
    future_evidence = replace(evidence, out_of_sample=future_oos)
    with pytest.raises(InvalidPromotionError, match="test segment ends in the future"):
        promote(
            strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
            kind="strategy",
            parameters=DEFAULTS,
            evidence=future_evidence,
            promoted_by="Fred",
            root=strategies_dir,
            now=now,
        )


def test_evidence_from_mapping_refuses_missing_parts() -> None:
    complete = good_evidence().to_dict()
    for part in ("out_of_sample", "paper_trading", "parity"):
        data = dict(complete)
        data.pop(part)
        with pytest.raises(InvalidPromotionError, match="incomplete"):
            PromotionEvidence.from_mapping(data)
    no_metric = json.loads(json.dumps(complete))
    no_metric["parity"]["deviations"] = {}
    with pytest.raises(InvalidPromotionError, match="at least one"):
        PromotionEvidence.from_mapping(no_metric)


# --------------------------------------------------------------------------- #
# Binding: file hash and parameter set
# --------------------------------------------------------------------------- #


def test_changing_one_byte_of_the_file_falls_back_to_experimental(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    path = strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py"
    path.write_bytes(path.read_bytes() + b" ")
    status = _status(strategies_dir)
    assert not status.is_production
    assert "changed" in status.reason


def test_changing_one_bound_parameter_falls_back_to_experimental(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    status = _status(strategies_dir, {**DEFAULTS, "slow": 31})
    assert not status.is_production
    assert "parameter set differs" in status.reason


def test_missing_file_is_experimental(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    (strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py").unlink()
    assert not _status(strategies_dir).is_production


@pytest.mark.parametrize(
    "ledger_text",
    [
        "",
        "{not json",
        "[]",
        json.dumps({"version": 99, "promotions": []}),
        json.dumps({"version": LEDGER_VERSION, "promotions": "x"}),
        json.dumps({"version": LEDGER_VERSION, "promotions": [1]}),
        json.dumps({"version": LEDGER_VERSION, "promotions": []}),
    ],
)
def test_malformed_ledger_is_experimental(strategies_dir: Path, ledger_text: str) -> None:
    ledger_path_for(strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py").write_text(
        ledger_text, encoding="utf-8"
    )
    assert not _status(strategies_dir).is_production


def test_ledger_with_an_unsupported_version_is_experimental(strategies_dir: Path) -> None:
    # A complete, valid record is not trusted under a ledger version this reader does not know.
    promote_strategy_file(strategies_dir)
    ledger = ledger_path_for(strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py")
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert _status(strategies_dir).is_production
    data["version"] = LEDGER_VERSION + 1
    ledger.write_text(json.dumps(data), encoding="utf-8")
    status = _status(strategies_dir)
    assert not status.is_production
    assert "unsupported version" in status.reason


def _rewrite_record(strategies_dir: Path, mutate) -> None:
    ledger = ledger_path_for(strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py")
    data = json.loads(ledger.read_text(encoding="utf-8"))
    mutate(data["promotions"][0])
    ledger.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda r: r.update(promoted_by=""), "malformed"),
        (lambda r: r.pop("evidence"), "malformed"),
        (lambda r: r["evidence"]["paper_trading"].update(trade_count=5), "thresholds"),
        (lambda r: r["evidence"]["out_of_sample"].update(overfitting_passed=False), "thresholds"),
        (lambda r: r["evidence"]["out_of_sample"].update(test_trade_count="60"), "malformed"),
        (
            lambda r: r["evidence"]["out_of_sample"].update(
                test_end=(datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
            ),
            "future",
        ),
        (lambda r: r.update(kind="signal_bot"), "different kind"),
        (lambda r: r.update(file="beta/beta.py"), "different file"),
        (
            lambda r: r.update(
                promoted_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
            ),
            "future",
        ),
    ],
)
def test_hand_edited_ledger_is_re_validated(strategies_dir: Path, mutate, expected: str) -> None:
    promote_strategy_file(strategies_dir)
    _rewrite_record(strategies_dir, mutate)
    status = _status(strategies_dir)
    assert not status.is_production
    assert expected in status.reason


def test_a_ledger_copied_next_to_another_strategy_does_not_promote_it(
    strategies_dir: Path,
) -> None:
    promote_strategy_file(strategies_dir)
    beta = make_strategy(strategies_dir, "beta")
    ledger_path_for(beta).write_text(
        ledger_path_for(strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    status = promotion_status(beta, DEFAULTS, kind="strategy", root=strategies_dir)
    assert not status.is_production


def test_repromoting_a_changed_file_drops_records_of_the_old_version(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    path = strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py"
    path.write_text(STRATEGY_SOURCE + "# v2\n", encoding="utf-8")
    promote_strategy_file(strategies_dir)
    records = json.loads(ledger_path_for(path).read_text(encoding="utf-8"))["promotions"]
    assert len(records) == 1
    assert _status(strategies_dir).is_production


# --------------------------------------------------------------------------- #
# Out-of-sample evidence read from the optimizer result (#164)
# --------------------------------------------------------------------------- #


def _optimizer_result(tmp_path: Path, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "id": "opt-7",
        "strategy": {"path": f"C:\\work\\strategies\\{STRATEGY_NAME}\\{STRATEGY_NAME}.py"},
        "effective_best_parameters": DEFAULTS,
        "final_test_period": {"start": "2025-01-01", "end": "2025-12-31"},
        "final_test_stats": {"total_trades": 75.0},
        "three_way": {"overfitting": {"passed": True}},
    }
    payload.update(overrides)
    path = tmp_path / "opt-7.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_out_of_sample_evidence_is_read_from_the_result_file(tmp_path: Path) -> None:
    evidence = out_of_sample_evidence_from_result(_optimizer_result(tmp_path))
    assert evidence.optimization_id == "opt-7"
    assert evidence.strategy_file == f"{STRATEGY_NAME}.py"
    assert evidence.test_trade_count == 75
    assert evidence.overfitting_passed is True
    assert evidence.parameters_sha256 == parameters_hash(DEFAULTS)
    assert len(evidence.result_sha256) == 64


def test_out_of_sample_evidence_reads_the_final_holdout_not_the_validation_window(
    tmp_path: Path,
) -> None:
    # Legacy keys `test_period`/`test_stats` now carry the VALIDATION window (#173).
    # A validation window that looks well-traded and long must not stand in for
    # the untouched final holdout the promotion is judged on.
    path = _optimizer_result(
        tmp_path,
        test_period={"start": "2024-01-01", "end": "2025-06-30"},
        test_stats={"total_trades": 500.0},
        validation_period={"start": "2024-01-01", "end": "2025-06-30"},
        validation_stats={"total_trades": 500.0},
        final_test_period={"start": "2025-07-01", "end": "2025-08-15"},
        final_test_stats={"total_trades": 10.0},
    )
    evidence = out_of_sample_evidence_from_result(path)
    assert evidence.test_trade_count == 10
    assert evidence.test_start == "2025-07-01"
    assert evidence.test_end == "2025-08-15"
    problems = evidence.problems(
        PromotionThresholds(),
        kind="strategy",
        file_name=f"{STRATEGY_NAME}.py",
        parameters_sha256=parameters_hash(DEFAULTS),
        now=datetime.now(timezone.utc),
    )
    assert any("10 trades in the test segment" in problem for problem in problems)
    assert any("test segment is 45.0 days" in problem for problem in problems)


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"three_way": {}}, "overfitting verdict"),
        ({"three_way": {"overfitting": {"passed": "yes"}}}, "overfitting verdict"),
        ({"final_test_stats": None}, "test segment"),
        ({"final_test_stats": {"total_trades": None}}, "total_trades"),
        ({"strategy": {}}, "strategy file"),
        ({"effective_best_parameters": None}, "effective_best_parameters"),
        ({"oos_status": "insufficient_oos_bars"}, "out-of-sample status"),
        ({"oos_status": "insufficient_overlap"}, "out-of-sample status"),
    ],
)
def test_out_of_sample_evidence_refuses_a_result_without_the_verdict(
    tmp_path: Path, overrides: dict[str, Any], expected: str
) -> None:
    result = _optimizer_result(tmp_path, **overrides)
    with pytest.raises(InvalidPromotionError, match=expected):
        out_of_sample_evidence_from_result(result)


# --------------------------------------------------------------------------- #
# The one enforcement function
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "flag, expected", [(True, False), (False, True), (None, True), (1, True), ("true", True)]
)
def test_is_real_money_fails_closed(flag: object, expected: bool) -> None:
    assert is_real_money(flag) is expected


@pytest.mark.parametrize("flag", [None, 0, 1, "false"])
def test_only_a_literal_false_skips_the_gate(strategies_dir: Path, flag: Any) -> None:
    # The verdict is fail-closed at the enforcement boundary too, not only in is_real_money.
    deployment = strategy_deployment()
    with pytest.raises(StrategyNotPromotedError):
        enforce_promotion_gate(deployment, real_money=flag)


def test_demo_account_passes_without_promotion(strategies_dir: Path) -> None:
    assert enforce_promotion_gate(strategy_deployment(), real_money=False) is None
    # Even a deployment that could not be resolved at all: paper/demo may run
    # experimental code.
    assert (
        enforce_promotion_gate(strategy_deployment("does_not_exist"), real_money=False)
        is None
    )
    # The skip is the demo flag's doing, not a gate that never looks: the same
    # deployments are refused the moment they count as real money.
    with pytest.raises(StrategyNotPromotedError):
        enforce_promotion_gate(strategy_deployment(), real_money=True)
    with pytest.raises(StrategyNotPromotedError):
        enforce_promotion_gate(strategy_deployment("does_not_exist"), real_money=True)


def test_real_money_refuses_a_non_promoted_strategy(strategies_dir: Path) -> None:
    deployment = strategy_deployment()
    with pytest.raises(StrategyNotPromotedError, match="no promotion ledger") as excinfo:
        enforce_promotion_gate(deployment, real_money=True)
    assert "real-money account 'acc-1'" in str(excinfo.value)


def test_real_money_accepts_a_promoted_strategy(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    assert enforce_promotion_gate(strategy_deployment(), real_money=True) is None
    # The accept is a decision, not a skipped check: the ledger really does
    # report this file production for these parameters.
    target = deployment_promotion_target(strategy_deployment())
    status = promotion_status(
        target.path, target.parameters, kind=target.kind, root=target.root
    )
    assert status.is_production


def test_deployment_settings_are_not_bound(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    deployment = strategy_deployment(settings={"risk_per_trade_pct": 0.5, "max_positions": 9})
    assert enforce_promotion_gate(deployment, real_money=True) is None


def test_per_deployment_strategy_parameters_are_refused_on_real_money(
    strategies_dir: Path,
) -> None:
    promote_strategy_file(strategies_dir)
    deployment = strategy_deployment(settings={"strategy_parameters": DEFAULTS})
    with pytest.raises(StrategyNotPromotedError, match="not applied by the live runner"):
        enforce_promotion_gate(deployment, real_money=True)


def test_per_deployment_strategy_parameters_are_refused_on_a_signal_bot_too(
    bots_root: Path,
) -> None:
    # Inert for a signal bot as for a strategy: it must not look applied either.
    deployment = strategy_deployment(
        "ramjet",
        signal_source="discord",
        signal_config={"signal_processor": "ramjet"},
        settings={"strategy_parameters": DEFAULTS},
    )
    with pytest.raises(StrategyNotPromotedError, match="not applied by the live runner"):
        enforce_promotion_gate(deployment, real_money=True)


def test_real_money_after_a_file_change_is_refused(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    path = strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py"
    path.write_bytes(path.read_bytes().replace(b"'slow': 30", b"'slow': 31"))
    deployment = strategy_deployment()
    with pytest.raises(StrategyNotPromotedError):
        enforce_promotion_gate(deployment, real_money=True)


@pytest.mark.parametrize("name", ["does_not_exist", "../alpha", "alpha/alpha.py", "", ".."])
def test_real_money_refuses_a_missing_or_path_like_strategy(
    strategies_dir: Path, name: str
) -> None:
    deployment = strategy_deployment(name)
    with pytest.raises(StrategyNotPromotedError, match="Promotion gate refused"):
        enforce_promotion_gate(deployment, real_money=True)


def test_a_strategy_name_that_resolves_to_a_promoted_file_is_refused(
    strategies_dir: Path,
) -> None:
    # alpha/../alpha resolves to the promoted file, so only the name check stands in its way.
    promote_strategy_file(strategies_dir)
    deployment = strategy_deployment(f"{STRATEGY_NAME}/../{STRATEGY_NAME}")
    with pytest.raises(StrategyNotPromotedError, match="not a strategy name"):
        enforce_promotion_gate(deployment, real_money=True)


def test_real_money_refuses_an_unreadable_strategy_meta(strategies_dir: Path) -> None:
    make_strategy(strategies_dir, "broken", "STRATEGY_META = build_meta()\n")
    deployment = strategy_deployment("broken")
    with pytest.raises(StrategyNotPromotedError, match="STRATEGY_META"):
        enforce_promotion_gate(deployment, real_money=True)


def test_signal_bot_is_bound_to_processor_file_and_processor_config(bots_root: Path) -> None:
    target = deployment_promotion_target(ramjet_deployment({"min_rr": 2.0}))
    assert target.kind == "signal_bot"
    assert target.path == bots_root / "ramjet" / "processor.py"
    assert target.parameters["signal_processor"] == "ramjet"
    assert target.parameters["signal_processor_config"] == {"min_rr": 2.0}

    promote(
        target.path,
        kind=target.kind,
        parameters=target.parameters,
        evidence=good_evidence(strategy_file="ramjet_strategy.py"),
        promoted_by="Fred",
        root=target.root,
    )
    enforce_promotion_gate(ramjet_deployment({"min_rr": 2.0}), real_money=True)

    # One changed processor_config value: experimental again.
    changed_config = ramjet_deployment({"min_rr": 2.5})
    with pytest.raises(StrategyNotPromotedError, match="parameter set differs"):
        enforce_promotion_gate(changed_config, real_money=True)
    # The bot's base config.yaml parameterises the processor as well.
    config = bots_root / "ramjet" / "config.yaml"
    original = config.read_text(encoding="utf-8")
    config.write_text(original.replace("1.5", "1.6"), encoding="utf-8")
    same_config = ramjet_deployment({"min_rr": 2.0})
    with pytest.raises(StrategyNotPromotedError):
        enforce_promotion_gate(same_config, real_money=True)
    config.write_text(original, encoding="utf-8")
    enforce_promotion_gate(same_config, real_money=True)
    # One changed byte of the processor module: experimental again.
    processor = bots_root / "ramjet" / "processor.py"
    processor.write_bytes(processor.read_bytes() + b"#")
    with pytest.raises(StrategyNotPromotedError, match="changed"):
        enforce_promotion_gate(same_config, real_money=True)


def test_editing_a_module_the_processor_imports_makes_the_bot_experimental(
    bots_root: Path,
) -> None:
    """The bot trades the code it imports: a changed import is a changed promotion (#165)."""
    shared = bots_root.parent / "shared"
    shared.mkdir()
    (shared / "__init__.py").write_text("", encoding="utf-8")
    (shared / "helper.py").write_text("LIMIT = 1\n", encoding="utf-8")
    processor = bots_root / "ramjet" / "processor.py"
    processor.write_text(
        "from shared.helper import LIMIT\n\nclass RamJetSignalProcessor:\n    pass\n",
        encoding="utf-8",
    )
    target = deployment_promotion_target(ramjet_deployment())
    assert set(target.parameters["imported_modules_sha256"]) == {
        "shared/__init__.py",
        "shared/helper.py",
    }
    promote(
        target.path,
        kind=target.kind,
        parameters=target.parameters,
        evidence=good_evidence(strategy_file="ramjet_strategy.py"),
        promoted_by="Fred",
        root=target.root,
    )
    enforce_promotion_gate(ramjet_deployment(), real_money=True)

    (shared / "helper.py").write_text("LIMIT = 2\n", encoding="utf-8")
    deployment = ramjet_deployment()
    with pytest.raises(StrategyNotPromotedError, match="parameter set differs"):
        enforce_promotion_gate(deployment, real_money=True)


def test_nitro_is_bound_to_the_ramjet_processor_it_imports() -> None:
    """Nitro imports ramjet's processor, so a ramjet edit must not leave nitro promoted."""
    from live_runner.promotion import signal_bot_target

    nitro = signal_bot_target("nitro")
    assert "signal_bots/ramjet/processor.py" in nitro.parameters["imported_modules_sha256"]


def test_signal_bot_variants_sharing_a_module_are_promoted_separately(bots_root: Path) -> None:
    (bots_root / "rocketfuel").mkdir()
    (bots_root / "rocketfuel" / "processor.py").write_text("x = 1\n", encoding="utf-8")
    (bots_root / "rocketfuel" / "config.yaml").write_text("rocket_fuel: {}\n", encoding="utf-8")
    from live_runner.promotion import signal_bot_target

    longs = signal_bot_target("rocketfuel_longsonly")
    both = signal_bot_target("rocketfuel_longandshort")
    assert longs.path == both.path
    assert parameters_hash(longs.parameters) != parameters_hash(both.parameters)


def test_real_money_refuses_an_unknown_processor_or_bad_processor_config(bots_root: Path) -> None:
    deployment = strategy_deployment(
        "mystery", signal_source="discord", signal_config={"signal_processor": "mystery"}
    )
    with pytest.raises(StrategyNotPromotedError, match="Unknown signal processor"):
        enforce_promotion_gate(deployment, real_money=True)
    bad = strategy_deployment(
        "ramjet",
        signal_source="discord",
        signal_config={"signal_processor": "ramjet", "signal_processor_config": ["x"]},
    )
    with pytest.raises(StrategyNotPromotedError, match="must be a mapping"):
        enforce_promotion_gate(bad, real_money=True)


def test_discord_deployment_without_processor_has_nothing_to_promote(strategies_dir: Path) -> None:
    deployment = strategy_deployment(
        "RAMJET", signal_source="discord", signal_config={"channel_id": "1"}
    )
    with pytest.raises(StrategyNotPromotedError, match="raw channel signals"):
        enforce_promotion_gate(deployment, real_money=True)
    enforce_promotion_gate(deployment, real_money=False)


@pytest.mark.parametrize(
    "overrides",
    [
        {"signal_source": "discord", "signal_source_type": "discord"},
        {"signal_source": "discord"},
        {"signal_source_type": "discord"},
    ],
)
def test_discord_deployment_without_processor_is_refused_even_if_a_same_named_strategy_is_promoted(
    strategies_dir: Path, overrides: dict[str, Any]
) -> None:
    """The daemon passes the raw channel signal through: the strategy file never runs."""
    promote_strategy_file(strategies_dir)
    internal = strategy_deployment()
    enforce_promotion_gate(internal, real_money=True)
    discord = strategy_deployment(signal_config={"channel_id": "1"}, **overrides)
    with pytest.raises(StrategyNotPromotedError, match="raw channel signals"):
        enforce_promotion_gate(discord, real_money=True)


def test_an_unknown_account_mode_runs_the_gate(strategies_dir: Path) -> None:
    """``real_money=None`` is not "provably paper": only a literal False skips the gate."""
    with pytest.raises(StrategyNotPromotedError):
        enforce_promotion_gate(strategy_deployment(), real_money=None)  # type: ignore[arg-type]


def _promote_ramjet(bots_root: Path) -> None:
    for package in (bots_root, bots_root / "ramjet"):
        (package / "__init__.py").write_text("", encoding="utf-8")
    target = promote_ramjet_bot()
    assert {"signal_bots/__init__.py", "signal_bots/ramjet/__init__.py"} <= set(
        target.parameters["imported_modules_sha256"]
    )
    enforce_promotion_gate(ramjet_deployment(), real_money=True)


@pytest.mark.parametrize("init_relative", ["__init__.py", "ramjet/__init__.py"])
def test_editing_a_processor_parent_package_init_makes_the_bot_experimental(
    bots_root: Path, init_relative: str
) -> None:
    """Importing the processor executes its package ``__init__.py`` files (#165)."""
    _promote_ramjet(bots_root)
    init = bots_root / init_relative
    init.write_text("# monkeypatch RamJetSignalProcessor.process_signal\n", encoding="utf-8")
    with pytest.raises(StrategyNotPromotedError, match="parameter set differs"):
        enforce_promotion_gate(ramjet_deployment(), real_money=True)


def test_the_daemon_and_the_gate_resolve_the_same_processor() -> None:
    from live_runner.daemon import _deployment_signal_processor_name
    from live_runner.promotion import deployment_signal_processor_name

    deployment = strategy_deployment(settings={"signal_processor": " RamJet "})
    assert _deployment_signal_processor_name(deployment) == "ramjet"
    assert deployment_signal_processor_name(deployment) == "ramjet"


# --------------------------------------------------------------------------- #
# CLI: the only way to record a promotion
# --------------------------------------------------------------------------- #


def _cli_args(tmp_path: Path, **overrides: Any) -> argparse.Namespace:
    paper_report = tmp_path / "paper.csv"
    paper_report.write_text("trades\n", encoding="utf-8")
    parity_report = tmp_path / "parity.json"
    parity_report.write_text("{}", encoding="utf-8")
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "strategy": STRATEGY_NAME,
        "signal_processor": None,
        "params": "",
        "promoted_by": "Fred",
        "optimization_result": str(_optimizer_result(tmp_path)),
        "paper_account_id": "paper-1",
        "paper_broker": "ctrader",
        "paper_start": (now - timedelta(days=65)).isoformat(),
        "paper_end": (now - timedelta(days=1)).isoformat(),
        "paper_trades": 35,
        "paper_report": str(paper_report),
        "parity_reference": "lean",
        "parity_deviation": ["total_return=0.01", "sharpe=0.015"],
        "parity_report": str(parity_report),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_cli_records_a_complete_promotion(strategies_dir: Path, tmp_path: Path) -> None:
    outcome = record_promotion(_cli_args(tmp_path))
    assert outcome["record"]["promoted_by"] == "Fred"
    assert outcome["record"]["evidence"]["parity"]["deviations"] == {
        "total_return": 0.01,
        "sharpe": 0.015,
    }
    enforce_promotion_gate(strategy_deployment(), real_money=True)


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"paper_trades": 10}, "paper trading: 10 trades"),
        ({"parity_deviation": ["total_return=0.05"]}, "parity"),
        ({"parity_deviation": ["total_return"]}, "METRIC=VALUE"),
        ({"parity_deviation": ["a=0.01", "a=0.01"]}, "twice"),
        ({"paper_report": "/nonexistent/paper.csv"}, "not found"),
        ({"promoted_by": " "}, "reviewer"),
        ({"params": '{"fast": 11}'}, "signal-processor only"),
        ({"params": "[1, 2]"}, "must be a JSON object"),
        ({"strategy": "does_not_exist"}, "no strategy file"),
    ],
)
def test_cli_refuses_incomplete_evidence(
    strategies_dir: Path, tmp_path: Path, overrides: dict[str, Any], expected: str
) -> None:
    args = _cli_args(tmp_path, **overrides)
    with pytest.raises(InvalidPromotionError, match=expected):
        record_promotion(args)
    assert not ledger_path_for(strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py").exists()


def test_cli_refuses_a_result_without_a_passed_verdict(
    strategies_dir: Path, tmp_path: Path
) -> None:
    args = _cli_args(tmp_path)
    Path(args.optimization_result).write_text(
        json.dumps(
            {
                "id": "opt-7",
                "strategy": {"path": f"{STRATEGY_NAME}.py"},
                "effective_best_parameters": DEFAULTS,
                "final_test_period": {"start": "2025-01-01", "end": "2025-12-31"},
                "final_test_stats": {"total_trades": 75},
                "three_way": {"overfitting": {"passed": False}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(InvalidPromotionError, match="overfitting verdict did not pass"):
        record_promotion(args)


def test_cli_promote_command_is_registered_and_exits_non_zero_on_refusal(
    strategies_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from runner.cli import build_parser

    args = _cli_args(tmp_path, paper_trades=1)
    argv = ["promote", "--strategy", STRATEGY_NAME, "--promoted-by", "Fred",
            "--optimization-result", args.optimization_result,
            "--paper-account-id", "paper-1", "--paper-broker", "ctrader",
            "--paper-start", args.paper_start, "--paper-end", args.paper_end,
            "--paper-trades", "1", "--paper-report", args.paper_report,
            "--parity-reference", "lean", "--parity-deviation", "total_return=0.01",
            "--parity-report", args.parity_report]
    parsed = build_parser().parse_args(argv)
    with pytest.raises(SystemExit) as excinfo:
        parsed.func(parsed)
    assert excinfo.value.code == 1
    assert "Promotion refused" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The evidence files are re-hashed on every check (#260)
# --------------------------------------------------------------------------- #


def _strip_evidence_paths(ledger: Path) -> None:
    """Drop the recorded evidence paths: how a ledger written before #260 reads."""
    data = json.loads(ledger.read_text(encoding="utf-8"))
    for record in data["promotions"]:
        for section in record["evidence"].values():
            section.pop("result_path", None)
            section.pop("report_path", None)
    ledger.write_text(json.dumps(data), encoding="utf-8")


def test_the_ledger_records_the_path_of_every_evidence_file(
    strategies_dir: Path, tmp_path: Path
) -> None:
    """The ledger names the file each piece of evidence was read from (#260)."""
    record = record_promotion(_cli_args(tmp_path))["record"]
    evidence = record["evidence"]
    assert evidence["out_of_sample"].get("result_path") == (
        _optimizer_result(tmp_path).resolve().as_posix()
    )
    paper_report = (tmp_path / "paper.csv").resolve().as_posix()
    parity_report = (tmp_path / "parity.json").resolve().as_posix()
    assert evidence["paper_trading"].get("report_path") == paper_report
    assert evidence["parity"].get("report_path") == parity_report
    assert _status(strategies_dir).is_production


def test_evidence_paths_are_recorded_relative_to_the_repository(
    strategies_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ledger stays portable: an evidence file in the repository is recorded relatively."""
    import runner.promotion_gate as gate

    monkeypatch.setattr(gate, "PROJECT_ROOT", tmp_path)
    args = _cli_args(tmp_path)
    record = record_promotion(args)["record"]
    evidence = record["evidence"]
    assert evidence["out_of_sample"].get("result_path") == "opt-7.json"
    assert evidence["paper_trading"].get("report_path") == "paper.csv"
    assert evidence["parity"].get("report_path") == "parity.json"
    # The relative form resolves against the repository tree of the checking process.
    assert _status(strategies_dir).is_production
    # ... which is what fails closed when that file is gone.
    Path(args.optimization_result).unlink()
    assert not _status(strategies_dir).is_production


def test_an_altered_evidence_file_refuses_the_promotion(
    strategies_dir: Path, tmp_path: Path
) -> None:
    """The files the evidence names are re-hashed, so an edit after the fact is caught (#260)."""
    args = _cli_args(tmp_path)
    record_promotion(args)
    assert _status(strategies_dir).is_production
    evidence_files = [
        Path(args.optimization_result),
        Path(args.paper_report),
        Path(args.parity_report),
    ]
    originals = {path: path.read_bytes() for path in evidence_files}
    for path in evidence_files:
        path.write_bytes(path.read_bytes() + b" ")
        status = _status(strategies_dir)
        assert not status.is_production, path.name
        assert "changed after the promotion" in status.reason, path.name
        path.write_bytes(originals[path])
    # Restoring the bytes restores the promotion: the check is a re-hash, not a one-way trip.
    assert _status(strategies_dir).is_production


def test_an_altered_evidence_file_refuses_a_promotion_recorded_through_the_api(
    strategies_dir: Path, tmp_path: Path
) -> None:
    """The same re-hash on the direct ``promote`` path, without the CLI."""
    evidence_dir = tmp_path / EVIDENCE_SUBDIR
    promote_strategy_file(strategies_dir, evidence_dir=evidence_dir)
    assert _status(strategies_dir).is_production
    paper_report = evidence_dir / "paper.csv"
    paper_report.write_text("trade_id,pnl\n1,0.9\n", encoding="utf-8")
    status = _status(strategies_dir)
    assert not status.is_production
    assert "paper trading" in status.reason


def test_promotion_refuses_an_evidence_file_that_vanished_before_recording(
    strategies_dir: Path, tmp_path: Path
) -> None:
    """Exactly what is written is checked, so the file must still be there to record it."""
    evidence_dir = tmp_path / EVIDENCE_SUBDIR
    evidence = good_evidence(evidence_dir=evidence_dir)
    (evidence_dir / "opt-1.json").unlink()
    with pytest.raises(InvalidPromotionError, match="missing or unreadable"):
        promote(
            strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
            kind="strategy",
            parameters=DEFAULTS,
            evidence=evidence,
            promoted_by="Fred",
            root=strategies_dir,
        )
    assert not ledger_path_for(strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py").exists()


def test_a_missing_evidence_file_refuses_the_promotion(
    strategies_dir: Path, tmp_path: Path
) -> None:
    args = _cli_args(tmp_path)
    record_promotion(args)
    assert _status(strategies_dir).is_production
    Path(args.parity_report).unlink()
    status = _status(strategies_dir)
    assert not status.is_production
    assert "missing or unreadable" in status.reason


def test_an_evidence_path_that_cannot_be_a_path_is_refused_not_raised(
    strategies_dir: Path, tmp_path: Path
) -> None:
    """A hand-edited ledger is bad input: ``promotion_status`` refuses it, it does not raise.

    The function promises "never raises for bad input", and only
    ``StrategyNotPromotedError`` is caught on the live paths (preflight, daemon
    entry preparation, viewer). A recorded path with an embedded NUL makes
    ``read_bytes`` raise ``ValueError``: it has to be reported as ``experimental``.
    """
    record_promotion(_cli_args(tmp_path))
    _rewrite_record(strategies_dir, lambda r: r["evidence"]["parity"].update(report_path="a\x00b"))
    status = _status(strategies_dir)
    assert status.stage == "experimental"
    assert not status.is_production
    assert "missing or unreadable" in status.reason
    assert "a\x00b" in status.reason
    # The one enforcement function refuses instead of crashing the caller.
    with pytest.raises(StrategyNotPromotedError, match="experimental"):
        require_promoted(
            strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
            DEFAULTS,
            kind="strategy",
            root=strategies_dir,
        )


def test_an_evidence_path_that_names_a_directory_is_refused(
    strategies_dir: Path, tmp_path: Path
) -> None:
    """Only a regular file backs a promotion: a directory is "missing or unreadable" too."""
    record_promotion(_cli_args(tmp_path))
    _rewrite_record(
        strategies_dir,
        lambda r: r["evidence"]["parity"].update(report_path=str(tmp_path)),
    )
    status = _status(strategies_dir)
    assert not status.is_production
    # Exactly that reason: opening it failed is reported with the exception's
    # type name, and "not a regular file" is not an exception at all.
    assert status.reason.endswith("is missing or unreadable")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_an_unreadable_evidence_file_reports_what_went_wrong(
    strategies_dir: Path, tmp_path: Path
) -> None:
    """A file that exists but cannot be read keeps the cause the sibling keeps."""
    record_promotion(_cli_args(tmp_path))
    report = tmp_path / "parity.json"
    report.chmod(0o000)
    try:
        status = _status(strategies_dir)
    finally:
        report.chmod(0o644)
    assert not status.is_production
    assert "missing or unreadable" in status.reason
    assert "PermissionError" in status.reason


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_an_evidence_path_that_names_a_fifo_is_refused_without_reading_it(
    strategies_dir: Path, tmp_path: Path
) -> None:
    """Opening a FIFO blocks until a writer appears: the check must not open one.

    The daemon runs this check on every entry order, so a hand-edited ledger
    naming a FIFO must not wedge the money path.
    """
    fifo = tmp_path / "parity-fifo"
    os.mkfifo(fifo)
    record_promotion(_cli_args(tmp_path))
    _rewrite_record(
        strategies_dir,
        lambda r: r["evidence"]["parity"].update(
            report_path=recorded_evidence_path(fifo),
            report_sha256=_sha256(tmp_path / "parity.json"),
        ),
    )
    status = _status(strategies_dir)
    assert not status.is_production
    assert "missing or unreadable" in status.reason


def test_a_record_that_does_not_name_its_evidence_files_is_refused(
    strategies_dir: Path, tmp_path: Path
) -> None:
    """A ledger written before #260 cannot be re-verified, so it is refused until re-recorded."""
    record_promotion(_cli_args(tmp_path))
    _strip_evidence_paths(ledger_path_for(strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py"))
    status = _status(strategies_dir)
    assert not status.is_production
    assert "does not name the file" in status.reason
    assert "record the promotion again" in status.reason


def test_a_record_with_an_evidence_path_of_the_wrong_type_is_malformed(
    strategies_dir: Path, tmp_path: Path
) -> None:
    record_promotion(_cli_args(tmp_path))
    _rewrite_record(strategies_dir, lambda r: r["evidence"]["parity"].update(report_path=42))
    status = _status(strategies_dir)
    assert not status.is_production
    # The classification, not only the refusal: a coerced path would fail later
    # with a different reason.
    assert "malformed" in status.reason
    assert "'report_path' must be a string" in status.reason


def test_an_empty_evidence_path_is_a_record_that_does_not_name_its_file(
    strategies_dir: Path, tmp_path: Path
) -> None:
    """``""`` and absent read the same: the record does not name its file."""
    record_promotion(_cli_args(tmp_path))
    _rewrite_record(strategies_dir, lambda r: r["evidence"]["parity"].update(report_path=""))
    status = _status(strategies_dir)
    assert not status.is_production
    assert "does not name the file" in status.reason
    assert "parity.report_path" in status.reason


def test_promoting_a_strategy_and_a_bot_keeps_both_promotions(
    strategies_dir: Path, bots_root: Path
) -> None:
    """Two promotions in one test keep their own evidence files.

    The helpers share a ``tmp_path``, so an evidence directory they shared would
    have the second promotion overwrite the first's files -- and the first
    re-check would then refuse it.
    """
    promote_strategy_file(strategies_dir)
    target = promote_ramjet_bot()
    assert _status(strategies_dir).is_production
    assert (
        promotion_status(
            target.path, target.parameters, kind=target.kind, root=target.root
        ).is_production
    )


def test_a_recorded_path_round_trips_through_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger form of a path is portable: POSIX separators, root-relative.

    ``pytest`` only runs on the Linux leg of CI, so this is the standing guard
    for the path handling on every platform: a repository file is recorded
    relative, with forward slashes (what ``PureWindowsPath.as_posix`` produces on
    Windows), and the relative form resolves back against the repository root of
    the checking process while an absolute one stands for itself.
    """
    import runner.promotion_gate as gate

    monkeypatch.setattr(gate, "PROJECT_ROOT", tmp_path)
    evidence = tmp_path / "evidence" / "nested.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}", encoding="utf-8")
    recorded = recorded_evidence_path(evidence)
    assert recorded == "evidence/nested.json"
    assert "\\" not in recorded
    assert gate._evidence_file(recorded) == evidence
    outside = tmp_path.parent / "outside.json"
    absolute = recorded_evidence_path(outside)
    assert Path(absolute).is_absolute()
    assert gate._evidence_file(absolute) == outside


def test_promotion_refuses_evidence_that_does_not_name_its_file(strategies_dir: Path) -> None:
    """Recording a promotion has to name the file each part was read from (#260)."""
    data = good_evidence().to_dict()
    data["out_of_sample"]["result_path"] = ""
    evidence = PromotionEvidence.from_mapping(data)
    with pytest.raises(InvalidPromotionError, match="name the file"):
        promote(
            strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
            kind="strategy",
            parameters=DEFAULTS,
            evidence=evidence,
            promoted_by="Fred",
            root=strategies_dir,
        )
    assert not ledger_path_for(strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py").exists()
