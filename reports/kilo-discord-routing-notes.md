# Discord-Signal-Routing-Review für Nitro/RamJet/RocketFuel zu Alpaca-Accounts
# Datum: 2026-06-17
# Keine Secrets, Fokus auf Routing-Logik und Exposure-Behandlung

## 1. Signal-Processor-Implementierung (Nitro/RamJet/RocketFuel)

### 1.1 NitroSignalProcessor (signal_bots/nitro/processor.py)
- **Erbt von RamJetSignalProcessor** und schränkt Action auf BUY/LONG ein (zeile 17)
- Ruft `super().process_signal()` auf, fügt dann Metadaten hinzu
- Setzt `source="nitro"` und `derived_from="ramjet"` im Metadaten (zeile 26-28)
- **Kein eigenständiger ATR/SL/TP Algorithmus** - leitet alles vom RamJet ab

### 1.2 RamJetSignalProcessor (signal_bots/ramjet/processor.py)
- Berechnet ATR via EMA(TR, span=atr_period) (zeile 93)
- Stop-Loss: `entry - ATR * sl_multiplier` (zeile 199)
- Take-Profit: `entry + ATR * tp1_multiplier` (zeile 200)
- Implementiert Supertrend für Trailing-Stop-Logik (zeile 98-166)
- **Wichtig**: Nur BUY/LONG Signale werden unterstützt (zeile 173)

### 1.3 RocketFuelSignalProcessor (signal_bots/rocketfuel/processor.py)
- Zwei Varianten: `longsonly` und `longandshort` (zeile 21, 49-55)
- Bei `longsonly` + SELL: Setzt `exit_only=True` im Metadaten (zeile 130), SL bleibt None
- Bei `longandshort`: SELL öffnet Short-Position, SL ist `entry + ATR * sl_multiplier` (zeile 126-127)
- **Beachte**: `longsonly` Variante erlaubt kein Short-Entry, nur Exit (zeile 129-130)

## 2. Discord-Signal-Routing-Analyse (live_runner/daemon.py)

### 2.1 Codepfad: _run_discord_signal_listener_for_account() (zeile 546-684)

**Routing-Strategie:**
1. Filtert aktive Discord-Deployments für den Account (zeile 553-559)
2. Gruppiert Deployments nach `channel_id` (zeile 567-579)
3. **Kritisch**: Loop über Deployments mit `break` nach erster erfolgreicher Order (zeile 605-672)
   - Signal wird nur an **erste passende Deployment** gesendet
   - Andere Deployment(s) mit gleichem Symbol verpassen das Signal
   - Keine Round-Robin oder Lastverteilung

**Exposure-Implementierung (zeile 453-518):**
- `submit_order_with_risk` hat `current_exposure: float = 0.0` Default (zeile 458)
- Für normale Buys: `broker_exposure` WIRD berechnet (zeile 488-503)
- Exposure-Berechnung ist **konservativ**: `open_positions_count * max_risk_per_trade`
- Nicht die tatsächliche Position-Größe/Exposure wird verwendet
- `get_positions()` Wird korrekt aufgerufen für Risk Engine Wire

### 2.2 Signal-Processor-Anwendung (_apply_signal_processor, zeile 361-395)
- Wird in Zeile 622 auf Discord-Signale angewendet
- Erfordert `market_data` (zeile 377) - wirft ValueError wenn fehlt (zeile 355-357)
- Ersetzt Metadaten mit Processor-Ergebnis (zeile 388-390)
- Fügt `signal_processor` Metadaten hinzu (zeile 390)

### 2.3 Größenordnung der Routing-Lücke
In der Schleife (zeile 605-672):
```python
for deployment in deployments_for_channel:
    # ... Symbol-Check ...
    # ... Signal-Processing ...
    broker_order_id = await submit_order_with_risk(adapter, deployment, live_signal)
    # ...
    break  # <-- ERSTER Erfolg bricht die Schleife!
```
**Problem**: Wenn deployment_a und deployment_b denselben Account + Symbol haben,
wird das Signal nur an deployment_a gesendet. deployment_b verliert es.

## 3. Alpaca-Adapter-Analyse (live_runner/alpaca_adapter.py)

### 3.1 Credential-Resolving (resolve_alpaca_credentials, zeile 57-147)
- Unterstützt Default-Credentials (ALPACA_API_KEY/SECRET_KEY) (zeile 66-70)
- Unterstützt ACC{N}-Blöcke (ALPACA_API_KEY_ACC{N}, etc.) (zeile 72-105)
- Matcht nach `account_id_hint` oder `login_hint` (zeile 110-126)
- **Fällt zurück auf ersten konfigurierten Block** wenn kein Match (zeile 142-146)

### 3.2 submit_order() (zeile 281-331)
- Nutzt Equity (nicht Cash) für Risk Sizing (zeile 294-295)
- Ruft `size_live_signal()` auf ohne `current_exposure` Parameter
- **Hinweis**: Exposure-Wire ist in `submit_order_with_risk()` (daemon.py) implementiert, nicht hier

### 3.3 get_positions() (zeile 341-357)
- Wird von `submit_order_with_risk()` aufgerufen für Exposure-Berechnung (daemon.py zeile 490)

## 4. Message-Deduplication-Analyse

### 4.1 Discord-Bot Reconnect (discord_source.py, zeile 476-479)
- `reconnect()` disconnect und connect erneut (zeile 476-479)
- **Kein Memory des letzten `discord_message_id`**
- Discord.py mit `reconnect=True` könnte Nachrichten seit letztem Connect lesen

