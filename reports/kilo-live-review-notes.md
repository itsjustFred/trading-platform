# Live-Trading Codepath Review - cTrader Token, Alpaca/Discord Routing
# Datum: 2026-06-17
# Keine Secrets, fokus auf ausführbare Risiken

## cTrader Token-Refresh Analyse

### Codepfad
- `live_runner/ctrader_adapter.py::connect()` (L535-656)
- `live_runner/ctrader_adapter.py::_authenticate()` (L435-485)
- `live_runner/daemon.py::_resolve_ctrader_credentials()` (L214-225)
- `live_runner/daemon.py::sync_deployments_loop()` (L1535-1627)

### Risiken

#### R1 [FEHLENDES FEATURE] - Kein automatischer Token-Refresh
- `CTraderLiveAdapter` hat keine Refresh-Logik implementiert
- Token-Ablauf (24h typical bei cTrader) führt zu `CTraderAuthTerminalError`
- Nach Terminal-Auth-Fehler: Account wird geblockt (L749-750 in daemon.py), aber
  kein automatischer Wiederholungsversuch mit frischem Token
- `_resolve_ctrader_credentials()` liest nur ENV, kein Refresh-Mechanismus

#### R2 [STATE LOSS] - `_authenticated` Flag wird nie zurückgesetzt bei 401
- `_on_disconnected()` setzt `_authenticated = False` (L332), aber nur bei
  TCP-Disconnect, nicht bei HTTP 401/Token-Invalid-Responses
- `_authenticate()` fängt 401 ab und wirft `CTraderAuthTerminalError`, blockiert
  danach `account_id` in `_CTRADER_AUTH_BLOCKED_ACCOUNTS`
- Kein Backoff-Vektor für "token expired, please re-auth" - dauerhafter Block

#### R3 [RACE CONDITION] - Discord-Reconnect ohne Balance Refresh
- In `sync_deployments_loop()` (L1549-1559) werden blocked accounts gecleart wenn
  `.accounts.env` mtime ändert
- Aber: `_start_missing_tasks()` (L1346-1388) prüft nur `_adapter_is_ready()`
- Falls Adapter connect() nach Token-Rotation crasht, wird kein neuer Task gestartet
- Fill-Loop (`listen_executions`) läuft weiter mit veralteten Credentials

#### R4 [TESTFÄHIGE LÜCKE] - Keine 401-Simulation im Test
- Kein Mock für `CTraderApiError` mit "INVALID_TOKEN" ErrorCode
- Kein Test für `_is_terminal_auth_error()` Rückgabe bei verschiedenen ErrorCodes
- `_raise_terminal_auth_from_api_error()` (L308-315) nicht isoliert testbar

### Testideen

```python
# Test 1: Simuliere abgelaufenen Token
def test_ctrader_expired_token_401():
    """Simuliere cTrader API Error 401 INVALID_TOKEN."""
    adapter = CTraderLiveAdapter(...)
    # Mock _send_request zu werfen CTraderApiError("INVALID_TOKEN", "...")
    # Erwartet: CTraderAuthTerminalError wird geworfen
    # Erwartet: Account wird in _CTRADER_AUTH_BLOCKED_ACCOUNTS

# Test 2: MTIME-Trigger löst credential reload
def test_accounts_env_mtime_clears_blocklist():
    """ credentials ändern löst .accounts.env reload + blocklist clear."""
    # Patch Path.stat().st_mtime_ns
    # Erwartet: Nach mtime-change ist account nicht mehr geblockt

# Test 3: Token-Refresh-Sequenz (ohne Secrets)
def test_token_refresh_sequence():
    """Sequenz: disconnect -> reconnect mit neuem Token -> connect()."""
    # Simuliere: access_token in ENV durch Refresh-Token ersetzen
    # Prüfe: _authenticated Flag nach reconnect
```

---

## Alpaca/Discord Routing Analyse

### Codepfad
- `live_runner/daemon.py::_run_discord_signal_listener_for_account()` (L545-684)
- `live_runner/alpaca_adapter.py::submit_order()` (L281-331)
- `live_runner/daemon.py::submit_order_with_risk()` (L452-517)

### Risiken

