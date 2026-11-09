# Live Risk Konsistenz

## Problem

Die Position-Sizing-Logik ist in zwei Modulen implementiert:

| Modul | Zweck | Unterschiede |
|-------|-------|--------------|
| `risk/engine.py` | Backtest-Risk-Engine | Vollständige RiskEngine-Klasse mit konfigurierbarem RiskConfig |
| `risk/live.py` | Live-Signal-Sizing | `resolve_live_levels()` (einziger Entry/SL/TP-Resolver, #125) und Wrapper `size_live_signal()` auf RiskEngine |
| `live_runner/daemon.py` | Live-Submission | `size_live_signal_for_submission()` / `prepare_order_with_risk()`: Sizing mit Deployment-`RiskConfig`, Levels aus `resolve_live_levels()`, Entry-Deviation-Gate |

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
nur Handels-Deskriptoren liefern (Entry-Price, Stop, Take-Profit, ATR, Timeframe).
Nie aus Metadata gelesen werden: `qty`/`explicit_qty` (Mengen-Override),
`max_risk_per_trade`, `max_total_exposure`, `max_positions`, `current_positions`,
`current_exposure`, `pip_value` (#125: ein Payload-Pip-Value skalierte die Menge
direkt) und `max_entry_deviation_sl_fraction`. Der Pip-Value kommt nur vom
vertrauten Aufrufer (Config/Broker), sonst gilt der Unit-Contract 1.0.
Risk-Policy kommt ausschliesslich aus Deployment-/Runtime-Config
(`RiskConfig`), State (`current_positions`, `current_exposure`) aus dem
Portfolio-/Broker-Source-of-Truth. Der früher `qty`/`explicit_qty`-Bypass um die
komplette Risk-Engine ist entfernt; `risk.live.size_live_signal` size't jeden
Signal durch `RiskEngine`.

## Einheitlicher Level-Resolver (#125)

`risk.live.resolve_live_levels()` ist die einzige Quelle für Entry, SL und TP
eines Live-Signals. Der Daemon sized **und** submitted daraus, damit Menge und
Broker-Levels nie auseinanderlaufen:

- **Entry:** Metadata `entry_price`, sonst `theoretical_price`. Der an den Adapter
  übergebene Signal-`theoretical_price` wird auf diesen Entry gesetzt (cTrader
  rechnet relative SL/TP-Distanzen davon).
- **SL:** erster gesetzter Alias aus `stop_loss`, `sl_price`, `stop_price`,
  `resolved_stop_loss`; fehlt er, setzt die RiskEngine den Fallback-Stop, und genau
  dieser geht an den Broker. Ein Payload-`resolved_stop_loss` kann den gesizten
  Stop nicht mehr überschreiben.
- **TP:** erster gesetzter Alias aus `take_profit`, `tp_price`. Ein fehlender TP
  ist kein Resolver-Fehler; ein fehlerhafter TP (nicht numerisch, Boolean, nicht
  endlich, `<= 0`) wird abgelehnt. Die Submission-Regel C1 bleibt: ohne TP nur mit
  explizitem `take_profit_policy="unbounded_exit_on_opposite_signal"`.

## Entry-Deviation-Gate (#125)

Vor dem Market-Order-Submit vergleicht `prepare_order_with_risk()` den Entry mit
dem aktuellen Broker-Quote (`adapter.get_quote()`): **Ask für Buys, Bid für Sells**.

```
deviation = (entry - quote) / (entry - stop)
```

als Bruchteil der SL-Distanz, mit Vorzeichen: positiv heißt **Richtung SL**,
negativ **weg vom SL**. Abgelehnt wird (Regeln in dieser Prüfreihenfolge, der Name
landet als `entry_gate_rejection_rule` im Log und in der Fehlermeldung). Zuerst
muss der Quote **aktuell** sein: ein After-Hours-"Latest"-Quote kann Stunden alt
sein, und kein Preis-Check darf auf ihm beruhen.

| Regel | Bedingung |
|-------|-----------|
| `quote_time_missing` | Quote ohne Zeitstempel |
| `quote_time_invalid` | Zeitstempel ist kein zeitzonen-behaftetes `datetime` |
| `quote_time_in_future` | Zeitstempel mehr als 2 s (Clock-Skew-Toleranz, inklusiv) in der Zukunft |
| `quote_stale` | Quote älter als `max_entry_quote_age_seconds` (genau am Limit ist erlaubt) |
| `quote_at_or_beyond_stop_loss` | Buy: Ask `<=` SL. Sell: Bid `>=` SL |
| `quote_at_or_beyond_take_profit` | nur mit TP. Buy: Ask `>=` TP. Sell: Bid `<=` TP |
| `deviation_toward_stop_loss` | `deviation > max_entry_deviation_sl_fraction` (Setup evtl. ungültig) |
| `deviation_away_from_stop_loss` | `-deviation > max_entry_deviation_sl_fraction` (Chasing: mit festem SL ist das echte Risiko größer als gesized) |

Beide Grenzen kommen pro Deployment aus `settings`, nie aus Signal-Metadata, und
werden von `risk.live.entry_gate_settings()` geprüft: beim Anlegen des Deployments
(`shared.database.create_live_deployment` und der API-Endpoint, 400) und erneut
beim Dispatch (fail-closed, vor dem Quote-Abruf):

| Setting | Default | Gültig |
|---------|---------|--------|
| `max_entry_deviation_sl_fraction` | **0.25** | `0 <= x <= 1` (Bruchteil der SL-Distanz; `25` gemeint als 25 % ist ein Fehler; `0` lässt nur einen Quote exakt am Entry zu) |
| `max_entry_quote_age_seconds` | **10** | endlich, `> 0` |
 Der Vergleich hat
eine Toleranz von `1e-9`, damit Float-Rundung an der Grenze nicht entscheidet.
Ohne TP (explizite Unbounded-Policy) entfällt nur die TP-Regel. Der Quote ist
**nur Gate**, nie Sizing-Input: Menge und Levels bleiben die des Signals.

Fail-closed: Adapter ohne Quote-Support, Fehler beim Abruf, Timeout, fehlende,
nicht-positive, nicht-endliche, gekreuzte (Bid > Ask) oder nicht aktuelle Quotes lehnen den Entry
ab und werden geloggt. Exit-only-Orders laufen nicht durch das Gate.

Quellen: Alpaca `get_stock_latest_quote` (IEX-Feed, wie die Bars), Zeitstempel
ist der des Quotes selbst, nie `now()`; cTrader abonniert kurz den Spot-Stream des
Symbols, nimmt den ersten vollständigen Bid/Ask und meldet sich wieder ab (Timeout
`quote_timeout_seconds`, Default 5 s). Zeitstempel ist die Broker-Zeit des
Spot-Events, sonst die lokale Empfangszeit (`timestamp_source="receive"`; ein
Spot-Event wird live gepusht, seine Empfangszeit begrenzt also sein Alter).

Persistiert wird auf der Signal-Metadata: `entry_quote_bid`, `entry_quote_ask`,
`entry_quote_side`, `entry_quote_price`, `entry_quote_time` (ISO-8601),
`entry_quote_time_source` (`broker` / `receive`), `entry_quote_age_seconds`,
`max_entry_quote_age_seconds`, `entry_quote_source`, `entry_deviation_sl_fraction` (mit Vorzeichen),
`entry_deviation_direction` (`toward_stop_loss` / `away_from_stop_loss` / `none`),
`max_entry_deviation_sl_fraction`, `entry_gate_rejection_rule` (`None` bei
akzeptierten Signalen; abgelehnte werden noch nicht persistiert, nur geloggt).
Payload-Werte unter diesen Keys werden überschrieben. Ein Schema für Quote-Snapshots (auch abgelehnter Signale) ist #134.

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