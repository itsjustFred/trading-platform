"""
Trade Viewer Backend — Flask API serving universal-format backtest results.

Endpoints:
    GET  /api/backtests               — List available backtest result files
    GET  /api/backtest/<id>           — Load a specific backtest result
    GET  /api/backtest/<id>/stats     — Compute stats for a backtest
    GET  /api/backtest/<id>/ohlcv     — OHLCV data for chart overlay
    GET  /                            — Serve the frontend

Usage:
    python -m viewer.app                         # default port 8080
    python -m viewer.app --port 9000
    python -m viewer.app --backtests ~/data/backtests
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

from runner.stats import compute_stats_for_result
from shared.models import BacktestResult

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("TRADING_DATA_DIR", Path.home() / "data"))
BACKTESTS_DIR = DATA_DIR / "backtests"
STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR))
CORS(app)


# ─── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/backtests")
def list_backtests():
    """List all available backtest result files."""
    results = []
    if BACKTESTS_DIR.exists():
        for f in sorted(BACKTESTS_DIR.glob("*.trades.json")):
            try:
                data = json.loads(f.read_text())
                meta = data.get("meta", {})
                results.append({
                    "id": f.stem.replace(".trades", ""),
                    "filename": f.name,
                    "strategy": meta.get("strategy", "unknown"),
                    "engine": meta.get("engine", "unknown"),
                    "trade_count": len(data.get("trades", [])),
                    "created_at": meta.get("created_at", ""),
                })
            except Exception as e:
                logger.warning("Could not read %s: %s", f, e)
    return jsonify(results)


@app.route("/api/backtest/<backtest_id>")
def get_backtest(backtest_id: str):
    """Load a specific backtest result."""
    path = BACKTESTS_DIR / f"{backtest_id}.trades.json"
    if not path.exists():
        return jsonify({"error": f"Backtest '{backtest_id}' not found"}), 404
    try:
        result = BacktestResult.load(str(path))
        return jsonify(result.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtest/<backtest_id>/stats")
def get_backtest_stats(backtest_id: str):
    """Compute stats for a backtest."""
    path = BACKTESTS_DIR / f"{backtest_id}.trades.json"
    if not path.exists():
        return jsonify({"error": f"Backtest '{backtest_id}' not found"}), 404
    try:
        result = BacktestResult.load(str(path))
        stats = compute_stats_for_result(result)
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtest/<backtest_id>/ohlcv")
def get_backtest_ohlcv(backtest_id: str):
    """Try to find OHLCV data for the symbols in this backtest."""
    path = BACKTESTS_DIR / f"{backtest_id}.trades.json"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    result = BacktestResult.load(str(path))
    symbols = result.meta.symbols
    tf = result.meta.data_timeframe or "1h"

    ohlcv_data = {}
    ohlcv_root = DATA_DIR / "ohlcv"
    for symbol in symbols[:5]:  # limit to 5 symbols
        # Search for matching parquet file
        matches = list(ohlcv_root.rglob(f"**/{symbol}/{tf}.parquet"))
        if not matches:
            continue
        try:
            df = pd.read_parquet(matches[0])
            df.columns = [c.lower() for c in df.columns]
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.strftime("%Y-%m-%dT%H:%M:%S")
            records = df.to_dict(orient="records")
            ohlcv_data[symbol] = records
        except Exception as e:
            logger.warning("Could not load OHLCV for %s: %s", symbol, e)

    return jsonify(ohlcv_data)


@app.route("/api/compare")
def compare_backtests():
    """Compare multiple backtests. Query: ?ids=id1,id2,id3"""
    from flask import request
    ids_param = request.args.get("ids", "")
    if not ids_param:
        return jsonify({"error": "Provide ?ids=id1,id2,id3"}), 400

    ids = [i.strip() for i in ids_param.split(",") if i.strip()]
    comparison = []

    for bt_id in ids:
        path = BACKTESTS_DIR / f"{bt_id}.trades.json"
        if not path.exists():
            continue
        try:
            result = BacktestResult.load(str(path))
            stats = compute_stats_for_result(result)
            comparison.append({
                "id": bt_id,
                "strategy": result.meta.strategy,
                "engine": result.meta.engine,
                "stats": stats,
            })
        except Exception as e:
            logger.warning("Could not load %s for comparison: %s", bt_id, e)

    return jsonify(comparison)


# ─── Frontend ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(str(STATIC_DIR), path)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Trade Viewer Server")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument("--backtests", type=str, help="Backtests directory override")
    args = parser.parse_args()

    global BACKTESTS_DIR
    if args.backtests:
        BACKTESTS_DIR = Path(args.backtests)

    logger.info("Serving backtests from %s", BACKTESTS_DIR)
    logger.info("Trade Viewer: http://localhost:%d", args.port)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
