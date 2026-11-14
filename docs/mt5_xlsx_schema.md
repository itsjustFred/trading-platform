# MT5 Strategy Tester XLSX — Spaltenschema & Mapping

**Datum:** 2026-06-24
**Branch:** `kimchi-kimi-continue`
**Quelle:** `strategy-comparisons/intraday_volatility_breakout/`
**Berichtssprache:** Deutsch (MT5 Build 5836)

---

## Verify-Befehl

```bash
python -c "import pandas as pd; df = pd.read_excel('strategy-comparisons/intraday_volatility_breakout/EURUSD_H1.xlsx'); print(list(df.columns))"
```

**Ausgabe:**
```python
['Strategietesterbericht', 'Unnamed: 1', 'Unnamed: 2', 'Unnamed: 3', 'Unnamed: 4', 'Unnamed: 5', 'Unnamed: 6', 'Unnamed: 7', 'Unnamed: 8', 'Unnamed: 9', 'Unnamed: 10', 'Unnamed: 11', 'Unnamed: 12']
```

> **Hinweis:** Der naive `pd.read_excel()`-Aufruf liest die erste Zeile des Report-Kopfes (`Strategietesterbericht`) als Header. Die echten Datentabellen beginnen erst weiter unten im Sheet.

---

## Gemeinsame Dateistruktur

| Eigenschaft | Wert |
|-------------|------|
| **Anzahl Sheets** | 1 |
| **Sheet-Name** | `Sheet1` |
| **Dateiformat** | `.xlsx` (openpyxl-kompatibel) |
| **Berichtstyp** | MT5 Strategietester-Export (nicht HTML) |
| **Sprache** | Deutsch |

Alle drei untersuchten Dateien teilen **identische Layout- und Spaltenstrukturen**. Unterschiede bestehen nur in:

- Zeilenanzahl (Summary bleibt fix, Orders-/Trades-Tabellen variieren)
- Symbol- und Zeitframe-Angaben im Kopf
- Konkreten Zahlenwerten

---

## Abschnitte innerhalb des Sheets

Das Sheet ist vertikal in drei logische Bereiche unterteilt:

1. **Settings / Parameter** (Zeilen 0–~29)
2. **Ergebnisse — Zusammenfassung** (Zeilen ~30–~75)
3. **Orders** Tabelle (Zeilen ~76–~[Trades-Start-1])
4. **Trades / Deals** Tabelle (Zeilen ~[Trades-Start]–Ende)

---

## Datei 1: `EURUSD_H1.xlsx`

### Meta-Informationen aus dem Kopf

| Attribut | Wert |
|----------|------|
| Expertenprogramm | `ZenomTrader Discord IntradayVolatilityBreakout` |
| Symbol | `EURUSD` |
| Periode | `H1 (2018.01.01 - 2026.06.23)` |
| Ersteinlage | `100000` |
| Hebel | `1:100` |

### Tabelle A — Orders (Zeile 77 ist Header)

**Zeilen:** 1.972 (inkl. Header)
**Erste Zeile:** 77

| Spalte (Deutsch) | Spalte (technisch) | Typ | Bemerkung |
|------------------|-------------------|-----|-----------|
| `Eröffnungszeit` | `Eröffnungszeit` | `datetime`-String | Zeitpunkt der Order-Erstellung / Order-Ausführung |
| `Auftrag` | `Auftrag` | `float/int` | Order-ID (autoincrement) |
| `Symbol` | `Symbol` | `str` | Handelssymbol, z.B. `EURUSD` |
| `Typ` | `Typ` | `str` | `buy` / `sell` |
| `Volumen` | `Volumen` | `str` | Format: `"3.33 / 3.33"` (Initial / Aktuell) |
| *(Unnamed: 5)* | `Unnamed: 5` | — | Leer |
| `Preis` | `Preis` | `float` | Order-Preis (Limit/Stop); bei Market-Orders `0` |
| `S / L` | `S / L` | `float` | Stop-Loss (nur auf Entry-Orders gesetzt) |
| `T / P` | `T / P` | `float` | Take-Profit (hier durchgehend `NaN`) |
| `Zeit` | `Zeit` | `datetime`-String | Ausführungszeit (identisch zu Eröffnungszeit bei Market-Orders) |
| *(Unnamed: 10)* | `Unnamed: 10` | — | Leer |
| `Status` | `Status` | `str` | `filled` (durchgehend in vorliegendem Datensatz) |
| `Kommentar` | `Kommentar` | `str` | `Breakout Long`, `Breakout Short`, `sl <price>`, etc. |

