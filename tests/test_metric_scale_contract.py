"""Cross-language percent-scale contract for `frontend/src/api/metricScale.ts`.

The scale table declares the unit of every percent-like metric the UI renders.
This module PARSES that table and, per key, requires the matching backend
producer to emit the declared unit — so a key cannot be added, or a producer
silently re-scaled, without a decision being recorded and enforced.

Two properties make the assertion bite:

* it is EXACT, not a range check. `-100 <= v <= 100` is satisfied by a fraction
  `1.5` and by percent points `12.5` alike, so it proves nothing about the unit.
  Each probe instead feeds the producer a known canonical fraction and demands
  the emitted value equal that fraction ("fraction") or 100x it ("points").
* it is DATA-DRIVEN from the table. Every declared key must have a probe, and
  every probe must correspond to a declared key.

`total_return` additionally gets a dedicated test because it is the key that had
two live units: the engine writes a fraction (`E_end / E_start - 1`, see
`runner/stats.py`), so every producer and every reader must agree on a fraction.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit

REPO_ROOT = Path(__file__).resolve().parent.parent
METRIC_SCALE_TS = REPO_ROOT / "frontend" / "src" / "api" / "metricScale.ts"
sys.path.insert(0, str(REPO_ROOT))

INITIAL_CAPITAL = 100_000.0
# +3.125% exactly: 3.125 as a fraction vs 312.5 as percent points are nowhere
# near each other, so a single flipped producer cannot pass both readings.
TRADE_PNLS = (2000.0, -500.0, 900.0, -275.0, 1000.0)
EXPECTED_TOTAL_RETURN_FRACTION = sum(TRADE_PNLS) / INITIAL_CAPITAL
EXPECTED_WIN_RATE_FRACTION = sum(1 for pnl in TRADE_PNLS if pnl > 0) / len(TRADE_PNLS)
PRE_DIP_PEAK = 102_000.0
DIP_TROUGH = 101_500.0
FINAL_EQUITY = INITIAL_CAPITAL + sum(TRADE_PNLS)
EXPECTED_MAX_DRAWDOWN_FRACTION = (PRE_DIP_PEAK - DIP_TROUGH) / PRE_DIP_PEAK
# runner/stats.py publishes every canonical stat rounded to 6 decimals.
CANONICAL_PLACES = 6

_SCALE_ROW = re.compile(r'^\s{2}([A-Za-z_][A-Za-z0-9_]*):\s*"(fraction|points)",\s*$', re.MULTILINE)


def _declared_scales() -> dict[str, str]:
    """Read the scale table out of the TypeScript source, not out of a copy."""
    text = METRIC_SCALE_TS.read_text(encoding="utf-8")
    assert "PERCENT_METRIC_SCALES" in text, (
        f"{METRIC_SCALE_TS} no longer exports PERCENT_METRIC_SCALES; "
        "update this contract to the table's new home"
    )
    body = text.split("PERCENT_METRIC_SCALES = {", 1)[1].split(
        "} as const satisfies", 1
    )[0]
    scales = dict(_SCALE_ROW.findall(body))
    assert scales, "no percent-scale declarations parsed from metricScale.ts"
    return scales


def _trade(index: int, pnl: float, day: int) -> Trade:
    return Trade(
        id=index,
        symbol="X",
        direction="LONG",
        entry_time=f"2021-{day:02d}-02T00:00:00Z",
        entry_price=100.0,
        quantity=10,
        exits=[
            TradeExit(
                time=f"2021-{day:02d}-02T12:00:00Z",
                price=100.0 + pnl / 10,
                quantity=10,
                pnl=pnl,
            )
        ],
        pnl=pnl,
    )


def _daily_equity_curve() -> list[EquityPoint]:
    """A year of daily equity points that rises, dips once, then rises again.

    The dip makes max drawdown non-zero, and its peak/trough pair is the same
    one the trade-by-trade equity path visits, so the engine and the portfolio
    trade-derived producer must agree on the drawdown fraction.
    """
    values = [INITIAL_CAPITAL]
    values += [
        INITIAL_CAPITAL + (PRE_DIP_PEAK - INITIAL_CAPITAL) * step / 10
        for step in range(1, 11)
    ]
    values.append(DIP_TROUGH)
    values += [DIP_TROUGH + (FINAL_EQUITY - DIP_TROUGH) * step / 19 for step in range(1, 20)]
    return [
        EquityPoint(time="2021-01-01T00:00:00Z", equity=values[0]),
        *[
            EquityPoint(
                time=f"2021-{1 + (index * 364) // (len(values) - 1):03d}-01T00:00:00Z",
                equity=value,
            )
            for index, value in enumerate(values[1:], start=1)
        ],
    ]


def _build_result() -> BacktestResult:
    result = BacktestResult(
        meta=BacktestMeta(
            id="scale-contract",
            created_at="2022-01-01T00:00:00Z",
            engine="vectorbt",
            strategies=["Scale Contract"],
            backtest_start="2021-01-01",
            backtest_end="2021-12-31",
            initial_capital=INITIAL_CAPITAL,
        ),
        trades=[_trade(index, pnl, 5 + index * 10) for index, pnl in enumerate(TRADE_PNLS, 1)],
        equity_curve=_daily_equity_curve(),
    )
    result.stats = {}
    return result


@dataclass(frozen=True)
class _ScaleProbe:
    """One declared key and the backend producer that must honour its unit."""

    payload: str
    emit: Callable[[], object]
    fraction: float
    places: int

    @property
    def tolerance(self) -> float:
        """Half of each quantum in play: the producer's and the engine's.

        A producer that rounds to 2 decimals cannot be compared to the 6-decimal
        engine value exactly, and the portfolio producer publishes 8. Comparing
        at the published precision of both sides is still five orders of
        magnitude tighter than the 100x a unit flip introduces.
        """
        return 0.5 * 10.0**-self.places + 0.5 * 10.0**-CANONICAL_PLACES + 1e-12


@dataclass(frozen=True)
class _Contract:
    result: BacktestResult
    canonical: dict
    probes: dict[str, _ScaleProbe]
    total_return_emitters: dict[str, Callable[[], object]]
    quick_stats: Callable[[], dict]
    compare_stats: Callable[[str], dict]


@pytest.fixture(scope="module")
def contract() -> _Contract:
    import viewer.main as viewer_app_module
    from runner.stats import compute_stats_for_result
    from viewer import core

    result = _build_result()
    canonical = compute_stats_for_result(result)
    trade_dicts = [trade.to_dict() for trade in result.trades]
    equity_dicts = [point.to_dict() for point in result.equity_curve]

    def stats_payload() -> dict:
        return core._stats_payload_from_result(result)

    def quick_stats() -> dict:
        return core._quick_stats_and_equity(
            trade_dicts,
            INITIAL_CAPITAL,
            "2021-01-01",
            "2021-12-31",
            equity_curve=equity_dicts,
            cached_stats={
                **canonical,
                "psr": canonical.get("psr") or 0.5,
                "p_value": canonical["p_value"],
            },
        )

    def portfolio_trade_stats() -> dict:
        return viewer_app_module._portfolio_stats_from_trades(
            trade_dicts, account_capital=INITIAL_CAPITAL
        )

    def portfolio_summary() -> dict:
        return viewer_app_module._portfolio_summary_from_payload(
            {
                "account_capital": INITIAL_CAPITAL,
                "combined_stats": {},
                "trades": trade_dicts,
            }
        )

    def backtest_list_item() -> dict:
        return viewer_app_module._backtest_all_item_from_meta(
            {
                "id": result.meta.id,
                "total_return": canonical["total_return"],
                "cagr": canonical["cagr"],
                "max_drawdown": canonical["max_drawdown_pct"],
                "win_rate": canonical["win_rate"],
            }
        )

    def compare_stats(backtest_id: str) -> dict:
        return viewer_app_module._backtest_compare_payload(backtest_id)["stats"]

    psr = canonical.get("psr")
    assert psr is not None, (
        "the contract fixture must produce a defined psr, otherwise the psr "
        "probe asserts nothing"
    )
    assert abs(canonical["max_drawdown_pct"]) == pytest.approx(
        EXPECTED_MAX_DRAWDOWN_FRACTION, abs=1e-6
    ), (
        "the designed dip must be the deepest drawdown on the curve, or the "
        "engine and the trade-derived portfolio producer are compared against "
        "two different numbers"
    )

    stats_payload_name = "GET /api/backtest/{id}/stats"
    probes = {
        "net_profit_pct": _ScaleProbe(
            stats_payload_name,
            lambda: stats_payload()["net_profit_pct"],
            EXPECTED_TOTAL_RETURN_FRACTION,
            2,
        ),
        "win_rate": _ScaleProbe(
            stats_payload_name,
            lambda: stats_payload()["win_rate"],
            EXPECTED_WIN_RATE_FRACTION,
            1,
        ),
        "max_drawdown_pct": _ScaleProbe(
            stats_payload_name,
            lambda: stats_payload()["max_drawdown_pct"],
            canonical["max_drawdown_pct"],
            2,
        ),
        "cagr": _ScaleProbe(
            stats_payload_name,
            lambda: stats_payload()["cagr"],
            canonical["cagr"],
            2,
        ),
        "psr": _ScaleProbe(
            stats_payload_name,
            lambda: stats_payload()["psr"],
            psr,
            4,
        ),
        "total_return": _ScaleProbe(
            stats_payload_name,
            lambda: stats_payload()["total_return"],
            canonical["total_return"],
            6,
        ),
        "max_drawdown": _ScaleProbe(
            "portfolio combined_stats (derived from trades)",
            lambda: portfolio_trade_stats()["max_drawdown"],
            abs(canonical["max_drawdown_pct"]),
            8,
        ),
    }

    total_return_emitters = {
        "core._stats_payload_from_result": lambda: stats_payload()["total_return"],
        "core._quick_stats_and_equity": lambda: quick_stats()["total_return"],
        "main._backtest_all_item_from_meta": lambda: backtest_list_item()["total_return"],
        "main._portfolio_stats_from_trades": lambda: portfolio_trade_stats()["total_return"],
        "main._portfolio_summary_from_payload": lambda: portfolio_summary()["total_return"],
    }

    return _Contract(
        result=result,
        canonical=canonical,
        probes=probes,
        total_return_emitters=total_return_emitters,
        quick_stats=quick_stats,
        compare_stats=compare_stats,
    )


def test_declared_scale_table_matches_frontend_contract(contract: _Contract) -> None:
    """Every declared key is a fraction or percent points — nothing else."""
    for key, scale in sorted(_declared_scales().items()):
        assert scale in {"fraction", "points"}, (
            f"metricScale.ts declares {key}: {scale!r}, which is neither "
            '"fraction" nor "points"'
        )


def test_every_declared_metric_key_has_a_backend_probe(contract: _Contract) -> None:
    """A new key cannot join the table without a producer decision behind it.

    This is the guard that makes the contract data-driven: a key that no payload
    emits (as `total_return_raw` used to be) cannot be declared, because there
    would be no producer to hold to the declared unit.
    """
    declared = set(_declared_scales())
    probed = set(contract.probes)
    assert declared - probed == set(), (
        "metricScale.ts declares keys with no backend producer probe: "
        f"{sorted(declared - probed)}. Add a probe to "
        "tests/test_metric_scale_contract.py, or drop the key if no payload "
        "emits it."
    )
    assert probed - declared == set(), (
        "probes reference keys metricScale.ts does not declare: "
        f"{sorted(probed - declared)}"
    )


def test_declared_scale_matches_what_the_backend_emits(contract: _Contract) -> None:
    """The producer's value must be the canonical fraction in the declared unit."""
    declared = _declared_scales()
    for key, probe in sorted(contract.probes.items()):
        scale = declared[key]
        expected = probe.fraction * 100.0 if scale == "points" else probe.fraction
        emitted = float(probe.emit())  # type: ignore[arg-type]
        assert emitted == pytest.approx(expected, abs=probe.tolerance), (
            f"{key}: {probe.payload} emitted {emitted!r} but metricScale.ts "
            f'declares "{scale}" and the canonical value is {probe.fraction!r} '
            f'(expected {expected!r} +/- {probe.tolerance!r}). Change the '
            "producer and the table together, never one alone."
        )


