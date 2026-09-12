# CR-Head-Review Findings — Fix-Todo (2026-09-04)

## CRITICAL (4)
- [x] viewer/main.py:7655 (PR #54/18-C): NameError ohlcv→window.ohlcv — 500er bei Trade-Endpoint mit Indikatoren
- [x] viewer/main.py:6616 (PR #67/14-B): normalized_timeframe nicht an _verify_missing_data weitergereicht
- [x] LiveTerminal.tsx:96 (PR #69/18-B): Job-Wechsel A→B early-return — Terminal hängt bei Job A
- [x] discord_source.py:577 (PR #71/15-C): Seen-IDs-Pruning an Cursor-Grenze → Duplikate re-emitted

## MAJOR (relevante, ~11)
- [x] grid_tester.py:72 (PR #56/16-B): total_trades=1-Fallback wenn Stats total_trades weglassen — 50-Trade-Zelle als 1 persistiert
- [x] auto_agent.py:838 (PR #56/16-B): blocking readline — Deadline-Check läuft nicht bei stillem Child
- [x] ramjet/processor.py:229 (PR #61/strategies): supertrend_ready nie gesetzt im pandas-ta-Zweig — Warm-up als ready
- [x] regenerate_backtests.py:291 (PR #49/20-C): shutil.copy2 auf live SQLite WAL — Online-Backup-API nutzen
- [x] mt5_xlsx.py:425 (PR #59/19-C): Locale-Scan nur SL/TP-Zellen — "2.500" als 2.5 statt 2500
- [x] mt5_xlsx.py:562 (PR #59/19-C): orphan "out" Deal in letzte geschlossene Position — phantom TradeExit
- [x] _numbers.py:57 (PR #59/19-C): 5-stelliger deutscher Bruch "1,08500" → None, SL/TP-Zelle dropped
- [x] risk/models.py:41 (PR #59/19-C): size auf Lot-Multiple gefloored = 0.0 passiert min_position_size=0.0-Check
- [x] portfolio_optimizer.py:296 (PR #65/16-D1): Sub-Daily-Sharpe ~2× vs runner.stats-Konvention
- [x] daemon.py:901 (PR #70/15-B): Dispatch early-return bei invalid boolean — gültige Deployments später kriegen Signal nicht
- [x] account_discovery.py:146 (PR #70/15-B): malformed Block verwirft auch valide Konten
- [x] BacktestDetailPage.tsx:629 (PR #54/18-C): PSR percentScale="points" statt "fraction" — 0.95 als 0.95%
- [x] auto_download.py:442 (PR #58/19-A): Alpaca→yfinance-Fallback verschleiert Provenance
- [x] pynecore_adapter.py:263+350 (**Code liegt auf PR #57/cleanup/16c, nicht #68** — Orchestrator-Zuordnung war falsch): __future__-Import + Docstring in strict_structure erlaubt. Fix-Commit 0d9d12a auf cleanup/16c; PR #68 hat Querverweis-Kommentar.
- [x] dolthub_manager.py:121 (PR #67/14-B): _export_stocks ohne Symbol-Regex-Validierung
- [x] regenerate_mt5_parity_artifacts.py:66 (PR #67/14-B): doppelte MT5_SYMBOL-Deklarationen
- [x] LiveTerminal.tsx:65 (PR #69/18-B): default heartbeat auf plain-text task feed — silent reconnect ab 45s

## MINOR (Backlog, keine Fixes)
- OptimizeModal fixe IDs, DownloadModal aria-pressed, LoggingPage role=button, test_engine_contract docstring, reports/cleanup-15b-design.md
