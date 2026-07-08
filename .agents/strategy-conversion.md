---
name: strategy-conversion
description: Definiert das verbindliche Konvertierungs-Protokoll für Handelsstrategien inklusive MQL5-spezifischer Regeln. Führt die Konvertierung nach einer strikten 8-Schritte-Checkliste durch.
---

# Agent: Strategy Conversion Protocol

## Wann diesen Skill verwenden

Verwende diesen Skill, wenn eine Handelsstrategie aus einer anderen Sprache oder Plattform in die vorliegende Python-Backtesting-Plattform überführt werden muss. Typische Ausgangssprachen sind Pine Script (TradingView), MQL5 (MetaTrader 5), C#/QuantConnect (LEAN), oder existierende Python-Implementierungen mit abweichender Engine-API. Der Skill ist Pflicht, sobald der User eine der unter "Trigger-Bedingung" genannten Formulierungen verwendet oder explizit eine Validierung gegen Referenz-Backtests anfordert.

## Beschreibung

Dieser Agent definiert das verbindliche Konvertierungs-Protokoll für Handelsstrategien. Er wird aktiviert, wenn der User sagt:

> „Konvertiere Strategie X nach dem Agent-Protokoll"

Das Protokoll ist eine **strikt sequenzielle Pflicht-Checkliste**. Jeder Schritt muss vollständig abgeschlossen sein, bevor der nächste beginnt. Kein Schritt ist optional. Bei nicht behebbaren Fehlern bricht der Agent mit einem strukturierten Report ab.

---

## Trigger-Bedingung

**Aktiviere dieses Protokoll ausschließlich, wenn der User eine der folgenden Formulierungen verwendet:**
- „Konvertiere Strategie X nach dem Agent-Protokoll"
- „Konvertiere X (Agent-Protokoll)"
- „Verwende das Konvertierungs-Protokoll für Strategie X"

---

## Pflicht-Workflow (8 Schritte)

### SCHRITT 1 — Analyse des Quell-Codes