def test_total_return_is_a_fraction_at_every_producer(contract: _Contract) -> None:
    """`total_return` means the engine's E_end/E_start - 1 ratio everywhere.

    The engine writes a fraction, the persisted artifact holds a fraction, and
    every reader that consumes `total_return` must receive a fraction.
    """
    assert contract.canonical["total_return"] == pytest.approx(
        EXPECTED_TOTAL_RETURN_FRACTION, rel=1e-9
    ), "the engine's own total_return definition moved; re-derive the contract"

    for name, emit in sorted(contract.total_return_emitters.items()):
        emitted = float(emit())  # type: ignore[arg-type]
        assert emitted == pytest.approx(EXPECTED_TOTAL_RETURN_FRACTION, abs=1e-6), (
            f"{name} emitted total_return={emitted!r}; the canonical unit is a "
            f"fraction ({EXPECTED_TOTAL_RETURN_FRACTION!r}), so emitting percent "
            f"points would render as {emitted * 100:.2f}%"
        )


def test_total_return_is_net_profit_pct_over_a_hundred_everywhere(
    contract: _Contract,
) -> None:
    """`total_return` and `net_profit_pct` are one number in two units.

    `net_profit_pct` stays in percent points — several readers render it as
    `value.toFixed(1) + "%"` with no conversion — so the pair is a free,
    unit-independent cross-check: if the two keys ever stop being a hundred to
    one apart, one of them was re-scaled.
    """
    from viewer import core

    stats_payload = core._stats_payload_from_result(contract.result)
    assert stats_payload["total_return"] * 100 == pytest.approx(
        stats_payload["net_profit_pct"], abs=0.01
    )
    assert stats_payload["total_return"] == pytest.approx(0.03125, abs=1e-6), (
        "a genuine +3.125% return must be served as 0.03125; serving 3.125 "
        "would be read as +312.50% by every fraction-scaled reader"
    )
    assert contract.canonical["total_return"] == pytest.approx(
        EXPECTED_TOTAL_RETURN_FRACTION, rel=1e-9
    )

    quick_stats = contract.quick_stats()
    assert quick_stats["total_return"] * 100 == pytest.approx(
        quick_stats["net_profit_pct"], abs=0.01
    )


