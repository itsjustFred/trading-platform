"""MT5 reference parser (audit): logs + XLSX -> per-trade CSVs + parity check.

Reproduces the reference side of the strategy-test-audit for
IntradayVolatilityBreakout without re-running MetaTrader:

  python parse_mt5_reference.py --reference-dir <dir>   (run from this audit/ directory)

The reference directory is taken from ``--reference-dir``, or from the
``TRADING_PLATFORM_REFERENCES`` environment variable when the flag is absent
(the flag wins if both are set). There is no built-in default: a missing or
non-existent directory is an error.

Inputs (read-only):
  <reference-dir>/{mt5-eurusd-h1,mt5-nas100-m15,mt5-sp500-m1}/*.log
  strategy-comparisons/intraday_volatility_breakout/{EURUSD_H1,NAS100_M15,SP500_M1}.xlsx

Outputs (beside this script, git-ignored via data/ — pass --out to override):
  mt5_trades_{eurusd,nas100,sp500}.csv   (deal-paired, USD-denominated PnL)
  xlsx_trades_{eurusd,nas100,sp500}.csv  (in/out-paired, EUR-denominated net)

Checks performed (fail loudly on mismatch):
  - deal tickets strictly sequential, volumes pair up, opened-lines match deals
  - log totals equal final-balance deltas (contract 100000 / 1 / 1)
  - XLSX per-trade (time/dir/prices) equals log deals; XLSX net sums equal
    the official summary stats (PF/WR/gross) to the cent
"""

from __future__ import annotations

import argparse
import math
import os
import pathlib
import re
import sys

REFERENCE_ENV_VAR = "TRADING_PLATFORM_REFERENCES"
CMP = pathlib.Path(__file__).resolve().parent.parent

JOBS = {
    "eurusd": ("mt5-eurusd-h1/eurusd_h1.log", "EURUSD_H1.xlsx", 100000.0),
    "nas100": ("mt5-nas100-m15/nas100_m15.log", "NAS100_M15.xlsx", 1.0),
    "sp500": ("mt5-sp500-m1/sp500_m1.log", "SP500_M1.xlsx", 1.0),
}

DEAL_RE = re.compile(
    r"(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})\s+deal #(\d+) (buy|sell) ([\d.]+) \S+ at ([\d.]+)"
)
OPEN_RE = re.compile(
    r"(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})\s+(Long|Short) opened @ ([\d.]+) \| Lots: ([\d.]+) \| SL: ([\d.]+)"
)


def _read_log(path: pathlib.Path) -> list[str]:
    return path.read_bytes().decode("utf-16", errors="replace").splitlines()


def _resolve_reference_dir(raw: str | None) -> pathlib.Path:
    """Return the MT5 reference directory from ``--reference-dir`` or the env var.

    The CLI value wins over ``TRADING_PLATFORM_REFERENCES``. Raises
    ``ValueError`` when neither is set or the directory does not exist, so the
    audit never reads a machine-specific default by accident.
    """
    value = raw or os.environ.get(REFERENCE_ENV_VAR)
    if not value:
        raise ValueError(
            f"no MT5 reference directory: pass --reference-dir or set {REFERENCE_ENV_VAR}"
        )
    ref = pathlib.Path(value).expanduser().resolve()
    if not ref.is_dir():
        raise ValueError(f"MT5 reference directory does not exist: {ref}")
    return ref


def _resolve_output_dir(raw: str) -> pathlib.Path:
    """Resolve the ``--out`` CLI argument and confine it (S8707).

    The argument must resolve inside the current working directory or the
    audit directory next to this script; anything else raises ``ValueError``
    instead of letting a crafted ``--out`` (``..``, absolute path,
    symlink escape) redirect output writes elsewhere.
    """
    candidate = pathlib.Path(raw).expanduser().resolve()
    allowed = (pathlib.Path.cwd().resolve(), CMP.resolve())
    if not any(candidate == base or base in candidate.parents for base in allowed):
        raise ValueError(f"--out {raw!r} escapes the allowed output bases")
    return candidate