**Beispiel-Zeilen (anonymisiert / typisch):**

| Eröffnungszeit | Auftrag | Symbol | Typ | Volumen | Preis | S / L | T / P | Zeit | Status | Kommentar |
|---------------|---------|--------|-----|---------|-------|-------|-------|------|--------|-----------|
| `2018.01.04 16:45:00` | 2 | EURUSD | buy | 3.33 / 3.33 | 0 | 1.20643 | NaN | `2018.01.04 16:45:00` | filled | Breakout Long |
| `2018.01.04 18:45:40` | 3 | EURUSD | sell | 3.33 / 3.33 | 0 | NaN | NaN | `2018.01.04 18:45:40` | filled | sl 1.20643 |

### Tabelle B — Trades / Deals (Zeile 1063 ist Header)

**Zeilen:** 986 (inkl. Header + Balance-Initialisierung)
**Erste Zeile:** 1063

| Spalte (Deutsch) | Spalte (technisch) | Typ | Bemerkung |
|------------------|-------------------|-----|-----------|
| `Zeit` | `Zeit` | `datetime`-String | Zeitstempel des Deals |
| `Trade` | `Trade` | `float/int` | Deal-ID (autoincrement) |
| `Symbol` | `Symbol` | `str` | Handelssymbol; bei Balance-Zeile `NaN` |
| `Typ` | `Typ` | `str` | `buy` / `sell` / `balance` |
| `Richtung` | `Richtung` | `str` | `in` (Entry) / `out` (Exit) / `NaN` |
| `Volumen` | `Volumen` | `float` | Deal-Volumen in Lots |
| `Preis` | `Preis` | `float` | Ausführungskurs |
| `Auftrag` | `Auftrag` | `float/int` | Verknüpfung zur Order-ID |
| `Kommission` | `Kommission` | `int/float` | Broker-Kommission (hier durchgehend `0`) |
| `Swap` | `Swap` | `float` | Overnight-Swap (fast durchgehend `0`; 2x ≠ 0 in EURUSD_H1) |
| `Gewinn` | `Gewinn` | `float` | Deal-PnL; bei Entry-Deal `0`; bei Exit-Deal realisierter Gewinn/Verlust |
| `Kontostand` | `Kontostand` | `float` | Kontostand nach diesem Deal |
| `Kommentar` | `Kommentar` | `str` | `Breakout Long`, `sl <price>`, etc. |

**Beispiel-Zeilen (anonymisiert / typisch):**

| Zeit | Trade | Symbol | Typ | Richtung | Volumen | Preis | Auftrag | Kommission | Swap | Gewinn | Kontostand | Kommentar |
|------|-------|--------|-----|----------|---------|-------|---------|------------|------|--------|------------|-----------|
| `2018.01.01 00:00:00` | 1 | NaN | balance | NaN | NaN | NaN | NaN | 0 | 0.0 | 100000.0 | 100000.0 | NaN |
| `2018.01.04 16:45:00` | 2 | EURUSD | buy | in | 3.33 | 1.20824 | 2 | 0 | 0.0 | 0.0 | 100000.0 | Breakout Long |
| `2018.01.04 18:45:40` | 3 | EURUSD | sell | out | 3.33 | 1.20643 | 3 | 0 | 0.0 | -499.60 | 99500.40 | sl 1.20643 |
| `2018.01.23 17:30:00` | 4 | EURUSD | buy | in | 2.55 | 1.22911 | 4 | 0 | 0.0 | 0.0 | 99500.40 | Breakout Long |
| `2018.01.23 22:55:00` | 5 | EURUSD | sell | out | 2.55 | 1.22937 | 5 | 0 | 0.0 | 53.93 | 99554.33 | NaN |

**Datentypen (pandas-inferiert):**

```text
Zeit           str
Trade          float64
Symbol         str
Typ            str
Richtung       str
Volumen        float64
Preis          float64
Auftrag        float64
Kommission     int64
Swap           float64
Gewinn         float64
Kontostand     float64
Kommentar      str
```

---

## Datei 2: `NAS100_M15.xlsx`

### Meta-Informationen aus dem Kopf

| Attribut | Wert |
|----------|------|
| Symbol | `USATECH.IDX` |
| Periode | `M15 (2018.01.01 - 2026.06.23)` |

### Tabelle A — Orders

