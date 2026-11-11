# Paper-Trading Readiness

**Datum:** 2026-06-24

## Ziel

Dieses Dokument beschreibt den *Paper-Trading-Readiness*-Validator, der anhand von
Backtest-Vergleichsergebnissen entscheidet, ob die eigene Plattform bereit wäre,
Orders an einen Broker weiterzureichen (Paper Trading).

## Readiness-Checks

Der :class:`~validation.readiness.validator.ReadinessValidator` führt
Transaktions-, Risiko-, Regressions- und Statistikprüfungen aus. Die
folgenden Artefakte sind für eine belastbare Entscheidung **verpflichtend**:
Trade-Delta-Report, Regression-Result, Risk-Comparison-Report und das
Paar `(expected_stats, actual_stats)`. Fehlt eines dieser Artefakte, ist das
Ergebnis `NOT_READY`; ein fehlendes Artefakt wird nicht durch leere, bestandene
Checks ersetzt. Die `require_*`-Optionen steuern die expliziten
`required_artifact_*`-Checks, können aber die fail-closed-Abhängigkeit der
nachfolgenden Prüfungen nicht aufheben.

| Check | Name | Bedeutung |
|-------|------|-----------|
| a | `trade_errors` | Error-Level-Abweichungen in Trades |
| b | `missing_trades` | Fehlende Trades innerhalb des konfigurierten Schwellenwerts |
| c | `extra_trades` | Zusätzliche Trades innerhalb des konfigurierten Schwellenwerts |
| d | `critical_mismatches` | Kritische Richtungs-/PnL-Abweichungen |
| e | `trade_warnings` | Warning-Level-Abweichungen; ein Fehlschlag ist `READY_WITH_WARNINGS` |
| f | `regression_status` | Regression bestanden, wenn `require_all_goldens_pass=True`; fehlende Evidenz ist ein Fehler |
| g | `risk_evidence` | Risk-Report enthält vergleichbare Feldwerte |
| h | `risk_failed_results` | Keine strukturell fehlgeschlagenen Risk-Ergebnisse |
| i | `risk_report_verdict` | Risk-Report insgesamt bestanden |
| j | `risk_position_size` | Position-Size-Differenz innerhalb Toleranz |
| k | `risk_stop_distance` | Stop-Distance-Differenz innerhalb Toleranz |
| l | `stats_risk_free_rate` | Beide Statistikseiten verwenden kanonisch `risk_free_rate=0` |
| m | `stats_win_rate` | Win-Rate-Differenz innerhalb Toleranz |
| n | `stats_net_profit` | Net-Profit-Differenz innerhalb Toleranz |
| o | `stats_sharpe` | Sharpe-Differenz innerhalb der/absoluten Sharpe-Toleranz |
| p | `stats_max_drawdown` | Absolute Max-Drawdown-Differenz höchstens `0.02` (2 Prozentpunkte) |
| q | `equity_evidence` | Equity-Evidenz für Referenz und Plattform vorhanden |

Jede Prüfung erzeugt ein :class:`~validation.readiness.models.ReadinessCheck` mit:
- `name` — maschinenlesbarer Name
- `description` — menschenlesbare Beschreibung
- `passed` — boolesches Ergebnis
- `severity` — `error` / `warning` / `info`
- `message` — erklärender Text
- `details` — strukturierte Zusatzdaten (z. B. Differenzen, Schwellen)

## Readiness-Level und fehlgeschlagene Warnungen

Aus den Checks wird ein aggregiertes
:class:`~validation.readiness.models.PaperReadinessLevel` abgeleitet:

| Level | Bedingung |
|-------|-----------|
| `NOT_READY` | Mindestens ein Check mit `severity=error` und `passed=False` |
| `READY_WITH_WARNINGS` | Keine Errors, aber mindestens ein Warning mit `passed=False` |
| `READY` | Alle Checks bestanden |

Ein fehlgeschlagener Check mit `severity=warning` führt zu
`READY_WITH_WARNINGS`; der aggregierte `ReadinessReport.passed` bleibt in
diesem Fall `True`. Zähler-Checks können dagegen bei einer Abweichung innerhalb
ihres Schwellenwerts `severity=warning` bei `passed=True` sein; nur
`passed=False` zählt als fehlgeschlagene Warnung. Ein fehlgeschlagener
Error-Check, ein fehlendes Pflichtartefakt oder ein fehlgeschlagener
Risk-Report-Gesamtstatus führt immer zu `NOT_READY` und `passed=False`.