def _output_csv_path(out_dir: pathlib.Path, filename: str) -> pathlib.Path:
    """Join a constant output filename onto a validated output dir (S8707).

    Re-checks containment of the final path so every file sink carries its
    own guard even if ``out_dir`` was built elsewhere.
    """
    base = out_dir.resolve()
    candidate = (base / filename).resolve()
    if candidate.parent != base:
        raise ValueError(f"refusing to write outside {base}: {filename!r}")
    return candidate


def parse_log_trades(log_path: pathlib.Path, contract: float) -> list[dict]:
    lines = _read_log(log_path)
    deals, opens = [], {}
    for line in lines:
        if "deal #" in line and "\tTrades\t" in line:
            m = DEAL_RE.search(line)
            if m:
                deals.append(
                    {
                        "time": m.group(1),
                        "deal": int(m.group(2)),
                        "side": m.group(3),
                        "vol": float(m.group(4)),
                        "price": float(m.group(5)),
                    }
                )
        if "opened @" in line:
            m = OPEN_RE.search(line)
            if m:
                opens[m.group(1)] = (
                    m.group(2),
                    float(m.group(3)),
                    float(m.group(4)),
                    float(m.group(5)),
                )
    if len(deals) % 2:
        raise ValueError(f"{log_path}: odd deal count {len(deals)}")
    tickets = [d["deal"] for d in deals]
    if tickets != list(range(tickets[0], tickets[0] + len(tickets))):
        raise ValueError(f"{log_path}: deal tickets not sequential")

    trades = []
    for i in range(0, len(deals), 2):
        d_in, d_out = deals[i], deals[i + 1]
        if d_in["vol"] != d_out["vol"]:
            raise ValueError(f"{log_path}: volume mismatch at deal {d_in['deal']}")
        if d_out["side"] == d_in["side"]:
            raise ValueError(
                f"{log_path}: same-side deal pair at {d_in['time']}: "
                f"deals {d_in['deal']}/{d_out['deal']} are both "
                f"{d_in['side']!r} (entry/exit must be opposite sides)"
            )
        direction = "LONG" if d_in["side"] == "buy" else "SHORT"
        sign = 1 if direction == "LONG" else -1
        pnl = sign * (d_out["price"] - d_in["price"]) * d_in["vol"] * contract
        metric = opens.get(d_in["time"], (None, None, None, None))
        side, open_price, lots, sl = metric[0], metric[1], metric[2], metric[3]
        if side is None or lots is None:
            raise ValueError(f"{log_path}: open line missing for {d_in['time']}")
        expected_side = "Long" if d_in["side"] == "buy" else "Short"
        if side != expected_side:
            raise ValueError(
                f"{log_path}: open side mismatch at {d_in['time']}: "
                f"{side!r} vs {expected_side!r}"
            )
        if open_price is None or not math.isclose(
            open_price, d_in["price"], rel_tol=1e-9, abs_tol=1e-9
        ):
            raise ValueError(
                f"{log_path}: open price mismatch at {d_in['time']}: "
                f"{open_price!r} vs {d_in['price']!r}"
            )
        # Tolerance (not ==): the EA logs Lots with a JS-float repr
        # (e.g. 1.6600000000000001) while deals log 1.66.
        if not math.isclose(lots, d_in["vol"], rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                f"{log_path}: open lots mismatch at {d_in['time']}: "
                f"{lots!r} vs {d_in['vol']!r}"
            )
        tol = abs(d_in["price"]) * 1e-5 + 1e-9
        reason = "sl" if abs(d_out["price"] - sl) <= tol else "time"
        trades.append(
            {
                "entry_time": d_in["time"],
                "exit_time": d_out["time"],
                "dir": direction,
                "vol": d_in["vol"],
                "entry": d_in["price"],
                "exit": d_out["price"],
                "sl": sl,
                "lots": lots,
                "pnl": pnl,  # full float precision (matches raw deal arithmetic)
                "reason": reason,
            }
        )
    # final balance cross-check
    finals = [l for l in lines if "final balance" in l]
    if finals:
        reported = float(finals[0].split("final balance")[1].split()[0])
        implied = 100000.0 + sum(t["pnl"] for t in trades)
        if abs(reported - implied) > 0.25:
            raise ValueError(
                f"{log_path}: balance mismatch reported={reported} implied={implied:.2f}"
            )
    return trades


