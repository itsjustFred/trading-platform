# Technische Zusammenfassung: trading-platform

Stand der Analyse: aktueller Repository-Zustand in `/home/ubuntu/Desktop/trading-platform`.

## 1. Projektüberblick

### Zweck und Ziel der Anwendung

Die Anwendung ist ein universelles Trading-/Backtesting-System mit einheitlichem Datenmodell (`shared/models.py::BacktestResult`) und Web-Viewer.

Kernziele:

- Strategien über mehrere Engines ausführen (`runner/cli.py`, `runner/engines/*`): `native`, `vectorbt`, `lean`, `pynecore`.
- Ergebnisse in einem einheitlichen Format persistieren (JSON + SQLite, siehe `shared/database.py`).
- Ergebnisse, Datenkatalog, Portfolios, Live-Deployments und Logs in einer React-Oberfläche bedienen (`frontend/src/*` + `viewer/main.py`).
- Datenmanagement für OHLCV (Download, Resample, Validate, Harvest) über Backend-Tasks (`viewer/main.py`, `viewer/core.py`, `data_tools/*`).

### Technologie-Stack (Frontend, Backend, Datenbank, externe Services)

| Bereich | Technologien | Nachweis |
|---|---|---|
| Frontend | React 19, TypeScript 5.9, Vite 7, React Router 7, Tailwind 4, Lucide, Recharts, lightweight-charts, react-markdown | `frontend/package.json`, `frontend/src/App.tsx`, `frontend/src/main.tsx` |
| Backend API | FastAPI, Starlette Middleware (CORS/GZip), Uvicorn | `viewer/main.py`, `pyproject.toml`, `requirements.txt` |
| Datenverarbeitung | pandas, pyarrow, numpy, aiohttp | `pyproject.toml`, `requirements.txt`, `data_tools/*` |
| Optimierung/ML-nahe Teile | Optuna | `runner/optimizer.py`, `pyproject.toml` |
| Datenbank | SQLite + SQLModel/SQLAlchemy | `shared/database.py` |
| Logging | JSON Logging + RotatingFileHandler (10 MB, 5 Backups) | `shared/logging.py`, `docs/logging_guide.md` |
| Externe Services | NVIDIA NIM (`openai` SDK), Dukascopy/JForex, Dolthub, HuggingFace, Alpaca, cTrader, LEAN | `runner/auto_agent.py`, `data_tools/downloaders/*`, `live_runner/*`, `runner/engines/lean_adapter.py` |

## 2. Architektur

### Systemarchitektur (wie kommunizieren Frontend & Backend?)

1. Browser lädt React-SPA (`frontend/dist`) über FastAPI-Static-Serving (`viewer/main.py::static_spa_middleware`).
2. Frontend spricht über `fetch` in `frontend/src/api/client.ts` gegen `/api/*`.
3. Im Dev-Modus proxyt Vite:
   - `/api -> http://localhost:5556`
   - `/ws -> ws://localhost:5556`
   (`frontend/vite.config.ts`)
4. Backend (`viewer/main.py`) liest/schreibt primär SQLite (`shared/database.py`) und startet Hintergrund-Tasks.
5. Task-Logs werden per WebSocket gestreamt (`/ws/tasks/{job_id}`, `viewer/main.py::task_log_socket`).

### Datenfluss (von A nach B nach C...)

1. **Backtest-Start (UI/REST):** `POST /api/backtests/run` (`viewer/main.py::run_backtest`).
2. **Ausführung:** subprocess ruft `python -m runner.cli run` auf (`viewer/main.py::_run` innerhalb `run_backtest`).
3. **Engine-Lauf:** `runner/cli.py` baut `BacktestConfig` und ruft Engine-Adapter (`runner/engines/base.py::EngineAdapter.run`).
4. **Ergebnis:** Engine liefert `BacktestResult` (Unified Contract, `shared/models.py`).
5. **Persistenz:** JSON-Datei in `$DATA_DIR/backtests/*.trades.json` + SQLite (`save_result_to_sqlite`, `shared/database.py`).
6. **Viewer-API:** lädt Result über `load_backtest_result` (mit JSON-Fallback in `viewer/main.py::_load_result`) und formatiert UI-Payloads.
7. **Frontend-Rendering:** Seiten in `frontend/src/pages/*` konsumieren Endpunkte über `frontend/src/api/client.ts`.

