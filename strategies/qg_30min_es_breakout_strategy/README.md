# QG-30Min ES Breakout Strategy

## Quelle
- Original: Pine Script v6 (`rndm_stuff/QG-30Min_ES_Breakout_Strategy.pine`), Kommentar-Link: `https://discord.com/channels/1230977283888648315/1387506825603059762/1387506825603059762`
- Version: 1.1.0 (Time-Exit eine Bar frueher, kein exit_price fuer den Market-Exit)
- Konvertiert: 2026-05-04, source-faithful Fix: 2026-09-09 (Fade-Semantik, Short-Level, Exit-Zeit direkt am Pine verifiziert)

## Kernlogik

### Entry
- Trading-Fenster (New York): nur zwischen 08:00 und 15:00.
- Maximal ein Trade pro NY-Tag (`trade_count == 0`).
- ADX(45, smooth=5, Wilder-RMA) als Regime-Filter — Fade-Semantik wie im Pine:
  - Long-Filter: `ADX < 30` (nicht-trendig).
  - Short-Filter: `ADX > 30` (trendig).
  - `ADX == 30` exakt handelt keine Seite.
- Breakout-Levels (beide mit `|open - low|`):
  - `breakout_long = low + FRACT * abs(open - low)` mit `FRACT = 3.3`
  - `breakout_short = high - FRACT * abs(open - low)` mit `FRACT = 3.3`
- Entry-Bedingungen (wie im Original — BEIDE Seiten unter dem Level):
  - Long: `can_trade and (adx < 30) and (close < breakout_long)`
  - Short: `can_trade and (adx > 30) and (close < breakout_short)`
- Warmup: Entries erst nach `adx_period + adx_smooth` Bars (Pine-ADX ist auf den ersten Bars `na` und handelt dort nicht).

### Exit
- Stop-Loss-Exit (Plattform-Erweiterung, dokumentiert): Exit bei Stop-Hit (`stop_loss`).
- Time-Exit: Offene Position wird per Signal zum **14:55-NY-Open** geschlossen (`time_exit_1455_ny`, wie `exit_time` im Pine; das Label folgt den `forced_exit_*`-Parametern). Signal-Exits fuellen am naechsten Bar-Open (`next_open`), deshalb geht das Signal auf der ersten Bar raus, deren Ende 14:55 erreicht (1m: 14:54, 1h: 14:00 -> Fill 15:00). Seit v1.1.0 ohne expliziten `exit_price` (der Market-Close-Preis kollidierte mit dem Next-Open-Fill).

### Stop-Loss
- Originalstrategie definiert keinen expliziten Stop-Loss.
- Plattform-Konvertierung (für Risk-Sizing via `--risk-pct`): initialer technischer Stop pro Entry-Bar
  - Long: `sl_price = low`
  - Short: `sl_price = high`
- Schutz-Exit: Position wird geschlossen, sobald der jeweilige Stop intrabar erreicht wird.

### Trailing Stop (falls vorhanden)
Nicht vorhanden.

### Partial Exits (falls vorhanden)
Nicht vorhanden.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| ADX (Wilder) | Length | 45 |
| ADX (Wilder) | Smooth (RMA auf DX) | 5 |
| Breakout | Fractal Factor (`FRACT`) | 3.3 |
| Session-Filter | Marktfenster (NY) | 08:00-15:00 |
| Session-Exit | Time Exit Trigger (NY) | 14:55 |

## Risk-Management
- Stop-Distanz: statisch pro Entry-Bar (`low` bei Long, `high` bei Short)
- Position Sizing: Runner berechnet Positionsgröße über `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — Die Logik ist als deterministische OHLCV-/Session-Regel ohne Pine-Runtime umsetzbar, benötigt kein komplexes Order-Book- oder Multi-Asset-Event-Handling und passt damit zur vektorbasierten Engine.
