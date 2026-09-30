# Live Risk Konsistenz

## Problem

Die Position-Sizing-Logik ist in zwei Modulen implementiert:

| Modul | Zweck | Unterschiede |
|-------|-------|--------------|
| `risk/engine.py` | Backtest-Risk-Engine | Vollständige RiskEngine-Klasse mit konfigurierbarem RiskConfig |
| `risk/live.py` | Live-Signal-Sizing | Wrapper-Funktion `size_live_signal()` die auf RiskEngine aufbaut |

## Kritische Unterschiede

### 1. Stop-Direction Enforcement

**Backtest (`risk/engine.py`):**
```python
enforce_stop_direction = bool(signal.metadata.get("enforce_stop_direction", False))
```
Standard: `False` (nicht erzwungen)

**Live (`risk/live.py`):**
```python
"enforce_stop_direction": True,  # hardcoded
```
Live erzwingt immer, dass Stop-Loss in die richtige Richtung zeigt.

### 2. RiskConfig Defaults

Beide Pfade verwenden dieselben Defaults aus `RiskConfig`:
- `max_risk_per_trade: 0.01` (1%)
- `max_total_exposure: 0.05` (5%)
- `max_positions: 5`
- `default_sl_atr_multiplier: 2.0`
- `fallback_stop_pct: 0.02` (2%)

### 3. Sizing-Formel

Beide Pfade verwenden dieselbe Formel:
```
position_size = risk_amount / (risk_distance * pip_value)
```

Dabei ist:
- `risk_amount = balance * risk_percent`
- `risk_percent = min(max_risk_per_trade, max_total_exposure - current_exposure)`
- `position_size = min(position_size, max_notional_per_trade * balance / entry_price)`
  (Notional-Obergrenze, `max_notional_per_trade` als Bruchteil des Balances, Default 0.20)

## Einheiten-Contract (Cleanup 20-A)

Alle Risk-Limits sind Bruchteile des Account-Balance (fraction of balance):

| Größe | Einheit |
|-------|---------|
| `max_risk_per_trade` | Bruchteil des Balances (max. Verlust pro Trade) |
| `max_total_exposure` / `current_exposure` | Bruchteil des Balances (aggregates Open-Risk) |
| `max_notional_per_trade` | Bruchteil des Balances (Notional-Obergrenze pro Position) |
| `SizedOrder.position_size` / `qty` | Account-Währungseinheiten des Symbols (Shares/Lots) |

Vertrauensgrenze: Signal-Metadata ist **unvertrautes** Publisher-Payload. Sie darf
nur Handels-Deskriptoren liefern (Entry-Price, Stop, Take-Profit, ATR, Timeframe,
Pip-Value). Nie aus Metadata gelesen werden: `qty`/`explicit_qty` (Mengen-Override),
`max_risk_per_trade`, `max_total_exposure`, `max_positions`, `current_positions`,
`current_exposure`. Risk-Policy kommt ausschliesslich aus Deployment-/Runtime-Config
(`RiskConfig`), State (`current_positions`, `current_exposure`) aus dem
Portfolio-/Broker-Source-of-Truth. Der früher `qty`/`explicit_qty`-Bypass um die
komplette Risk-Engine ist entfernt; `risk.live.size_live_signal` size't jeden
Signal durch `RiskEngine`.

## Implizite Divergenz

Der Unterschied bei `enforce_stop_direction` führt dazu, dass:

1. **Backtest**: Trades mit "falsch herum" gesetzten Stops werden akzeptiert und unterschiedlich gestartet
2. **Live**: Solche Trades werden mit `position_size=0` abgelehnt

Das verhindert unrealistische P&L im Live-Trading, erzeugt aber Abweichungen zwischen Backtest und Realität.

## Empfehlung

Der aktuelle Ansatz ist beabsichtigt: Live-Trading soll strengere Validierung haben als Backtests. Die Dokumentation hierfür ist nun vorhanden.

## Tests

Ein parametrierter Test in `tests/test_risk_consistency.py` sollte folgende Fälle abdecken:

1. Identische Inputs → identische Outputs zwischen `engine.size_*` und `live.size_*`
2. Strikter Stop-Direction Check wird im Live-Pfad erzwungen
3. ATR-basierter Fallback wird in beiden Pfaden gleich berechnet

Alle drei sind abgedeckt (Stand 2026-09-30). Fall 3 war die letzte Lücke: der Test
verglich nur die `RiskConfig`-Defaults und lief nie mit einem Signal ohne Stop durch
beide Pfade. Jetzt wird ein stop-loser Pfad über eine Matrix aus `buy`/`sell` und
ATR vorhanden/fehlend geschickt, und **der erwartete Stop wird explizit geprüft** —
nur ein Vergleich der Pfade miteinander wäre auch erfüllt, wenn in beiden der
ATR-Zweig tot wäre und beide auf den Prozent-Fallback zurückfielen.

Der Erwartungswert ist dabei nicht redundant: er ist das, was den Fall von
"gleiche Defaults" zu "gleicher berechneter Fallback" macht. Zusätzlich geprüft wird
die Verweigerungs-Parität: ein unbrauchbarer ATR (0, negativ, NaN, inf, nicht
parsebar) muss in **beiden** Pfaden ablehnen. Ein Pfad, der heilen würde, würde
gegen eine nie meaningful Stop-Distanz size'n.

Eine Asymmetrie, die dabei auffiel: `TradeSignal.action` akzeptiert
`buy`/`sell`/`long`/`short`, `LiveSignal.action` nur `buy`/`sell`. Die Matrix bleibt
in der Schnittmenge — eine long/short-Schreibweise, die der Backtest-Pfad akzeptiert,
kann den Live-Pfad gar nicht erst erreichen.