def parse_xlsx_summary(df) -> dict:
    """Extract the official MT5 report summary block (label -> raw value).

    Labels sit in one cell, the value in the next non-empty cell of the same
    row (the MT5 report is a two-column label/value grid laid out across the
    sheet). Missing labels raise — an unexpected report layout must not
    silently skip the financial cross-check.
    """
    pd = __import__("pandas")
    labels = {
        "net": "Nettogewinn gesamt:",
        "gross_profit": "Bruttogewinn:",
        "gross_loss": "Bruttoverlust:",
        "profit_factor": "Profitfaktor:",
        "trade_count": "Gesamtanzahl Trades:",
        "won": "Gewonnene Trades",
    }
    summary: dict = {}
    for key, label in labels.items():
        found = None
        for i in range(len(df)):
            for j in range(df.shape[1]):
                cell = df.iat[i, j]
                if isinstance(cell, str) and cell.strip().startswith(label):
                    for k in range(j + 1, df.shape[1]):
                        v = df.iat[i, k]
                        if pd.notna(v):
                            found = v
                            break
                    break
            if found is not None:
                break
        if found is None:
            raise ValueError(f"summary label not found: {label!r}")
        summary[key] = found
    return summary


def _summary_number(raw) -> float:
    return float(str(raw).replace("\xa0", "").replace(" ", "").replace("%", ""))


def _summary_won_fraction(raw) -> float:
    """``"222 (45.12%)"`` (won count with percent share) -> 0.4512."""
    text = str(raw).replace("\xa0", "").replace(" ", "")
    match = re.search(r"\(([\d.,]+)%\)", text)
    if not match:
        raise ValueError(f"cannot parse won-percentage from {raw!r}")
    return float(match.group(1).replace(",", ".")) / 100.0


