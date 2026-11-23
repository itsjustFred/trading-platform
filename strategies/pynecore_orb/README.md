# Opening Range Breakout (PyneCore) — slug `pynecore_orb`

## Quelle
- Original: `opening_range_breakout` (`algorithm.py`, Variante 2 mit Preis-Prozent-Trailing-Stop)
  aus `alexisbouley2/quantconnect`
  (`https://github.com/alexisbouley2/quantconnect/tree/main/strategies/opening_range_breakout`)
- Konvertiert: 2026-09-09 (Rewrite als echte Opening-Range-Breakout-Strategie, v2.0.0;
  die v1.x-Implementierung war ein SMA-Crossover mit ATR-Bracket, das unter diesem
  Slug versehentlich weitergefuehrt wurde)

## Kernlogik

### Entry
Single-Symbol Intraday-Breakout der ersten 30 Minuten (09:30-10:00 ET):
- `or_range = or_high - or_low`
- Long, wenn `close > or_high + or_range * breakout_buffer` (Default `0.2` aus `variant_2.py`)
- Short, wenn `close < or_low - or_range * breakout_buffer`
- Nur nach Abschluss der Opening Range, nur ein Trade pro ET-Tag.

Die Session-Zeiten (09:30 ET Open, 15:45 ET Zwangs-Exit) werden aus den
Bar-Timestamps in `America/New_York` abgeleitet. Bars vor 09:30 ET
(z. B. Overnight-CFD-Bars) fliessen nicht in die Range ein.

### Exit
- Trailing Stop (Variante 2): Reversion um 1 % des Water Marks
  - Long: Exit wenn `close < HWM - HWM * 0.01`
  - Short: Exit wenn `close > LWM + LWM * 0.01`
- Time-Exit: Zwangsglattstellung ab 15:45 ET (`time_exit`).
- Hinweis: Auf 24h-CFD-Feeds (z. B. US500 mit Handelspause 16:00-18:00 ET) kann
  der Fill des 15:45-Exits auf der naechsten verfuegbaren Bar liegen.

### Stop-Loss
Der initiale Schutzabstand entspricht dem Trailing-Start (1 % vom Entry, da
`HWM/LWM = Entry` bei Eroeffnung). Fuer das Runner Risk-Sizing emittiert der
Context-Helper `sl_price` (Long: `close * 0.99`, Short: `close * 1.01`).

### Trailing Stop (falls vorhanden)
Siehe Exit: 1 % Reversion ab HWM/LWM, manuell nachgefuehrt ( Faithful zum
`high_water_mark`/`low_water_mark` der Quelle).

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Opening Range | Minuten ab 09:30 ET | 30 |
| Breakout Buffer | Vielfaches der OR-Range | 0.2 |
| Trailing Reversion | Anteil vom Water Mark | 0.01 (1 %) |
| Zwangs-Exit | Zeit (ET) | 15:45 |
| Trades | Limit | 1 pro ET-Tag |

## Risk-Management
- Stop-Distanz: 1 % vom Entry-Preis (Trailing-Start)
- Position Sizing: PyneCore-Adapter skaliert die finalen Trades auf `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)
- Hinweis: Bei 100-%-Equity-Sizing koennen Short-Positionen bei adversen Ticks
  vom Broker-Emulator per Margin-Call teilglattgestellt werden (1-Kontrakt-Stubs
  im Ledger, Exit-Reason `margin_call`); mit `--risk-pct`-Sizing tritt das nicht auf.

## Framework-Empfehlung
pynecore — die Strategie nutzt Pine-/PyneCore-typische `strategy.entry()` /
`strategy.close()`-Orderlogik mit Session-Zeiten und wird als PyneCore-Skript
ausgefuehrt. Der Context-Helper (`pynecore_orb_context.py`) bleibt bewusst
reines pandas/numpy (kein `pynecore`-Import): Er laeuft im Runner-Prozess,
das Skript selbst nur in der `pyne`-CLI.
