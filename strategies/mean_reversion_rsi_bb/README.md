# Overnight Gap Mean Reversion (Daily Proxy)

## Quelle
- Original: `overnight_gap_mean_reverse` (`algorithm.py` + `variants/variant_1.py`) aus `alexisbouley2/quantconnect`
  (`https://github.com/alexisbouley2/quantconnect/tree/main/strategies/overnight_gap_mean_reverse`)
- Konvertiert: 2026-09-09 (Rewrite; ersetzt die vorherige generische BB/RSI-Implementierung,
  die diesem Source-Eintrag faelschlicherweise zugeordnet war)

## Kernlogik

### Entry
Fade des Overnight-Gaps auf Tagesbasis:
- `gap = open[t] - close[t-1]`
- `volatility = StdDev(close der 60 Bars VOR Tag t)`
- Signal wenn `abs(gap) >= sigma * volatility` (Default `sigma = 10.0` wie in `algorithm.py`;
  `variant_1.py` nutzt Default `3.0`).
- Richtung: Gap up (`gap > 0`) -> SHORT, Gap down (`gap < 0`) -> LONG.

### Exit
Vollausstieg einen Bar nach dem Entry (`time_exit_next_open`).
Tages-Proxy fuer die intraday-Liquidation um 09:45 ET (15 Minuten nach Entry um 09:31 ET).

### Intraday-Limitation (nicht auf 1d abbildbar)
Die Quelle handelt auf Minutenbasis (Entry 09:31 ET, Liquidation 09:45 ET).
Auf Tagesbars ist dieses 14-Minuten-Fenster nicht ausdrueckbar: Das Entry-Signal
faellt auf den Gap-Tag, das Exit-Signal auf den Folgetag. Durch die
Next-Open-Ausfuehrung der Plattform verschieben sich die Fills zusaetzlich um
einen Bar. Backtest-P&L entspricht daher einem um einen Tag verzoegerten Fade,
nicht dem 09:31->09:45-Fenster.

### Stop-Loss
Die Quelle definiert keinen Stop-Loss (reiner Time-Exit).
Fuer das Runner Risk-Sizing (`--risk-pct`) emittiert die Konvertierung einen
strukturellen Schutz-Level pro Entry-Bar (Plattform-Ergaenzung, keine Source-Regel):
- SHORT (Gap up): `sizing_sl_price = high` der Entry-Bar
- LONG (Gap down): `sizing_sl_price = low` der Entry-Bar

### Trailing Stop (falls vorhanden)
Kein Trailing Stop implementiert (Quelle hat keinen).

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Gap | Definition | `open - prev_close` |
| Volatility | Fenster (Closes) | 60 |
| Sigma | Threshold-Multiplikator | 10.0 |
| Hold | Bars | 1 (Exit am naechsten Open) |

## Risk-Management
- Stop-Distanz: strukturell pro Entry-Bar (`high` bei SHORT, `low` bei LONG), nur fuer Sizing
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sizing_sl_price` (A-M3: Sizing-Level, kein Live-Stop)
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — Gap-/Filter-Logik ist rein vektorisierbar (Zustandsautomat mit 1-Bar-Hold)
und benoetigt keine event-getriebene Engine.
