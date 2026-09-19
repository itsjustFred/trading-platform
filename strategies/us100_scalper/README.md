# US100 Intraday Scalper

## Quelle
- Original: MQL5-EA `us100 intrayday scalper single-symbol mql5.txt` (+ `infos.txt`)
  aus `discord-zenomtrader-forum-strategies / US100 INTRADAY SCALPER`
  (`https://discord.com/channels/1230977283888648315/1453162378509418657/1453162378509418657`)
- Konvertiert: 2026-09-09

## Kernlogik

### Entry
Long-only NQ-Scalper mit Mitternachts-Referenz (H1-Close in die NY-Mitternacht):
1. **Referenz (00:00 ET):** `midnight_close` = Close des H1-Balkens in die Mitternacht.
2. **Breakout-Detect (00:00-08:00 ET):** sobald ein High ueber
   `midnight_close + BreakoutBuffer` liegt, ist der Tag scharf (`armed`).
   Mit Code-Defaults: `25000 * 0.12 % = 30 Punkte`.
3. **Buy-Stop-Fenster (08:00-16:00 ET):** pro Stunde ein Buy-Stop auf
   `prev-H1-Close + EntryOffset` (Default `30 Punkte`), SL auf
   `prev-H1-Close - StopLoss` (Default `25 Punkte`). Fill-Naeherung: Einstieg,
   wenn das Bar-High den Stop beruehrt. Genau **ein Fill pro ET-Tag**.

Serverzeit-Mapping: Die MQL5-Stunden (GMT+3) 07:00 / 07-15h / 15-23h / 23:00
entsprechen NY-Mitternacht / 00:00-08:00 ET / 08:00-16:00 ET / 16:00 ET. Alle
Session-Zeiten werden aus den Bar-Timestamps in `America/New_York` abgeleitet.

### Exit
- Stop-Loss-Exit am SL-Preis, sobald das Bar-Low den Stop beruehrt.
- Time-Exit (EOD-Flatten) an der ersten Bar ab 16:00 ET zum Close.

### Engine + Daten
- Buy-Stop-Fills brauchen den `entry_stop`-Pfad des vectorbt-Adapters
  (Fill am Stop bei Touch, kein Fill ohne Touch — D2 strict; nur ohne
  nutzbares `entry_stop`-Level greift der Open-Fallback); ein invertierter Stop
  (SL auf der falschen Seite des Fills) wird lautstark uebersprungen.
- Feed-Anforderung: konsistenter Single-Feed mit Stundenbalken
  (`dukascopy/indices/USTEC/1h.parquet` ist defekt — interleavte Plain-
  und Bid-Serien mit ~2000 Punkten Offset — und darf nicht verwendet
  werden; validiert auf US500/1h bid-only).

### Parameter-Konflikt (infos.txt vs Code)
`infos.txt` nennt Entry 0.5 / SL 0.1 / Buffer 0.12, der `.txt`-CODE nutzt
`EntryOffsetPercent = 0.12` / `StopLossPercent = 0.10` /
`BreakoutBufferPercent = 0.12` bei `ReferencePrice = 25000.0`.
**Implementiert sind die CODE-Werte** (Entry 30pt / SL 25pt / Buffer 30pt);
der Konflikt ist in `STRATEGY_META.notes` dokumentiert.

### Stop-Loss
`sl_price = prev-H1-Close - 25 Punkte` pro Entry-Bar; dient zusaetzlich dem
Runner Risk-Sizing (`--risk-pct`).

### Trailing Stop (falls vorhanden)
Kein Trailing Stop implementiert (der EA nutzt einen fixen SL + EOD-Flatten).

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Referenzpreis | Fix (Code) | 25000.0 |
| Entry-Offset | % der Referenz | 0.12 (= 30 pt) |
| Stop-Loss | % der Referenz | 0.10 (= 25 pt) |
| Breakout-Buffer | % der Referenz | 0.12 (= 30 pt) |
| Session | Referenz / Detect / Entry / Flatten (ET) | 00:00 / 00:00-08:00 / 08:00-16:00 / 16:00 |

## Risk-Management
- Stop-Distanz: fix 25 Punkte pro Entry-Bar
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Framework-Empfehlung
vectorbt — Buy-Stop-/Session-Logik ist als deterministische OHLCV-Regel auf
1h-/1m-Bars abbildbar und benoetigt keine event-getriebene Engine. Fill-Preise
weichen per Next-Open-Konvention vom Stop-Preis ab (dokumentierte Naeherung).
