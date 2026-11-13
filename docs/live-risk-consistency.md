# Live Risk Konsistenz

## Problem

Die Position-Sizing-Logik baut in Backtest und Live auf derselben `RiskEngine` auf.
Live gibt es genau **einen** Sizing-Einstieg (#125):
`live_runner.daemon.size_live_signal_for_submission()`. Der frühere zweite
Einstieg `risk.live.size_live_signal()` hatte keinen Produktions-Aufrufer und ist
entfernt.

| Modul | Zweck | Unterschiede |
|-------|-------|--------------|
| `risk/engine.py` | Backtest-Risk-Engine | Vollständige RiskEngine-Klasse mit konfigurierbarem RiskConfig |
| `risk/live.py` | Live-Levels und Gate-Regeln | `resolve_live_levels()` (einziger Entry/SL/TP-Resolver), `entry_gate_settings()`, `entry_deviation_sl_fraction()`; kein Sizing |
| `live_runner/daemon.py` | Live-Sizing und -Submission | `size_live_signal_for_submission()` (einziger Live-Sizing-Einstieg) / `prepare_order_with_risk()`: Sizing mit Deployment-`RiskConfig` über `RiskEngine`, Levels aus `resolve_live_levels()`, Entry-Deviation-Gate |

## Kritische Unterschiede

### 1. Stop-Direction Enforcement

**Backtest (`risk/engine.py`):**
```python
enforce_stop_direction = bool(signal.metadata.get("enforce_stop_direction", False))
```
Standard: `False` (nicht erzwungen)

**Live (`live_runner/daemon.py`, `size_live_signal_for_submission`):**
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
- `position_size = min(position_size, max_notional_per_trade * balance / (entry_price * pip_value))`
  (Notional-Obergrenze, `max_notional_per_trade` als Bruchteil des Balances, Default 0.20).
  Das Notional einer Position in Account-Währung ist `position_size * entry_price * pip_value`
  (#146: ohne `* pip_value` war die Obergrenze für cTrader-FX 1000x zu locker).
- `pip_value` ist der P&L **einer** `position_size`-Einheit pro Preisbewegung um 1.0, in
  Account-Währung (nicht pro Pip). Aktien in USD auf einem USD-Konto: 1.0; cTrader EURUSD
  auf einem USD-Konto: 1000 (eine qty = 0.01 Lot = 1.000 EUR).

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
direkt) und `max_entry_deviation_sl_fraction`. Der Pip-Value kommt vom Broker
(`adapter.get_pip_value()`, #146) oder von einem vertrauten Aufrufer; ein implizites
1.0 gibt es im Live-Pfad nicht mehr.
Risk-Policy kommt ausschliesslich aus Deployment-/Runtime-Config
(`RiskConfig`), State (`current_positions`, `current_exposure`) aus dem
Portfolio-/Broker-Source-of-Truth. Der früher `qty`/`explicit_qty`-Bypass um die
komplette Risk-Engine ist entfernt; `size_live_signal_for_submission` size't
jedes Live-Signal durch `RiskEngine`.

## Pip-Value aus dem Broker (#146)

`prepare_order_with_risk()` holt vor dem Sizing `adapter.get_pip_value(symbol)`
(`LivePipValue`: `value = units_per_qty * conversion_rate`):

- **cTrader:** aus `ProtoOASymbol.lotSize` (in Cent der Basiswährung, EURUSD
  10.000.000 = 100.000 EUR), also `units_per_qty = lotSize / 100 / 100`, und der
  Umrechnung der Quote- in die Konto-Währung entlang der Broker-Kette
  (`ProtoOASymbolsForConversionReq`) mit Live-Quotes. Pro Hop wird die Seite
  genommen, die den Kurs **höher** macht (Ask beim Multiplizieren, Bid beim
  Dividieren): ein höherer Pip-Value sized kleiner, der Spread kann den Verlust am
  SL also nur unter das Budget drücken. Nur ein vom Broker gesendetes `lotSize` zählt;
  die statische Fallback-Tabelle (in Basis-Einheiten, für FX/XAU 100x unter den
  Broker-Cent, #184) wird nie fürs Sizing benutzt. Ändert sich die Skala zwischen
  Sizing und Submit (z. B. Reconnect mit fehlgeschlagenem Props-Fetch), verweigert
  der Adapter die Order (`pip_value_units_per_qty`).
- **Alpaca:** 1.0 (Shares/Coins in USD auf einem USD-Konto). Ein Nicht-USD-Konto oder
  ein nicht in USD quotiertes Paar (`ETH/BTC`, `BTC/USDT`) wird abgelehnt.

Fail closed (Eintrag abgelehnt, Grund im Log `Pip value unavailable`): Adapter ohne
`get_pip_value`, Fehler oder Timeout (`PIP_VALUE_FETCH_TIMEOUT_SECONDS`), Wert nicht
endlich und positiv, `value != units_per_qty * conversion_rate`, ein Kurs ungleich 1
ohne Quotes, oder ein Umrechnungs-Quote, der die Frische-Regel des Entry-Gates
verletzt (fehlende/ungültige Zeit, Zukunft, älter als `max_entry_quote_age_seconds`).
Exit-only-Orders brauchen keinen Pip-Value.

Persistiert am Signal: `pip_value`, `pip_value_source`, `pip_value_units_per_qty`,
`pip_value_conversion_rate`, `pip_value_quote_asset`, `pip_value_account_asset`,
`pip_value_conversion_symbols`, `pip_value_conversion_quote_times`,
`pip_value_conversion_quote_ages_seconds`. Payload-Werte unter diesen Namen werden
immer überschrieben.

Folge der korrigierten Notional-Obergrenze: `RiskConfig` erlaubt
`max_notional_per_trade <= 1` (keine Hebelwirkung). Auf einem 100-USD-Konto ist
damit schon 0.01 Lot EURUSD (~1.100 USD Notional) über der Grenze, solche Einträge
werden abgelehnt (unter `minVolume`).

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

Der Quote-Abruf selbst ist im Daemon auf 20 s begrenzt
(`ENTRY_QUOTE_FETCH_TIMEOUT_SECONDS`); ein Timeout lehnt den Entry ab. Der Vergleich hat
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

## Broker-Raster (Alpaca)

Alpaca nimmt Bracket-/OTO-Entries nur in ganzen Stück und mit SL/TP auf dem
Preisraster an (0.01 ab 1 USD, 0.0001 darunter). `AlpacaLiveAdapter` normalisiert
jede Entry vor dem Persistieren: die risk-gesizte `qty` wird auf ganze Stück
**abgerundet** (unter 1 Stück → abgelehnt), SL und TP werden **zum Entry hin**
gerundet. Damit liegt das tatsächliche Risiko nie über dem gesizten Budget. Exit-only
Orders (simple) bleiben unverändert.

## Implizite Divergenz

Der Unterschied bei `enforce_stop_direction` führt dazu, dass:

1. **Backtest**: Trades mit "falsch herum" gesetzten Stops werden akzeptiert und unterschiedlich gestartet
2. **Live**: Solche Trades werden mit `position_size=0` abgelehnt

Das verhindert unrealistische P&L im Live-Trading, erzeugt aber Abweichungen zwischen Backtest und Realität.

## Empfehlung

Der aktuelle Ansatz ist beabsichtigt: Live-Trading soll strengere Validierung haben als Backtests. Die Dokumentation hierfür ist nun vorhanden.

## Tests

Ein parametrierter Test in `tests/test_risk_consistency.py` sollte folgende Fälle abdecken:

1. Identische Inputs → identische Outputs zwischen `RiskEngine.calculate_position_size`
   und dem Live-Pfad `size_live_signal_for_submission`
2. Strikter Stop-Direction Check wird im Live-Pfad erzwungen
3. ATR-basierter Fallback wird in beiden Pfaden gleich berechnet