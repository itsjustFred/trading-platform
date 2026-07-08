# Test Suite Failure Report

## Executive Summary

Die vielen Fehler stammen aus **zwei Hauptursachen**:

1. **Playwright-Browser fehlt** (führt zu einem initialen E2E-Setup-Error).
2. Dieser E2E-Fehler hinterlässt einen laufenden Event-Loop, wodurch mehrere spätere Tests mit `asyncio.run(...)` scheitern.

Zusätzlich gibt es **einen echten, unabhängigen Assertion-Fail** in `tests/test_api.py` (veraltete Parameter-Erwartung).

## Testlauf-Ergebnis

Vollständiger Lauf:

```bash
.venv/bin/python -m pytest tests -q
```

Ergebnis:

- **13 failed**
- **1 error**
- **265 passed**
- **69 skipped**

## Detaillierte Ursachenanalyse

### 1) Primärer Setup-Fehler: Playwright-Binary nicht installiert

Fehler:

- `tests/e2e/lab/test_conversion.py::test_lab_conversion_generates_valid_python[chromium]`
- `playwright._impl._errors.Error: BrowserType.launch: Executable doesn't exist ...`

Kernaussage: Die lokale Playwright-Installation hat keine heruntergeladenen Browser-Binaries.

### 2) Kaskadierende Async-Fehler durch laufenden Event-Loop

Symptom:

- Mehrere Tests schlagen mit
  `RuntimeError: asyncio.run() cannot be called from a running event loop`
  fehl.

Betroffene Tests:

- `tests/test_ctrader_adapter.py::test_get_balance_marks_terminal_auth_failure_for_not_authorized`
- `tests/test_dukascopy_downloader.py::{...4 Tests...}`
- `tests/test_live_daemon.py::{...6 Tests...}`
- `tests/test_risk_engine.py::test_submit_order_with_risk_applies_qty_before_submit`

Wichtige Evidenz:

- Dieselben Tests **bestehen einzeln**.
- Wenn der E2E-Playwright-Test davor läuft und scheitert, schlagen sie danach mit `asyncio.run`-Fehlern fehl.
- Repro:
  ```bash
  .venv/bin/python -m pytest -q \
    tests/e2e/lab/test_conversion.py::test_lab_conversion_generates_valid_python \
    tests/test_risk_engine.py::test_submit_order_with_risk_applies_qty_before_submit
  ```
  Ergebnis: erst Playwright-Error, danach `asyncio.run`-Failure.

Interpretation: Das ist kein eigenständiger Defekt in allen betroffenen Modulen, sondern überwiegend ein **Folgefehler durch Test-Umgebung/Fixture-Zustand** nach dem Playwright-Setup-Error.

### 3) Unabhängiger inhaltlicher Test-Fail (nicht kaskadierend)

Fehler:

- `tests/test_api.py::TestStrategiesAPI::test_get_strategy_detail_includes_parameters`

Ist-Zustand API:

- `/api/strategies/example_sma` liefert:
  ```json
  {"fast_length": 9, "slow_length": 21}
  ```

Test erwartet:

```json
{"fast": 10, "slow": 30}
```

Root Cause:

- `strategies/example_sma/example_sma.py` definiert in `STRATEGY_META` bereits `fast_length/slow_length` mit `9/21`.
- Der Test ist gegenüber dem aktuellen Strategievertrag veraltet.

## Gegenprobe ohne E2E

```bash
.venv/bin/python -m pytest tests -q -m 'not e2e'
```

Ergebnis:

- **1 failed**, **277 passed**, **69 skipped**, **1 deselected**
- Der einzige Fail ist der oben genannte API-Parameter-Test.

Das bestätigt: Der Großteil der Fehler ist durch den E2E/Playwright-Pfad verursacht.

## Empfehlungen (priorisiert)

1. **Playwright stabilisieren**
   - Browser installieren:
     ```bash
     playwright install
     ```
   - Alternativ E2E im Standardlauf ausschließen, falls Browser nicht Teil der CI-Umgebung sind.

2. **Async-Testrobustheit erhöhen**
   - Tests, die aktuell `asyncio.run(...)` verwenden, in pytest-async-kompatible Form überführen (`async def` + `await`) oder strikt von Playwright/E2E trennen.
   - Ziel: keine Folgekaskade bei externen Fixture-Fehlern.

3. **API-Test an aktuellen Strategievertrag anpassen**
   - Erwartung in `test_get_strategy_detail_includes_parameters` auf `fast_length/slow_length` (9/21) aktualisieren
   - oder Backend bewusst auf Legacy-Namen (`fast/slow`) normalisieren, falls das fachlich gewünscht ist.

## Fazit

Die „vielen Fehler“ sind größtenteils **sekundäre Folgefehler** eines fehlenden Playwright-Browsers.
Funktional unabhängig ist aktuell primär der **eine veraltete API-Parameter-Test**.