**Zeilen:** 2.392
**Erste Zeile:** 77
**Spalten:** identisch zu EURUSD_H1 (`Eröffnungszeit`, `Auftrag`, `Symbol`, `Typ`, `Volumen`, `Preis`, `S / L`, `T / P`, `Zeit`, `Status`, `Kommentar`)

### Tabelle B — Trades / Deals

**Zeilen:** 1.196
**Erste Zeile:** 1273
**Spalten:** identisch zu EURUSD_H1 (`Zeit`, `Trade`, `Symbol`, `Typ`, `Richtung`, `Volumen`, `Preis`, `Auftrag`, `Kommission`, `Swap`, `Gewinn`, `Kontostand`, `Kommentar`)

**Beispiel-Zeilen:**

| Zeit | Trade | Symbol | Typ | Richtung | Volumen | Preis | Gewinn | Kontostand | Kommentar |
|------|-------|--------|-----|----------|---------|-------|--------|------------|-----------|
| `2018.01.05 18:00:00` | 2 | USATECH.IDX | buy | in | 31.0 | 6633.272 | 0.0 | 100000.0 | Breakout Long |
| `2018.01.05 22:55:00` | 3 | USATECH.IDX | sell | out | 31.0 | 6653.569 | 522.61 | 100522.61 | NaN |
| `2018.01.09 16:45:00` | 4 | USATECH.IDX | sell | in | 29.0 | 6668.570 | 0.0 | 100522.61 | Breakout Short |
| `2018.01.09 18:15:40` | 5 | USATECH.IDX | buy | out | 29.0 | 6685.275 | -406.30 | 100116.31 | sl 6685.275 |

---

## Datei 3: `SP500_M1.xlsx`

### Meta-Informationen aus dem Kopf

| Attribut | Wert |
|----------|------|
| Symbol | `USA500.IDX` |
| Periode | `M1 (2018.01.01 - 2026.06.23)` |

### Tabelle A — Orders

**Zeilen:** 2.052
**Erste Zeile:** 77
**Spalten:** identisch zu oben

### Tabelle B — Trades / Deals

**Zeilen:** 1.026
**Erste Zeile:** 1103
**Spalten:** identisch zu oben

**Beispiel-Zeilen:**

| Zeit | Trade | Symbol | Typ | Richtung | Volumen | Preis | Gewinn | Kontostand | Kommentar |
|------|-------|--------|-----|----------|---------|-------|--------|------------|-----------|
| `2018.01.16 16:45:00` | 2 | USA500.IDX | buy | in | 123.0 | 2804.971 | 0.0 | 100000.0 | Breakout Long |
| `2018.01.16 17:31:40` | 3 | USA500.IDX | sell | out | 123.0 | 2800.906 | -409.24 | 99590.76 | sl 2800.906 |
| `2018.01.22 17:15:00` | 4 | USA500.IDX | buy | in | 88.0 | 2817.152 | 0.0 | 99590.76 | Breakout Long |
| `2018.01.22 22:55:00` | 5 | USA500.IDX | sell | out | 88.0 | 2829.870 | 912.98 | 100503.74 | NaN |

---

## Gemeinsames Schema

### Trades-Tabelle (primäre Datenquelle für Positionen)

Die Trades-Tabelle enthält **Deal-Zeilen**, nicht zwingend genau zwei Zeilen pro
Position. Ein Position-Trade kann aus mehreren Entry- und/oder Exit-Fills
bestehen. Die Zuordnung erfolgt bevorzugt über eine gemeinsame Position-ID,
sofern mehrere Zeilen dieselbe ID verwenden; andernfalls werden die Fills in
Report-Reihenfolge positionsweise gepaart. `balance`-Zeilen werden ignoriert.

