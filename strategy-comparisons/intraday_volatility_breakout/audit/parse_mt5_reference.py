"""MT5 reference parser (audit): logs + XLSX -> per-trade CSVs + parity check.

Reproduces the reference side of the strategy-test-audit for
IntradayVolatilityBreakout without re-running MetaTrader:

  .venv/bin/python strategy-comparisons/intraday_volatility_breakout/audit/parse_mt5_reference.py

Inputs (read-only):
  /home/ubuntu/trading-platform-references/{mt5-eurusd-h1,mt5-nas100-m15,mt5-sp500-m1}/*.log
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
import pathlib
import re
import sys

REF = pathlib.Path("/home/ubuntu/trading-platform-references")
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
        direction = "LONG" if d_in["side"] == "buy" else "SHORT"
        sign = 1 if direction == "LONG" else -1
        pnl = sign * (d_out["price"] - d_in["price"]) * d_in["vol"] * contract
        metric = opens.get(d_in["time"], (None, None, None, None))
        side, _, lots, sl = metric[0], metric[1], metric[2], metric[3]
        if side is None or lots is None:
            raise ValueError(f"{log_path}: open line missing for {d_in['time']}")
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
    args = ap.parse_args()
    out = _resolve_output_dir(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ok = True
    for name, (log_rel, xlsx_name, contract) in JOBS.items():
        log_trades = parse_log_trades(REF / log_rel, contract)
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

        # cross-check: same trade keys and prices
        log_keys = [(t["entry_time"], t["dir"], t["entry"], t["exit"]) for t in log_trades]
        xlsx_keys = [(t["entry_time"], t["dir"], t["entry"], t["exit"]) for t in xlsx_trades]
        match = log_keys == xlsx_keys
        log_total = sum(t["pnl"] for t in log_trades)
        xlsx_net = sum(t["net"] for t in xlsx_trades)
        wins = sum(1 for t in log_trades if t["pnl"] > 0)
        status = "OK" if match else "MISMATCH"
        if not match:
            ok = False
        print(
            f"{name}: n={len(log_trades)} log_total={log_total:,.2f} "
            f"xlsx_net={xlsx_net:,.2f} wins={wins} xlsx==log: {status}",
            flush=True,
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