def test_compare_endpoint_serves_total_return_as_a_fraction(viewer_client, tmp_path) -> None:
    """The compare payload serves total_return as a fraction, delta included."""
    backtests_dir = tmp_path / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    for backtest_id in ("scale-compare-a", "scale-compare-b"):
        result = _build_result()
        result.meta.id = backtest_id
        result.save(backtests_dir / f"{backtest_id}.trades.json")

    resp = viewer_client.get("/api/backtests/compare?ids=scale-compare-a,scale-compare-b")

    assert resp.status_code == 200
    payload = resp.json()
    for side in ("backtest1", "backtest2"):
        assert payload[side]["stats"]["total_return"] == pytest.approx(
            EXPECTED_TOTAL_RETURN_FRACTION, abs=1e-6
        ), (
            f"{side} serves total_return as "
            f"{payload[side]['stats']['total_return']!r}; the compare page "
            "renders it with the declared fraction scale"
        )
    assert payload["comparison"]["delta_return"] == pytest.approx(0.0, abs=1e-6)


def test_portfolio_detail_serves_total_return_as_a_fraction(viewer_client, tmp_path) -> None:
    """The portfolio page multiplies `combined_stats.total_return` by 100."""
    import json

    import viewer.main as viewer_app_module

    portfolios_dir = tmp_path / "portfolios"
    portfolios_dir.mkdir(parents=True, exist_ok=True)
    (portfolios_dir / "scale-portfolio.json").write_text(
        json.dumps(
            {
                "id": "scale-portfolio",
                "name": "Scale Portfolio",
                "created_at": "2026-01-01T00:00:00Z",
                "account_capital": INITIAL_CAPITAL,
                "components": [
                    {
                        "component_id": "c1",
                        "strategy_name": "Scale",
                        "symbol": "AAPL",
                        "timeframe": "1h",
                        "weight": 1.0,
                        "risk_config": {},
                        "settings": {},
                    }
                ],
                "combined_equity": [
                    {"time": "2026-01-01T00:00:00Z", "equity": INITIAL_CAPITAL},
                    {"time": "2026-12-31T00:00:00Z", "equity": FINAL_EQUITY},
                ],
                "trades": [
                    {
                        "component_id": "c1",
                        "symbol": "AAPL",
                        "entry_time": "2026-03-01T00:00:00Z",
                        "exit_time": "2026-06-01T00:00:00Z",
                        "pnl": sum(TRADE_PNLS),
                        "fees": 0.0,
                        "net_pnl": sum(TRADE_PNLS),
                    }
                ],
                "combined_stats": {},
            }
        ),
        encoding="utf-8",
    )
    with viewer_app_module._PORTFOLIO_META_CACHE_LOCK:
        viewer_app_module._PORTFOLIO_META_CACHE.clear()
        viewer_app_module._PORTFOLIO_META_CACHE_WARMED = False

    detail = viewer_client.get("/api/portfolios/scale-portfolio")
    summary = viewer_client.get("/api/portfolios")

    assert detail.status_code == 200
    assert detail.json()["combined_stats"]["total_return"] == pytest.approx(
        EXPECTED_TOTAL_RETURN_FRACTION, abs=1e-6
    ), "PortfolioDetailPage renders this value with percentScale='fraction'"
    rows = [row for row in summary.json() if row["id"] == "scale-portfolio"]
    assert rows and rows[0]["total_return"] == pytest.approx(
        EXPECTED_TOTAL_RETURN_FRACTION, abs=1e-6
    )