### Ordnerstruktur mit kurzer Erklärung jedes Ordners/Moduls

- `frontend/`: React-SPA (Routing, Seiten, UI-Komponenten, API-Client).
- `viewer/`: FastAPI-Anwendung inkl. REST/WebSocket-Endpunkte und Task-Orchestrierung.
- `runner/`: CLI und Backtest-Engines, Stats, Optimizer, Auto-Agent.
- `shared/`: gemeinsame Konstanten, Models, Logging, DB-Zugriff.
- `data_tools/`: Download, Katalogaufbau, Validierung, Resampling, Datenquellen-Integrationen.
- `live_runner/`: Live-Adapter (Alpaca/cTrader), Deployment- und Balance-Poller-Daemon.
- `strategies/`: Strategiemodule (pro Strategie i. d. R. eigener Unterordner).
- `tests/`: Pytest-Suite.
- `docs/`: technische und fachliche Dokumentation.
- `scripts/`: operative Hilfsskripte (z. B. `adaptive_harvester.py`).
- `data/`: lokale Runtime-Daten (DB, Backtests, OHLCV, Catalog) im Default-Setup.
- `logs/`: Runtime-Logs und Task-Logs.
- `risk/`: Risiko-bezogene Module/Artefakte.
- `download/`: Download-/Integrationsartefakte (z. B. JForex SDK Ablagen).
- `server_planning/`: Planungs-/Konzeptdokumente, nicht produktiver Runtime-Code.

## 3. Frontend

### Framework & wichtige Libraries

- Framework/Build: `react`, `react-dom`, `typescript`, `vite`.
- Routing: `react-router-dom` (`frontend/src/App.tsx`, `frontend/src/main.tsx`).
- Styling: `tailwindcss`, `@tailwindcss/vite`.
- Datenzugriff: zentraler API-Client in `frontend/src/api/client.ts`.
- Visualisierung: `recharts`, `lightweight-charts`.
- UX-Helfer: `lucide-react`, `react-markdown`.

### Seitenstruktur / Routing

Routen sind in `frontend/src/App.tsx` definiert:

| Route | Komponente | Datei |
|---|---|---|
| `/` | Redirect auf `/strategies` | `App.tsx` |
| `/data` | `DataManagementPage` | `pages/DataManagementPage.tsx` |
| `/backtests` | `BacktestsPage` | `pages/BacktestsPage.tsx` |
| `/backtests/compare` | `ComparePage` | `pages/ComparePage.tsx` |
| `/backtests/:id` | `BacktestDetailPage` | `pages/BacktestDetailPage.tsx` |
| `/backtests/:id/trade/:nr` | `TradeChartPage` | `pages/TradeChartPage.tsx` |
| `/optimizations` | `OptimizationsPage` | `pages/OptimizationsPage.tsx` |
| `/optimizations/:id` | `OptimizationResultsPage` | `pages/OptimizationResultsPage.tsx` |
| `/live` | `LiveHubPage` | `pages/LiveHub.tsx` |
| `/live/:accountId` | `LiveDetailPage` | `pages/LiveDetail.tsx` |
| `/strategies` | `StrategiesPage` | `pages/StrategiesPage.tsx` |
| `/strategies/:name` | `StrategyDetailPage` | `pages/StrategyDetailPage.tsx` |
| `/portfolios` | `PortfoliosPage` | `pages/PortfoliosPage.tsx` |
| `/portfolios/:id` | `PortfolioDetailPage` | `pages/PortfolioDetailPage.tsx` |
| `/lab` | `StrategyLabPage` | `pages/StrategyLabPage.tsx` |
| `/logging` | `LoggingPage` | `pages/LoggingPage.tsx` |