| Konzept | XLSX-Spalte(n) | Typ | Kommentar |
|---------|---------------|-----|-----------|
| **Entry Time** | `Zeit` auf jeder `Richtung == "in"`-Zeile | `datetime`-String | Erster Entry-Fill ist der Trade-Entry |
| **Exit Time** | `Zeit` auf jeder `Richtung == "out"`-Zeile | `datetime`-String | Jeder Exit-Fill wird als eigener `TradeExit` übernommen |
| **Direction** | `Typ` auf dem ersten Entry-Fill | `str` | `buy` → `LONG`, `sell` → `SHORT` |
| **Entry Price** | `Preis` auf dem ersten Entry-Fill | `float` | |
| **Exit Prices** | `Preis` auf allen Exit-Fills | `float` | Ein Exit je Zeile |
| **Volume / Lots** | `Volumen` auf allen Fills | `float` | Entry-Volumen wird summiert; Exit-Volumen bleibt je Fill erhalten |
| **PnL** | `Gewinn` auf allen Exit-Fills | `float` | Trade-PnL ist die Summe der Exit-PnL |
| **Stop Loss** | `S / L` aus **Orders**-Tabelle | `float` | Join über die Entry-Order-ID |
| **Take Profit** | `T / P` aus **Orders**-Tabelle | `float` | Join über die Entry-Order-ID; `None`, wenn nicht gesetzt |
| **Commission** | `Kommission` auf allen Fills | `float/int` | Wird über alle Entry- und Exit-Fills summiert |
| **Swap** | `Swap` auf allen Fills | `float` | Wird über alle Entry- und Exit-Fills summiert |
| **Symbol** | `Symbol` | `str` | |
| **Timeframe** | Aus Report-Kopf (nicht in Tabelle) | `str` | `H1`, `M15`, `M1` etc. |

### Fill-Status und Evidence

`MT5XLSXImporter` klassifiziert jede rekonstruierte Position und legt die
vollständige Evidence unter `raw_metadata.position_fills` sowie
`raw_metadata.open_positions` ab:

| Status | Bedeutung | Ergebnis |
|--------|-----------|----------|
| `closed` | Entry- und Exit-Menge stimmen innerhalb der Toleranz überein | Ein `Trade` mit allen Exit-Fills |
| `partial` | Exit-Menge ist kleiner als Entry-Menge | Kein ausführbarer `Trade`; Restmenge wird als `residual_quantity` evidence |
| `open` | Entry ohne Ausstieg | Kein `Trade`; Position bleibt Open/Partial Evidence |
| `orphan` | Exit ohne vorheriges Entry | Kein `Trade`; Exit wird als `orphan_exit` evidence |

Nur `closed`-Positionen werden in `ValidationReference.trades` ausgegeben.
`open_position_count` umfasst offene und partielle Positionen;
`open_only_count` zählt ausschließlich Positionen ohne Exit. Die Felder
`closed_position_count`, `partial_position_count`, `open_only_count` und
`orphan_exit_count` sind daher die kanonischen Zählwerte.

### Orders-Tabelle (Sekundär für SL/TP)

| Konzept | XLSX-Spalte | Typ | Kommentar |
|---------|------------|-----|-----------|
| Order Open Time | `Eröffnungszeit` | `datetime`-String | |
| Order ID | `Auftrag` | `float/int` | Foreign Key zu Trades.Auftrag |
| Order Type | `Typ` | `str` | `buy` / `sell` |
| Order Volume | `Volumen` | `str` | `"initial / current"`; kann mehrere Fills abbilden |
| Order Price | `Preis` | `float` | `0` bei Market-Orders |
| Stop Loss | `S / L` | `float` | |
| Take Profit | `T / P` | `float` | |
| Execution Time | `Zeit` | `datetime`-String | |
| Status | `Status` | `str` | `filled` / `pending` / `cancelled` |
| Comment | `Kommentar` | `str` | |

---

## Identifizierte Unterschiede zwischen den Dateien

| Aspekt | EURUSD_H1 | NAS100_M15 | SP500_M1 | Bemerkung |
|--------|-----------|------------|----------|-----------|
| **Trades-Startzeile** | 1063 | 1273 | 1103 | Verschiebung durch unterschiedliche Orders-Tabelle |
| **Anzahl Trades** | 492 | 597 | 512 | Vollständig geschlossene Positionen |
| **Anzahl Deals** | 984 | 1194 | 1024 | In den Fixtures je ein Entry- und ein Exit-Fill; der Vertrag erlaubt mehrere Fills |
| **Swap ≠ 0** | 2 Fälle | 0 Fälle | 0 Fälle | Vom Timeframe / Asset abhängig |
| **TP gesetzt** | Nein | Nein | Nein | Strategie verwendet kein Take-Profit |
| **Commission** | Immer 0 | Immer 0 | Immer 0 | Demo-Account / Broker-Einstellung |

**Fixture-Hinweis:** Die drei Referenzdateien enthalten nur vollständig
geschlossene Positionen. Das ist keine allgemeine XLSX-Annahme: der Importer
macht Multi-Fill-, Partial-, Open- und Orphan-Fälle explizit sichtbar.

---

## Mapping zu `ValidationReference`

