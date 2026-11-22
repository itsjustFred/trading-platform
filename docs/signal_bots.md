# Signal Bots

## Processor-Contract

Jeder Processor implementiert:
```python
process_signal(raw_signal: dict, market_data: pd.DataFrame) -> TradeSignal
```

- `raw_signal`: symbol/action/timestamp + optionale Zusatzdaten
- `market_data`: OHLC-Kontext zur SL/TP-Berechnung (z. B. ATR)
- Rückgabe: normalisiertes `TradeSignal` inkl. `metadata`

## Registry

Zentrale Registry in `signal_bots/registry.py`:

| ID | Klasse | Config | Status |
|----|--------|--------|--------|
| `ramjet` | RamJetSignalProcessor | `signal_bots/ramjet/config.yaml` | ✅ Verfügbar |
| `rocketfuel_longsonly` | RocketFuelSignalProcessor | `signal_bots/rocketfuel/config.yaml` | ✅ Verfügbar |
| `rocketfuel_longandshort` | RocketFuelSignalProcessor | `signal_bots/rocketfuel/config.yaml` | ✅ Verfügbar |
| `nitro` | NitroSignalProcessor | `signal_bots/nitro/config.yaml` | ✅ Verfügbar (RamJet-Formeln, BUY-only) |

**Hinweis:** StockKit ist die Discord-Quelle/der Server, über den die Bots
signale beziehen — kein Processor. Ein ehemaliger `stockkit`-Alias auf den
RamJet-Processor wurde entfernt (Decision Record 2026-08-31): jeder Bot hat
seine eigene Processor-Klasse und ihr eigenes Config-File; gemeinsame
Low-Level-Helfer sind erlaubt, Processor-Semantik und Konfigurationen bleiben
pro Bot.

## Bots im Detail

### Nitro
- **Status:** Aktiv, registriert
- **Features:** RamJet-Formeln, BUY-only Signalzusammenführung
- **Last-Run:** Über Health-Check-Job validierbar