### State Management

- Primär lokaler Component-State (`useState`, `useMemo`, `useEffect`) in den Seiten/Komponenten.
- Context:
  - `ToastProvider` (`frontend/src/components/ToastProvider.tsx`)
  - `StrategiesProvider` (`frontend/src/components/StrategiesProvider.tsx`)
- Task-State wird gepollt über `TaskManager` (`/api/data/tasks`, Polling jede Sekunde).
- Auffällig: `StrategiesProvider` nutzt `useQueryClient` (`@tanstack/react-query`), aber `QueryClientProvider` fehlt in `frontend/src/main.tsx`.

### API-Kommunikation

- Zentrale Schicht: `frontend/src/api/client.ts`.
- `fetchJSON<T>()` kapselt Requests, Fehler werden als `ApiError` geworfen.
- `BASE_URL = "/api"`.
- Beispiel-Funktionsgruppen:
  - Backtests: `getBacktests`, `runBacktest`, `getBacktestStats`, `getBacktestTrade`, `compareBacktests`.
  - Data: `downloadData`, `resampleData`, `harvestData`, `listDataTasks`, `pauseDataTask`, `cancelDataTask`.
  - Live: `getLiveAccounts`, `getLiveDeployments`, `getLiveTrades`, `getLiveBalances`.

## 4. Backend

### Framework & wichtige Libraries

- API-Framework: FastAPI (`viewer/main.py`, `app = FastAPI(...)`).
- Middleware: `CORSMiddleware`, `GZipMiddleware`, eigene HTTP-Middleware für Request-Logging.
- Datenlayer: SQLModel/SQLAlchemy (`shared/database.py`), SQLite-PRAGMAs: WAL, busy timeout 30s, foreign keys on.
- Datenanalyse: pandas/pyarrow.
- Task-Ausführung: `threading`, `subprocess`, `asyncio`.

### API-Endpunkte (Methode, Pfad, Zweck)

#### Strategien

| Methode | Pfad | Zweck | Handler |
|---|---|---|---|
| GET | `/api/strategies` | Strategieliste (Meta) | `list_strategies` |
| GET | `/api/strategies/{name:path}` | Strategy-Detail inkl. Doku | `get_strategy` |
| PATCH | `/api/strategies/{name:path}` | Strategy-Displayname umbenennen | `rename_strategy` |

#### Backtests & Analyse

