# Trend Following

## Quelle
- Original: `desenyon/quantconnect-strategies` — `trend_following_cta.py` (Multi-Market long/short CTA)
- Konvertiert: 2026-03-08, korrigiert: 2026-09-09 (SMA→EMA 20/50/200, ATR 14→20 Wilder, Stop 2.0→2.5xATR mit 2.0-ATR-Aktivierung)

## Kernlogik

### Entry
Long-Entry, wenn die schnelle EMA die mittlere EMA von unten nach oben kreuzt und die mittlere EMA oberhalb der langsamen EMA liegt (Proxy fuer den Composite-Long-Trigger `|composite| > 0.5` der Quelle).
Signal: `EMA(20) > EMA(50)` und `EMA(20)[-1] <= EMA(50)[-1]` sowie `EMA(50) > EMA(200)`.

### Exit
Vollausstieg bei einem von zwei Events:
1. `EMA(20)` kreuzt die `EMA(50)` von oben nach unten (`ema_cross_down`, Signalumkehr).
2. Der Schlusskurs faellt auf/unter den aktiven Stop (`trailing_stop`).

**Fill-Konvention (Version 1.2.0):** Marktausstiege auf Close-Basis (EMA-Kreuz und Trailing-Stop-Hit) tragen kein `exit_price` mehr (kein Close-Fill) und fuellen am naechsten Bar-Open (`signal_exit_fill="next_open"`). Der Stop-Level-Fill entfaellt fuer diesen Exit-Pfad.

### Stop-Loss
Initialer Stop-Loss beim Entry (exakt wie die Quelle): `sl_price = entry_close - ATR_Wilder(20) * 2.5`.

### Trailing Stop (falls vorhanden)
Ja, quellentreu: Der Stop wird NUR nachgezogen, sobald der Positionsgewinn `> 2.0 x ATR` betraegt (`trailing_stop_activation`).
Formel: `max(previous_stop, close - ATR_Wilder(20) * 2.5)`.
Beim Exit wird das Stop-Level in `trailing_stop_at_exit` geschrieben.

### Partial Exits (falls vorhanden)
Keine Partial Exits implementiert.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| EMA Fast | Length | 20 |
| EMA Mid | Length | 50 |
| EMA Slow | Length | 200 |
| ATR | Wilder Period | 20 |
| ATR Stop | Multiplier | 2.5 |
| Trailing-Aktivierung | ATR-Profit | 2.0 |

## Risk-Management
- Stop-Distanz: 2.5 x ATR(Wilder, 20) ab Entry
- Position Sizing: Runner berechnet die Positionsgroesse ueber `--risk-pct` und `sl_price`
- Empfohlenes `--risk-pct`: 0.01 (1 % pro Trade)

## Hinweis zur Vereinfachung (Scope)
Das Original handelt 14 Maerkte long UND short mit Donchian-55-Breakout-Bein, Composite-Gewichtung (0.25/0.40/0.35), Weak-Signal-Scale-out (`|composite| < 0.3`), volatilitaetsbasiertem Sizing (1 % Risiko je Trade) sowie Sektor- (25 %) und Korrelations-Caps (40 %). Davon ist nichts single-symbol-exprimierbar bzw. adaptergetragen und nichts davon ist implementiert: pro Lauf wird nur die Long-Seite eines einzelnen Instruments gehandelt (Entry-Cross + Trendfilter als Proxy), Sizing liegt bei der Engine.

## Framework-Empfehlung
vectorbt — die Strategie ist ein klassisches vektorisiertes EMA/ATR-System ohne Event-Driven-Orderlogik.
