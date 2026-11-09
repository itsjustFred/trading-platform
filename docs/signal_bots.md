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
- **Features:** ATR + Supertrend BUY-only Setup mit TP1 und trailing metadata
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
- **Ledger-Abgleich vor jedem Exit:** Die Ledger aller Deployments auf demselben
  Account und Symbol (jeder Status, `get_account_net_filled_qty_by_deployment`)
  müssen vorzeichenrichtig summiert der Broker-Position entsprechen, innerhalb
  der Mengenpräzision des Instruments (`position_qty_tolerance` des Adapters:
  cTrader eine halbe Broker-Volumeneinheit, Alpaca 0,5e-9; unbekannt → strikt
  1e-9). Sonst wird der Exit abgelehnt und auf `ERROR` geloggt, mit allen
  Ledger-Mengen (`ledger_qty_by_deployment`, `ledger_qty_total`) und
  `broker_qty`. Grund: Broker-seitige Schließungen (SL/TP-Legs, cTrader-SL/TP,
  Flatten, manuelles Schließen) landen nicht im Ledger; ohne Abgleich könnte ein
  Exit auf einem genetteten Account die Anteile eines anderen Deployments
  schließen. Fills von Bracket-Legs gegen das Parent-Signal zu buchen ist die
  eigentliche Lösung und ein eigenes Issue; bis dahin muss nach so einer
  Schließung das Ledger abgeglichen werden, bevor Exits wieder laufen.
- **Ein Exit nach dem anderen:** Solange ein früherer Exit desselben
  Deployments und Symbols beim Broker noch arbeitet (nicht voll gefüllt und in
  den offenen Orders des Brokers, per Broker- oder Client-Order-ID), wird ein
  neuer Exit abgelehnt (`WARNING`, nicht gequeued). Kann der Adapter seine
  offenen Orders nicht auflisten, gilt das als „nicht bewiesen beendet“ → ebenfalls
  abgelehnt.
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
  `WARNING` (siehe oben), ein noch laufender früherer Exit `WARNING`.
- Fallback-Ticker-Extraktion (erstes GROSSBUCHSTABEN-Wort) ist entfernt; ein Signal
  ohne strukturiertes Tickersymbol (`**$TICKER**`, `Ticker:`, `buy signal AAPL`,
  `$TICKER`, `TICKER $price`) wird verworfen.
- Nachraten über `POLL_LIMIT` (20) hinaus werden nicht mehr still verworfen: der
  Seen-Set ist kumulativ, Lücken werden als Warnung mit Kanal und den Grenz-IDs
  (`last_seen_message_id`, `oldest_fetched_message_id`) geloggt. Snowflake-IDs
  sind nicht fortlaufend, daher wird keine Nachrichtenanzahl berechnet.
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