### 4.2 DB-Constraint Prüfung
- `live_signals` Tabelle hat **keinen UNIQUE Constraint** auf `discord_message_id`
- Keine Prüfung vor `save_live_signal()` (database.py zeile 881-885)
- `merge()` wird verwendet, aber `id` ist UUID generiert, nicht `discord_message_id`

## 5. Testabdeckung - Lücken

### 5.1 Bestehende Tests (tests/test_signal_processor_registry.py)
- test_registry_options_expose_all_expected_ids ✓
- test_registry_returns_expected_processor_classes ✓
- test_rocketfuel_variants_differ_on_sell_signal ✓
- test_nitro_rejects_sell_signals ✓

### 5.2 Fehlende Tests
- Kein Test für `_run_discord_signal_listener_for_account()` Routing-Verhalten
- Kein Test für Exposure-Berechnung mit tatsächlichen Positionswerten
- Kein Test für Multi-Deployment-Same-Account-Symbol-Routing
- Kein Test für Discord-Reconnect Deduplication
- Kein Test für `size_live_signal_for_submission` mit `current_exposure`

## 6. Empfohlene Tests (ohne Secrets)

```python
# Test-Draft für reports/kilo-discord-routing-notes.md

def test_discord_routing_multiple_deployments_same_account_symbol():
    """
    Discord-Signal wird nur an erste passende Deployment gesendet.
    
    Setup:
    - 2 ACTIVE Deployments für Account A mit gleichem Symbol (z.B. AAPL)
    - Beide nutzen gleichen Discord Channel
    
    Erwartet:
    - Nur 1 Order-Submission statt 2
    - deployment_b verliert Signal (silent signal loss)
    - Eventuell Log-Eintrag über skipped signal fehlt aktuell
    """
    pass


def test_submit_order_with_risk_uses_conservative_exposure():
    """
    Positions-Check wird für normale Buy-Signale konservativ gehandhabt.
    
    Setup:
    - Mock AlpacaLiveAdapter mit offener AAPL-Position (qty=100)
    - Deployment mit max_positions=1
    
    Erwartet:
    - Signal wird gesendet
    - broker_exposure = open_positions_count * max_risk_per_trade (konservative Schätzung)
    - Tatsächliche Positionsgröße wird nicht als exposure verwendet
    """
    pass


def test_rocketfuel_longsonly_sell_sets_exit_only():
    """
    RocketFuel longsonly Variante: SELL Signal sollte exit_only=True setzen.
    
    Setup:
    - RocketFuelSignalProcessor mit variant="longsonly"
    - SELL action signal
    
    Erwartet:
    - metadata["exit_only"] == True
    - stop_loss == None
    - submit_order_with_risk prüft Positions-Größe vor Exit (zeile 476-482)
    """
    pass


def test_rocketfuel_longandshort_sell_opens_short():
    """
    RocketFuel longandshort Variante: SELL Signal öffnet Short.
    
    Setup:
    - RocketFuelSignalProcessor mit variant="longandshort"
    - SELL action signal
    
    Erwartet:
    - stop_loss > entry_price (protection for short)
    - Kein exit_only Flag
    """
    pass


def test_market_data_required_for_processor():
    """
    _build_processor_market_data wirft ValueError wenn kein Market Data.
    
    Setup:
    - TradeSignal ohne metadata["market_data"]
    - Deployment mit signal_processor="rocketfuel_longsonly"
    
    Erwartet:
    - ValueError wird geworfen
    - Signal wird nicht zur Order
    """
    pass
```

## 7. Bemerkungen zur Sicherheit

1. **Keine Secrets** wurden gelesen oder verwendet
2. **Exposure-Berechnung ist konservativ**: Benutzt `max_risk_per_trade` statt tatsächlicher Positionswerte
3. **Routing-Lücke**: Signal geht nur an erste passende Deployment ("first match wins")
4. **Deduplication-Lücke**: `discord_message_id` hat keinen UNIQUE Constraint

## 8. Priorität für Fixes

1. **Exposure-Genauigkeit**: Tatsächliche Positionswerte statt `max_risk_per_trade` Schätzung verwenden
2. **Routing-Strategie**: Dokumentation oder Änderung des "first match wins" Verhaltens
3. **Duplikation**: `discord_message_id` Unique Constraint in DB + Pre-Check in `_parse_trade_signal_from_message`
4. **Testabdeckung**: Die oben genannten Tests implementieren

---

## 9. Executive Summary

### Was funktioniert:
- Nitro erbt vom RamJet und schränkt auf BUY-only ein (wie dokumentiert)
- RocketFuel longsonly/longandshort Varianten unterscheiden sich korrekt bei SELL-Signalen
- Exposure-Wire ist in `submit_order_with_risk` implementiert (broker_exposure wird berechnet)
- `get_positions()` wird für Risk Engine Wire aufgerufen

### Was problematisch ist:
- **Routing**: Erstes passendes Deployment gewinnt ("first match wins") ohne Log-Eintrag für verlorene Signale
- **Exposure-Berechnung**: Konservative Schätzung `open_positions_count * max_risk_per_trade` statt tatsächlicher Positionswerte
- **Deduplication**: Bei Discord-Bot Reconnect könnten Nachrichten dupliziert werden, da kein `discord_message_id` Unique Constraint existiert

### Keine Secrets verwendet:
- Keine .accounts.env gelesen
- Keine Commits gelesen
- Analyse ausschließlich über Code-Parsing und Regex-Patterns