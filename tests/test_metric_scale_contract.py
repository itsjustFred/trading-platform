"""Cross-language percent-scale contract for `frontend/src/api/metricScale.ts`.

The scale table declares the unit of every percent-like metric the UI renders,
PER PAYLOAD. This module PARSES that table and, per (payload, key) cell,
requires the matching backend producer to emit the declared unit — so neither a
new key nor a re-scaled producer can land without a decision being recorded and
enforced.

Why per payload: the unit is a property of the producer, not of the key name.
`win_rate` is percent points in `/api/backtest/{id}/stats` (the payload
multiplies the engine fraction by 100), a fraction in `/api/backtests/all`
(served unchanged from the canonical stats), and points again in the portfolio
summary. A single flat table cannot hold both, which is why the frontend
accessor requires the caller to name the payload.

Three properties make the assertion bite:

* it is EXACT, not a range check. `-100 <= v <= 100` is satisfied by a fraction
  `1.5` and by percent points `12.5` alike, so it proves nothing about the unit.
  Each probe instead feeds the producer a known canonical fraction and demands
  the emitted value equal that fraction ("fraction") or 100x it ("points").
* it is DATA-DRIVEN from the table. Every payload must declare every key (as a
  unit or explicitly "not-applicable"), every declared (payload, key) cell that
  is not "not-applicable" must have a probe, and every probe must correspond to
  a declared cell.
* it refuses a collapsed table. A test asserts that at least one key really does
  carry two different units, so "simplifying" the table back to one unit per key
  fails here instead of silently rendering three metrics 100x off.

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

from shared import models as shared_models
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

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_TABLE_CONST = "PERCENT_METRIC_SCALES_BY_PAYLOAD"
_ENTRY = "fraction|points|not-applicable"


def _strip_ts_comments(source: str) -> str:
    """Drop `//` and block comments from TypeScript source, keeping string contents.

    A comment is not code, but nothing about a substring or regex over raw source tells it
    apart from one - and this file's whole job is to assert the TypeScript table and the
    Python model agree.

    Measured: commenting out every entry of the real `PERCENT_METRIC_KEYS` array, and
    re-declaring the same seven keys inside the `// ` comment block, left this file
    **9 passed**. The frontend would have had an *empty* percent-metric key list - no
    metric scaled as a percentage - with the cross-language contract green.

    Hand-written rather than a regex, because `//` occurs inside string literals - every
    URL - and a regex strip would eat a line holding `https://`. Regex literals are *not*
    tracked: a `/` followed by a `/` inside one would be read as a comment start. The file
    this is applied to was checked for a regex literal containing `//` and there is none;
    the limitation is recorded here rather than left as a surprise for whoever adds one.
    """
    quotes = {"'", '"', "`"}
    out: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if quote is not None:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in quotes:
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            i += 2
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _ts_text() -> str:
    """The metricScale table with comments removed - see `_strip_ts_comments`."""
    return _strip_ts_comments(METRIC_SCALE_TS.read_text(encoding="utf-8"))


def _string_array(const_name: str) -> list[str]:
    """Read a `const NAME = [ ... ] as const` list out of the TypeScript source."""
    text = _ts_text()
    assert const_name in text, (
        f"{METRIC_SCALE_TS} no longer declares {const_name}; update this contract "
        "to the table's new home"
    )
    body = text.split(f"{const_name} = [", 1)[1].split("] as const", 1)[0]
    return [name.strip('"') for name in re.findall(rf'"{_NAME}"', body)]


def _declared_keys() -> list[str]:
    return _string_array("PERCENT_METRIC_KEYS")


def _declared_payloads() -> list[str]:
    return _string_array("METRIC_PAYLOADS")


def _declared_scales() -> dict[str, dict[str, str]]:
    """Read the per-payload scale table out of the TypeScript source.

    Not out of a copy, and not flattened: the payload column is the whole
    reason the table exists, so parsing it away would parse away the contract.
    """
    text = _ts_text()
    assert _TABLE_CONST in text, (
        f"{METRIC_SCALE_TS} no longer exports {_TABLE_CONST}; "
        "update this contract to the table's new home"
    )
    body = text.split(f"{_TABLE_CONST} = {{", 1)[1].split("} as const satisfies", 1)[0]
    tables: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in body.splitlines():
        payload_row = re.fullmatch(rf"\s{{2}}({_NAME}): \{{\s*", line)
        if payload_row:
            current = payload_row.group(1)
            tables[current] = {}
            continue
        cell = re.fullmatch(rf'\s{{4}}({_NAME}):\s*"({_ENTRY})",\s*', line)
        if cell:
            assert current is not None, (
                f"a scale cell ({cell.group(1)}) appears before any payload in "
                f"{METRIC_SCALE_TS}"
            )
            tables[current][cell.group(1)] = cell.group(2)
            continue
        if re.fullmatch(r"\s*\},?\s*", line):
            current = None
    assert tables, f"no per-payload scale declarations parsed from {METRIC_SCALE_TS}"
    return tables


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
    """One (payload, key) cell and the producer that must honour its unit."""

    payload: str
    key: str
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
    #: (payload, key) -> the producer that must honour that cell's unit.
    probes: dict[tuple[str, str], _ScaleProbe]
    #: key -> the canonical fraction every payload must express, in its own unit.
    canonical_fraction: dict[str, float]
    #: payload -> key -> fraction, for the payloads that are NOT built from a
    #: backtest's canonical stats and therefore design their own values.
    designed_fractions: dict[str, dict[str, float]]
    total_return_emitters: dict[str, Callable[[], object]]
    quick_stats: Callable[[], dict]
    compare_stats: Callable[[str], dict]


@pytest.fixture(scope="module")
def contract(request) -> _Contract:
    from types import SimpleNamespace

    import viewer.main as viewer_app_module
    from runner.stats import compute_stats_for_result
    from viewer import core

    result = _build_result()
    canonical = compute_stats_for_result(result)
    trade_dicts = [trade.to_dict() for trade in result.trades]
    equity_dicts = [point.to_dict() for point in result.equity_curve]

    # Every producer below is exercised against the in-memory result, so the
    # probes never open the backtests database: `load_backtest_result` is
    # stubbed with the fixture result, and the two SQLite writers those code
    # paths call are stubbed out as a second line of defence.
    def compare_stats_direct(loaded: BacktestResult) -> dict:
        return viewer_app_module._backtest_compare_payload(loaded.meta.id)["stats"]

    def _load_live_inputs(account_id: str):
        # peak 100_000 then a 10% dip: a known 10.00% max_drawdown_pct.
        return (
            [
                SimpleNamespace(balance=100_000.0, equity=100_000.0),
                SimpleNamespace(balance=90_000.0, equity=90_000.0),
            ],
            [],
        )

    def _realized_live_pnl(rows):
        # 3 wins and 2 losses: a known 60.00% win rate.
        return 1_234.0, 3, 2

    patches = {
        "load_backtest_result": lambda backtest_id: (
            result if str(backtest_id) == result.meta.id else None
        ),
        "save_result_to_sqlite": lambda _result: None,
        "load_live_account_stats_inputs": _load_live_inputs,
        "_calculate_realized_live_pnl": _realized_live_pnl,
    }
    for name, replacement in patches.items():
        assert hasattr(viewer_app_module, name), (
            f"viewer.main no longer has {name}; this probe is pinning a producer "
            "that moved, so re-point it instead of deleting the assertion"
        )
    originals = {name: getattr(viewer_app_module, name) for name in patches}
    for name, replacement in patches.items():
        setattr(viewer_app_module, name, replacement)
    request.addfinalizer(
        lambda: [setattr(viewer_app_module, name, value) for name, value in originals.items()]
    )

    meta_row = {
        "id": result.meta.id,
        "total_return": canonical["total_return"],
        "cagr": canonical["cagr"],
        "max_drawdown": canonical["max_drawdown_pct"],
        "win_rate": canonical["win_rate"],
        "psr": canonical["psr"],
    }

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

    # Every percent-scaled key resolves to ONE canonical value; a payload may
    # express it as a fraction or as percent points, may serve its magnitude
    # (max_drawdown_pct is negative in the engine's canonical stats) and may
    # publish a different number of decimals. The declared cell says which.
    canonical_fraction = {
        "net_profit_pct": canonical["total_return"],
        "win_rate": EXPECTED_WIN_RATE_FRACTION,
        "max_drawdown_pct": canonical["max_drawdown_pct"],
        "max_drawdown": canonical["max_drawdown_pct"],
        "cagr": canonical["cagr"],
        "psr": psr,
        "total_return": canonical["total_return"],
    }

    meta_row = {
        "id": result.meta.id,
        "total_return": canonical["total_return"],
        "cagr": canonical["cagr"],
        "max_drawdown": canonical["max_drawdown_pct"],
        "win_rate": canonical["win_rate"],
        "psr": canonical["psr"],
    }

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

    def backtest_list_item() -> dict:
        return viewer_app_module._backtest_all_item_from_meta(dict(meta_row))

    def backtest_page_item() -> dict:
        return viewer_app_module._backtest_summary_from_meta(dict(meta_row))

    # The persisted combined_stats, exactly as the portfolio builder writes
    # them: a plain pass-through of the canonical stats, i.e. fractions.
    persisted_combined = {
        "sharpe": canonical["sharpe_ratio"],
        "cagr": canonical["cagr"],
        "max_drawdown": abs(canonical["max_drawdown_pct"]),
        "net_profit": canonical["total_pnl"],
        "win_rate": EXPECTED_WIN_RATE_FRACTION,
        "total_return": canonical["total_return"],
        "psr": psr,
        "p_value": canonical["p_value"],
    }

    def portfolio_combined_stats() -> dict:
        """GET /api/portfolios/{id} -> combined_stats, as PortfolioDetailPage reads it.

        A pass-through of the persisted fractions, which is what makes this
        payload fraction-based while the portfolio SUMMARY rescales cagr and
        win_rate by 100.
        """
        return shared_models.PortfolioStats.from_dict(persisted_combined).to_dict()

    def portfolio_summary() -> dict:
        """GET /api/portfolios -> one summary row.

        The same persisted stats, rescaled: this is the payload where cagr and
        win_rate are percent points and max_drawdown is still a fraction.
        """
        return viewer_app_module._portfolio_summary_from_payload(
            {
                "id": "scale-contract",
                "account_capital": INITIAL_CAPITAL,
                "components": [
                    {
                        "component_id": "c1",
                        "strategy_name": "Scale",
                        "symbol": "X",
                        "timeframe": "1h",
                        "weight": 1.0,
                    }
                ],
                "combined_stats": dict(persisted_combined),
                "trades": trade_dicts,
            }
        )

    def portfolio_contribution_row() -> dict:
        """One row of GET /api/portfolios/{id}/contributions?group_by=component.

        The component's linked backtest is served from the in-memory result, so
        no database is touched: the unit under test is the producer's rescaling,
        not where the component's canonical stats came from.
        """
        rows = viewer_app_module._compute_portfolio_contributions(
            portfolio={
                "id": "scale-contract",
                "account_capital": INITIAL_CAPITAL,
                "combined_stats": {
                    "total_return": canonical["total_return"],
                    "max_drawdown": abs(canonical["max_drawdown_pct"]),
                    "net_profit": canonical["total_pnl"],
                },
                "components": [
                    {
                        "component_id": "c1",
                        "strategy_name": "Scale",
                        "symbol": "X",
                        "timeframe": "1h",
                        "weight": 1.0,
                        "backtest_id": result.meta.id,
                    }
                ],
                "trades": [{**trade, "component_id": "c1"} for trade in trade_dicts],
            },
            group_by="component",
        )
        assert len(rows) == 1, f"expected one contribution row, got {len(rows)}"
        return rows[0]

    def optimization_panel_stats() -> dict:
        """An optimization study's train_stats / test_stats panel.

        runner/optimizer.py::_canonical_stats is the producer, and it is the
        engine's canonical dict verbatim: the panels are the one reader that
        takes cagr, win_rate and max_drawdown_pct as fractions.
        """
        from runner.optimizer import _canonical_stats

        return _canonical_stats(result)

    def live_account_stats() -> dict:
        """GET /api/live/accounts/{account_id}/stats.

        The balance series and the closed-round-trip counts are injected: the
        unit under test is the `* 100.0` this producer applies, and building it
        from a real account would be a database read for no extra signal.
        """
        return viewer_app_module._live_account_stats_payload("acc-1")

    #: payload -> (producer, {key: (published decimals, serve the magnitude)})
    payload_producers: dict[str, tuple[Callable[[], dict], dict[str, tuple[int, bool]]]] = {
        "backtest_stats": (
            stats_payload,
            {
                "net_profit_pct": (2, False),
                "win_rate": (1, False),
                "max_drawdown_pct": (2, False),
                "cagr": (2, False),
                "psr": (4, False),
                "total_return": (6, False),
            },
        ),
        "backtest_compare": (
            lambda: compare_stats(result.meta.id),
            {
                "win_rate": (6, False),
                "max_drawdown": (6, True),
                "cagr": (6, False),
                "psr": (6, False),
                "total_return": (6, False),
            },
        ),
        "backtest_list": (
            backtest_list_item,
            {
                "win_rate": (6, False),
                "max_drawdown": (6, True),
                "cagr": (6, False),
                "psr": (6, False),
                "total_return": (6, False),
            },
        ),
        "backtest_page": (
            backtest_page_item,
            {
                "net_profit_pct": (2, False),
                "win_rate": (6, False),
                "max_drawdown_pct": (2, True),
                "cagr": (2, False),
                "psr": (4, False),
            },
        ),
        "portfolio_combined_stats": (
            portfolio_combined_stats,
            {
                "win_rate": (8, False),
                "max_drawdown": (8, True),
                "cagr": (6, False),
                "psr": (6, False),
                "total_return": (6, False),
            },
        ),
        "portfolio_summary": (
            portfolio_summary,
            {
                "net_profit_pct": (2, False),
                "win_rate": (2, False),
                "max_drawdown_pct": (2, True),
                "max_drawdown": (8, True),
                "cagr": (2, False),
                "psr": (4, False),
                "total_return": (8, False),
            },
        ),
        "portfolio_contributions": (
            portfolio_contribution_row,
            {
                "win_rate": (2, False),
                "max_drawdown": (6, True),
                "cagr": (6, False),
                "psr": (6, False),
            },
        ),
        "live_account_stats": (
            live_account_stats,
            {"win_rate": (2, False), "max_drawdown_pct": (2, True)},
        ),
        "optimization_panel_stats": (
            optimization_panel_stats,
            {
                "win_rate": (CANONICAL_PLACES, False),
                "max_drawdown_pct": (CANONICAL_PLACES, False),
                "cagr": (CANONICAL_PLACES, False),
                "psr": (CANONICAL_PLACES, False),
                "total_return": (CANONICAL_PLACES, False),
            },
        ),
    }

    # The live payload is built from an account's balances and closed round
    # trips, not from a backtest's canonical stats, so the values its probe is
    # pinned to are designed here: 3 wins of 5 round trips is 60%, and the
    # injected 100_000 -> 90_000 dip is 10%. Declaring them keeps the probe
    # exact instead of borrowing a backtest number that means nothing here.
    designed_fractions: dict[str, dict[str, float]] = {
        "live_account_stats": {"win_rate": 0.6, "max_drawdown_pct": 0.10},
    }

    probes: dict[tuple[str, str], _ScaleProbe] = {}
    for payload, (producer, cells) in payload_producers.items():
        designed = designed_fractions.get(payload, {})
        for key, (published_places, magnitude) in cells.items():
            base = designed.get(key, canonical_fraction[key])
            probes[(payload, key)] = _ScaleProbe(
                payload,
                key,
                lambda producer=producer, key=key: producer()[key],
                abs(base) if magnitude else base,
                published_places,
            )

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
        canonical_fraction=canonical_fraction,
        designed_fractions=designed_fractions,
        total_return_emitters=total_return_emitters,
        quick_stats=quick_stats,
        compare_stats=compare_stats,
    )


def test_every_payload_declares_every_key(contract: _Contract) -> None:
    """Totality: a payload that forgets a key is a compile error AND a test failure.

    The `satisfies` clause in metricScale.ts already refuses a missing key at
    build time; this repeats it against the parsed source so a future edit that
    loosens the annotation (or a payload added to only one of the two lists)
    cannot leave a key silently undeclared for one endpoint.
    """
    tables = _declared_scales()
    keys = _declared_keys()
    assert keys, "no percent-metric keys parsed from metricScale.ts"
    assert set(_declared_payloads()) == set(tables), (
        "METRIC_PAYLOADS and PERCENT_METRIC_SCALES_BY_PAYLOAD disagree: "
        f"{sorted(set(_declared_payloads()) ^ set(tables))}"
    )
    for payload, table in sorted(tables.items()):
        assert set(table) == set(keys), (
            f"metricScale.ts's {payload} table declares {sorted(set(table))} but "
            f"PERCENT_METRIC_KEYS is {sorted(keys)}; every payload must declare "
            "every key, as a unit or explicitly as not-applicable"
        )


def test_every_declared_cell_is_a_decision(contract: _Contract) -> None:
    """A cell is a fraction, percent points, or an explicit "not applicable"."""
    for payload, table in sorted(_declared_scales().items()):
        for key, entry in sorted(table.items()):
            assert entry in {"fraction", "points", "not-applicable"}, (
                f"metricScale.ts declares {payload}.{key}: {entry!r}, which is "
                'neither "fraction", "points" nor "not-applicable"'
            )


def test_every_declared_metric_key_has_a_backend_probe(contract: _Contract) -> None:
    """A new (payload, key) cell cannot appear without a producer decision behind it.

    This is the guard that makes the contract data-driven: a cell that no
    producer serves cannot be declared, because there would be nothing to hold
    to the declared unit, and a producer that serves a cell nobody declared is a
    reader that will guess its scale.
    """
    tables = _declared_scales()
    declared = {
        (payload, key)
        for payload, table in tables.items()
        for key, entry in table.items()
        if entry != "not-applicable"
    }
    probed = set(contract.probes)
    assert declared - probed == set(), (
        "metricScale.ts declares (payload, key) cells with no backend producer "
        f"probe: {sorted(declared - probed)}. Add a probe to "
        "tests/test_metric_scale_contract.py, or mark the cell not-applicable if "
        "no payload emits it."
    )
    assert probed - declared == set(), (
        "probes reference (payload, key) cells metricScale.ts does not declare "
        f"as served: {sorted(probed - declared)}"
    )
    not_applicable_with_probe = {
        (payload, key)
        for payload, table in tables.items()
        for key, entry in table.items()
        if entry == "not-applicable" and (payload, key) in probed
    }
    assert not_applicable_with_probe == set(), (
        "these cells are declared not-applicable yet are probed: "
        f"{sorted(not_applicable_with_probe)}"
    )


def test_the_same_key_really_does_carry_two_units(contract: _Contract) -> None:
    """The reason the table is per payload: one key, more than one unit.

    A future "simplification" back to a single scale per key would render
    win_rate/cagr/max_drawdown 100x off in half the payloads and pass every
    other test, because the other tests only check each cell against its own
    producer. This one fails the moment the payload column disappears.
    """
    tables = _declared_scales()
    split_keys = {
        key
        for key in _declared_keys()
        if len(
            {
                table[key]
                for table in tables.values()
                if table[key] != "not-applicable"
            }
        )
        > 1
    }
    assert split_keys == {
        "win_rate",
        "max_drawdown",
        "max_drawdown_pct",
        "cagr",
        "psr",
    }, (
        "the keys whose unit genuinely differs per payload changed: "
        f"{sorted(split_keys)}. If a payload really did change unit, update this "
        "test and the table together."
    )
    # And the two readers that were hard-coded around the divergence.
    assert tables["backtest_stats"]["win_rate"] == "points"
    assert tables["backtest_list"]["win_rate"] == "fraction"
    assert tables["portfolio_combined_stats"]["cagr"] == "fraction"
    assert tables["portfolio_summary"]["cagr"] == "points"
    # The keys a reader must never hard-code, with the payloads that disagree:
    # the two portfolio payloads and the two backtest-list payloads.
    assert {
        payload: tables[payload]["win_rate"]
        for payload in (
            "backtest_stats",
            "backtest_list",
            "portfolio_combined_stats",
            "portfolio_summary",
            "portfolio_contributions",
        )
    } == {
        "backtest_stats": "points",
        "backtest_list": "fraction",
        "portfolio_combined_stats": "fraction",
        "portfolio_summary": "points",
        "portfolio_contributions": "points",
    }


def test_declared_scale_matches_what_the_backend_emits(contract: _Contract) -> None:
    """The producer's value must be the canonical fraction in the declared unit."""
    tables = _declared_scales()
    for (payload, key), probe in sorted(contract.probes.items()):
        scale = tables[payload][key]
        designed = contract.designed_fractions.get(payload, {})
        if key in designed:
            assert probe.fraction == pytest.approx(designed[key], rel=1e-9), (
                f"{payload}/{key}: the probe is pinned to {probe.fraction!r} but "
                f"this payload's designed value is {designed[key]!r}"
            )
        else:
            assert abs(probe.fraction) == pytest.approx(
                abs(contract.canonical_fraction[key]), rel=1e-9
            ), (
                f"{payload}/{key}: the probe is pinned to {probe.fraction!r} but "
                f"the canonical value is {contract.canonical_fraction[key]!r}"
            )
        expected = probe.fraction * 100.0 if scale == "points" else probe.fraction
        emitted = float(probe.emit())  # type: ignore[arg-type]
        assert emitted == pytest.approx(expected, abs=probe.tolerance), (
            f"{key}: {payload} emitted {emitted!r} but metricScale.ts "
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
