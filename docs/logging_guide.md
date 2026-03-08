# Logging Guide — trading-platform

## Übersicht

Das zentrale Logging-Modul (`shared/logging.py`) erzeugt structured JSON-Logs mit automatischer File-Rotation. Alle Services nutzen dasselbe Format.

## Schnellstart

```python
from shared.logging import get_logger

logger = get_logger("mein_service")

logger.info("Etwas passiert")
logger.warning("Achtung", extra={"data": {"key": "value"}})
logger.error("Fehler aufgetreten", exc_info=True)
```

## Log-Format

Jeder Log-Eintrag ist eine JSON-Zeile:

```json
{
    "timestamp": "2026-02-25T14:30:22.123Z",
    "level": "INFO",
    "service": "mein_service",
    "module": "main",
    "message": "Etwas passiert"
}
```

Mit `extra={"data": {...}}` wird ein `data`-Feld hinzugefügt:

```json
{
    "timestamp": "2026-02-25T14:30:22.123Z",
    "level": "WARNING",
    "service": "mein_service",
    "module": "main",
    "message": "Achtung",
    "data": {"key": "value"}
}
```

## Konfiguration

| Env-Variable | Default | Beschreibung |
|-------------|---------|--------------|
| `LOG_LEVEL` | `INFO` | Minimum Log-Level (DEBUG, INFO, WARNING, ERROR, CRITICAL) |
| `LOG_DIR` | `~/logs` | Verzeichnis für Log-Dateien |

## File-Rotation

- Jeder Service schreibt in `~/logs/<service>.log`
- Rotation bei **10 MB**
- Maximal **5 Backup-Dateien** (`service.log.1` bis `service.log.5`)
- Das Verzeichnis wird automatisch angelegt

## Einbindung in stockkit_ai_bot

Um das Logging-Modul in stockkit einzubinden:

### 1. Import-Pfad sicherstellen

Der `trading-platform`-Ordner muss im Python-Path sein:

```bash
export PYTHONPATH="/home/ubuntu/Desktop/trading-platform:$PYTHONPATH"
```

Oder in der `.env` / Startskript des Bots.

### 2. Logger erstellen

```python
from shared.logging import get_logger

logger = get_logger("stockkit")
```

Das erzeugt `~/logs/stockkit.log` mit dem gleichen JSON-Format.

### 3. Bestehende print()-Aufrufe ersetzen

```python
# Vorher
print(f"Signal erkannt: {symbol} {direction}")

# Nachher
logger.info("Signal erkannt", extra={"data": {"symbol": symbol, "direction": direction}})
```

### 4. Fehlerbehandlung

```python
try:
    result = execute_trade(order)
except Exception as e:
    logger.error("Trade fehlgeschlagen", exc_info=True, extra={"data": {"order": str(order)}})
```

## Aktuelle Services

| Service | Logger-Name | Log-Datei |
|---------|------------|-----------|
| Viewer | `viewer` | `~/logs/viewer.log` |
| Runner | `runner` | `~/logs/runner.log` |
| stockkit | `stockkit` | `~/logs/stockkit.log` (nach Einbindung) |