> `ValidationReference` ist das zentrale Domänenmodell des Strategy Validation Frameworks. Es aggregiert normalisierte Backtest-Daten unabhängig vom externen Framework.

### Pflichtfelder (Top-Level)

| ValidationReference-Feld | Quelle im MT5-XLSX | Transformationsregel |
|--------------------------|--------------------|----------------------|
| `strategy_name` | Report-Kopf: `Expertenprogramm:` | String extrahieren, z.B. `"ZenomTrader Discord IntradayVolatilityBreakout"` |
| `source_framework` | Konstant | `"mt5_xlsx"` |
| `symbol` | Report-Kopf: `Symbol:` | z.B. `"EURUSD"`, `"USATECH.IDX"`, `"USA500.IDX"` |
| `timeframe` | Report-Kopf: `Periode:` | Regex auf Zeitframe, z.B. `"H1"`, `"M15"`, `"M1"` |
| `settings` | Settings-Block (Zeilen 3–~29) | Dictionary aus `Parameter=Wert`-Paaren |
| `trades` | Trades-Tabelle (Zeilen ab ~1063) | Rekonstruktion aus vollständig geschlossenen Positionen; Multi-Fills werden zu einem Trade mit mehreren Exits zusammengefasst |
| `stats` | Ergebnisse-Block (Zeilen ~30–~55) | Mapping auf `BacktestStats`-Felder |
| `raw_metadata` | Gesamter Report-Kopf + Summary | Rohes Dictionary zur Nachvollziehbarkeit |

### Trade-Rekonstruktion (Position-Fills → ein `Trade`)

Der Importer erzeugt für jede vollständig geschlossene Position genau ein
`Trade`. Die Zuordnung ist fill-basiert:

1. Entry- und Exit-Fills mit gemeinsamer Position-ID werden gruppiert.
2. Wenn IDs je Zeile eindeutig sind, werden offene Positionen in der
   Reihenfolge des Reports gepaart.
3. Jede Entry-Zeile erhöht die Entry-Menge; jede Exit-Zeile schließt die
   zugehörige Position anteilig.
4. Nur wenn die Mengen vollständig ausgeglichen sind, wird der Trade in
   `ValidationReference.trades` aufgenommen.

| `Trade`-Feld (intern) | Quelle | Mapping-Logik |
|-----------------------|--------|---------------|
| `id` | Import-Reihenfolge | Fortlaufend, beginnend bei 1 |
| `symbol` | `Symbol` des ersten Entry-Fills | Direkt übernehmen |
| `direction` | `Typ` des ersten Entry-Fills | `"buy"` → `"LONG"`, `"sell"` → `"SHORT"` |
| `entry_time` | `Zeit` des ersten Entry-Fills | In ISO-8601-UTC konvertieren |
| `entry_price` | `Preis` des ersten Entry-Fills | Direkt übernehmen |
| `quantity` | `Volumen` aller Entry-Fills | Summe der Entry-Mengen |
| `exits` | Alle Exit-Fills | Ein `TradeExit` pro Exit-Zeile |
| `pnl` | `Gewinn` aller Exit-Fills | Summe der Exit-PnL |
| `fees` | `Kommission` + `Swap` aller Fills | Über Entry- und Exit-Fills summieren |
| `net_pnl` | `pnl - fees` | Berechnen |
| `exit_reason` | Letzter Exit-Kommentar | z.B. `"sl 1.20643"` → `"stop_loss"` |
| `tags` | Kommentar des ersten Entry-Fills | z.B. `["Breakout Long"]` |

### `TradeExit`-Rekonstruktion (aus Exit-Deal-Zeilen)

Jede Exit-Zeile wird zu einem `TradeExit`; es gibt pro Position einen
`TradeExit` pro Exit-Fill. Die Felder werden zeilenweise übernommen:

| `TradeExit`-Feld | Quelle |
|------------------|--------|
| `time` | `Zeit` |
| `price` | `Preis` |
| `quantity` | `Volumen` |
| `pnl` | `Gewinn` |
| `fees` | `Kommission + Swap` |
| `reason` | Geparst aus `Kommentar` |

### Stop-Loss & Take-Profit Mapping

| Feld | Quelle | Join-Key |
|------|--------|----------|
| `stop_loss` | Orders-Tabelle: `S / L` | `Trades.Auftrag == Orders.Auftrag` (Entry-Order) |
| `take_profit` | Orders-Tabelle: `T / P` | `Trades.Auftrag == Orders.Auftrag` (Entry-Order) |

