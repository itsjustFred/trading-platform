# Paper-Trading Readiness

**Datum:** 2026-06-24

## Ziel

Dieses Dokument beschreibt den *Paper-Trading-Readiness*-Validator, der anhand von
Backtest-Vergleichsergebnissen entscheidet, ob die eigene Plattform bereit wäre,
Orders an einen Broker weiterzureichen (Paper Trading).

## Readiness-Checks

Der :class:`~validation.readiness.validator.ReadinessValidator` führt folgende
Prüfungen durch:

| Check | Name | Bedeutung |
|-------|------|-----------|
| a | `trade_errors` | Keine Error-Level-Abweichungen in gematchten Trades |
| b | `missing_trades` | Anzahl fehlender Trades unter Schwellenwert |
| c | `extra_trades` | Anzahl zusätzlicher Trades unter Schwellenwert |
| d | `critical_mismatches` | Keine Mismatches in Richtung oder PnL |
| e | `stats_win_rate` | Win-Rate-Differenz innerhalb Toleranz |
| f | `stats_net_profit` | Net-Profit-Differenz innerhalb Toleranz |
| g | `risk_position_size` | Position-Size-Differenz innerhalb Toleranz |
| h | `risk_stop_distance` | Stop-Distance-Differenz innerhalb Toleranz |
| i | `regression_status` | Regression bestanden (sofern vorhanden) |

Jede Prüfung erzeugt ein :class:`~validation.readiness.models.ReadinessCheck` mit:
- `name` — maschinenlesbarer Name
- `description` — menschenlesbare Beschreibung
- `passed` — boolesches Ergebnis
- `severity` — `error` / `warning` / `info`
- `message` — erklärender Text
- `details` — strukturierte Zusatzdaten (z. B. Differenzen, Schwellen)

## Readiness-Levels

Aus den Checks wird ein aggregiertes :class:`~validation.readiness.models.PaperReadinessLevel` abgeleitet:

| Level | Bedingung |
|-------|-----------|
| `NOT_READY` | Mindestens ein Check mit `severity=error` und `passed=False` |
| `READY_WITH_WARNINGS` | Keine Errors, aber mindestens ein Warning mit `passed=False` |
| `READY` | Alle Checks bestanden |

## Schwellenwerte (`ReadinessConfig`)

Die :class:`~validation.readiness.models.ReadinessConfig` enthält die
konfigurierbaren Schwellen:

| Feld | Standard | Beschreibung |
|------|----------|--------------|
| `max_missing_trades` | `0` | Maximal akzeptable Anzahl fehlender Trades |
| `max_extra_trades` | `0` | Maximal akzeptable Anzahl zusätzlicher Trades |
| `max_mismatched_trades` | `0` | Maximal akzeptable Anzahl Mismatched-Trades |
| `max_error_count` | `0` | Maximal akzeptable Anzahl Error-Level-Abweichungen |
| `require_all_goldens_pass` | `True` | Regression muss bestehen, wenn vorhanden |
| `max_position_size_diff_pct` | `0.01` (1 %) | Maximale relative Position-Size-Abweichung |
| `max_stop_distance_diff_pct` | `0.01` (1 %) | Maximale relative Stop-Distance-Abweichung |
| `stats_win_rate_tolerance_pct` | `0.01` (1 %) | Maximale relative Win-Rate-Abweichung |
| `stats_net_profit_tolerance_pct` | `0.01` (1 %) | Maximale relative Net-Profit-Abweichung |

## Was würde Paper Trading bedeuten?

Paper Trading (auch *Simulated Trading* oder *Virtual Trading* genannt) ist die
Ausführung von Handelssignalen über einen Broker, ohne dass echtes Kapital
riskiert wird.  Der Broker führt die Orders in einer simulierten Umgebung aus
und meldet gefüllte Orders, Slippage und Gebühren zurück.

Der :class:`~validation.readiness.validator.ReadinessValidator` prüft **nicht**
die technische Infrastruktur für Paper Trading.  Er beantwortet lediglich die
Frage: "Sind die Backtest-Ergebnisse unserer Plattform hinreichend identisch
mit der externen Referenz, sodass wir Vertrauen in die Signalgenerierung haben?"

## Nächste Schritte für echtes Paper Trading

1. **Broker-Adapter**
   - Implementierung eines Broker-Clients (z. B. Alpaca, Interactive Brokers,
     cTrader), der Paper-Trading-Accounts unterstützt.
   - Abstraktion über ein einheitliches `BrokerAdapter`-Interface (place_order,
     cancel_order, get_positions).

2. **Order-Routing**
   - Mapping von internen `TradeSignal`-Objekten auf Broker-spezifische
     Order-Typen (Market, Limit, Stop).
   - Berücksichtigung von Order-Time-In-Force (GTC, IOC, FOK).

3. **State-Management**
   - Persistentes Tracking offener Positionen, Orders und Equity in einer
     Datenbank (z. B. SQLite oder PostgreSQL).
   - Wiederanlauf nach Neustart des Paper-Trading-Dienstes.

4. **Risk-Gate**
   - Live-Validierung jeder ausgehenden Order gegen konfigurierte Limits
     (max. Positionsgrösse, tägliches Verlustlimit, Exposure-Limits).
   - Harte Blockade von Orders, die Limits überschreiten.

5. **Monitoring & Alerting**
   - Dashboard für offene Positionen, unrealisiertes PnL und Order-Status.
   - Alerts bei Abweichungen zwischen erwartetem und tatsächlichem Fill-Preis.

6. **Schrittweise Eskalation**
   - Start mit kleinen Lots / niedrigem Kapital.
   - Nach einer definierten "Burn-In"-Phase (z. B. 100 Trades) Vergleich von
     Paper-Trading-Results mit Backtest-Erwartung.

> **Hinweis:** Diese Liste ist ausdrücklich außerhalb des Scopes des
> Strategy Validation Frameworks und wird hier nur zur Orientierung dokumentiert.