def parse_xlsx_trades(xlsx_path: pathlib.Path) -> list[dict]:
    pd = __import__("pandas")
    df = pd.read_excel(xlsx_path, sheet_name="Sheet1", header=None)
    hdr = None
    for i, row in df.iterrows():
        vals = [str(v) for v in row.values]
        if "Zeit" in vals and "Gewinn" in vals and "Richtung" in vals:
            hdr = i
            break
    if hdr is None:
        raise ValueError(f"{xlsx_path}: trades block not found")
    body = df.iloc[hdr + 1 :].copy()
    body.columns = [str(v) for v in df.iloc[hdr].values]
    body = body[body["Zeit"].notna() & body["Trade"].notna()]
    body = body[body["Typ"] != "balance"]
    body = body.reset_index(drop=True)
    if (body["Richtung"].values[::2] != "in").any() or (
        body["Richtung"].values[1::2] != "out"
    ).any():
        raise ValueError(f"{xlsx_path}: in/out alternation broken")
    trades = []
    for i in range(0, len(body), 2):
        a, b = body.iloc[i], body.iloc[i + 1]
        if float(a["Volumen"]) != float(b["Volumen"]):
            raise ValueError(f"{xlsx_path}: volume mismatch at row {i}")
        direction = "LONG" if a["Typ"] == "buy" else "SHORT"
        net = float(b["Gewinn"]) + float(b["Kommission"]) + float(b["Swap"])
        trades.append(
            {
                "entry_time": str(a["Zeit"]),
                "exit_time": str(b["Zeit"]),
                "dir": direction,
                "vol": float(a["Volumen"]),
                "entry": float(a["Preis"]),
                "exit": float(b["Preis"]),
                "swap": float(b["Swap"]),
                "profit": float(b["Gewinn"]),
                "net": round(net, 2),
            }
        )
    return trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(CMP / "audit"))
    ap.add_argument(
        "--reference-dir",
        default=None,
        help=f"directory holding the MT5 reference logs (default: ${REFERENCE_ENV_VAR})",
    )
    args = ap.parse_args()
    try:
        ref = _resolve_reference_dir(args.reference_dir)
    except ValueError as exc:
        ap.error(str(exc))
    out = _resolve_output_dir(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ok = True
    for name, (log_rel, xlsx_name, contract) in JOBS.items():
        log_trades = parse_log_trades(ref / log_rel, contract)
        xlsx_trades = parse_xlsx_trades(CMP / xlsx_name)

        import csv as _csv

        with open(_output_csv_path(out, f"mt5_trades_{name}.csv"), "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=list(log_trades[0].keys()), lineterminator="\n")
            w.writeheader()
            w.writerows(log_trades)
        with open(_output_csv_path(out, f"xlsx_trades_{name}.csv"), "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=list(xlsx_trades[0].keys()), lineterminator="\n")
            w.writeheader()
            w.writerows(xlsx_trades)

        # cross-check: same trade keys and prices (entry AND exit times,
        # direction, prices). Volume is deliberately NOT compared: the log
        # trades come from the USD runs and the XLSX trades from the EUR
        # runs (separate backtests with independent sizing), so volumes
        # legitimately differ between the two sources.
        log_keys = [
            (t["entry_time"], t["exit_time"], t["dir"], t["entry"], t["exit"])
            for t in log_trades
        ]
        xlsx_keys = [
            (t["entry_time"], t["exit_time"], t["dir"], t["entry"], t["exit"])
            for t in xlsx_trades
        ]
        match = log_keys == xlsx_keys
        log_total = sum(t["pnl"] for t in log_trades)
        xlsx_net = sum(t["net"] for t in xlsx_trades)
        wins = sum(1 for t in log_trades if t["pnl"] > 0)
        status = "OK" if match else "MISMATCH"
        if not match:
            ok = False

        # XLSX financial data vs the report's own official summary block:
        # every computed aggregate must reproduce the printed stats to the
        # cent (net, gross profit/loss, PF, trade count, win rate).
        # MT5 computes the summary on the NET per-trade result (Gewinn +
        # Kommission + Swap), so the gross classification uses `net`, not
        # the raw `Gewinn` column.
        pd = __import__("pandas")
        xlsx_df = pd.read_excel(CMP / xlsx_name, sheet_name="Sheet1", header=None)
        summary = parse_xlsx_summary(xlsx_df)
        gross_profit = sum(t["net"] for t in xlsx_trades if t["net"] > 0)
        gross_loss = sum(t["net"] for t in xlsx_trades if t["net"] < 0)
        xlsx_wins = sum(1 for t in xlsx_trades if t["net"] > 0)
        pf = abs(gross_profit / gross_loss) if gross_loss else float("inf")
        wr = xlsx_wins / len(xlsx_trades) if xlsx_trades else 0.0
        summary_checks = {
            "net": (xlsx_net, _summary_number(summary["net"]), 0.01),
            "gross_profit": (gross_profit, _summary_number(summary["gross_profit"]), 0.01),
            "gross_loss": (gross_loss, _summary_number(summary["gross_loss"]), 0.01),
            "profit_factor": (pf, _summary_number(summary["profit_factor"]), 1e-4),
            "trade_count": (
                float(len(xlsx_trades)),
                _summary_number(summary["trade_count"]),
                0.0,
            ),
            "win_rate": (wr, _summary_won_fraction(summary["won"]), 1e-4),
        }
        summary_ok = True
        for check_name, (computed, reported, tol) in summary_checks.items():
            if abs(computed - reported) > tol:
                summary_ok = False
                ok = False
                print(
                    f"  {name} SUMMARY MISMATCH {check_name}: "
                    f"computed={computed:.6f} reported={reported:.6f}",
                    flush=True,
                )
        print(
            f"{name}: n={len(log_trades)} log_total={log_total:,.2f} "
            f"xlsx_net={xlsx_net:,.2f} wins={wins} xlsx==log: {status} "
            f"summary: {'OK' if summary_ok else 'MISMATCH'}",
            flush=True,
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
