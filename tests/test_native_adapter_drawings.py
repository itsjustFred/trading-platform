"""Drawings from the native engine must be JSON-safe, like the vectorbt ones.

The native engine stored a strategy's drawings verbatim while the vectorbt
adapter ran them through `_normalize_drawings`. A strategy that put a pandas
Timestamp in `time` — the obvious thing to write — therefore produced a payload
that raised "Object of type Timestamp is not JSON serializable" the moment the
result was persisted. The strategy-pipeline test only reached that code once the
four-tuple contract made strategies actually return drawings.
"""

from __future__ import annotations

import json

import pandas as pd

from runner.engines._drawings import _normalize_drawings


def _strategy_returning_timestamp_drawings(df: pd.DataFrame, params: dict):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[1] = True
    exits.iloc[5] = True
    drawings = [
        {
            "type": "marker",
            "time": df["timestamp"].iloc[1],  # a real pd.Timestamp
            "position": "belowBar",
            "shape": "arrowUp",
            "color": "#16a34a",
            "text": "Entry",
        },
        {
            "type": "marker",
            "time": df["timestamp"].iloc[5],
            "position": "aboveBar",
            "shape": "arrowDown",
            "color": "#dc2626",
            "text": "Exit",
        },
    ]
    return entries, exits, {}, drawings


def _frame() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=10, freq="1D", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": index,
            "open": [100.0 + i for i in range(10)],
            "high": [101.0 + i for i in range(10)],
            "low": [99.0 + i for i in range(10)],
            "close": [100.5 + i for i in range(10)],
            "volume": [1000] * 10,
        }
    )


def test_normalizer_makes_timestamp_drawings_json_serializable():
    frame = _frame()
    _, _, _, drawings = _strategy_returning_timestamp_drawings(frame, {})

    # Precondition: this is genuinely what a strategy would hand over.
    assert isinstance(drawings[0]["time"], pd.Timestamp)

    normalized = _normalize_drawings(drawings, symbol="TEST")

    encoded = json.dumps(normalized)  # must not raise
    assert encoded
    assert all(isinstance(d["time"], str) for d in normalized)


def test_native_adapter_output_is_json_serializable(tmp_path, monkeypatch):
    """End to end through the adapter, which is where the leak reached storage."""
    from runner.engines import native_adapter
    from runner.engines.base import BacktestConfig

    strategy = tmp_path / "ts_drawings.py"
    strategy.write_text(
        "import pandas as pd\n"
        "\n"
        "\n"
        "def generate_signals(df, params):\n"
        "    entries = pd.Series(False, index=df.index)\n"
        "    exits = pd.Series(False, index=df.index)\n"
        "    entries.iloc[1] = True\n"
        "    exits.iloc[5] = True\n"
        "    drawings = [\n"
        "        {\n"
        "            'type': 'marker',\n"
        "            'time': df['timestamp'].iloc[1],\n"
        "            'position': 'belowBar',\n"
        "            'shape': 'arrowUp',\n"
        "            'color': '#16a34a',\n"
        "            'text': 'Entry',\n"
        "        }\n"
        "    ]\n"
        "    return entries, exits, {}, drawings\n",
        encoding="utf-8",
    )

    frame = _frame()
    monkeypatch.setattr(native_adapter, "_load_ohlcv", lambda *_a, **_k: frame)

    config = BacktestConfig(
        strategy_name="native-drawings",
        symbols=["TEST"],
        timeframe="1d",
        start_date="2024-01-01",
        end_date="2024-01-11",
        initial_capital=100_000,
        # Sizing and costs must be explicit: the engine refuses the full-equity
        # default and a silently cost-free run.
        risk_per_trade_pct=0.01,
        parameters={"fees": "zero"},
    )
    result = native_adapter.NativeAdapter().run(config, str(strategy))

    assert result.drawings, "the strategy did return drawings"
    json.dumps(result.to_dict())  # must not raise
