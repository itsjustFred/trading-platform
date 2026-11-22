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
  SL also nur unter das Budget drücken. Das gilt nur für den Umrechnungs-Quote zum
  Sizing-Zeitpunkt: Cross-Währungs-Kurse können sich bis zum Stop bewegen, und der
  Submit prüft nur die Symbol-Skala, nicht den Umrechnungskurs. Das SL-Budget ist
  daher keine harte Verlustgrenze in Konto-Währung. Ein expliziter `pip_value`-Override
  (`source="caller"`) trägt keine `units_per_qty`; cTrader verweigert ihn, weil die
  Skala beim Submit nicht prüfbar wäre. Jede Nutzung des Overrides schreibt eine
  Warnung ins Log (`Caller-supplied pip_value override in use`), da er den
  Broker-Lookup samt Frische-Prüfung umgeht.
  Der cTrader-Volumen-Divisor (`CTRADER_VOLUME_PER_BASE_UNIT = 100`) ist nur auf den
  Demo-Symbolklassen geprüft, auf denen er verifiziert wurde (EURUSD/FX, XAUUSD,
  US500); für andere Klassen ist er unverifiziert.
  Nur ein vom Broker gesendetes `lotSize` zählt;
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
Bei Entries werden Payload-Werte unter diesen Keys überschrieben; bei Exits (die kein Gate durchlaufen) werden sie entfernt. Ein Schema für Quote-Snapshots (auch abgelehnter Signale) ist #134.

## Pre-Submit-Hart-Caps (#302)