| Methode | Pfad | Zweck | Handler |
|---|---|---|---|
| GET | `/api/backtests` | paginierte/filtrierte Backtest-Liste | `list_backtests` |
| GET | `/api/backtests/all` | vollständige Liste (optional Equity-Preview) | `list_backtests_all` |
| GET | `/api/backtests/duplicates` | doppelte Backtests gruppieren | `list_backtest_duplicates` |
| POST | `/api/backtests/deduplicate` | Duplikate automatisiert bereinigen | `deduplicate_backtests` |
| POST | `/api/backtests/delete-bulk` | Bulk-Delete | `delete_backtests_bulk` |
| GET | `/api/backtest/{backtest_id}` und `/api/backtests/{backtest_id}` | komplettes BacktestResult laden | `get_backtest` |
| DELETE | `/api/backtests/{backtest_id}` | Backtest löschen | `delete_single_backtest` |
| PATCH | `/api/backtests/{backtest_id}` | Backtest-Titel/Notes ändern | `rename_backtest` |
| GET | `/api/backtest/{backtest_id}/info` und `/api/backtests/{backtest_id}/info` | Metadaten-Payload | `get_backtest_info` |
| GET | `/api/backtest/{backtest_id}/stats` und `/api/backtests/{backtest_id}/stats` | KPI-Statistiken | `get_backtest_stats` |
| GET | `/api/backtest/{backtest_id}/equity` und `/api/backtests/{backtest_id}/equity` | Equity-Kurve | `get_equity_curve` |
| GET | `/api/backtests/{backtest_id}/equity-preview` | Mini-Equity für Listenansicht | `get_backtest_equity_preview` |
| GET | `/api/backtest/{backtest_id}/capm` | CAPM ggü. Benchmark | `get_backtest_capm` |
| GET | `/api/backtest/{backtest_id}/trades` und `/api/backtests/{backtest_id}/trades` | Trade-Liste | `get_backtest_trades` |
| GET | `/api/backtest/{backtest_id}/trade/{trade_nr}` | Trade+OHLCV+Indikator-Kontext | `get_trade_with_context` |
| GET | `/api/backtest/{backtest_id}/replay` | Replay-Payload über Symbole | `get_backtest_replay` |
| GET | `/api/backtest/{backtest_id}/ohlcv` | OHLCV-Daten je Symbol | `get_backtest_ohlcv` |
| GET | `/api/compare` | Multi-Backtest-Vergleich | `compare_backtests_endpoint` |
| POST | `/api/backtests/run` | Backtest-Job starten | `run_backtest` |
| GET | `/api/backtests/run/{job_id}/status` | Laufstatus/Progress | `get_backtest_run_status` |

#### Optimierung

| Methode | Pfad | Zweck | Handler |
|---|---|---|---|
| POST | `/api/optimizations/run` | Optuna-Job starten | `run_optimization_job` |
| GET | `/api/optimizations` | Optimierungsläufe listen | `list_optimizations` |
| GET | `/api/optimizations/{optimization_id}` | Detail eines Optimierungslaufs | `get_optimization` |

#### Portfolios

| Methode | Pfad | Zweck | Handler |
|---|---|---|---|
| GET | `/api/portfolios/compare` | Portfolios vergleichen | `compare_portfolios` |
| GET | `/api/portfolios` | Portfolio-Übersicht | `list_portfolios` |
| POST | `/api/portfolios` | Portfolio aus Backtests erzeugen | `create_portfolio` |
| GET | `/api/portfolios/{portfolio_id}` | Portfolio-Details | `get_portfolio` |
| DELETE | `/api/portfolios/{portfolio_id}` | Portfolio löschen | `delete_portfolio` |

#### Live

| Methode | Pfad | Zweck | Handler |
|---|---|---|---|
| GET | `/api/live/accounts` | erkannte Live-Konten | `get_live_accounts` |
| GET | `/api/live/deployments` | aktive/gespeicherte Deployments | `get_live_deployments` |
| POST | `/api/live/deployments` | Deployment anlegen | `create_live_deployment_endpoint` |
| PUT | `/api/live/deployments/{deployment_id}/status` | Status (`ACTIVE/PAUSED/STOPPED`) ändern | `update_live_deployment_status_endpoint` |
| DELETE | `/api/live/deployments/{deployment_id}` | Deployment löschen | `delete_live_deployment_endpoint` |
| GET | `/api/live/balances/{account_id}` | Balance/Equity-Zeitreihe | `get_live_balances` |
| GET | `/api/live/trades/{account_id}` | Executions/Slippage | `get_live_trades` |
| GET | `/api/live/stats/{account_id}` | aggregierte Live-Stats | `get_live_stats` |

#### Datenmanagement & Task-Steuerung