### RamJet
- **Status:** Aktiv, registriert
- **Features:** ATR + Supertrend BUY-only Setup mit TP1; Trailing-Modus konfigurierbar
- **Trailing-Modus (`trailing_mode`):** `none` (Default), `percent` (benötigt `trailing_percent`
  in (0, 1)) oder `supertrend_flip`. Der Modus ist eine Einstellung des Bots bzw. des Deployments
  (`signal_config.signal_processor_config.trailing_mode`), nicht fest verdrahtet. Zentrale
  Definition: `shared/trailing.py`.
  - **Fail closed:** Ein unbekannter Modus oder einer, den der ausführende Pfad nicht kann, wird
    abgelehnt: bei der Deployment-Validierung und erneut zur Signalzeit. Das Signal wird dann
    nicht gehandelt; ein angeforderter Trail wird nie stillschweigend weggelassen. Bei der
    Deployment-Validierung wird der Modus sowohl aus einem expliziten Override
    (`signal_config.signal_processor_config.trailing_mode`) als auch aus dem Default der eigenen
    Bot-Config gelesen — der Prozessor des Deployments wird dafür genauso gebaut wie vom Daemon
    (`shared/trailing.py: validate_live_deployment_trailing_mode`).
  - **Unterstützt:** Replay-Backtest `none`, `percent`, `supertrend_flip`. Live je Broker: Alpaca `none`, `percent`;
    cTrader nur `none` (der Daemon hat keine cTrader-Marktdaten zum Nachziehen). `percent` wird
    live pro geschlossener Kerze des Signal-Timeframes mit derselben Arithmetik wie im Replay
    nachgezogen und der Stop beim Broker ersetzt; Details und Betriebsgrenzen in
    `docs/live-deploy-runbook.md` ("Live Trailing Stops"). `supertrend_flip` wird live auf keinem
    Broker ausgeführt und dort abgelehnt (Grund und Design-Vorschlag im Runbook).
  - **`supertrend_flip` im Replay (#168):**
    1. TP1: Teilausstieg von `tp1_exit_percent` (Bruchteil, 0.5 = halbe Position) zu `tp1_price`
       — in ganzen Shares: `qty * Bruchteil` abgerundet, wie im Original-Bot
       (`int(qty * tp1_exit_percent)`, `shared/trailing.tp1_partial_quantity`). Ergibt sich 0
       (z. B. 1 Share bei 0.5), wird der Teilausstieg übersprungen, genau wie im Original-Bot:
       kein Verkauf, kein Einstands-Stop, und der restliche Workflow (Aktivierung, Flip) startet
       nicht. Die Position bleibt voll unter ihrem ursprünglichen Stop; im Trade-Meta steht
       dann `supertrend_flip.tp1_skipped_time` (und `tp1_time` bleibt leer).
    2. Danach Stop auf Einstand (Entry-Preis), wenn `breakeven_after_tp1`; wirksam ab der Kerze
       nach der TP1-Kerze.
    3. Aktivierung: erste geschlossene Kerze NACH der TP1-Kerze, deren Supertrend bereit und
       grün ist. Die TP1-Kerze selbst wird nach dem TP1-Fill nicht mehr ausgewertet — nichts, was
       in oder an der TP1-Kerze nach dem Fill passiert, darf den Trail bewegen oder auslösen.
       "Nicht bereit" (Warm-up) zählt weder als grün noch als rot.
    4. Ausstieg: erste bereite rote Kerze nach der Aktivierung; der Rest wird zum OPEN der
       folgenden Kerze geschlossen (der Flip ist erst beim Schluss der Flip-Kerze bekannt). Ist die
       Flip-Kerze die letzte Kerze der Daten, wird die Position wie jede andere am Datenende
       geschlossen (`end_of_data`).
    - Ein Signal ohne `stop_loss` wird abgelehnt: vor TP1 ist der Stop der einzige Schutz,
      der Workflow setzt keinen eigenen.
    - Stop und TP1 in derselben Kerze: Stop zuerst (Konvention des Adapters,
      `resolve_same_bar_exit`); dann gibt es kein TP1.
    - Supertrend: dieselbe Implementierung wie der Processor (`calculate_supertrend`), mit
      `supertrend_period`/`supertrend_multiplier` aus `trailing_sl_config`, nur aus geschlossenen
      Kerzen.
    - Ein Trade mit mehreren Exits (`tp1`, dann `supertrend_flip`/`breakeven_stop`/`stop_loss`/
      `end_of_data`); der TP1-Tranche ist auf ganze Shares abgerundet, MFE/MAE werden nach dem
      Teilausstieg auf den verbleibenden Shares gemessen und die Equity-Kurve bucht die
      gefloorten Beine. Gebühren einmal zum mengengewichteten Exit-Preis. Fehlende oder ungültige
      Parameter brechen den Lauf ab (keine erfundenen Defaults). Nur Long-Einstiege.
  - Mit dem Default `none` läuft RamJet bewusst nur mit dem initialen ATR-Stop.
- **Last-Run:** Über Health-Check-Job validierbar

### RocketFuel (Long-only & Long+Short)
- **Status:** Aktiv, registriert
- **Features:** Komplexere Entry-Logik, Varianten für Long-only vs Long+Short
- **Last-Run:** Über Health-Check-Job validierbar

### StockKit
- **Status:** Kein Processor — Discord-Quelle/Server (siehe Registry-Hinweis)
- **Routing:** `live_runner/stockkit_routing.py` mappt die Discord-Bots
  (`nitro`, `ramjet`, `rocketfuel`) auf ihre Alpaca-Accounts
- **Explizite Markierung (#121):** Ein StockKit-Deployment setzt
  `signal_config.stockkit_route` (z. B. `rocketfuel`). Ist der Wert unbekannt,
  leer oder kein String (etwa `rocketfuel_shortsonly`, `nitro_v2`), oder
  widerspricht er der aus dem Bot-Namen abgeleiteten Route, wird das Deployment
  beim Anlegen abgelehnt und beim Dispatch blockiert; der zugehörige
  Discord-Service wird auf `FAILED` gesetzt (ein vom Operator gesetztes
  `PAUSED`/`STOPPED` bleibt). Ohne Markierung ist es ein normaler Discord-Bot;
  ein Bot-Name, der ein bekannter Alias ist, wird aber weiterhin gegen seinen
  Account geprüft.

## Health-Check Status

**Stand 2026-08-31:**
- Alle 4 registrierten Prozessoren (`ramjet`, `rocketfuel_longsonly`,
  `rocketfuel_longandshort`, `nitro`) sind importierbar und haben gültige
  Konfigurationsdateien.
- Health-Check-CLI:
  ```bash
  python -m signal_bots.health_check
  ```
- Scheduler-Job: `scheduler/jobs/signal_bot_health_check.py` aktualisiert den
  Hintergrundtask-Status und protokolliert Registry-/Config-Fehler.
- Tests:
  - `tests/test_signal_bot_health_check.py`
  - `tests/test_scheduler_signal_bot_health.py`

### Betrieb (#158, Entscheidung 2026-11-21)

Der Health-Check bleibt bewusst ein **Standalone-Skript plus Scheduler-Job** —
kein `runner.cli`-Subcommand. So wird er ausgeführt und geplant:

- **Manuell:** `python -m signal_bots.health_check` (keine Flags). Gibt das
  JSON-Ergebnis auf stdout aus; Exit-Code `0` = alle Processor ok, `1` =
  mindestens ein Fehler. Geprüft werden pro Processor: Import/Factory,
  `process_signal`-Probe mit synthetischen Marktdaten und die Existenz der
  Config-Datei aus der Registry.
- **Geplant:** Viewer → **Services** → „+ Add Scheduled Job" mit Job Type
  `signal_bot_health_check` und einem Cron-Ausdruck, oder direkt
  `POST /api/services` mit `type: "scheduled_job"`, `status: "running"` und
  `config: {"cron": "...", "job_type": "signal_bot_health_check"}`. Der Cron
  wird in UTC interpretiert (`scheduler/scheduler.py`, `CronTrigger.from_crontab`).
  Der `ServiceScheduler` läuft innerhalb von `live_runner.daemon`; der
  Job-Handler schreibt das Ergebnis als Background-Task-Status in die DB
  (Services/Tasks im Viewer). `PAUSED`/`STOPPED` des Service pausiert den Lauf.

## Risk-Engine Integration

1. Signal-Processor berechnet `entry_price`, `stop_loss`, `take_profit`
2. Risk-Engine (`risk.engine.RiskEngine`) sized auf Basis von:
   - explizitem `stop_loss` (bevorzugt), oder
   - Fallback (`atr` aus `metadata`, sonst pip-basiert)
3. Ergebnis (`position_size`, `risk_amount`, `risk_percent`) wird im Replay und im Live-Orderflow verwendet

## Empfehlung

Der Health-Check-Job ist implementiert. Für Produktionsbetrieb sollte die
Scheduler-Konfiguration den Job regelmäßig ausführen und bei `failed`-Status
einen Alert auslösen.

## Discord Ingestion Gate (live_runner)

Discord-Signalquellen vertrauen ausschließlich einer explizit konfigurierten
Autor-/Bot-ID-Allowlist (`trusted_author_ids`); der Autor-Check ist die
Quell-Gate. Nachrichten jedes anderen Autors werden verworfen und geloggt.

Konfiguration:

- Deployment (`live_deployments.yaml` → `signal_config.trusted_author_ids`,
  Liste oder einzelner String; mehrere Deployments auf einem Kanal werden
  zusammengeführt).
- Discord-Bot-Service, in dieser Reihenfolge:
  1. Service-Config `trusted_author_ids` (bzw. `trusted_bot_ids`)
  2. Umgebungsvariable `DISCORD_BOT_<NAME>_TRUSTED_AUTHOR_IDS`, getrennt durch
     Komma **oder** Whitespace
  3. Umgebungsvariable `DISCORD_TRUSTED_AUTHOR_IDS` (Fallback für Single-Bot-Setups)

  Die bot-spezifische Variante ist wichtig, wenn mehrere Bots dieselbe
  `.accounts.env` teilen: ohne sie fällt jeder Bot auf die gemeinsame Liste
  zurück und vertraut damit auch den *anderen* Bots.

Ohne Allowlist startet der Listener nicht (fail-closed). Zusätzlich gilt:

- **Leer ist nicht „nicht gesetzt".** Eine vorhandene, aber leere
  `DISCORD_BOT_<NAME>_TRUSTED_AUTHOR_IDS` bedeutet „dieser Bot vertraut
  niemandem" und fällt **nicht** auf die gemeinsame Liste zurück. Ein
  unauflösbarer Wert (keine `.accounts.env`-Zuordnung) ergibt ebenfalls eine
  leere Allowlist; `os.environ` wird dafür bewusst nicht gelesen.
- **Dispatch-Gate pro Deployment (fail-closed).** Die zusammengeführte Allowlist
  eines Kanals ist nur der Ingestion-Filter. Vor jeder Order muss der Autor in der
  *eigenen* Allowlist des Ziel-Deployments stehen. Ein Deployment mit leerer,
  leerer-aber-gesetzter oder nicht auflösbarer Allowlist handelt für niemanden;
  der Autor eines anderen Bots kann also nie auf diesem Deployment handeln.
- Autoren, die nicht in der Allowlist stehen, werden verworfen und geloggt. Eine
  Serie solcher Nachrichten löst einen einmaligen ERROR-Log mit den abgelehnten
  Autor-IDs aus, setzt den Service aber **nicht** auf `FAILED` (sonst könnte jedes
  Kanalmitglied den Bot stilllegen). Vom Operator gesetztes `PAUSED`/`STOPPED`
  wird von automatischen Status-Schreibvorgängen (Connect, Reconnect, Teardown)
  nie überschrieben; ein normaler Teardown schreibt kein `FAILED`.
- Preisparsing akzeptiert strenge Tausendergruppen (`$1,234.56` → 1234.56) und
  verwirft mehrdeutige Tokens (`$12,34`) statt sie als Dezimalzahl zu interpretieren.
  Ein Komma nach dem Preis (`@ 100, stop $95`) gehört nicht zum Preis.

### Exit-, Report- und Bedingungsnachrichten, Preis-Level (#121)

Keine Nachricht, die einen Exit, einen Gewinnmitnahme-Bericht oder eine
Bedingung nennt, erzeugt eine neue Entry-Order.

- **Preis-Level:** Entry ist ein `Price:`-Label, sonst der erste `@`/`at`-Preis,
  sonst der erste `$`-Preis. `target`/`TP`/`take profit`/`PT` wird Take-Profit,
  `stop`/`SL`/`stop loss` Stop-Loss; diese Klauseln werden vor jeder anderen
  Extraktion entfernt, können also nie Entry sein. Zwei verschiedene Werte für
  dasselbe Level, ein fehlerhafter Level-Preis oder ein Level auf der falschen
  Seite des Entrys → Nachricht verworfen. `at` nach einem Indikator (`RSI at 28`)
  ist kein Preis.
- **Reports** (`Took profit on …`, `stopped out`, `target hit`, `Closed …`,
  `Exited …`) und **Bedingungen/Absicherungen** (`wait`, `if`, `once`, `when`,
  `watch for`, `looking for`, `potential`, `before you buy` …) werden verworfen
  und geloggt.
- **Exit/Close** (`Exit long AAPL @ 100`, `Closing long AAPL at $100`,
  `Close short TSLA @ 50`) wird ein Exit-only-Signal
  (`metadata.signal_intent = "exit"`, `exit_fraction`, optional
  `exit_position_side`). Die Close-Seite kommt aus der Position, nie aus der
  Nachricht; ein Exit trägt kein SL/TP und läuft nicht durch den
  Entry-Signal-Processor. Ein Exit braucht genau einen Ticker und einen Preis.
  Ohne Preis (`Trim half AAPL`) wird er verworfen und auf `WARNING` geloggt
  (`exit without a price`), weil ein Signal einen positiven Preis braucht.
- **Exit ohne eindeutigen Ticker:** Ein Exit-Befehl, der eine Seite nennt
  (`Exit long …`, `Close the short …`) oder dessen Objekt wie ein Ticker
  geschrieben ist (Großbuchstaben, `$`, `**`), aus dem sich aber kein Exit
  parsen lässt, wird verworfen (`WARNING`) und erreicht nie den Entry-Parser.
  Ein nacktes `ON`, `IT`, `A`, `SO` ist ein englisches Wort, kein Ticker:
  `Exit long ON @ 50` wird verworfen (früher BUY ON), `Exit long $ON @ 50` ist
  ein Exit auf ON. Kleingeschriebene Prosa (`exit on close below 95`) in einem
  Entry bleibt erlaubt.
- **Teil-Exits** (`Trim half`, `Took partials 1/3`, `Sell 25%`, `Scale out 50%`)
  schließen genau diesen Anteil. Ohne quantifizierbaren Anteil (`Took partials`,
  `Trim AAPL`), mit zwei verschiedenen Anteilen oder einem Anteil außerhalb
  (0, 1] → verworfen, nie geraten. Ein Voll-Exit mit Größenangabe ist mehrdeutig
  und wird verworfen.
- **Sizing im Daemon** (`live_runner/exit_signals.py`): geschlossen wird nur die
  *eigene* Menge des Deployments, d. h. die Netto-Fills seiner Signale für das
  Symbol (Ledger), gedeckelt auf die Broker-Position, mal Anteil; ganzzahlige
  Positionen werden abgerundet, ein Teil-Exit, der auf 0 fällt, wird abgelehnt.
  Keine Position → ignoriert, `INFO`; keine eigene Menge, aber eine
  Broker-Position (gehört jemand anderem oder das Ledger hat einen Fill
  verpasst) → ignoriert, `WARNING` mit `broker_qty` (`Exit signal ignored`).
  Eigene Seite ≠ Broker-Seite oder ≠ in der Nachricht genannte Seite →
  abgelehnt.
- **Exit-Mengen werden nie aufgerundet:** Jeder Exit (Teil- und Voll-Exit) wird
  auf den Mengenschritt des Instruments **ab**gerundet (`exit_qty_step` des
  Adapters: cTrader `stepVolume` aus den Symbol-Properties, unbekannt → eine
  Broker-Volumeneinheit; Alpaca 1e-9), im Daemon vor dem Persistieren und im
  Adapter noch einmal (cTrader-Volumen per Floor statt `round()`). Ein Exit
  schließt so nie mehr als den verlangten Anteil bzw. die eigene Menge. Fällt
  er auf 0, wird er ignoriert und auf `WARNING` geloggt, mit `requested_qty`
  und `qty_step`. Entries werden ebenfalls nie aufgerundet: der cTrader-Adapter
  rundet das Entry-Volumen per Floor auf den Broker-`stepVolume` ab
  (`_floor_to_volume_step`); liegt das Ergebnis unter `minVolume`, wird die
  Order wie bisher abgelehnt, nie auf das Minimum hochgesetzt.
- **Ledger-Überhang: Deckelung am Broker + Alert (#121, Entscheidung 2):** Ein
  Exit schließt nur die eigene Ledger-Menge des Deployments, höchstens aber die
  Broker-Position: `Menge = min(eigene Ledger-Menge, Broker-Menge)`, dann der
  Anteil (Teil-Exit) und das Abrunden auf den Mengenschritt. Weichen die Ledger
  aller Deployments auf demselben Account und Symbol (jeder Status,
  `get_account_net_filled_qty_by_deployment`) vorzeichenrichtig summiert von der
  Broker-Position ab (Toleranz: `position_qty_tolerance` des Adapters, cTrader
  eine halbe Broker-Volumeneinheit, Alpaca 0,5e-9; unbekannt oder größer als
  eine ganze Einheit → strikt 1e-9 + `WARNING`),
  oder übersteigt die eigene Menge die Broker-Position, wird der gedeckelte Exit
  **trotzdem abgeschickt** und ein Alert ausgelöst: `ERROR`-Log mit
  `alert=exit_ledger_mismatch` (Sentry macht daraus ein Event), allen
  Ledger-Mengen (`ledger_qty_by_deployment`, `ledger_qty_total`), `broker_qty`
  und `own_ledger_qty`. Grund der Abweichung sind meist Broker-seitige
  Schließungen (SL/TP-Legs, cTrader-SL/TP, Flatten, manuelles Schließen), die
  nicht im Ledger landen. **Restrisiko:** Auf einem genetteten Account senkt eine
  Broker-seitige Schließung für ein anderes Deployment die Broker-Position; die
  Deckelung schließt dann einen Teil von dessen Anteil mit. Der Alert nennt das,
  das Ledger muss danach abgeglichen werden.
- **Broker-seitige Schließungen im Ledger (#144):** Ein Alpaca-Bracket-Leg
  (Stop- oder Target-Leg) schließt die Position des Parent-Signals, ohne dass
  diese Plattform je eine Order dafür abgeschickt hat. Seit #144 wird so ein Fill
  **gegen das Ledger des Parent-Deployments gebucht**:
  `record_live_broker_side_close_fill` legt ein Signal derselben Deployment mit
  der *reduzierenden* Seite an (Sell für ein Buy-Entry; `source=broker_close`,
  deterministische Id aus der Parent-Signal-Id, Metadaten `broker_side_close`,
  `broker_close_reason`, `parent_signal_id`) und bucht den Fill im selben
  gesperrten Insert-Pfad wie jeden anderen Fill, mit dem Broker-eigenen
  Dedupe-Key (Leg-Order-Id + kumulierte Leg-Menge). Das Ledger des Deployments
  sinkt damit um genau das, was der Broker geschlossen hat, und
  `get_account_net_filled_qty_by_deployment` stimmt wieder mit der
  Broker-Position überein - kein `exit_ledger_mismatch`-Alert mehr und der Exit
  sized gegen den eigenen Anteil statt gegen die Deckelung.
  Die Zuordnung Leg-Order-Id → Parent-Signal wird beim Submit und in jedem
  Reconciliation-Pass aus den verschachtelten Legs der Parent-Order gelernt (der
  Pass läuft beim Connect, bevor der Trading-Stream Events verarbeitet); ein
  Fill, dessen Order-Id keine bekannte Leg ist, bleibt unverändert auf dem
  bisherigen Pfad. Eine Leg auf der *aufbauenden* Seite, ein unbekanntes
  Parent-Signal, ein Fill ohne Zeitstempel oder ohne nutzbare Menge wird
  abgelehnt (geloggt, nichts geschrieben) - nie erfunden.
  **cTrader (#144):** Eine broker-seitige Positionsschließung (SL/TP, Flatten,
  manuell - cTrader unterscheidet die nicht) wird über die Kette **Deal →
  `positionId` → `OrderListByPositionId` → `clientOrderId`/Order-Id des
  eröffnenden Orders → Signal** derselben Parent-Deployment zugeordnet und über
  denselben `record_live_broker_side_close_fill`-Pfad gebucht
  (`broker_close_reason=ctrader_position_close`, Dedupe-Key
  `ctrader:deal:{dealId}` - derselbe Schlüssel, den der normale Fill-Pfad für
  dasselbe Deal ableitet, damit ein Deal, der beide Pfade erreicht, nur einmal
  gebucht wird, gedeckelt am kumulierten Volumen des
  schließenden Orders). Die Zuordnung zählt nur, wenn **genau ein** Signal der
  Position zugeordnet ist: null (manuelle Position, nie eingereichte Orders)
  oder mehrere (genettetes Buch mehrerer Deployments) werden **nicht geraten**,
  sondern das Account+Symbol wird als `ledger_reconciliation_required` im
  Control-Audit geflaggt und ein `ERROR`-Alert (`alert=
  ledger_reconciliation_required`) ausgelöst - die periodische
  Broker-Reconciliation (#330) bzw. der nächste Exit-Mismatch-Alert handelt
  danach. Derselbe Flag wird bei `exit_ledger_mismatch` geschrieben. Der
  Reconciliation-Pass (`reconcile_executions`) holt zusätzlich die
  Account-Deal-Liste der letzten 7 Tage nach, um vom Stream verpasste
  Schließungen nachzubuchen; Deals eigener Orders werden übersprungen. Ein
  Manuell-/Flatten-Close einer Position, die genau ein Signal eröffnete, ist
  damit **exakt** zugeordnet (kein Raten); was die Kette nicht eindeutig
  auflösen kann, bleibt geflaggt.
  Unverändert: Seiten-Konflikt (eigene Seite ≠ Broker-Seite oder ≠ Nachricht)
  → abgelehnt; keine eigene Menge → ignoriert + `WARNING`; auf 0 gedeckelt bzw.
  abgerundet → ignoriert + `WARNING`.
- **Bekannte Einschränkung: Exits auf `PAUSED`-Deployments (#126).** Ein Exit
  wird wie jede Order nur für `ACTIVE`-Deployments zugestellt (Fan-out und
  Re-Check vor dem Absenden). Auf einem `PAUSED`-Deployment wird „Exit AAPL"
  verworfen (INFO-Log), die Position bleibt offen. Das Gate ist in #121
  unverändert; Exits trotz `PAUSED` anzunehmen ist offen (#126).
- **Ein Exit nach dem anderen (pro Account und Symbol):** Ein früherer Exit
  **desselben Deployments** für dasselbe Symbol, der beim Broker noch arbeitet
  (nicht voll gefüllt und in den offenen Orders des Brokers, per Broker- oder
  Client-Order-ID), blockiert einen neuen Exit: abgelehnt, `ERROR`, nicht
  gequeued (sonst würde gegen ein Ledger ohne dessen Fills gesized). Ein noch
  arbeitender Exit **eines anderen Deployments** desselben Accounts lehnt
  dagegen **nicht** ab (eine Nachricht geht an alle Deployments; bei
  geschlossenem Markt arbeitet der erste Exit immer noch, wenn der zweite dran
  ist): seine noch ungefüllte Menge (`requested_qty − filled_qty`, gleiche
  Schließ-Seite) wird reserviert, der Exit wird gegen
  `|Broker-Position| − Σ Reserviert` (mindestens 0) gedeckelt, also
  `min(eigene Menge, Broker − reserviert)`. Liegt die Obergrenze unter der
  eigenen Menge, geht der gedeckelte Exit raus und der Ledger-Alert (`ERROR`)
  feuert. Bleibt nichts übrig, wird nichts gesendet und mit `ERROR` geloggt
  (die Position bleibt offen), mit den blockierenden Orders im Log. Jede
  Ablehnung eines Exits, der eine Position offen lässt, loggt auf `ERROR`.
  Innerhalb des Prozesses laufen Exits derselben Account/Symbol-Kombination
  zudem strikt nacheinander (Lock von der Größenbestimmung bis zur
  gespeicherten Order); die Broker-Position wird erst danach gelesen. Kann der
  Adapter seine offenen Orders nicht auflisten, gilt das als „nicht bewiesen
  beendet“ → ebenfalls abgelehnt (`ERROR`). Alpaca liest die offenen Orders mit
  `nested=True` und `limit=500`; eine volle Seite gilt als unvollständig und
  schlägt fehl.
- **Jeder Exit schließt nur die eigene Menge:** Ein einfaches `SELL …` oder ein
  Momentum-Reversal (RocketFuel long-only) auf einem Deployment ohne Shorts ist
  ein Voll-Exit und wird nach derselben Regel gesized (Anteil 1, Seite aus der
  eigenen Position, ein eigener Short wird zurückgekauft). Der Anteil eines
  anderen Deployments an derselben genetteten Broker-Position bleibt offen; ohne
  eigene Menge → ignoriert und geloggt. Auf einem Long+Short-Deployment bleibt
  ein einfaches `SELL` ein Short-Entry.
- **Abgelehnte Exits loggen auf `ERROR`:** Ein Exit, der vor dem Senden
  abgelehnt wird (z. B. eigene Seite ≠ Broker-Seite), vom Adapter vor dem
  Broker-Request abgelehnt wird (z. B. Mindestvolumen) oder dessen Broker-Antwort
  ein Fehler ist, lässt die Position offen und wird auf `ERROR` geloggt
  (`Exit signal refused …` bzw. `… unknown outcome`, mit `"exit": true`). Ein
  abgelehnter Entry bleibt `WARNING`; „nichts zu schließen“ ist `INFO` bzw.
  `WARNING` (siehe oben), ein noch laufender früherer Exit desselben Deployments (oder nichts übrig
  neben anderen laufenden Exits) `ERROR`; ein als veraltet verworfener Exit `WARNING`.
- Fallback-Ticker-Extraktion (erstes GROSSBUCHSTABEN-Wort) ist entfernt; ein Signal
  ohne strukturiertes Tickersymbol (`**$TICKER**`, `Ticker:`, `buy signal AAPL`,
  `$TICKER`, `TICKER $price`) wird verworfen.
- Nachraten über `POLL_LIMIT` (20) hinaus werden nicht mehr still verworfen: der
  Poll paginiert **vorwärts** ab dem Cursor (`after=<letzte ID>`), jede Seite wird
  nach Snowflake-ID chronologisch emittiert, und der Cursor rückt nur bis zur
  neuesten tatsächlich emittierten Nachricht vor. Pro Poll werden höchstens
  `MAX_BACKFILL_PAGES` (5) Seiten geholt; eine größere Lücke wird in den nächsten
  Poll übernommen (im laufenden Betrieb geht dadurch nichts verloren) und als Warnung mit Kanal und den
  Grenz-IDs (`last_seen_message_id`, `newest_fetched_message_id`, `pages_fetched`)
  geloggt. IDs unterhalb des Cursors werden nie erneut emittiert; ein nicht
  numerischer Cursor wird verworfen und neu aufgesetzt (Warnung). Eine Seite ganz ohne
  numerische ID kann keinen Folge-Request verankern: der Poll stoppt mit Warnung,
  der Cursor bleibt stehen, und kommt dauerhaft nur so eine Seite, nimmt der Kanal
  bis zu einem Neustart nichts auf (danach greift das Start-Verhalten unten). Snowflake-IDs
  sind nicht fortlaufend, daher wird keine Nachrichtenanzahl berechnet.
- **Ein einzelnes kaputtes Message-Objekt verliert nicht die ganze Seite:** Die
  geparste Nachricht wird pro Nachricht isoliert (`_emit_polled_message`). Wirft
  das Parsen (z. B. ein strukturell kaputtes `author`-Feld oder ein von
  `TradeSignal` abgelehnter Preis), wird die Nachricht auf `ERROR` geloggt
  (`Discord message dropped: emission failed` mit `channel_id`, `message_id` und
  `error_type`), ihre ID bleibt im Seen-Set (kein Endlos-Retry) und der Poll
  arbeitet mit dem Rest der Seite weiter. `asyncio.CancelledError` wird nicht
  verschluckt; Auth-/Channel-Forbidden-/Rate-Limit-Fehler entstehen beim Abruf
  und behalten ihr bestehendes Verhalten.
  Grundlage ist die Semantik von `after`: Discord liefert mit `after=<id>&limit=N`
  die N Nachrichten direkt nach der ID, im Array neueste zuerst
  ([Get Channel Messages](https://docs.discord.com/developers/resources/message#get-channel-messages):
  "Returns an array of message objects from newest to oldest"). Dass es die
  ältesten N nach der ID sind, steht dort nicht wörtlich; discord.py verlässt sich
  aber genauso darauf: in der gepinnten Version `discord-py==2.7.1`
  (`discord/abc.py`, `Messageable.history`, `_after_strategy`) setzt die
  Vorwärts-Paginierung `after` auf `data[0]` (die neueste Nachricht der Seite) und
  dreht die Seite für die chronologische Ausgabe um; das funktioniert nur, wenn
  `after` die direkt folgenden Nachrichten liefert.
  Der Cursor liegt nur im Speicher: beim Start (`_prime_cursors`) wird er auf die
  neueste Seite gesetzt, ohne sie zu emittieren. Nachrichten aus einer Downtime,
  die älter als diese Seite sind, werden daher nicht nachgeholt und erreichen auch
  die Altersprüfung (`max_signal_age_seconds`) nie. Bei langen Timeframes
  (z. B. `1d`, Budget 2 Tage) kann so ein während eines Neustarts geposteter Exit
  verloren gehen; ein persistierter Cursor ist als Follow-up offen.
- Ein Discord-Signal wird auf jedes passende Deployment verteilt (Fan-out), nicht
  nur auf das erste.
- Transiente Reconnect-Fehler schreiben einen Bot-Service nicht dauerhaft auf
  `FAILED`; der Reconnect wird nach einem Backoff-Floor (30 s) erneut versucht.

### Credential-Typ: Bot- vs. Account-Token

Discord akzeptiert zwei **nicht** austauschbare Credentials: ein
Developer-Portal-Bot-Token wird als `Bot <token>` gesendet, ein
Account-(User-)Token unverändert. Eine falsche Kombination antwortet mit
**HTTP 401**, deshalb wird das Schema nie geraten:

- `DISCORD_BOT_<NAME>_AUTH_SCHEME`, sonst global `DISCORD_AUTH_SCHEME`
- Werte: `bot`, `raw` bzw. `account` (beide bezeichnen ein Account-Token)
- Vorgabe: `raw` (bestehendes Operator-Setup)
- Ein unbekannter Wert wird abgelehnt, nicht auf einen Default abgebildet.
  Ein pro Deployment gesetztes `signal_config.discord_auth_scheme` gewinnt.
  Im Account-Modus loggt der Daemon den Self-Bot-/ToS-Hinweis.

### Signal-Alter und Candle-Close-Wait

- `signal_config.max_signal_age_seconds` begrenzt das Alter eines Signals;
  ohne expliziten Wert gilt `max(300, 2 × Timeframe-Dauer)`. Die Prüfung läuft
  **vor** dem Candle-Close-Wait. Ein Discord-Signal ohne lesbares `received_at`
  wird fail-closed abgewiesen (es wird keine Empfangszeit erfunden).
- Ein unbekanntes Timeframe-Label lässt `wait_for_candle_close` scheitern,
  statt den Wait stillschweigend zu überspringen; der Daemon verwirft dann
  dieses Signal und führt den Listener weiter.


### Raw-Signal-Archiv (#308)

Jede Nachricht, die `DiscordSignalSource` zur Signalverarbeitung emittiert,
wird append-only archiviert — ob sie nun in einen `TradeSignal` geparst
wird oder vom Parser abgelehnt wurde. Auch Nachrichten von Autoren außerhalb
der Trusted-Author-Allowlist werden archiviert (für die
Impostor-Auditierbarkeit) und tragen `author_trusted: false`; Inhalte werden
nicht redigiert. Die Startup-Cursor-Priming-Seite wird nur als gesehen
gemarkiert und nicht archiviert — auch Nachrichten, die während eines
Daemon-Stops eintrafen, können also fehlen. Ohne dieses Archiv existieren
die Rohsignale nur im Log-Stream und nur ab dem Tag der Aufzeichnung —
Backtests und Meta-Labeling der Bots (nitro, ramjet, rocketfuel) brauchen
die historische Rohdatenmenge.

**Speicherort und Format.** JSON Lines unter
`DATA_DIR / "signal_log" / "discord_signals-<YYYY-MM-DD>.jsonl"` (eine Datei
pro UTC-Tag). JSONL statt SQLite `backtests.db` aus drei Gründen: Das Archiv
darf nie mit der Handelsdatenbank um Locks konkurrieren, es muss für die
offline Label-Pipeline trivial exportierbar bleiben, und „append-only" ist
wörtlich — eine Zeile wird nie umgeschrieben. File-basierte Stores liegen im
Repo bereits neben der DB (`DATA_DIR / "backtests"`, `"grid_results"`,
`"signals"`), das Format passt also in das bestehende Persistenz-Layout.
Implementierung: `live_runner/signal_archive.py`.

Jeder Record trägt `schema_version` (aktuell `1`). Die Offline-Pipeline muss
Zeilen, die nicht parsen (z. B. eine durch einen Absturz abgeschnittene
Zeile), überspringen und auf `discord_message_id` deduplizieren — ein
Daemon-Neustart kann zuletzt emittierte Nachrichten erneut archivieren.

**Record-Typen.**

- `discord_signal_received` — einmal pro zur Signalverarbeitung emittierter
  Nachricht: Roh-Content,
  Autor (ID, Username, Bot-Flag, `author_trusted`), Channel, Message-ID,
  `message_created_at`, Archivierzeit (`logged_at`), Bot-Name, Account,
  Service, `parse_status` (`parsed`/`rejected`),
  `refusal_reason`/`refusal_detail`
  (z. B. `untrusted_author`, `non_entry_rejection`, `malformed_levels`),
  die geparsten Felder (`signal`: action, symbol, entry, SL, TP, timeframe,
  signal_type) und receipt-time `features` (Clock-Features: Stunde/Wochentag
  UTC, Content-Länge, explizites SL/TP, Exit-Flag).
- `discord_signal_decision` — einmal pro (Signal, Deployment): Outcome
  `traded` / `refused` / `ignored` / `unknown`, `reason` (z. B.
  `deployment_not_active`, `symbol_not_covered`, `author_not_allowed`,
  `unmapped_channel`, `stale_signal`, `signal_gate_rejected`,
  `invalid_deployment_config`, `submission_claim_failed`,
  `broker_refused_pre_request`, `broker_submit_outcome_unknown`,
  `nothing_to_exit`, `exit_below_step`, `exit_in_flight`,
  `unhandled_error`), `detail`,
  `broker_order_id`, `signal_age_seconds`, Deployment-/Strategy-IDs und
  `features`.

**Signal-Pfad bleibt frei.** `record()` ist ein bounded `put_nowait` auf
 einer Queue; Serialisierung und Disk-I/O laufen auf einem dedizierten
Writer-Thread (`signal-archive-writer`). Eine volle Queue droppt den Record
und zählt ihn (`dropped`), Schreibfehler zählen `write_errors`/`enqueue_errors`
 und warnen — nichts davon blockiert, verzögert oder wirft in den
Signalpfad. Abschalten mit `TP_SIGNAL_ARCHIVE=0`.

**Market-Features — offline, nicht auf dem Signal-Pfad.** Produktiv
tragen Discord-Signale heute keine OHLCV-Frame (`signal.metadata["market_data"]`
wird von keinem Producer gesetzt); die Decision-Records erhalten deshalb
keine Market-Features. Die Feature-Berechnung
(`market_features_from_frame`) läuft netzwerkfrei über eine bereits
vorhandene OHLCV-Frame — ATR(14) samt Perzentil-Rang im Frame, Abstände zu
SMA20/SMA50 und VWAP (nur wenn `volume` vorhanden), Gap der letzten Bar,
grobe Trend-Regime (`above_sma50`/`below_sma50`) — und ist damit auch für
die spätere Offline-Berechnung aus historischen OHLCV-Daten per
`message_created_at` nutzbar (außer Spread, der Quotes bräuchte). Zusätzlich
schreibt der Decision-Record Prozessor-Metadaten, wenn ein Processor läuft
(Processor-ATR, Supertrend-Entry-State, Slippage, Trailing-Mode). Die
Clock-/Parse-Features (Stunde/Wochentag UTC, Content-Länge, explizites
SL/TP, Exit-Flag) stehen im `discord_signal_received`-Record. Bewusst
**nicht** drin, weil sie extra Daten bräuchten (Follow-ups): Bid/Ask-Spread
(braucht Quotes), session-ankererter VWAP, Multi-Window-Perzentile.

**Nächste Schritte (Issue #308, separat).** Jeder Signal bekommt später ein
Triple-Barrier-Label mit den SL/TP des Bots; daraus wird ein
regularisierter Filter trainiert und walk-forward validiert.