`live_runner/pre_submit_caps.py::enforce_pre_submit_caps()` ist die letzte
Prüfung vor dem Broker-Submit der Daemon-Pfade — nach dem Sizing, nach dem
Submission-Claim, unmittelbar vor `adapter.submit_order()`. Sie wird an beiden
Submit-Chokepoints aufgerufen (`submit_order_with_risk()` und der Discord-
Dispatch in `_prepare_claim_and_submit_signal()`). Innerhalb von
`scripts/live_smoke.py` laufen nur die direkten `adapter.submit_order`-Aufrufe
(:596 Einstieg, :1481 Close) an den Caps vorbei; die Aufrufe über
`submit_order_with_risk` (:702, :1130, :1570) unterliegen ihnen wie jeder
Daemon-Pfad. `live_runner/test_signal_trigger.py:86` ist ein weiterer direkter
Submit. Alles Operator-Werkzeuge für manuelle Tests, kein Daemon-Pfad; ein
Fehler dort hat damit kein Cap-Backstop.
**Unabhängig vom Sizing-Code** sind die Broker-Fakten: Quote, Pip-Value,
Balance, die benutzte Marge (`get_used_margin`, #359) und die maximale
Hebel des Brokers für Account und Asset
(`get_max_leverage`; cTrader über `ProtoOASymbol.leverageId` +
`GetDynamicLeverageByID` — der Tier-Wert steht in Cents wie
`leverageInCents`, 1:200 = 20000, und wird vor der Nutzung durch 100
gerechnet — geklemmt an die eigene Account-Hebel `leverageInCents` aus
`ProtoOATrader`, wie Spotwares Beispiel mit `min(trader, tier)`;
`ProtoOATrader.maxLeverage` wird bewusst nicht gelesen — es ist ein
uint32 ohne „InCents“-Suffix, nur die Obergrenze für *Änderungen* der
Hebel, gegen die `leverageInCents` bereits validiert wird; fehlt dem
Symbol eine `leverageId`, ist die Account-Hebel allein die Obergrenze;
Alpaca über `min(account.multiplier, 2)` — der 4x-Multiplikator ist nur
Intraday-Buying-Power, über Nacht gilt Reg T mit 2x, und kein Plattform-Order
ist als intraday-only markiert; Crypto bleibt bei
1x; unbekannt → 1x). Die Tier-Hebel ist eine Obergrenze: bei cTrader sinkt
die Hebel mit dem Volumen, daher gilt der erste (kleinste) Tier — der
tatsächlich angewandte Hebel kann niedriger sein und wird vom Broker
weiter erzwungen.
Menge und Preislevel stammen
dagegen aus dem gesizeten Signal — nichts wird aus dem Payload neu erfunden.

Abgelehnt wird (fail-closed, ERROR-Alert `pre_submit_cap_refusal` plus
Audit-Eintrag über `record_live_control_audit`), wenn

| Regel | Bedingung |
|-------|-----------|
| `qty_cap` | `qty > pre_submit_max_qty` (nur wenn konfiguriert) |
| `price_deviation` | `|entry - quote_side| / quote_side > pre_submit_max_price_deviation_fraction` (Ask für Buy, Bid für Sell, absolut in beide Richtungen) |
| `notional_cap` | `qty * quote * pip_value > pre_submit_max_notional_equity_fraction * equity` (nur wenn der Bruchteil explizit konfiguriert ist) |
| `stop_risk` | `qty * |entry - stop| * pip_value > pre_submit_max_risk_multiple * (max_risk_per_trade * equity)` |

Die Caps fügen **keine eigene Notional-Obergrenze unter dem Broker-Maximum
hinzu** — die einzige Notional-Obergrenze dieses Moduls ist die maximale
Hebel des Brokers für Account und Asset (#240, Entscheidung auf #329):
braucht ein korrekt gesizter Order mehr Hebel als der Broker
erlaubt, wird die Menge **auf die freie Marge geklemmt** (abgerundet auf
den Lot-Step) statt abgelehnt — verbleibende Caps laufen gegen die geklemmte
Größe. Die freie Marge ist MT5-Stil und entspricht der Backtest-Regel aus
#355 (#359, die Live-Hälfte von #347): `equity − used_margin`, also das
Account-Equity minus der Marge, die der Broker bereits für offene Positionen
hält (`adapter.get_used_margin` — cTrader summiert das broker-gemeldete
`ProtoOAPosition.usedMargin` über alle offenen Positionen, Alpaca liest
`account.initial_margin` und fällt ohne ihn auf Σ |Market Value| /
Effektiv-Hebel zurück); die Notional-Obergrenze ist
`freie_Marge × Broker-Maximum`. Eine unbekannte Broker-Hebel failt closed
auf 1x; eine **unbekannte Used Margin lehnt ab** (`broker_facts_unavailable`)
— anders als bei der Hebel ist „niedriger ansetzen“ hier die unsichere
Richtung, weil eine zu kleine Used Margin die freie Marge überschätzt. Ist
die freie Marge aufgebraucht, bleibt nach der Klemme weniger als ein
Lot-Step und der Order wird abgelehnt (`broker_leverage_cap`) — live gibt
es kein stilles Überspringen wie im Backtest, die Verweigerung gibt das
Submission-Lease frei. Die Klemme wird
mit WARNING geloggt und als `pre_submit_leverage_clamp` auditiert (das Feld
`broker_max_leverage_is_upper_bound` markiert die Obergrenzen-Eigenschaft;
die Audit-Daten tragen `used_margin`, `free_margin` und `leverage_ceiling`;
folgt auf die Klemme eine Verweigerung durch einen anderen Cap, entstehen
bewusst zwei Audit-Zeilen — Klemme und Verweigerung); lässt die
freie Marge nicht einmal einen Lot-Step zu, wird abgelehnt
(`broker_leverage_cap`). Die Klemme ist damit ein Backstop gegen eine
kaputte Größe, keine Platform-Notional-Policy: das Sizing deckelt den
Notional pro Order weiterhin bei `max_notional_per_trade` (Default 0.20;
`RiskConfig` lehnt Werte > 1 ab) — diese Obergrenze bleibt bis zur
#240-Entscheidung bestehen.

Notional und Stop-Risk rechnen gegen das Account-**Equity** — und auf dem
Produktionspfad rechnen auch Sizing und Caps gegen dieselbe Basis: beide lesen
`get_balance()[1]`, das auf Alpaca das Broker-Equity (`account.equity`) und
auf cTrader den realisierten Balance ist (`ProtoOATrader` hat kein
Equity-Feld); nur ein explizit übergebener `account_balance`-Parameter weicht
ab. Die Caps sind damit kein zweites Sizing, sondern ein hartes Backstop
gegen kaputte Größen auf derselben Basis.

Zusätzlich lehnt jede **Nicht-Verifizierbarkeit** ab: fehlender Stop, ungültige
Cap-Settings, Quote/Pip-Value/Balance/Used-Margin nicht abrufbar oder unbrauchbar,
insbesondere eine Quote ohne verwertbare oder veraltete Broker-Zeit
(`stale_quote`, gleiche Regel wie das Entry-Gate, `max_entry_quote_age_seconds`)
(`broker_facts_unavailable` / `broker_facts_unusable` / `stale_quote` /
`invalid_cap_config` / `invalid_entry_gate_config` / `missing_stop_loss` /
`unresolved_levels` / `invalid_qty`). Ob ein Order ein Exit ist, entscheidet
`is_exit_order` — dieselbe Klassifikation, nach der das Sizing dispatched —
nie ein Payload-Marker: ein Entry mit `risk_sizing_mode="exit_only"` wird
normal gecappt, und der Entry-Pfad entfernt `exit_only`/`risk_sizing_mode`
vor dem Adapter. Exit-Orders laufen nie durch die Caps — sie reduzieren
Exposure, und eine Cap darf einen Flatten nicht blockieren.

Settings pro Deployment (deployment = account + symbols, also der
Per-Account/-Per-Symbol-Scope), validiert beim Anlegen
(`validate_pre_submit_cap_settings` in `create_live_deployment`: Typ,
Endlichkeit, Positivität und eindeutige Symbol-Keys) und erneut
fail-closed beim Submit:

| Setting | Default | Gültig |
|---------|---------|--------|
| `pre_submit_max_qty` | **unbegrenzt** | `> 0`, endlich (kein universeller Default: Share, Lot und 0.01-Lot sind verschiedene Instrumente) |
| `pre_submit_max_notional_equity_fraction` | **Broker-Hebel** (unbekannt → 1x) | `> 0` (Order-Notional als Bruchteil des Account-Equity; nur explizit gesetzt = harter Reject-Cap) |
| `pre_submit_max_price_deviation_fraction` | **0.02** | `> 0` |
| `pre_submit_max_risk_multiple` | **2.0** | `> 0` (Vielfaches des konfigurierten `max_risk_per_trade`-Risikos) |
| `pre_submit_symbol_caps` | — | Mapping `SYMBOL -> {gleiche Keys}`, überschreibt nur für dieses Symbol |

Die Defaults sind konservative Backstops gegen Sizing-Bugs (Basis: #146
~1000x, #184 100x), keine Trading-Policy — Risk-Multiple 2.0 und
Price-Deviation 2% sind per Maintainer-Entscheidung (2026-11-21) akzeptiert,
pro Symbol überschreibbar. Ein Timeout von 30 s
(`PRE_SUBMIT_CAPS_FETCH_TIMEOUT_SECONDS`) begrenzt die Broker-Lesungen;
bei Timeout wird abgelehnt.

## Commission-Vorzeichen (#184 Finding 14)

`live_executions.commission` ist bei **allen** Brokern ein **positiver Kostenwert**
(Gebühr = positiv, Rebate = negativ, bekannt-null = `0.0`; ein Rebate kann nur der
cTrader-Pfad speichern, `apply_live_execution_commissions` (Alpaca-CFEE) lehnt negative Werte ab). Net-PnL ist überall
`gross - commission` (Viewer, `net_pnl`, Alpaca-CFEE-Backfill aus #143).
Adapter normalisieren vor dem Speichern: cTrader meldet `ProtoOADeal.commission`
als vorzeichenbehafteten Cashflow (Gebühr negativ) und wird über
`base_adapter.commission_cost` negiert; Alpaca meldet keine Commission (bekannte Null `0.0` oder NULL, siehe #124) und
backfillt CFEE als positive Kosten.
Bereits gespeicherte cTrader-Zeilen (vor dieser Änderung) trugen das rohe negative
Vorzeichen; `init_db` wandelt sie einmalig per Migration
(`_migrate_ctrader_commission_to_cost`, `abs` des gespeicherten Werts) in einen positiven
Kostenwert um. Nur Konten mit Broker `ctrader` sind betroffen, NULL (unbekannt) und `0.0`
bleiben. Die Migration ist in `schema_data_migrations` vermerkt und läuft genau einmal:
ein erneutes `abs` würde einen vom korrigierten Adapter gespeicherten Rebate (negativ)
in eine Gebühr verwandeln. Ein vor der Änderung gespeicherter cTrader-Rebate (roh positiv)
ist von einer neuen Gebühr nicht unterscheidbar und bleibt positiv.
`commission_cost` lehnt nicht-endliche Werte mit `ValueError` ab (kein stilles `0.0`).
Deploy-Hinweis: Daemon vor dem ersten Start des neuen Builds stoppen (siehe
`docs/live-deploy-runbook.md`); ein alter Build, der nach dem Migrationsvermerk noch eine
rohe negative Gebühr schreibt, wird nicht mehr umgewandelt.

## Broker-Raster (Alpaca)

Alpaca nimmt Bracket-/OTO-Entries nur in ganzen Stück und mit SL/TP auf dem
Preisraster an (0.01 ab 1 USD, 0.0001 darunter). `AlpacaLiveAdapter` normalisiert
jede Entry vor dem Persistieren: die risk-gesizte `qty` wird auf ganze Stück
**abgerundet** (unter 1 Stück → abgelehnt), SL und TP werden **zum Entry hin**
gerundet. Damit liegt das tatsächliche Risiko nie über dem gesizten Budget. Exit-only
Orders (simple) bleiben unverändert.

Vor dem Persistieren werden zusätzlich abgelehnt (ValueError, kein Signal-Row, keine
Order): SL/TP auf der falschen Seite des Entry, SL/TP innerhalb eines Ticks vom Entry,
fehlende/ungültige SL/TP-Level, Extended-Hours-Brackets und Crypto-Symbole (Alpaca
kennt keine Bracket-/OTO-Orders für Crypto).


## Broker-Raster (cTrader)

cTrader bekommt SL/TP als relative Distanzen in 1/100000 des Preises. Das Raster ist
`10 ** max(0, 5 - price_digits)` des Symbols. Beide Distanzen werden **zum Entry hin
abgerundet** (`floor`), damit das tatsächliche Risiko nie über dem gesizten Budget
liegt: ein Stop von 1,6 Ticks geht als 1 Tick raus. Ein Stop unter einem Tick wird
abgelehnt (ValueError vor dem Senden), ein Take-Profit unter einem Tick wird als ein
Tick gesendet (nie 0).
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