| Methode | Pfad | Zweck | Handler |
|---|---|---|---|
| GET | `/api/data/overview` | Datenkategorien-Übersicht | `data_overview` |
| GET | `/api/data/category/{category_name}` | Kategorie-Details | `data_category_details` |
| POST | `/api/data/cache/invalidate` | Cache invalidieren | `invalidate_data_cache` |
| POST | `/api/data/download` | Download-Task starten | `data_download` |
| GET | `/api/data/dukascopy/genesis` | Dukascopy-Verfügbarkeiten/Genesis | `get_dukascopy_genesis` |
| POST | `/api/data/resample` | Resample-Task starten | `data_resample` |
| POST | `/api/data/harvest` | Smart Harvest starten | `data_harvest` |
| POST | `/api/data/validate` | Validierungs-Task starten | `data_validate` |
| POST | `/api/data/calibrate` | Genesis-Kalibrierung starten | `data_calibrate` |
| GET | `/api/data/tasks` | alle Tasks listen | `list_data_tasks` |
| GET | `/api/data/tasks/{task_id}` | Task-Status | `get_data_task` |
| POST | `/api/data/tasks/{task_id}/restart` | Task neu starten | `restart_data_task` |
| POST | `/api/data/tasks/{task_id}/pause` | Task pausieren | `pause_data_task` |
| POST | `/api/data/tasks/{task_id}/resume` | Task fortsetzen | `resume_data_task` |
| POST | `/api/data/tasks/{task_id}/cancel` | Task abbrechen | `cancel_data_task` |
| GET | `/api/data/tasks/{task_id}/logs` | Task-Logs lesen | `get_data_task_logs` |
| DELETE | `/api/data/tasks/{task_id}` | abgeschlossenen Task entfernen | `delete_data_task` |
| DELETE | `/api/data/symbol` | Datendatei unter `DATA_DIR` löschen | `delete_symbol_data` |
| POST | `/api/agent/convert` | KI-Strategiekonvertierung starten | `agent_convert` |

#### Logs, Referenzdaten, Streams

| Methode | Pfad | Zweck | Handler |
|---|---|---|---|
| GET | `/api/logs` | verfügbare Logdateien listen | `list_logs` |
| GET | `/api/logs/{log_name}` | Loginhalt (tail/filter/search) | `get_log` |
| GET | `/api/symbols` | Symbol-/Kategorieübersicht | `list_symbols` |
| GET | `/api/benchmark/{symbol}` | Benchmark-Equity erzeugen | `get_benchmark` |
| WebSocket | `/ws/tasks/{job_id}` | Live-Tasklog-Streaming | `task_log_socket` |

### Authentifizierung / Autorisierung

Aktueller Zustand: **keine API-Authentifizierung/Autorisierung**.

- In `viewer/main.py` existieren keine Security-Dependencies (`Depends`, OAuth/JWT/Bearer etc.).
- CORS ist offen konfiguriert (`allow_origins=["*"]`, `allow_methods=["*"]`).
- Sensible Broker-Credentials werden aus Environment/.accounts-Datei gelesen (z. B. `live_runner/daemon.py`, `live_runner/ctrader_adapter.py`, `live_runner/alpaca_adapter.py`).

### Datenbank-Schema (Tabellen mit wichtigsten Feldern)

SQLModel-Tabellen sind in `shared/database.py` definiert:

| Tabelle | Wichtigste Felder | Zweck |
|---|---|---|
| `backtests` | `id`, `strategies` (JSON), `engine`, `data` (JSON), `parameters` (JSON), `stats` (JSON), `created_at` | Backtest-Metadaten |
| `trades` | `id`, `backtest_id` (FK), `trade_nr`, `symbol`, `direction`, `entry_time`, `entry_price`, `exits` (JSON), `pnl` | Einzeltrades |
| `equity_points` | `id`, `backtest_id` (FK), `time`, `equity` | Equity-Kurve |
| `drawings` | `id`, `backtest_id` (FK), `trade_nr`, `type`, `data` (JSON) | Chart-/Annotationselemente |
| `indicators` | `id`, `backtest_id` (FK), `series_nr`, `data` (JSON) | Indikator-Serien |
| `live_accounts` | `id`, `broker`, `name`, `ctid`, `broker_login` | Live-Konto-Master |
| `live_deployments` | `id`, `strategy_name`, `account_id` (FK), `symbols` (JSON), `timeframe`, `status`, `settings` (JSON) | Live-Deployments |
| `live_signals` | `id`, `deployment_id`, `symbol`, `action`, `theoretical_price`, `timestamp`, `metadata` (JSON) | Signale vor Broker-Execution |
| `live_executions` | `id`, `signal_id` (FK), `actual_price`, `filled_qty`, `commission`, `timestamp`, `broker_order_id` | Broker-Fills |
| `live_balances` | `id`, `account_id`, `timestamp`, `balance`, `equity` | Konto-Snapshots |
| `background_tasks` | `id`, `type`, `status`, `config_json`, `progress_message`, `name`, `description`, `source` | persistente Task-Historie |

