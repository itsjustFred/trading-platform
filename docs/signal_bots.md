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

