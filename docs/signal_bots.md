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
     Komma **oder Whitespace** — z. B. `DISCORD_BOT_NITRO_TRUSTED_AUTHOR_IDS`
  3. Umgebungsvariable `DISCORD_TRUSTED_AUTHOR_IDS`, getrennt durch Komma oder
     Whitespace (Fallback für Single-Bot-Setups)

  Die Bot-spezifische Variante ist wichtig, wenn mehrere Bots dieselbe
  `.accounts.env` teilen: ohne sie fällt jeder Bot auf dieselbe gemeinsame Liste
  zurück und vertraut damit auch den *anderen* Bots. Ein Bot-Account mit
  Schreibzugriff auf den Kanal eines anderen Bots könnte dort posten und dessen
  Deployment handeln. Vorherrschaft in dieser Reihenfolge: die spezifischste
  gewinnt, ein gesetzter Wert fällt nicht auf die allgemeinere zurück.

Ohne Allowlist startet der Listener nicht (fail-closed). Zusätzlich gilt:

**Leer ist nicht „nicht gesetzt".** Für die Broker-Credentials
(`CTRADER_DEMO`, `ALPACA_PAPER`, `CTRADER_APP_ID` …) gilt dieselbe Regel wie oben:
Ein in `.accounts.env` vorhandener, aber leerer Wert ist ein absichtliches Leeren und
wird **nicht** als „nicht gesetzt" behandelt. `resolve_env_value` liefert für beide
Fälle `""`; die Anwesenheit wird deshalb getrennt geprüft, und der Wert geht roh in
den strikten Parser. Ein leeres `CTRADER_DEMO` oder `ALPACA_PAPER` führt damit zu
einem Abbruch mit Nennung der Variablen, nicht zu einem stillen Default.

Der Grund ist die Richtung, in die ein Fehler hier zeigt: der Default ist `true`, also
Demo/Paper. Ein leerer Wert wird also nicht versehentlich zu *Live* — aber er wird
stillschweigend zu einer Annahme, und dieselbe Codezeile mit `default=False` wäre
Live-Trading. Der Parse-Fehler ist die Form, in der die Absicht sichtbar bleibt.

- Preisparsing akzeptiert strenge Tausendergruppen (`$1,234.56` → 1234.56) und
  verwirft mehrdeutige Tokens (`$12,34`) statt sie als Dezimalzahl zu interpretieren.
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

## Live-Signal-Härtung

- `max_signal_age_seconds` ist ein optionales `signal_config`-Limit. Ohne
  expliziten Wert gilt automatisch `max(300, 2 × Timeframe-Dauer)`, damit ein
  regulärer Candle-Close-Wait auf `15m`, `1h` oder `1d` kein gültiges Signal
  grundsätzlich verwirft. Die Prüfung läuft **vor** dem Wait; ein Discord-Signal
  ohne parsebares `received_at` wird fail-closed abgewiesen.
- `signal_processor_market_data_bars` steuert die Anzahl echter Broker-Bars
  (20–1000, Standard 200). Der Alpaca-Adapter lädt den Bar-Schnitt aus dem
  aktuellen Zeitfenster, nimmt die neuesten N Bars und verwirft Daten, deren
  jüngster Bar älter als 72 Stunden (bzw. 7 Tage bei `1d`) ist.
- Nach dem Candle-Close-Wait muss der Processor dieselbe Timeframe beibehalten,
  die auch für das Wait verwendet wurde; ein nachträglicher Timeframe-Wechsel
  wird verworfen.
- Vor jeder Submission werden Deployment-Status, Demo-Modus und die
  PAUSE-Concurrency nochmals geprüft. Exit-only-Metadaten sind nur für SELL
  erlaubt und werden bei Entries entfernt.

## Credential-Typ: Bot-Token vs. Account-Token

Discord akzeptiert zwei **nicht** austauschbare Credentials:

| Typ | Authorization-Header | Bezug |
| --- | --- | --- |
| Bot-/Application-Token | `Bot <token>` | Discord Developer Portal |
| Account-(User)-Token | `<token>` (ohne Präfix) | Browser-Session |

Ein Account-Token mit `Bot `-Präfix wird mit **HTTP 401** abgelehnt. Der
Quelltyp muss deshalb explizit konfiguriert werden und wird **nicht geraten**:

- `DISCORD_BOT_<NAME>_AUTH_SCHEME=bot|account` (je Bot)
- sonst global `DISCORD_AUTH_SCHEME=bot|account`
- Vorgabe: `bot`

Ein Token, das bereits ein Schema-Präfix trägt, wird nicht doppelt präfigiert.
Ein unbekanntes Schema wird abgelehnt. Im `account`-Modus loggt der Daemon
ausdrücklich, dass ein Self-Bot betrieben wird: das verletzt die Discord-ToS
und kann zur Sperrung des Kontos führen. Für unbeaufsichtigten Handel ist ein
Bot-Token die dauerhafte Lösung.
