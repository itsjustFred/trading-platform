"""
Regression-Test für Strategie: Custom Swings + Supertrend

Smoke-Test: stellt sicher, dass zukünftige Core-Updates die Strategie-Logik
nicht unbemerkt brechen. Getestet auf einem festen, kurzen Zeitraum.

Validiert am: 2026-03-11
Symbol/Timeframe: XAUUSD / 1h
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter


STRATEGY_PATH = str(
    Path(__file__).parent.parent.parent
    / "strategies"
    / "custom_swings_supertrend"
    / "custom_swings_supertrend.py"
)

# Fester, kurzer Zeitraum (ca. 3 Monate) — NICHT verändern
TEST_START = "2023-01-01"
TEST_END = "2023-04-01"
TEST_SYMBOL = "XAUUSD"
TEST_TIMEFRAME = "1h"

# Erwartete Werte — aus dem validierten Backtest auf diesem Zeitraum ermittelt
EXPECTED_TRADE_COUNT = 13
EXPECTED_NET_PROFIT = -2197.47


@pytest.fixture(scope="module")
def backtest_result():
    engine = VectorbtAdapter()
    config = BacktestConfig(
        strategy_name="custom_swings_supertrend",
        symbols=[TEST_SYMBOL],
        timeframe=TEST_TIMEFRAME,
        start_date=TEST_START,
        end_date=TEST_END,
        initial_capital=100_000.0,
        risk_per_trade_pct=0.01,
    )
    return engine.run(config, STRATEGY_PATH)


class TestStrategyRegression:
    def test_trade_count(self, backtest_result):
        """Anzahl der Trades darf sich nicht verändern."""
        assert len(backtest_result.trades) == EXPECTED_TRADE_COUNT, (
            f"Erwartet {EXPECTED_TRADE_COUNT} Trades, erhalten {len(backtest_result.trades)}"
        )

    def test_net_profit(self, backtest_result):
        """Net Profit darf sich nicht wesentlich verändern (Toleranz: 0.5 $)."""
        actual = sum(t.pnl for t in backtest_result.trades)
        assert math.isclose(actual, EXPECTED_NET_PROFIT, abs_tol=0.50), (
            f"Net Profit: erwartet {EXPECTED_NET_PROFIT:.2f}, erhalten {actual:.2f}"
        )

    def test_generate_signals_returns_4_elements(self):
        """generate_signals muss exakt 4 Elemente zurückgeben (entries, exits, indicators_dict, drawings)."""
        import importlib.util

        import pandas as pd
        import numpy as np

        spec = importlib.util.spec_from_file_location("s", STRATEGY_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        n = 200
        df = pd.DataFrame({
            "open":   np.random.uniform(1800, 2000, n),
            "high":   np.random.uniform(2000, 2100, n),
            "low":    np.random.uniform(1700, 1800, n),
            "close":  np.random.uniform(1850, 1950, n),
            "volume": np.ones(n) * 1000,
        })
        result = mod.generate_signals(df)
        assert len(result) == 4, (
            f"generate_signals gibt {len(result)} Elemente zurück — erwartet 4 "
            "(entries, exits, indicators_dict, drawings)."
        )
        _, _, indicators_dict, drawings = result
        assert isinstance(indicators_dict, dict), (
            f"indicators_dict muss ein dict sein, ist aber: {type(indicators_dict)}"
        )
        assert isinstance(drawings, list), (
            f"drawings muss eine list sein, ist aber: {type(drawings)}"
        )

    def test_generate_signals_has_parameter_defaults(self):
        """generate_signals muss konfigurierbare Parameter mit Default-Werten in der Signatur haben."""
        import ast
        import pathlib

        src = pathlib.Path(STRATEGY_PATH).read_text()
        tree = ast.parse(src)
        fn = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "generate_signals"
            ),
            None,
        )
        assert fn is not None, "generate_signals Funktion nicht gefunden!"
        n_defaults = len(fn.args.defaults) + len(fn.args.kw_defaults)
        assert n_defaults > 0, (
            "generate_signals hat keine Keyword-Argumente mit Default-Werten — "
            "der AST-Parser kann kein Optimierungs-UI generieren!"
        )

    def test_all_trades_have_sl_in_meta(self, backtest_result):
        """Jeder Trade muss sl_price in meta haben (Risk-Sizing-Kontrolle)."""
        for trade in backtest_result.trades:
            assert "sl_price" in trade.meta, (
                f"Trade {trade.id}: 'sl_price' fehlt in trade.meta"
            )

    def test_no_trade_exceeds_risk_limit(self, backtest_result):
        """Kein Trade darf mehr als 1.5% des Eigenkapitals riskieren."""
        import math

        equity = 100_000.0
        for trade in backtest_result.trades:
            sl = trade.meta.get("sl_price")
            assert sl is not None, f"Trade {trade.id}: 'sl_price' fehlt in trade.meta"
            sl_val = float(sl)
            assert not math.isnan(sl_val), f"Trade {trade.id}: 'sl_price' ist NaN"
            stop_dist = abs(trade.entry_price - sl_val)
            risk_usd = stop_dist * trade.quantity
            risk_pct = risk_usd / equity
            assert risk_pct <= 0.015, (
                f"Trade {trade.id}: Risk {risk_pct * 100:.2f}% überschreitet Limit von 1.5%"
            )
            equity += trade.pnl

    def test_indicators_dict_has_required_keys(self):
        """indicators_dict muss stop_loss, atr und direction enthalten."""
        import importlib.util

        import pandas as pd
        import numpy as np

        spec = importlib.util.spec_from_file_location("s", STRATEGY_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        n = 200
        df = pd.DataFrame({
            "open":   np.random.uniform(1800, 2000, n),
            "high":   np.random.uniform(2000, 2100, n),
            "low":    np.random.uniform(1700, 1800, n),
            "close":  np.random.uniform(1850, 1950, n),
            "volume": np.ones(n) * 1000,
        })
        _, _, indicators_dict, _ = mod.generate_signals(df)
        required = {"stop_loss", "atr", "direction"}
        missing = required - set(indicators_dict.keys())
        assert not missing, f"Fehlende Pflicht-Keys in indicators_dict: {missing}"

    def test_strategy_meta_completeness(self):
        """STRATEGY_META muss alle Pflichtfelder enthalten."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("s", STRATEGY_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        meta = mod.STRATEGY_META
        assert "name" in meta
        tags = meta.get("tags", {})
        for required_tag in ("type", "source", "asset_class", "complexity"):
            assert required_tag in tags, f"Pflicht-Tag '{required_tag}' fehlt in STRATEGY_META.tags"