## Schwellenwerte (`ReadinessConfig`)

| Feld | Standard | Beschreibung |
|------|----------|--------------|
| `max_missing_trades` | `0` | Maximal akzeptable Anzahl fehlender Trades |
| `max_extra_trades` | `0` | Maximal akzeptable Anzahl zusätzlicher Trades |
| `max_mismatched_trades` | `0` | Maximal akzeptable Anzahl kritischer Mismatches |
| `max_error_count` | `0` | Maximal akzeptable Anzahl Error-Level-Abweichungen |
| `require_all_goldens_pass` | `True` | Regression muss bestehen, wenn vorhanden |
| `max_position_size_diff_pct` | `0.01` (1 %) | Maximale relative Position-Size-Abweichung |
| `max_stop_distance_diff_pct` | `0.01` (1 %) | Maximale relative Stop-Distance-Abweichung |
| `stats_win_rate_tolerance_pct` | `0.01` (1 %) | Maximale relative Win-Rate-Abweichung |
| `stats_net_profit_tolerance_pct` | `0.01` (1 %) | Maximale relative Net-Profit-Abweichung |
| `stats_sharpe_tolerance_pct` | `0.10` (10 %) | Relativer Sharpe-Anteil |
| `stats_sharpe_tolerance_abs` | `0.1` | Mindestabsolute Sharpe-Toleranz |
| `stats_max_drawdown_tolerance_pp` | `0.02` (2 Prozentpunkte) | Absolute Drawdown-Toleranz |

### Nenner und Spezialfälle

Relative Abweichungen werden gegen den **erwarteten** Wert berechnet:
`abs(expected - actual) / abs(expected)`. Bei `expected == 0` gilt die
Abweichung nur dann als null, wenn auch `actual == 0`; jede Abweichung von
null ist `1.0` und damit ein Fehlschlag. Die Sharpe-Prüfung verwendet
`max(stats_sharpe_tolerance_pct * abs(expected_sharpe),
stats_sharpe_tolerance_abs)`. Die Drawdown-Prüfung verwendet die direkte
absolute Differenz, nicht den Nenner der relativen Statistikprüfungen.


Die Validation-CLI führt Readiness immer aus. Ohne Golden-Datei fehlt die
Regression-Evidenz; ohne Equity-Kurve auf beiden Seiten fehlt die
Kalender-Evidenz. Beide Fälle blockieren die Zertifizierung.

## Was würde Paper Trading bedeuten?

Paper Trading (auch *Simulated Trading* oder *Virtual Trading* genannt) ist die
Ausführung von Handelssignalen über einen Broker, ohne dass echtes Kapital
riskiert wird.  Der Broker führt die Orders in einer simulierten Umgebung aus
und meldet gefüllte Orders, Slippage und Gebühren zurück.

Der :class:`~validation.readiness.validator.ReadinessValidator` prüft **nicht**
die technische Infrastruktur für Paper Trading.  Er beantwortet lediglich die
Frage: "Sind die Backtest-Ergebnisse unserer Plattform hinreichend identisch
mit der externen Referenz, sodass wir Vertrauen in die Signalgenerierung haben?"

## Strategy Promotion Gate (#165)

Paper trading is one of three required pieces of evidence for promoting a
strategy or signal bot to `production`, the precondition for a real-money
account (details: `docs/live-deploy-runbook.md`, section "Strategy Promotion
Gate").

The paper-trading evidence needs:
- a run on a paper/demo account (Alpaca paper or cTrader demo) of at least
  **60 days** that has ended (default, configurable),
- at least **30 trades** (default, configurable),
- a report file, whose SHA256 is recorded in the promotion ledger.

The other two are an out-of-sample result with a passed overfitting verdict
(test segment ≥ 180 days and ≥ 50 trades) and cross-engine or reference parity
within 2 %.

| Aspect | Paper readiness validator (this document) | Promotion gate |
|--------|---------------------------|----------------|
| Scope | Platform-wide backtest vs. reference parity | One strategy file or bot processor with one parameter set |
| Output | `NOT_READY` / `READY_WITH_WARNINGS` / `READY` | `experimental` / `production` |
| Enforcement | Advisory | Fail closed on real-money accounts (preflight, daemon, viewer) |

A parity report produced here can serve as the parity evidence of a promotion.
Paper/demo accounts never need a promotion; the existing demo-only block on
real money is unchanged by the gate.

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
