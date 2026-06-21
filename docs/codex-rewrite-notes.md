# Codex Rewrite Notes

## Plan

1. Auto-Download fehlender Daten: Ich beginne bei den gemeinsamen Backtest-Einstiegen (`runner/cli.py`, `runner/backtest_grid.py`, `runner/backtest_matrix.py`) und der Datenauflösung in den Engine-Adaptern. Ziel ist eine kleine gemeinsame Ensure-Schicht, die vor dem eigentlichen Backtest prüft, ob die benötigten OHLCV-Parquets für Symbol, Timeframe und Zeitraum vorhanden sind, und bei Lücken den vorhandenen Downloader-Pfad nutzt statt den Run abbrechen zu lassen. Betroffen sind voraussichtlich `data_tools/downloaders/dukascopy_native.py`, `data_tools/download_grid_market_data.py`, `runner/engines/base.py` und die CLI-/Grid-Wrapper.

2. Backtest-Grid bei Risk Parity überarbeiten: Ich prüfe zuerst `strategies/risk_parity/risk_parity.py`, den aktuellen Optimizer (`runner/optimizer.py`) und das Grid-System (`runner/backtest_grid.py`). Klassische Parameter-Kombinationen sind für Risk Parity nur begrenzt sinnvoll; ich werde daher einen symbol-/universumsorientierten Ansatz vorziehen, bei dem Kandidaten-Portfolios, Rebalancing-Varianten und Volatilitätsfenster bewertet werden. Zusätzlich baue ich eine Möglichkeit ein, eigene feste Symbol-Listen zu übergeben, damit die Optimierung nicht nur aus einem kartesischen Parameter-Grid besteht.

3. Matrix-Grid komplett überarbeiten: Ich gehe Punkt 2, 3 und 7 zusammenhängend an, weil alle am Matrix-/Grid-Subsystem hängen. Zuerst stelle ich sicher, dass jede Matrix-Zelle als vollwertiger `BacktestResult` mit Trades persistiert wird und eindeutig referenzierbar ist; danach erweitere ich Summary/CSV/JSON um Backtest-IDs und Trade-Zugriffe. Betroffen sind vor allem `runner/backtest_grid.py`, `runner/backtest_matrix.py`, `shared/database.py`, `scripts/generate_matrix_grid_overview.py` und die API in `viewer/main.py`.

4. Mittelklick/Strg-Klick-Bug im Frontend: Ich suche alle Stellen mit `navigate(...)`, `onClick`-Navigation und kartenartigen Listen-Items im Frontend. Wo ein Ziel eine echte Route ist, ersetze ich programmatische Navigation durch `Link` oder `<a href>`, inklusive Tabellenzeilen-/Card-Layouts, damit Mittelklick, Strg/Cmd-Klick und Kontextmenü überall funktionieren. Betroffen sind voraussichtlich `frontend/src/pages/StrategiesPage.tsx`, `BacktestsPage.tsx`, `OptimizationsPage.tsx`, `PortfoliosPage.tsx`, `LiveHub.tsx` und ähnliche Übersichtsseiten.

5. Strategie-Vergleich gegen Referenzen: Ich lese die lokalen Dateien unter `strategy-comparisons/` und vergleiche sie mit `strategies/risk_parity/risk_parity.py` sowie der implementierten Intraday-Volatility-Breakout-Strategie. Dabei prüfe ich explizit Timeframe, Datenquelle, 1-Minuten-Bar-Modelling, MT5/Dukascopy-Annahmen und Signal-/Rebalance-Logik. Abweichungen dokumentiere ich in diesen Notes; Fixes erfolgen nur dort, wo der Referenzbefund eindeutig ist.

6. Risk-Logik allgemein prüfen: Ich folge der Risk-Konfiguration von den Requests/API-Modellen über `runner/engines/base.py`, die Engine-Adapter, `shared/models.py` und Persistenz in `shared/database.py`. Position Sizing, Stop-Loss, Risk Parity und Grenzen wie `max_position_pct`/`max_leverage` werden anhand vorhandener Backtests oder durch Code-Plausibilisierung geprüft. Jede Änderung an Order-Ausführung, Position Sizing, Stop-Loss oder Geldbeträgen markiere ich zusätzlich mit "⚠️ Review erforderlich".

7. Matrix-View/Grid besser ins Frontend einbauen: Sobald Trade-Level-Daten für Matrix-Zellen verfügbar sind, baue ich die Matrix-Ansicht näher an die normalen Backtest-Detailflows an. Das heißt: Zellen sollen auf eine Detailansicht oder auf den bestehenden Backtest-Detailpfad verlinken, Trade-Tabellen sollen wiederverwendet werden, und API/Types werden passend erweitert. Betroffen sind `frontend/src/pages/MatrixGridPage.tsx`, `BacktestDetailPage.tsx`, `TradeTable.tsx`, `frontend/src/api/types.ts` und die Viewer-Endpunkte.

8. Repo aufräumen: Zum Schluss identifiziere ich verwaiste Debug-, Scratch- und Agent-Artefakte mit `rg`, Importsuche und Git-Kontext. Ich lösche nur Dateien, die offensichtlich unbenutzt oder dokumentierte temporäre Reports sind, und liste jede Entfernung mit Begründung in diesen Notes. Untracked Referenzdaten oder Secrets wie `.accounts.env` fasse ich nicht an.

## Work Log

- 2026-06-21: Branch `codex_rewrite` vom aktuellen Arbeitsstand erstellt. Vorhandene fremde Änderungen bleiben unangetastet: `.accounts.env` ist als gelöscht markiert, `strategy-comparisons/` ist untracked.
