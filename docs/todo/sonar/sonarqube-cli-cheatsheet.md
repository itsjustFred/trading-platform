# SonarQube CLI — Issues auf main fixen

Projekt: `itsjustFred_trading-platform` · Branch: `main` · Auth via Env: `SONARQUBE_CLI_TOKEN`, `SONARQUBE_CLI_ORG=itsjustfred`

> **Achtung:** Endpunkte immer mit `/api/...` Präfix. Ohne → falscher Host (`api.sonarcloud.io`) → 403.

## Alle offenen Issues (517)

```bash
sonar api get "/api/issues/search?componentKeys=itsjustFred_trading-platform&branch=main&resolved=false&ps=500&p=1"
```

Jedes Issue: `key`, `rule`, `message`, `component` (`<projekt>:<pfad>:zeile` via `component`+`line`), `severity`, `effort`, `flows[]` (Taint-Pfad).

## Nach Datei / Regel / Severity filtern

```bash
B="/api/issues/search?componentKeys=itsjustFred_trading-platform&branch=main&resolved=false"
sonar api get "$B&components=itsjustFred_trading-platform:shared/strategy_registry.py"  # eine Datei
sonar api get "$B&rules=pythonsecurity:S2083"                                          # eine Regel
sonar api get "$B&types=VULNERABILITY&severities=BLOCKER"                               # Typ+Severity
sonar api get "$B&ps=1&facets=severities"                                               # Verteilung
```

## Ein Issue im Detail (Rule-Doku, Taint-Flow)

```bash
sonar api get "/api/issues/search?issues=<KEY>"
```

Browser-Deep-Link: `https://sonarcloud.io/project/issues?id=itsjustFred_trading-platform&branch=main&issues=<KEY>&open=<KEY>`
Rule-Doku im Browser: `https://sonarcloud.io/coding_rules?open=<RULE>&rule_key=<RULE>`

## Verifikation nach Fix

```bash
# Issue wiederholt abfragen → Status FIXED (nach nächster Analyse) bzw. nicht mehr in Liste
sonar api get "/api/issues/search?issues=<KEY>"
```
