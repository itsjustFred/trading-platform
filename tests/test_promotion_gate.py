"""Promotion gate (#165): ledger, evidence, binding and the one enforcement function.

Enforcement-point tests (preflight, daemon, viewer) live in
``tests/live_runner/test_promotion_enforcement.py`` and reuse the helpers here.
"""

from __future__ import annotations

import argparse
import json
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
    OutOfSampleEvidence,
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
)
from shared.models import LiveDeploymentConfig

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
_SHA = "a" * 64


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
) -> PromotionEvidence:
    moment = now or datetime.now(timezone.utc)
    test_end = moment - timedelta(days=100)
    return PromotionEvidence(
        out_of_sample=OutOfSampleEvidence(
            optimization_id="opt-1",
            result_file="opt-1.json",
            result_sha256=_SHA,
            strategy_file=strategy_file,
            parameters_sha256=parameters_hash(DEFAULTS if parameters is None else parameters),
            test_start=(test_end - timedelta(days=test_days)).isoformat(),
            test_end=test_end.isoformat(),
            test_trade_count=test_trades,
            overfitting_passed=overfitting_passed,
        ),
        paper_trading=PaperTradingEvidence(
            account_id="paper-1",
            broker="ctrader",
            start=(moment - timedelta(days=paper_days + 1)).isoformat(),
            end=(moment - timedelta(days=1)).isoformat(),
            trade_count=paper_trades,
            report_file="paper.csv",
            report_sha256=_SHA,
        ),
        parity=ParityEvidence(
            reference="lean",
            deviations={"total_return": 0.01} if deviations is None else deviations,
            report_file="parity.json",
            report_sha256=_SHA,
        ),
    )


def promote_strategy_file(strategies_dir: Path, name: str = STRATEGY_NAME) -> dict[str, Any]:
    return promote(
        strategies_dir / name / f"{name}.py",
        kind="strategy",
        parameters=DEFAULTS,
        evidence=good_evidence(strategy_file=f"{name}.py"),
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
    with pytest.raises(InvalidPromotionError, match="reviewer"):
        promote(
            strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
            kind="strategy",
            parameters=DEFAULTS,
            evidence=good_evidence(),
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
        ({"strategy_file": "other.py"}, "not for"),
        ({"parameters": {**DEFAULTS, "fast": 11}}, "effective_best_parameters"),
    ],
)
def test_promotion_refuses_incomplete_or_insufficient_evidence(
    strategies_dir: Path, evidence_kwargs: dict[str, Any], expected: str
) -> None:
    with pytest.raises(InvalidPromotionError, match=expected):
        promote(
            strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
            kind="strategy",
            parameters=DEFAULTS,
            evidence=good_evidence(**evidence_kwargs),
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
    with pytest.raises(InvalidPromotionError, match="future"):
        promote(
            strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py",
            kind="strategy",
            parameters=DEFAULTS,
            evidence=good_evidence(now=now + timedelta(days=10)),
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


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"three_way": {}}, "overfitting verdict"),
        ({"three_way": {"overfitting": {"passed": "yes"}}}, "overfitting verdict"),
        ({"final_test_stats": None}, "test segment"),
        ({"final_test_stats": {"total_trades": None}}, "total_trades"),
        ({"strategy": {}}, "strategy file"),
        ({"effective_best_parameters": None}, "effective_best_parameters"),
    ],
)
def test_out_of_sample_evidence_refuses_a_result_without_the_verdict(
    tmp_path: Path, overrides: dict[str, Any], expected: str
) -> None:
    with pytest.raises(InvalidPromotionError, match=expected):
        out_of_sample_evidence_from_result(_optimizer_result(tmp_path, **overrides))


# --------------------------------------------------------------------------- #
# The one enforcement function
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "flag, expected", [(True, False), (False, True), (None, True), (1, True), ("true", True)]
)
def test_is_real_money_fails_closed(flag: object, expected: bool) -> None:
    assert is_real_money(flag) is expected