### Externe Integrationen (inkl. `.env`-Variablen)

| Integration | Implementierung | Wichtige Variablen |
|---|---|---|
| NVIDIA NIM (LLM) | `runner/auto_agent.py` (OpenAI SDK, `base_url=https://integrate.api.nvidia.com/v1`) | `NVIDIA_API_KEY` |
| Dukascopy JForex | `data_tools/downloaders/jforex_dl.py` | `DUKASCOPY_USERNAME`, `DUKASCOPY_PASSWORD`, `DUKASCOPY_JNLP_URL`, `JFOREX_CLASSPATH`/`JFOREX_JARS`/`JFOREX_HOME` |
| Dukascopy Native Fallback | `data_tools/downloaders/dukascopy_native.py`, aufgerufen via `viewer/core.py::_start_jforex_task` | indirekt `DATA_DIR`, ggf. Dukascopy-Parameter |
| HuggingFace Datasets | `data_tools/downloaders/huggingface_dl.py` | `HF_TOKEN` (optional) |
| Dolthub | `data_tools/dolthub_manager.py`, `data_tools/downloaders/dolthub_dl.py` | keine festen Secrets im Kernfluss |
| Alpaca Live | `live_runner/alpaca_adapter.py`, `live_runner/daemon.py` | `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER`, plus `*_ACCn` Varianten |
| cTrader Live | `live_runner/ctrader_adapter.py`, `live_runner/account_discovery.py`, `live_runner/daemon.py` | `CTRADER_APP_ID`, `CTRADER_APP_SECRET`, `CTRADER_ACCESS_TOKEN`, `CTRADER_DEMO` |
| LEAN | `runner/engines/lean_adapter.py` | `LEAN_PROJECT_DIR` |
| Runtime-Grundkonfig | `shared/constants.py`, `.env.example` | `DATA_DIR`, `TRADING_DATA_DIR`, `LOG_DIR`, `LOG_LEVEL`, `UVICORN_HOST`, `UVICORN_PORT` |

## 5. Datenpipeline / Hintergrundprozesse

### Viewer-interne Task-Orchestrierung

- Task-State im Speicher + persistent in SQLite (`background_tasks`), siehe `viewer/core.py`:
  - `_create_task_record`, `_update_task_progress`, `_mark_task_finished`, `_serialize_task`.
- Prozess-/Thread-Start:
  - `_start_data_task` für generische Subprocess-Tasks.
  - `_start_jforex_task` mit nativer Dukascopy-Fallback-Logik.
  - `run_backtest` (Thread + Subprocess `runner.cli`).
  - `run_optimization_job` (Thread + Optuna-Ausführung).
  - `_queue_agent_task` / `_launch_agent_task` (AI-Konvertierung).
- Task-Steuerung: Pause/Resume/Cancel/Restart über `/api/data/tasks/*`.

### Startup- und Recovery-Prozesse

In `viewer/main.py` (`lifespan`):

- Prewarm-Tasks per `asyncio.create_task`:
  - `_prewarm_data_views_background`
  - `_prewarm_live_accounts_background`
  - `_prewarm_strategies_background`
  - `_prewarm_backtest_metadata_background`
  - `_prewarm_jforex_jvm_background`