**Eingabe:** Quell-Code der Strategie (Pine Script v5/v6, C#, QuantConnect, MQL5, Python, etc.)

**Führe aus:**
1. Lese den vollständigen Quell-Code.
2. Beantworte zwingend alle folgenden Fragen schriftlich (als Ausgabe in der Session):
   - **Entry-Trigger:** Welcher exakte Preis / welches Indikator-Event triggert den Einstieg? (z. B. „Close der Bar überschreitet EMA(20) von unten")
   - **Exit-Trigger:** Welcher Preis / welches Event triggert den Ausstieg? Gibt es mehrere Exit-Bedingungen (Trailing Stop, Take-Profit-Stufen, Time-Exit)?
   - **Stop-Loss-Berechnung:** Wird der Stop statisch, ATR-basiert oder Swing-basiert berechnet? Formel notieren.
   - **Partial Exits:** Gibt es Teil-Ausstiege? Wenn ja: bei welchen Bedingungen und mit welchem Anteil (z. B. 50 % bei TP1)?
   - **Trailing Stops:** Wird der Stop nachgezogen? Wenn ja: nach welcher Logik (z. B. Supertrend-Band, ATR * Multiplikator)?
   - **Position Sizing:** Gibt es eine explizite Sizing-Logik (z. B. `percent_of_equity`, fixe Contracts)?
   - **Verwendete Indikatoren:** Liste alle Indikatoren mit ihren Parametern auf.
3. **Gate:** Können nicht alle Fragen beantwortet werden → frage den User nach dem fehlenden Quell-Code. Fahre nicht fort.

**Ausgabe:** Schriftliche Analyse-Zusammenfassung in der Session.

---

### SCHRITT 2 — Dokumentation (Ground-Truth Markdown)

**Eingabe:** Analyseergebnis aus Schritt 1.

**Führe aus:**
1. Erstelle die Datei `strategies/<name>/README.md` (wobei `<name>` der snake_case-Name der Strategie ist).
2. Die Datei muss folgende Abschnitte enthalten:

```markdown
# <Strategiename>

## Quelle
- Original: <Sprache/Plattform, Link oder Beschreibung>
- Konvertiert: <Datum>

## Kernlogik

### Entry
<Exakte Bedingung(en) mit Indikator-Parametern>

### Exit
<Exakte Bedingung(en), alle Exit-Typen aufgelistet>

### Stop-Loss
<Berechnungsformel>

### Trailing Stop (falls vorhanden)
<Logik und Update-Bedingung>

### Partial Exits (falls vorhanden)
| Stufe | Anteil | Bedingung |
|-------|--------|-----------|
| TP1   | 50 %   | ...       |

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|

## Risk-Management
- Stop-Distanz: <ATR-basiert / statisch / Swing>
- Position Sizing: <Formel oder Methode>
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
<vectorbt / pynecore / lean> — Begründung
```

3. **Gate:** Die Datei muss physisch auf Disk existieren, bevor Schritt 3 beginnt.

**Ausgabe:** `strategies/<name>/README.md`

---

### SCHRITT 3 — Framework-Wahl

**Führe aus:**
Wähle das Engine-Framework nach dieser Entscheidungsmatrix und dokumentiere die Wahl in `strategies/<name>/README.md` unter „Framework-Empfehlung":

| Kriterium | Empfehlung |
|-----------|------------|
| Vektoriell, schnell, keine Pine-spezifischen Funktionen | `vectorbt` |
| Strategie nutzt `ta.*`-Funktionen aus Pine (rsi, supertrend, sar, etc.) | `pynecore` |
| Komplexe Event-Logik, Order-Management, Multi-Asset-Portfolios | `lean` |
| User hat Engine explizit vorgegeben | Vorgabe des Users verwenden |
| MQL5 EAs mit Indikatoren aus `ta.*` oder barbasierter Event-Logik (kein `CTrade`, kein Portfolio) | `vectorbt` |
| MQL5 mit komplexer Multi-Asset-Portfolio-Logik, `CTrade`, Bracket-Orders, Account-Abfragen | `lean` |
| MQL5 mit PineScript-Äquivalenten (z. B. `iRSI`, `iATR`, einfache Zeitfilter) | `pynecore` |

**Hinweis für `pynecore`:** Die Strategie muss manuell in PyneCore-Python (mit `@pyne`-Decorator) übersetzt werden — kein automatischer Pine-v5-Compiler vorhanden. Vgl. `docs/pynecore_evaluation.md`.

**Hinweis für MQL5 → `vectorbt`:** MQL5 arbeitet tickbasiert (`OnTick`), vectorbt barbasiert. Die Konvertierung muss die tickbasierte Logik auf Bar-Events abbilden: Entry-Bedingungen werden am Close der Signal-Bar ausgewertet, Stops werden intrabar durch den Low/High der Exit-Bar approximiert.

**Gate:** Die Wahl ist dokumentiert. Fahre nicht fort, ohne das Framework festgelegt zu haben.

---

### SCHRITT 4 — Coding der Strategie

**Eingabe:** Ground-Truth aus `strategies/<name>/README.md`, gewähltes Framework.

**Führe aus:**
1. Erstelle `strategies/<name>/<name>.py`.
2. Das `STRATEGY_META`-Dictionary muss vollständig sein:

```python
STRATEGY_META = {
    "name": "<Strategiename>",
    "version": "1.0.0",
    "source_url": "<direkter Link zur Originalstrategie (Gist/Repo/Script)>",
    "notes": "<Markdown: bekannte Bugs, Limitierungen, Besonderheiten>",
    "tags": {
        "type": ["<trend-following|mean-reversion|breakout|momentum|...>"],
        "source": "<original-quelle oder link>",
        "link": "<url falls verfügbar>",
        "asset_class": ["<equities|forex|crypto|futures>"],
        "complexity": "<simple|medium|complex>",
    },
}
```

   **Pflichtregel:** `source_url` und `notes` müssen immer explizit gefüllt sein (kein Weglassen).
   - `source_url`: direkter URL zur primären Quelle (GitHub, Gist, TradingView, Repo-Datei).
   - `notes`: Markdown-Notizen mit Bugs, bekannten Abweichungen zur Original-Logik, offenen Punkten und wichtigen Betriebs-Hinweisen.

3. **Pflicht — Trade-`meta`-Felder:** Schreibe alle für die spätere Validierung relevanten Indikator-Werte in `Trade.meta`. Mindestpflichtfelder:
   - `sl_price` — berechneter Stop-Loss-Preis zum Zeitpunkt des Entries
   - Alle weiteren Indikator-Werte, die Entry/Exit bestimmen (z. B. `atr_value`, `supertrend_band`, `rsi_value`, `tp1_price`)
   - Bei Trailing Stops: `trailing_stop_at_exit` — Wert des Trailing-Stop-Levels beim Exit-Bar

   **STRIKTE REGEL:** Jeder Indikator, der Entry-, Exit-, Stop-Loss- oder Trailing-Stop-Logik direkt bestimmt, **muss** im Backtest pro Trade in `Trade.meta` landen. Das ist nicht optional. Beispiele:
   - RSI-/Stoch-/MACD-/VIX-Wert beim Entry, wenn der Indikator den Einstieg filtert oder triggert
   - SMA-/EMA-/Supertrend-/Bollinger-/Channel-Level, wenn diese Werte Entry oder Exit bestimmen
   - ATR-Wert und daraus abgeleitete Stop-/Take-Profit-Level, wenn ATR das Risk-Sizing oder Exit-Management beeinflusst
   - `trailing_stop_at_exit`, wenn der Exit über einen dynamischen Stop erfolgt

   Für `vectorbt` bedeutet das: `generate_signals()` soll die Logik-Indikatoren über `indicators_dict` (bzw. kompatible Meta-Spalten nur für Legacy-Code) an den Adapter liefern, damit sie beim Trade-Bau automatisch in `trade.meta` geschrieben werden.
   Für `pynecore` bedeutet das: ergänze eine Hilfsfunktion wie `build_trade_context(df, params)`, damit der Adapter die relevanten Indikatorwerte nach dem Run an die Trades anreichern kann.

   Beispiel für vectorbt-Strategies: Gib `meta={...}` direkt beim Erstellen des `Trade`-Objekts mit:
   ```python
   trade = Trade(
       ...,
       meta={
           "sl_price": round(stop_price, 6),
           "atr_value": round(atr_at_entry, 6),
           "trailing_stop_at_exit": round(trailing_stop, 6),
       }
   )
   ```

4. Für vectorbt-Strategies: Die bevorzugte Schnittstelle ist `return entries, exits, indicators_dict`. Darin müssen alle logikrelevanten Indikatoren enthalten sein; `sl_price`/`stop_loss` und ATR-bezogene Werte sind Pflicht, sobald sie Risk-Sizing oder Exit-Logik beeinflussen. Legacy-DataFrame-Rückgaben sind nur für Altcode tolerierbar und müssen die gleiche Meta-Information transportieren.

5. **Partial Exits (Pflicht):** Wenn die Pine-Strategie Teilverkäufe nutzt (z. B. TP1 50 %, Runner 50 %), muss die Strategie ein Array von Custom-Exits oder ein Size-Array zurückgeben, das von Vectorbt unterstützt wird. Generiere zusätzlich für jeden Partial Exit ein Marker-Drawing im `drawings` Array (z. B. `shape: arrowDown`, `text: "TP1 50%"`).

6. **Plots (Pflicht):** Alle `plot()`, `plotshape()` oder `bgcolor()` aus PineScript **müssen** in das `drawings` Array (`type: line`, `marker`, `box`) übersetzt werden, damit der User die Trades visuell verifizieren kann.

7. **Gate:** Compilierbarkeit prüfen:
   ```bash
   python -c "import importlib.util; spec = importlib.util.spec_from_file_location('s', 'strategies/<name>/<name>.py'); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); print('OK')"
   ```
   Schlägt die Prüfung fehl → korrigiere Syntax-/Import-Fehler. Fahre nicht fort.

#### MQL5 → vectorbt Minimal-Mapping

Bei der Konvertierung von MQL5 in eine vectorbt-Strategie gelten folgende bevorzugte Übersetzungen:

| MQL5-Konstrukt | Python-Äquivalent | Hinweise |
|----------------|-------------------|----------|
| `iATR(_Symbol, PERIOD_D1, period)` | `talib.ATR(high, low, close, timeperiod=period)` oder `pandas_ta.atr(high, low, close, length=period)` | Vektorisiert auf dem gesamten DataFrame berechnen; ATR der Entry-Bar für Stop-Distanz verwenden. |
| `iRSI(_Symbol, PERIOD_H1, period, PRICE_CLOSE)` | `talib.RSI(close, timeperiod=period)` | Gleitender Mittelwert; bei `PRICE_OPEN` stattdessen `open`-Spalte übergeben. |
| `iTime(_Symbol, PERIOD_H1, shift)` | `df.index[-(shift+1)]` bzw. vektoriell `df.index` | MQL5 zählt rückwärts (0 = aktueller Bar); Python-DataFrame-Zugriff analog. |
| `PositionSelect(_Symbol)` | `len(open_trades) > 0` (im barbasierten Model) | In vectorbt trackt die Engine die Positionsgröße; in `generate_signals` nur Entry/Exit-Signale erzeugen. |
| `SymbolInfoDouble(_Symbol, SYMBOL_LAST)` | `df['close']` im barbasierten Backtest | `SYMBOL_LAST` ist der letzte Tick-Preis; in einer Bar-Engine wird der Close als Entry-Proxy verwendet. |
| `ORDER_FILLING_FOK` | Nicht explizit erforderlich | In vectorbt werden Orders als Market-Orders am nächsten Bar simuliert; Slippage-Model ggf. im Adapter konfigurieren. |
| `CTrade` Klasse (Bracket-Orders, Modify) | `lean` bevorzugen oder manuelle Stop-Logik in vectorbt via `sl_stop` / `tsl_stop` Parameter | `CTrade`-Level-Abstraktion hat kein direktes vectorbt-Pendant. |
| `Magic Number` | Strategie-String in `STRATEGY_META["name"]` | Dient zur Filterung und Zuordnung von Trades in der Datenbank. |

Beispielhafte vollständige `generate_signals`-Signatur für MQL5-Konvertierung:

```python
def generate_signals(df: pd.DataFrame, params: dict):
    # MQL5: iATR(_Symbol, PERIOD_D1, 14)
    atr = pandas_ta.atr(df["high"], df["low"], df["close"], length=params["atr_period"])

    # MQL5 Entry: Close > EMA(20) and ATR-Filter
    ema20 = df["close"].ewm(span=params["ema_period"], adjust=False).mean()
    entries = (df["close"] > ema20) & (atr > params["atr_threshold"])

    # MQL5 Exit: Stop-Loss = Close - 1.5 * ATR
    sl_stop = (1.5 * atr / df["close"]).where(entries)

    indicators_dict = {
        "sl_stop": sl_stop,
        "atr_value": atr,
        "ema20": ema20,
    }
    return entries, pd.Series(False, index=df.index), indicators_dict
```

**Ausgabe:** `strategies/<name>/<name>.py`

---

### SCHRITT 5 — Data Pre-Check

**Führe aus:**
1. Öffne `~/data/catalog.json` und suche nach einem Symbol/Timeframe-Kombination, die für diese Strategie sinnvoll ist:
   ```bash
   python -c "
   import json, pathlib
   cat = json.loads(pathlib.Path('~/data/catalog.json').expanduser().read_text())
   for entry in cat.get('symbols', cat if isinstance(cat, list) else []):
       print(entry)
   " | head -40
   ```
2. Wähle das am besten geeignete Symbol und Timeframe aus dem Katalog.
3. Wenn kein geeignetes Symbol verfügbar ist: Informiere den User mit einer klaren Nachricht:
   > „Kein passendes Symbol/Timeframe im lokalen Datenkatalog gefunden. Benötigt: `<asset_class>` auf `<timeframe>`. Bitte Daten herunterladen oder ein vorhandenes Symbol wählen."
   Fahre erst nach Rückmeldung des Users fort.

**Gate:** Symbol und Timeframe sind festgelegt und lokal verfügbar.

**Ausgabe:** Festgelegtes Testsymbol und Timeframe (z. B. `AAPL 1h`).

---

### SCHRITT 6 — Execution (Backtest starten)

**Führe aus:**
```bash
python -m runner.cli run \
  --engine <engine> \
  --strategy strategies/<name>/<name>.py \
  --symbols <SYMBOL> \
  --timeframe <tf> \
  --risk-pct 0.01 \
  --capital 100000
```

- Verwende `--risk-pct 0.01` (1 % Risiko pro Trade), außer der User hat explizit einen anderen Wert vorgegeben.
- Notiere den vollständigen Ausgabepfad der generierten `*.trades.json`-Datei.

**Gate:**
- Der Backtest muss mit Exit-Code 0 enden.
- Das Output-File muss unter `~/data/backtests/` existieren.
- Die Datei muss mindestens **10 Trades** enthalten. Wenn weniger Trades erzeugt wurden: überprüfe die Signal-Logik (zu restriktive Bedingungen?) und korrigiere sie. Fahre nicht fort.

**Ausgabe:** Pfad zur `*.trades.json`-Datei.

---

### SCHRITT 7 — Validation Loop ⚠️ KRITISCHER SCHRITT ⚠️

**Dieser Schritt ist nicht optional. Er muss vollständig durchgeführt werden.**

**Eingabe:** Pfad zur `*.trades.json`, OHLCV-Daten (`~/data/ohlcv/.../<symbol>/<timeframe>.parquet`).

#### 7.1 — Trade-Selektion

Lade die `*.trades.json` und identifiziere:
- **Trade A:** Trade mit dem **größten Verlust** (niedrigstes `pnl`)
- **Trade B:** Trade mit dem **größten Gewinn** (höchstes `pnl`)
- **Trade C:** Trade mit **Partial Exits** (`len(exits) > 1`) — falls keiner existiert, wähle den Trade mit der längsten `holding_bars`-Dauer
- **Trade D:** Ein **zufälliger Trade** (z. B. Index `len(trades) // 3`)
- **Trade E:** Ein weiterer **zufälliger Trade** (z. B. Index `len(trades) * 2 // 3`)

Nutze das folgende Skript als Vorlage und passe den Pfad an:
```python
import json, random
data = json.loads(open("~/data/backtests/<file>.trades.json".replace("~", __import__("os").path.expanduser("~"))).read())
trades = data["trades"]
trade_a = min(trades, key=lambda t: t["pnl"])
trade_b = max(trades, key=lambda t: t["pnl"])
partial = [t for t in trades if len(t["exits"]) > 1]
trade_c = partial[0] if partial else sorted(trades, key=lambda t: t.get("holding_bars", 0))[-1]
trade_d = trades[len(trades) // 3]
trade_e = trades[len(trades) * 2 // 3]
for label, t in [("A=MaxLoss", trade_a), ("B=MaxWin", trade_b), ("C=Partial/Long", trade_c), ("D=Random1", trade_d), ("E=Random2", trade_e)]:
    print(f"\n--- {label} (id={t['id']}) ---")
    print(f"  Entry: {t['entry_time']} @ {t['entry_price']} | Qty: {t['quantity']}")
    for ex in t['exits']:
        print(f"  Exit:  {ex['time']} @ {ex['price']} | pnl={ex.get('pnl')} | reason={ex.get('reason')}")
    print(f"  Total PnL: {t['pnl']} | meta: {t.get('meta', {})}")
```

#### 7.2 — Pflichtprüfungen pro Trade

**Führe für jeden der 5 Trades (A–E) ALLE folgenden Prüfungen durch:**

##### Prüfung R — Risk-Sizing

Berechne manuell oder per Skript:

```python
# Für Trade X:
entry_price    = trade["entry_price"]
quantity       = trade["quantity"]
sl_price       = trade["meta"]["sl_price"]          # aus Trade.meta
stop_distance  = abs(entry_price - sl_price)
risk_amount    = stop_distance * quantity            # = geplantes Risiko in $
# Equity zum Zeitpunkt des Entries: rekonstruiere aus equity_curve oder kumuliere PnL
equity_at_entry = <equity vor diesem Trade>
risk_pct_actual = risk_amount / equity_at_entry

assert risk_pct_actual <= 0.011, (
    f"Trade {trade['id']}: Risk-Sizing-Fehler! "
    f"Erwartet: ≤1.0%, Tatsächlich: {risk_pct_actual*100:.2f}%"
)
```

**Akzeptierte Toleranz:** `risk_pct_actual ≤ 1.1 %` (10 % Puffer für Leverage-Cap-Capping).
**Bei Trade A (Max Loss):** Überprüfe zusätzlich, ob der tatsächliche Verlust (`pnl` absolut) nicht mehr als **2× das geplante Risiko** übersteigt (mögliches Gap-Risk).

##### Prüfung L — Entry/Exit-Logik

1. **Entry-Preis:** Lade den OHLCV-Datensatz und finde den Bar zum `entry_time`. Verifiziere:
   - Entry-Preis ≈ `close`-Preis dieses Bars (Toleranz: ±0.1 %)
   - Der Indikator-Cross (oder die Entry-Bedingung aus Schritt 1) trifft auf dem **vorherigen Bar** zu (Lookahead-Schutz).

2. **Exit-Preis (bei Trailing Stop):** Prüfe, ob `exit_price` ≈ `trailing_stop_at_exit` aus `Trade.meta` (Toleranz: ±0.1 %). Wenn der Trailing-Stop-Wert fehlt, ist das selbst ein Fehler → gehe zu Schritt 7.3.

3. **Partial Exit (Trade C):** Überprüfe, ob der erste Exit-Preis der TP1-Bedingung entspricht (aus Ground-Truth in `strategies/<name>/README.md`).

4. **Dokumentiere** die Ergebnisse als kurze Tabelle in der Session:

| Trade | R-Check | L-Entry | L-Exit | Status |
|-------|---------|---------|--------|--------|
| A     | ✅/❌   | ✅/❌   | ✅/❌  | OK/FAIL|
| B     | ✅/❌   | ...     | ...    | ...    |
| C     | ...     | ...     | ...    | ...    |
| D     | ...     | ...     | ...    | ...    |
| E     | ...     | ...     | ...    | ...    |

#### 7.3 — Fehlerbehandlung und Retry-Zyklus

**Wenn mindestens eine Prüfung fehlschlägt:**

1. Klassifiziere den Fehler:
   - **Risk-Sizing-Fehler:** `sl_price` fehlt in `Trade.meta`, `stop_distance` falsch berechnet, Sizing-Formel in `generate_signals` fehlerhaft.
   - **Logic-Fehler:** Falscher Entry/Exit-Bar (Lookahead-Bug), falscher Indikator-Wert, falscher Partial-Exit-Level.

2. Korrigiere den Fehler in `strategies/<name>/<name>.py`.

3. Starte den Backtest neu (Schritt 6) und wiederhole die Validierung (Schritt 7).

4. **Maximale Versuche: 3.**

5. **Wenn nach 3 Versuchen noch Fehler vorliegen:**
   - **Brich die Konvertierung ab.**
   - Erstelle einen Failure-Report mit diesem Format in der Session:

```
## KONVERTIERUNGS-FEHLER-REPORT: <Strategiename>

**Versuch:** 3/3 — Abbruch

### Fehlgeschlagene Prüfungen:
| Trade | Prüfung | Erwarteter Wert | Tatsächlicher Wert | Fehlertyp |
|-------|---------|-----------------|---------------------|-----------|
| <id>  | <R/L>   | <erwartet>      | <tatsächlich>       | <Typ>     |

### Analyse der Ursache:
<Detaillierte Erklärung, warum die Prüfung fehlschlägt>

### Empfehlung:
<Konkrete nächste Schritte für den User>
```

**Gate:** Alle 5 Trades bestehen alle Prüfungen (oder Abbruch mit Report). Fahre nicht fort.

**Ausgabe:** Validierungstabelle (✅ alle grün) oder Failure-Report.

---

### SCHRITT 8 — Regression Test erstellen

**Eingabe:** Validierte Strategie aus Schritt 7.

**Führe aus:**
1. Erstelle das Verzeichnis `tests/strategies/` falls es nicht existiert.

2. Erstelle `tests/strategies/test_<name>.py` mit folgendem Pflichtinhalt:

```python
"""
Regression-Test für Strategie: <Strategiename>

Smoke-Test: stellt sicher, dass zukünftige Core-Updates die Strategie-Logik
nicht unbemerkt brechen. Getestet auf einem festen, kurzen Zeitraum.

Validiert am: <Datum der Validierung>
Symbol/Timeframe: <SYMBOL> / <TIMEFRAME>
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter  # oder pynecore_adapter


STRATEGY_PATH = str(Path(__file__).parent.parent.parent / "strategies" / "<name>.py")

# Fester, kurzer Zeitraum (ca. 3 Monate) — NICHT verändern
TEST_START = "<YYYY-MM-DD>"  # Startdatum des Regressionszeitraums
TEST_END   = "<YYYY-MM-DD>"  # Enddatum (ca. 3 Monate nach Start)
TEST_SYMBOL = "<SYMBOL>"
TEST_TIMEFRAME = "<TIMEFRAME>"

# Erwartete Werte — aus dem validierten Backtest auf diesem Zeitraum ermittelt
EXPECTED_TRADE_COUNT = <N>       # Exakte Anzahl Trades aus validiertem Run
EXPECTED_NET_PROFIT  = <float>   # Net Profit ($) aus validiertem Run


@pytest.fixture(scope="module")
def backtest_result():
    engine = VectorbtAdapter()
    config = BacktestConfig(
        strategy_name="<name>",
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

    def test_all_trades_have_sl_in_meta(self, backtest_result):
        """Jeder Trade muss sl_price in meta haben (Risk-Sizing-Kontrolle)."""
        for trade in backtest_result.trades:
            assert "sl_price" in trade.meta, (
                f"Trade {trade.id}: 'sl_price' fehlt in trade.meta"
            )

    def test_no_trade_exceeds_risk_limit(self, backtest_result):
        """Kein Trade darf mehr als 1.5% des Eigenkapitals riskieren."""
        equity = 100_000.0
        for trade in backtest_result.trades:
            sl = trade.meta.get("sl_price")
            if sl:
                stop_dist = abs(trade.entry_price - sl)
                risk_usd = stop_dist * trade.quantity
                risk_pct = risk_usd / equity
                assert risk_pct <= 0.015, (
                    f"Trade {trade.id}: Risk {risk_pct*100:.2f}% überschreitet Limit von 1.5%"
                )
            equity += trade.pnl
```

3. Führe den neuen Test aus, um sicherzustellen, dass er auf der aktuellen Codebase grün ist:
   ```bash
   .venv/bin/python -m pytest tests/strategies/test_<name>.py -v
   ```
   Schlägt der Test fehl → ermittle die korrekten `EXPECTED_TRADE_COUNT` und `EXPECTED_NET_PROFIT` aus dem validierten Backtest und trage sie ein.

4. **Gate:** Alle Tests in `tests/strategies/test_<name>.py` müssen grün sein.

**Ausgabe:** `tests/strategies/test_<name>.py` (grün)

---

## Abschluss-Checkliste

Bevor die Konvertierung als abgeschlossen gilt, müssen ALLE folgenden Dateien existieren:

- [ ] `strategies/<name>/README.md` — Ground-Truth-Dokumentation
- [ ] `strategies/<name>/<name>.py` — Strategie-Code mit vollständigem `STRATEGY_META` und `Trade.meta`-Feldern
- [ ] `~/data/backtests/<name>_<timestamp>.trades.json` — validierter Backtest-Output
- [ ] `tests/strategies/test_<name>.py` — grüner Regressionstest

Melde dem User den Abschluss mit einer kurzen Zusammenfassung:

```
## Konvertierung abgeschlossen: <Strategiename>

**Framework:** <engine>
**Symbol/Timeframe:** <SYMBOL> / <TF>
**Backtest:** <N> Trades, Net Profit: <$X>, Win-Rate: <X>%

**Validierung (5 Trades geprüft):**
| Trade | Risk-Check | Logik-Check |
|-------|------------|-------------|
| A (Max Loss) | ✅ | ✅ |
| B (Max Win)  | ✅ | ✅ |
| C (Partial)  | ✅ | ✅ |
| D (Random)   | ✅ | ✅ |
| E (Random)   | ✅ | ✅ |

**Regression-Test:** `tests/strategies/test_<name>.py` — alle Tests grün
```

---

## MQL5-spezifische Hinweise

MQL5 (MetaQuotes Language 5) unterscheidet sich von Pine Script durch tickbasierte Abarbeitung, explizite Order-Management-APIs und Account-basierte Positionsverwaltung. Bei der Konvertierung ist Folgendes zu beachten:

### Event-Handler

- `OnInit()` → Einmalige Initialisierung vor dem Backtest. In Python/vectorbt werden Parameter im `params`-Dictionary übergeben; Indikatoren werden vektoriell im gesamten DataFrame berechnet.
- `OnTick()` → Wird bei jedem Tick aufgerufen. In einer Bar-Engine wird die Logik pro Bar ausgewertet. Entry-Signale werden typischerweise am Close der aktuellen Bar generiert, Stop- und Take-Profit-Checks werden am High/Low der Folgebar simuliert.
- `OnDeinit()` → Aufräumlogik. Nicht erforderlich in vectorbt/pynecore.

### Zeitfilter

Viele MQL5-EAs nutzen explizite Zeitfilter:
- `Entry_Time`, `Max_Entry_Time`, `Exit_Time`
- Diese werden als boolesche Masken auf `df.index` abgebildet:
  ```python
  import pandas as pd
  entry_allowed = (df.index.hour >= params["entry_hour_start"]) & (df.index.hour < params["entry_hour_end"])
  ```

### Preisabfragen

- `SymbolInfoDouble(_Symbol, SYMBOL_LAST)`: Letzter Tick-Preis. In Bar-Engines wird `df["close"]` als Ersatz verwendet.
- `Close[0]`, `Open[0]`, `High[0]`, `Low[0]`: MQL5-Zeitreihenzugriff. In Pandas direkt über `df["close"].iloc[i]` (vektoriell: `df["close"]`).

### Positionsmanagement

- `PositionSelect(_Symbol)`: Prüft, ob eine Position existiert. In vectorbt wird die Positionsgröße von der Engine verwaltet; `generate_signals` liefert nur boolesche Entry/Exit-Signale.
- `PositionGetDouble(POSITION_VOLUME)`: Aktuelle Positionsgröße. In vectorbt wird das Sizing über `size`-Parameter oder Adapter-Logik gesteuert.
- `PositionGetInteger(POSITION_MAGIC)`: Magic Number zur Zuordnung. In der Python-Plattform wird `STRATEGY_META["name"]` als eindeutiger Schlüssel verwendet.

### Order-Management

- `CTrade`: High-Level-Order-Management-Klasse in MQL5. Enthält Methoden wie `Buy()`, `Sell()`, `PositionModify()`, `OrderOpen()`.
  - Bei einfachen Market-Orders mit Stop-Loss/Take-Profit → direkt in vectorbt via `sl_stop`, `tp_stop` abbildbar.
  - Bei komplexen Bracket-Orders, Trailing-Stops mit `PositionModify()` oder Multi-Asset-Logik → `lean` bevorzugen.
- `ORDER_FILLING_FOK` (Fill Or Kill): In vectorbt gibt es kein explizites FOK-Modell; Orders werden als Market-Orders am Open der nächsten Bar simuliert. Slippage-Konfiguration erfolgt im Adapter.

### Indikatoren

- MQL5-Indikatoren wie `iATR`, `iRSI`, `iMACD` liefern Handle-basierte Zeitreihen. In Python werden diese durch `talib`, `pandas_ta` oder `vectorbt.indicators` ersetzt.
- Beachte die unterschiedliche Indexierung: MQL5 `iATR(..., 1)` bezieht sich auf den vorherigen (vollendeten) Bar. In Pandas entspricht dies `.shift(1)` nach der vektoriellen Berechnung.

---

## Referenz-Materialien

Lies diese Dateien bei Bedarf:
- `docs/pynecore_evaluation.md` — PyneCore-spezifische Einschränkungen und API
- `docs/DATA_SOURCES.md` — Datenpfad-Konventionen und verfügbare Datenquellen
- `shared/models.py` — `Trade.meta`, `TradeExit`, `BacktestResult` Datenmodell
- `runner/engines/vectorbt_adapter.py` — Risk-Sizing-Logik (`stop_loss`/ATR/Fallback)
- `runner/engines/base.py` — `BacktestConfig` mit `risk_per_trade_pct`
- `strategies/example_sma/example_sma.py` — Minimales vectorbt-Strategie-Template
- `strategies/pine_supertrend_dema/pine_supertrend_dema.py` — Komplexeres Template mit Trailing Stop

---

## Template: `strategies/<name>/README.md`

Kopiere den folgenden Block und ersetze alle Platzhalter inklusive `<...>`-Klammern durch konkrete Werte der zu konvertierenden Strategie:

```markdown
# <Strategiename>

## Quelle
- Original: <Sprache/Plattform, Link oder Beschreibung>
- Konvertiert: <Datum (ISO-8601)>

## Kernlogik

### Entry
<Exakte Bedingung(en) mit Indikator-Parametern und Zeitfilter>

### Exit
<Exakte Bedingung(en), alle Exit-Typen aufgelistet (Stop-Loss, Take-Profit, Trailing Stop, Time-Exit)>

### Stop-Loss
<Berechnungsformel, z. B. Entry_Price - 1.5 * ATR(14)>

### Trailing Stop (falls vorhanden)
<Logik und Update-Bedingung, z. B. Supertrend-Band wird nachgezogen sobald Preis um 1 ATR gestiegen>

### Partial Exits (falls vorhanden)
| Stufe | Anteil | Bedingung |
|-------|--------|-----------|
| TP1   | 50 %   | <Bedingung für ersten Teilausstieg> |
| TP2   | 50 %   | <Bedingung für zweiten Teilausstieg> |

## Indikator-Einstellungen
| Indikator | Parameter | Wert | Quelle |
|-----------|-----------|------|--------|
| EMA       | Länge     | 20   | Pine/MQL5/ta-lib |
| ATR       | Periode   | 14   | Pine/MQL5/ta-lib |
| RSI       | Periode   | 7    | Pine/MQL5/ta-lib |

## Risk-Management
- Stop-Distanz: <ATR-basiert / statisch / Swing>
- Position Sizing: <Formel oder Methode, z. B. 1 % Equity / Stop-Distanz>
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)
- Maximales Leverage: <wenn explizit begrenzt>

## Framework-Empfehlung
<engine> — Kurzbegründung, welches Engine-Kriterium zutrifft

## Bekannte Abweichungen
<Liste bewusster Unterschiede zur Originalimplementierung, z. B. "FOK-Modell nicht simuliert", "Tick-Daten nicht verfügbar, Close als Proxy verwendet">

## Backtest-Ergebnis (Referenz)
- Symbol/Timeframe: <SYMBOL> / <TIMEFRAME>
- Zeitraum: <Start> bis <End>
- Trades: <N>
- Net Profit: <$X>
- Win-Rate: <X>%

---

## Häufige Validierungs-Blocker (aus dem Intraday-Volatility-Breakout-Ferment abgeleitet)

### 1. Historische Datenabdeckung
Prüfe vor dem ersten Backtest, ob die lokalen Marktdaten den Zeitraum der Referenz abdecken. Fehlende Daten vor 2020 führen zu systematisch unterschiedlicher Trade-Anzahl und Statistiken.

### 2. Zeitzone und Tagesabschluss
MT5-Berichte verwenden die Broker-Serverzeit (typisch UTC+2/UTC+3). Dukascopy-Daten liegen in UTC vor. Unterschiedliche Tagesabschlüsse verändern Tages-ATR, Hoch/Tief und damit alle ATR-basierten Levels. Wenn möglich, Dokumentiere den verwendeten Offset; ein exakter Match ohne identische Tageskerzen ist oft nicht möglich.

### 3. 24h-Daten vs. RTH-Session (Indizes)
Viele Indizes (SP500, NAS100) werden in den Referenzen auf Basis der Cash-/RTH-Session gehandelt. Liegen 24h-Daten vor, wird die Tages-ATR über Nacht-Gaps aufgebläht und Breakout-Levels werden zu weit. Abhilfe: ATR nur auf der RTH-Session (z. B. 13:30–20:00 UTC für US-Indizes) berechnen oder einen session-basierten Filter implementieren.

### 4. Execution-Timing
MQL5 arbeitet tickbasiert und kann auf Candle-Close (`Use_Candle_Close=true`) oder Intrabar-Trigger reagieren. vectorbt führt standardmäßig am Next-Bar-Open aus. Zur Approximation:
- `use_candle_close=true`: Signale am Close der Signal-Bar, Ausführung Next-Open.
- `use_candle_close=false`: Verwende `high`/`low` der aktuellen Bar als Proxy für Tick-Preise, um Entries während der Bar zu simulieren.

Dokumentiere die gewählte Approximation und ihre erwartete Abweichung.
```
