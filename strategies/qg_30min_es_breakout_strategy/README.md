# QG-30Min ES Breakout Strategy

## Quelle
- Original: Pine Script v6 (`rndm_stuff/QG-30Min_ES_Breakout_Strategy.md`), Kommentar-Link: `https://discord.com/channels/1230977283888648315/1387506825603059762/1387506825603059762`
- Konvertiert: 2026-05-04

## Kernlogik

### Entry
- Trading-Fenster (New York): nur zwischen 08:00 und 15:00.
- Maximal ein Trade pro NY-Tag (`trade_count == 0`).
- Long-Filter: `ADX(45, smooth=5) > 30`.
- Short-Filter: `ADX(45, smooth=5) > 30`.
- Breakout-Levels:
  - `breakout_long = low + FRACT * abs(open - low)` mit `FRACT = 3.3`
  - `breakout_short = high - FRACT * abs(high - open)` mit `FRACT = 3.3`
- Entry-Bedingungen (wie im Original):
  - Long: `can_trade and (adx > 30) and (close > breakout_long)`
  - Short: `can_trade and (adx > 30) and (close < breakout_short)`

### Exit
- Stop-Loss-Exit (Plattform-Erweiterung): Exit bei Stop-Hit.
- Time-Exit: Offene Position wird zum Session-Ende geschlossen.
  Wegen Next-Open-Ausführung im vectorbt-Adapter wird das Exit-Signal auf 14:30 NY gesetzt, damit der Fill auf der 15:00-NY-Bar liegt (nahe am Pine-Ziel 14:55).

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
| Session-Exit-Signal | Time Exit Trigger (NY) | 14:30 (Fill auf 15:00-Bar) |

## Risk-Management
- Stop-Distanz: statisch pro Entry-Bar (`low` bei Long, `high` bei Short)
- Position Sizing: Runner berechnet Positionsgröße über `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — Die Logik ist als deterministische OHLCV-/Session-Regel ohne Pine-Runtime umsetzbar, benötigt kein komplexes Order-Book- oder Multi-Asset-Event-Handling und passt damit zur vektorbasierten Engine.