- Markiert alte `queued/running` Tasks als `interrupted` und versucht Wiederaufnahme (`_resume_interrupted_tasks`).

### Datenmanagement-Pipeline

- Katalogaufbau: `data_tools/catalog_builder.py`.
- Validierung: `data_tools/validate.py`.
- Resampling: `data_tools/resample.py`.
- Downloader: `data_tools/downloaders/*` (JForex/Dukascopy, Dolthub, HuggingFace, HistData, cTrader-CSV).
- Smart Harvest: `scripts/adaptive_harvester.py` über `/api/data/harvest`.

### Live-Hintergrundprozess

- `live_runner/daemon.py::run_daemon` startet zwei dauerhafte asyncio-Loops:
  - `sync_deployments_loop` (Deployment-/Adapter-Sync)
  - `_poll_balances` (Balance-Polling, default 60s).

### Cronjobs / Scheduler

- Kein klassischer Cron im Repo.
- Stattdessen dauerhafte Loops/Threads/async Tasks (Viewer + Live-Daemon).

## 6. Bekannte Schwachstellen & TODOs

### 1) Frontend baut aktuell nicht

- `npm run build` schlägt fehl mit `TS2307`:
  - `frontend/src/components/CreateBacktestModal.tsx` importiert `../api/queries`.
  - `frontend/src/components/StrategiesProvider.tsx` importiert `../api/queries`.
  - `frontend/src/components/SymbolInput.tsx` importiert `../api/queries`.
- Datei `frontend/src/api/queries.ts` existiert im aktuellen Stand nicht.

### 2) React-Query-Integration inkonsistent

- `StrategiesProvider.tsx` verwendet `useQueryClient` (`@tanstack/react-query`), aber in `frontend/src/main.tsx` wird kein `QueryClientProvider` gesetzt.
- Das ist ein potenzieller Runtime-Fehler, selbst wenn die fehlende `queries`-Datei ergänzt wird.

### 3) Lint-Baseline ist stark rot

- `ruff check .` meldet aktuell **403** Verstöße (u. a. `E501`, `E402`, `F401`, `I001`, `F841`, `F821`, `W293`).
- Betroffen sind mehrere Kernmodule (`data_tools/*`, `runner/engines/*`, `viewer/*`, etc.).

### 4) yfinance-Downloadpfad ist inkonsistent

- `viewer/main.py::data_download` referenziert `data_tools.downloaders.yfinance_dl`.
- Laut `docs/DATA_SOURCES.md` gibt es aktuell **kein dediziertes** `yfinance_dl.py`.
- Ergebnis: `source="yfinance"` ist im aktuellen Codepfad voraussichtlich nicht lauffähig.

### 5) Auth/Security fehlt auf API-Ebene

- Keine AuthN/AuthZ für `/api/*` vorhanden.
- CORS ist offen (`allow_origins=["*"]`).
- Für produktive Nutzung fehlt ein Sicherheitskonzept (Token, Rollen, Rate-Limits, Secret-Management).

### 6) Sensitives Konfigurationsfile ist versioniert

- `.accounts.env` ist im Git-Index vorhanden (`git ls-files .accounts.env` liefert Treffer).
- Das ist ein hohes Risiko für Credential-Leaks; aktuell ignoriert `.gitignore` nur `.env`, nicht `.accounts.env`.

### 7) Teilweise provisorische Integrationen

- `runner/engines/lean_adapter.py` ist laut Modulkommentar ein „thin wrapper“; robuste LEAN-Orchestrierung ist nur eingeschränkt umgesetzt.
- `data_tools/downloaders/ctrader_dl.py` unterstützt primär CSV-Import und wirft ohne CSV explizit „API download not implemented“.

### 8) Positiver Baseline-Hinweis

- Backend-Teststatus ist aktuell gut: `pytest` lief mit **184 passed, 69 skipped**.