> **Hinweis:** In den vorliegenden Reports ist `T / P` durchgehend `NaN`. Der Importer muss trotzdem das Feld befüllen (None), um zukünftige Reports mit TP zu unterstützen.

### Stats-Mapping (Summary → `BacktestStats`)

| `BacktestStats`-Feld | XLSX-Quelle (Begriff) | Bemerkung |
|----------------------|----------------------|-----------|
| `total_trades` | `Gesamtanzahl Trades` | |
| `winners` | `Gewonnene Trades (in % von Gesamt)` | Zahl vor Klammer extrahieren |
| `losers` | `Verlorene Trades (in % von Gesamt)` | Zahl vor Klammer extrahieren |
| `win_rate` | `Gewonnene Trades (in % von Gesamt)` | Prozentwert in Klammer |
| `profit_factor` | `Profitfaktor` | |
| `total_pnl` | `Nettogewinn gesamt` | |
| `avg_winner` | `Durchschnitt Gewinntrade` | |
| `avg_loser` | `Durchschnitt Verlusttrade` | |
| `max_drawdown_pct` | `Rückgang Kontostand relativ` | Prozentwert extrahieren |
| `sharpe_ratio` | `Sharpe-Ratio` | |
| `total_return` | `Nettogewinn gesamt` und `Ersteinlage` | **Fraktion**: `total_pnl / initial_capital` (z. B. `0.2186881`), nicht Prozent |
| `max_consecutive_wins` | `Maximum Gewinntrades in Folge` | Zahl extrahieren |
| `max_consecutive_losses` | `Maximum Verlusttrades in Folge` | Zahl extrahieren |

---

## Offene Fragen / Lücken

1. **Zeitzone:** Die Zeitstempel enthalten keine TZ-Info. Annahme: Broker-Zeit (Dukascopy-Demo). Für `ValidationReference` ist eine Normalisierung auf UTC empfohlen.
2. **Equity-Kurve:** Die XLSX-Dateien enthalten keine ticksweise Equity-Kurve, nur den `Kontostand` pro Deal. Für Drawdown-Berechnungen kann eine synthetische Equity-Kurve aus den Deal-Balances rekonstruiert werden.
3. **Teilschließungen und offene Positionen:** Der Vertrag unterstützt
   `Volumen_in != Volumen_out` sowie offene Positionen. Diese Fälle werden
   nicht als abgeschlossene Trades exportiert, sondern in
   `raw_metadata.position_fills` mit Restmenge und Status dokumentiert.
4. **Sprachabhängigkeit:** Die Report-Kopf-Begriffe sind deutsch (`Strategietesterbericht`, `Ergebnisse`, `Eröffnungszeit`, etc.). Der Importer sollte auf Spalten-Index-Ebene arbeiten (nicht auf Begriffsebene), um Mehrsprachigkeit zu unterstützen.
5. **Vorzeichen von Kommission und Swap (offen, Bug):** Die Importer speichern `fees = Kommission + Swap` mit MT5-Vorzeichen (Kosten negativ). `net_pnl` wird aber als `pnl - fees` berechnet, also mit umgekehrtem Vorzeichen. Der MT5-Summenwert `Nettogewinn gesamt` entspricht in allen drei Referenz-Goldens genau `Σ pnl + Σ fees`. Bis zur Korrektur markieren `xfail(strict=True)`-Tests in `tests/validation/test_golden_reconciliation.py` und `test_mt5_xlsx_fixture_shapes.py` den Fehler.
6. **Keine HTML-Reports:** Es liegen ausschließlich XLSX-Dateien vor. Der geplante `MT5HTMLConverter` benötigt separate Fixture-Dateien.

---

## Zusammenfassung

Die drei MT5-XLSX-Reports sind **strukturell identisch** und enthalten zwei maschinell auswertbare Tabellen:

- **Orders** (SL/TP, Kommentare)
- **Trades/Deals** (Entry/Exit, PnL, Balance)

Die primäre Import-Arbeit besteht in:
1. Lokalisierung der Header-Zeilen (row-scan nach `"Trades"` → nächste Zeile = Header)
2. Gruppierung von Entry-/Exit-Fills und Klassifizierung als `closed`, `partial`, `open` oder `orphan`
3. Erzeugung eines Trades nur für vollständig geschlossene Positionen
4. Join mit Orders-Tabelle für SL/TP
5. Parsen des Summary-Blocks für Statistiken
6. Normalisierung in `ValidationReference` → `BacktestResult`
