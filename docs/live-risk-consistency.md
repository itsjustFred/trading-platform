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