def test_demo_account_passes_without_promotion(strategies_dir: Path) -> None:
    enforce_promotion_gate(strategy_deployment(), real_money=False)
    # Even a deployment that could not be resolved at all: paper/demo may run
    # experimental code.
    enforce_promotion_gate(strategy_deployment("does_not_exist"), real_money=False)


def test_real_money_refuses_a_non_promoted_strategy(strategies_dir: Path) -> None:
    with pytest.raises(StrategyNotPromotedError, match="no promotion ledger") as excinfo:
        enforce_promotion_gate(strategy_deployment(), real_money=True)
    assert "real-money account 'acc-1'" in str(excinfo.value)


def test_real_money_accepts_a_promoted_strategy(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    enforce_promotion_gate(strategy_deployment(), real_money=True)


def test_deployment_settings_are_not_bound(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    deployment = strategy_deployment(settings={"risk_per_trade_pct": 0.5, "max_positions": 9})
    enforce_promotion_gate(deployment, real_money=True)


def test_per_deployment_strategy_parameters_are_refused_on_real_money(
    strategies_dir: Path,
) -> None:
    promote_strategy_file(strategies_dir)
    deployment = strategy_deployment(settings={"strategy_parameters": DEFAULTS})
    with pytest.raises(StrategyNotPromotedError, match="not applied by the live runner"):
        enforce_promotion_gate(deployment, real_money=True)


def test_real_money_after_a_file_change_is_refused(strategies_dir: Path) -> None:
    promote_strategy_file(strategies_dir)
    path = strategies_dir / STRATEGY_NAME / f"{STRATEGY_NAME}.py"
    path.write_bytes(path.read_bytes().replace(b"'slow': 30", b"'slow': 31"))
    with pytest.raises(StrategyNotPromotedError):
        enforce_promotion_gate(strategy_deployment(), real_money=True)


@pytest.mark.parametrize("name", ["does_not_exist", "../alpha", "alpha/alpha.py", "", ".."])
def test_real_money_refuses_a_missing_or_path_like_strategy(
    strategies_dir: Path, name: str
) -> None:
    with pytest.raises(StrategyNotPromotedError, match="Promotion gate refused"):
        enforce_promotion_gate(strategy_deployment(name), real_money=True)


def test_real_money_refuses_an_unreadable_strategy_meta(strategies_dir: Path) -> None:
    make_strategy(strategies_dir, "broken", "STRATEGY_META = build_meta()\n")
    with pytest.raises(StrategyNotPromotedError, match="STRATEGY_META"):
        enforce_promotion_gate(strategy_deployment("broken"), real_money=True)


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
    with pytest.raises(StrategyNotPromotedError, match="parameter set differs"):
        enforce_promotion_gate(ramjet_deployment({"min_rr": 2.5}), real_money=True)
    # The bot's base config.yaml parameterises the processor as well.
    config = bots_root / "ramjet" / "config.yaml"
    original = config.read_text(encoding="utf-8")
    config.write_text(original.replace("1.5", "1.6"), encoding="utf-8")
    with pytest.raises(StrategyNotPromotedError):
        enforce_promotion_gate(ramjet_deployment({"min_rr": 2.0}), real_money=True)
    config.write_text(original, encoding="utf-8")
    enforce_promotion_gate(ramjet_deployment({"min_rr": 2.0}), real_money=True)
    # One changed byte of the processor module: experimental again.
    processor = bots_root / "ramjet" / "processor.py"
    processor.write_bytes(processor.read_bytes() + b"#")
    with pytest.raises(StrategyNotPromotedError, match="changed"):
        enforce_promotion_gate(ramjet_deployment({"min_rr": 2.0}), real_money=True)


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
    with pytest.raises(StrategyNotPromotedError, match="no strategy file"):
        enforce_promotion_gate(deployment, real_money=True)
    enforce_promotion_gate(deployment, real_money=False)


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
        ({"strategy": "does_not_exist"}, "no strategy file"),
    ],
)
def test_cli_refuses_incomplete_evidence(
    strategies_dir: Path, tmp_path: Path, overrides: dict[str, Any], expected: str
) -> None:
    with pytest.raises(InvalidPromotionError, match=expected):
        record_promotion(_cli_args(tmp_path, **overrides))
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