#### A1 [ROUTING BUG] - Erster Treffer gewinnt, andere Deployments verlieren Signale
- In `_run_discord_signal_listener_for_account()` (L604-671) gibt es `break` nach
  erfolgreichem `submit_order_with_risk()`
- Mehrere ACTIVE Deployments auf gleichem Account + Symbol bekommen das Signal nur
  als "first match wins"
- Kein Round-Robin, keine Lastverteilung, keine Intent-Basierten Zuteilungen
- Dokumentiert nicht als beabsichtigt (siehe M4 im CODE-REVIEW-REPORT)

#### A2 [EXPOSURE BYPASS] - Discord-Listener überspringt Exposure-Checks
- Aufruf in L463: `submit_order_with_risk(adapter, deployment, live_signal)`
- `submit_order_with_risk` hat `current_exposure=0.0` Default (L452)
- `adapter.get_positions()` wird nur für `exit_only` Signale genutzt (L466-475)
- Alpaca-Positions-Fetch ignoriert für Normal-Buy-Signals
- `max_total_exposure` und `max_positions` in RiskConfig werden umgangen

#### A3 [SYNCHRONISIERUNGSLÜCKE] - Signal-Timeframe vs. Position-Fetch
- `_build_processor_market_data()` (L338-357) verlangt market_data oder wirft Fehler
- Aber: `get_positions()` (alpaca L341-357) nutzt ALPACA API, kein timeframe-Param
- Kein Mechanismus, um Positionsgröße an aktuelle Preise anzupassen
- `entry_price` im Signal ist vom ursprünglichen Signal, kein Re-Quote vor Order

#### A4 [DEDUP LÜCKE] - Discord-Bot Token-Reuse ohne Dedup
- `_SYNC_DISCORD_BOT_SERVICES` (L965-1092) erzeugt neuen `DiscordSignalSource` bei
  Token/Channel-Änderung (L1026-1041)
- Aber: Keine Idempotenz-Prüfung für identische Nachrichten
- Wenn Bot reconnectet, kann er alte Messages nochmal sehen und duplizieren
- Kein `discord_message_id` Uniqueness-Check in DB (siehe C3)

### Testideen

```python
# Test 4: Erster-Match-Routing
def test_discord_signal_routing_first_match_only():
    """Signal wird nur an erste passende Deployment gesendet."""
    channel_id = "12345"
    deployment_a = LiveDeploymentConfig(symbols=["AAPL"], ...)
    deployment_b = LiveDeploymentConfig(symbols=["AAPL"], ...)
    # Beide auf gleichem account + channel
    # Sende AAPL-Signal
    # Erwartet: Nur eine order submission
    # Erwartet: ETWAS log warning über skipped signal

# Test 5: Exposure-Bypass
def test_discord_listener_ignores_existing_positions():
    """Positions-Check wird für normalen Buy ignoriert."""
    # Mock adapter.get_positions() return {"qty": 100, "symbol": "AAPL"}
    # Submit AAPL Buy
    # Erwartet: Positioncount wird NICHT als exposure eingesetzt
    # Erwartet: Log eintrag "current_exposure not provided"

# Test 6: Message-Deduplication
def test_discord_reconnect_does_not_duplicate_signals():
    """Reconnect nach disconnect sendet keine doppelten Signale."""
    # Patch DiscordSignalSource reconnect
    # Erwartet: discord_message_id uniqueness in save_live_signal call count

# Test 7: Symbol Mapping Leak
def test_unknown_symbol_reauth_keeps_symbol_cache():
    """Nach Re-Auth ist symbol cache leer, symbol_id None."""
    # In ctrader_adapter.py L811-815: Bei unknown symbol Auth retry
    # Nach connect() ist _symbol_name_to_id evtl. leer
    # Test: Symbol-Requests zwischen connect() und submit_order()
```

---

## Priorität für Fixes (Empfehlung)

1. **Token-Refresh**: Implement Refresh-Token Flow (cTrader OAuth2) statt ENV-Polling
2. **Exposure-Bindung**: `get_positions()` Ergebnis in `submit_order_with_risk()` wiren
3. **Routing-Dokumentation**: Eindeutige Policy für Multi-Deployment-Signale
4. **Deduplication**: `discord_message_id` Unique Constraint in DB + Pre-Check
5. **Testabdeckung**: Die 7 oben genannten Tests als erstes Coverage-Target