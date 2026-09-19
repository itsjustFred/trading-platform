# Channel Breakout

## Quelle
- Original: `Intraday Channel Breakout nt8.txt` (NQ 5 min US Equities RTH, bi-direktional mit EMA-Filter, Gap-Filter, Trailing-ATR-Stop)
- Discord: `https://discord.com/channels/1230977283888648315/1383712509831155782/1388096847498707044`
- Konvertiert: 2026-03-08, source-faithful Fix: 2026-09-09

## Kernlogik

### Entry
Long- und Short-Breakouts (beide default AN — `EnableLongTrades`/`EnableShortTrades = true`).
Stop-Orders am Channel werden als Close gg. Vorbar-Channel mit Next-Open-Execution approximiert:
- Long: `close > upper_channel[-1]` und (falls aktiv) `close > EMA(200)`.
- Short: `close < lower_channel[-1]` und (falls aktiv) `close < EMA(200)`.
Gap-Gate (default AN, `MinimumGapSize = 0.5` Punkte): positiver Gap nur Longs, negativer Gap nur Shorts, Gap < Minimum (inkl. Gap == 0) KEINE Trades. Ohne `timestamp`-Spalte ist der Gap-Filter bypassed (dokumentierte Ausnahme).
Standardparameter: Channel 5 Bars, EMA(200)-Filter aktiv.

Der vectorbt-Adapter unterstuetzt Shorts via `direction` (`LONG`/`SHORT` mit gespiegelter PnL) — Shorts sind daher restauriert, kein Long-Only.

### Exit
Prioritaet pro Bar: Stop zuerst, dann Channel-Exit, dann EMA-Exit, dann Session-Close.
1. ATR-Trailing-Stop (default): Long `close <= trail`, Short `close >= trail` (Close-basiert wie im Original), `trail = close -/+ ATR(14)*2.0`, nur engend.
2. Channel-Exit (default AN): Gegenseiten-Stop — Long bei `low <= lower_channel[-1]`, Short bei `high >= upper_channel[-1]`, Fill am Channel-Level.
3. EMA-Exit (Opt-in, default AUS): Long bei `close < EMA`, Short bei `close > EMA`.
4. Session-Close (default AN): Flatten an der letzten Bar jedes NY-Tages (keine Overnight-Positionen; `IsExitOnSessionCloseStrategy`, 30 s).
5. Fix-$ Stop (Opt-in, default AUS, `$100`): ersetzt das ATR-Trailing; Long bei `low <= entry - amount`, Short bei `high >= entry + amount`.

### Stop-Loss
Default: ATR-Trailing `sl_price = entry_close -/+ ATR(14) * 2.0`.
Opt-in Fix-$ Stop: `sl_price = entry -/+ StopLossAmount`. NT8 Currency-Mode braucht Kontrakt-Punktwert (fuer NQ $100 ~ 5 Punkte bei $20/Punkt); in vectorbt als Preisdistanz approximiert.

### Trailing Stop (falls vorhanden)
Ja (default). Long: `max(prev, close - ATR*mult)`; Short: `min(prev, close + ATR*mult)`. Level in `trailing_stop_at_exit`.

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert (Original hat keine).

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Channel | Length | 5 |
| EMA Filter | Length / Enabled | 200 / true |
| ATR | Period / Multiplier | 14 / 2.0 |
| Gap Filter | Enabled / MinGap | true / 0.5 |
| Channel Exit | Enabled | true |
| EMA Exit | Enabled | false (Opt-in) |
| Fixed Stop | Enabled / Amount | false / 100.0 (Opt-in) |
| Trade Direction | Long / Short | true / true |
| Session Close | Enabled | true |

## Risk-Management
- Stop-Distanz: ATR-basiert (bzw. Fix-Distanz im Opt-in)
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price` (Shorts via `direction` gespiegelt)
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — Channel-, EMA- und ATR-Berechnung ist voll vektorisierbar und benoetigt keine Pine- oder LEAN-spezifische Laufzeit. Shorts laufen ueber das `direction`-Signal des Adapters.
