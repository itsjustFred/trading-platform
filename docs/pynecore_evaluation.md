# PyneCore Evaluation

> Evaluiert: 2026-02-26
> Version: pynesys-pynecore 6.3.5 (GitHub: PyneSys/pynecore)

---

## Installation

```bash
# Nicht auf PyPI verfügbar — Installation von GitHub:
pip install git+https://github.com/PyneSys/pynecore.git
pip install typer typing_extensions  # Abhängigkeiten die fehlen können
```

## Ergebnis: ✅ Positiv — PyneCore funktioniert und ist als Engine-Adapter nutzbar

---

## Test: SMA Crossover Strategie

- **Daten:** AAPL 1h, 5078 Bars (2023-03 → 2026-02)
- **Laufzeit:** 0.4 Sekunden → ~12.700 Bars/Sekunde
- **Ergebnis:** 92 Trades, $28K Net Profit, 40% Win Rate, Profit Factor 1.30

## API-Key

- **Nicht nötig für .py Strategien** — nur für Pine Script (.pine) Kompilierung
- **Pine v6 only** — v4/v5 werden nicht unterstützt
- API-Key via https://pynesys.io (kostenlos? — nicht getestet, da unsere Strategien v5 sind)
- **Empfehlung:** Pine-Strategien manuell nach PyneCore-Python übersetzen statt zu kompilieren

## Trade-Extraktion

PyneCore gibt Trades als CSV aus via `--trade` Flag:

```bash
pyne run script.py data.csv --trade trades.csv --strat stats.csv
```

**Trade CSV Format:**
```
Trade #,Bar Index,Type,Signal,Date/Time,Price USD,Contracts,Profit USD,Profit %,
Cumulative profit USD,Cumulative profit %,Run-up USD,Run-up %,Drawdown USD,Drawdown %
```

**Stats CSV Format:**
Standard-Metriken: Net Profit, Sharpe, Sortino, Profit Factor, Max Drawdown, Win Rate, etc.

## Daten-Format

- **Input:** CSV, TXT, JSON oder eigenes `.ohlcv` Binary-Format
- **Auto-Konvertierung:** CSV wird automatisch zu .ohlcv konvertiert beim ersten Run
- **CSV-Format:** Muss timestamp, open, high, low, close, volume Spalten haben
- **Parquet:** Nicht direkt unterstützt — Konvertierung Parquet → CSV nötig (trivial)

## Unterstützte Features

### ta.* (Technical Analysis) — 70+ Funktionen
```
sma, ema, rma, wma, vwma, hma, alma, swma           # Moving Averages
rsi, stoch, cci, mfi, cmo, roc, mom, tsi, wpr        # Oscillators
macd, bb, bbw, kc, kcw                                # MACD, Bollinger, Keltner
atr, tr, supertrend, sar                              # Volatility
cross, crossover, crossunder                          # Crossover Detection
highest, lowest, highestbars, lowestbars              # Extremes
vwap, obv, pvt, accdist, wad, nvi, pvi               # Volume
linreg, correlation, stdev, variance, dev             # Statistics
pivothigh, pivotlow, pivot_point_levels               # Pivot Points
```

### strategy.* — Vollständig
```
entry, exit, close, close_all, cancel, cancel_all, order  # Order Management
long, short                                               # Direction
percent_of_equity, cash, fixed                            # Position Sizing
position_size, position_avg_price                         # Position Info
equity, netprofit, grossprofit, grossloss                 # Equity
opentrades, closedtrades, wintrades, losstrades           # Trade Counts
max_drawdown, max_runup, openprofit                       # Risk Metrics
```

### Fehlende Features
- `request.security()` (Multi-Timeframe) — **nicht verfügbar**
- Pine v4/v5 Kompilierung — nur v6
- Parquet-Input — muss zu CSV konvertiert werden
- `array.*`, `map.*`, Custom Types — nicht getestet

## Performance

| Bars | Zeit | Bars/Sekunde |
|------|------|-------------|
| 5.078 (AAPL 1h) | 0.4s | ~12.700 |
| ~31.000 (XAUUSD 1h) | ~2.5s (geschätzt) | ~12.400 |
| ~124.000 (XAUUSD 15m) | ~10s (geschätzt) | ~12.400 |

Performant genug für alle aktuellen Datenmengen.

## Strategie-Schreibweise (PyneCore Python)

```python
"""
@pyne
Strategy Description
"""
from pynecore import Series
from pynecore.lib import script, input, strategy, ta, plot, color

@script.strategy(
    title="My Strategy",
    overlay=True,
    default_qty_type=strategy.percent_of_equity,
    default_qty_value=100,
    initial_capital=100000,
)
def main(
    src: Series[float] = input.source("close", title="Source"),
    fast: int = input.int(10, title="Fast Period"),
):
    fast_sma = ta.sma(src, fast)
    if ta.crossover(fast_sma, ta.sma(src, 30)):
        strategy.entry("Long", strategy.long)
    if ta.crossunder(fast_sma, ta.sma(src, 30)):
        strategy.close("Long")
```

## Integration als Engine-Adapter

**Architektur:**
1. Parquet → CSV (temporär) — trivial mit pandas
2. `pyne run strategy.py data.csv --trade trades.csv --strat stats.csv`
3. Trades CSV → Universal Trade Format (BacktestResult)
4. Cleanup temporärer Dateien

**Vorteile gegenüber manuellem Pine→Python Converter:**
- Kein eigener Indicator-Code nötig (ta.rsi, ta.sma etc. sind identisch zu Pine)
- strategy.entry/exit/close funktioniert nativ
- Position Sizing, Commission etc. built-in
- Trades und Stats werden automatisch berechnet

**Limitierungen:**
- Kein Multi-Timeframe (request.security) — nur Single-TF
- Pine v5 Strategien müssen manuell nach PyneCore-Python überführt werden
- CSV als Zwischenformat (kein direktes Parquet)
