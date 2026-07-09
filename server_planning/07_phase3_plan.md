# Phase 3 — Strategien, Daten, Infrastruktur & Features

> Ziel: Aus dem funktionierenden Framework (Phase 1+2) eine vollwertige Trading-Plattform machen — mit gesichteten Strategien, zuverlässigen Daten, automatisierten Alerts, Risk-Management und Container-Deployment.

---

## Nicht in Phase 3

| Feature | Warum nicht | Wann |
|---------|------------|------|
| **Frontend Auth** | Viewer ist lokal, Auth kommt erst wenn öffentlich erreichbar | Phase 4+ |
| **Zweite Disk** | 100 GB reichen mit LRU-Cache, kein akuter Engpass | Phase 4+ |
| **Domain + HTTPS** | Kein öffentlicher Zugriff geplant | Phase 4+ |

---

## Priorität 1: Strategien + Daten

### 1.1 Ausgangslage

- **~100 externe Strategien** ungesichtet (Pine, MT5, NT8, LEAN) aus GitHub, Discord, YouTube
- **~28 eigene Strategien** im Indikatoren-Repo (PineScript v5/v6, MQL5)
- **97 Parquet-Dateien** in `~/data/ohlcv/` (86 Symbole, 18 MB) — nur US-Equities + XAUUSD
- **Keine automatische Daten-Validierung** — Gaps und Duplikate unerkannt
- **catalog.json** listet Dateien, aber ohne Validierungs-Status oder Quellen-Metadaten

### 1.2 Strategie-Triage

**Ziel:** Die ~100 externen + ~28 eigenen Strategien sichten und in drei Kategorien einteilen.

**Triage-Kriterien:**
- Hat die Strategie eine klare Logik (nicht nur Curve-Fitting)?
- Ist sie auf unsere Assets anwendbar (US-Equities, Forex, Gold)?
- Welches Format (Pine/MT5/NT8/LEAN/Python)?
- Gibt es Backtest-Ergebnisse vom Autor?
- Wie komplex ist die Umsetzung (Entry/Exit-Regeln, Indikatoren)?

**Output: `~/Desktop/server_planning/strategy_triage.md`**

```markdown
## Vielversprechend (Top 10-20)
| # | Name | Quelle | Format | Logik | Assets | Komplexität | Notizen |
|---|------|--------|--------|-------|--------|-------------|---------|

## Vielleicht (20-40)
| # | Name | Quelle | Format | Logik | Assets | Komplexität | Notizen |

## Müll (Rest)
| # | Name | Quelle | Grund |
```

### 1.3 Daten-Download

**Neue Datenquellen anbinden** — je nachdem was die Top-Strategien brauchen:

| Quelle | Was | Format | Priorität |
|--------|-----|--------|-----------|
| **Dolthub** | Historische Options-Daten, Earnings, Fundamentals | SQL/CSV-Export | Hoch (wenn Strategien es brauchen) |
| **HuggingFace** | Große Tick-Datasets, ML-Features | Parquet (direkt) | Mittel |
| **Histdata** | Forex Tick/Minute-Daten (kostenlos) | CSV → Parquet | Hoch (für Forex-Strategien) |
| **cTrader** | Tick-Daten (via ML_tick_data_prepare) | CSV → Parquet | Mittel (Code existiert bereits) |

**Downloader-Scripts in `data_tools/`:**

```
data_tools/
├── catalog_builder.py          # existiert
├── migrate_csv.py              # existiert
├── downloaders/
│   ├── base.py                 # Downloader-Interface
│   ├── histdata_dl.py          # Forex Minute/Tick
│   ├── dolthub_dl.py           # Dolt-Repos klonen + exportieren
│   ├── huggingface_dl.py       # HF Datasets API
│   └── ctrader_dl.py           # Portiert von ML_tick_data_prepare
└── validate.py                 # Neu: Data-Validierung
```

### 1.4 Data-Validierung

**`data_tools/validate.py`** — automatisierte Qualitätsprüfung für alle Parquet-Dateien:

```python
@dataclass
class ValidationResult:
    path: str
    symbol: str
    timeframe: str
    status: str           # "ok", "warning", "error"
    total_rows: int
    date_range: tuple[str, str]
    gaps: list[dict]      # {"start": ..., "end": ..., "expected_bars": N}
    duplicates: int
    null_counts: dict     # {"open": 0, "high": 0, ...}
    ohlc_violations: int  # high < low, etc.
    zero_volume_pct: float
```

**Prüfungen:**
1. **Gap-Detection** — Fehlende Bars erkennen (Wochenenden/Feiertage ausgenommen)
2. **Duplikate** — Doppelte Timestamps
3. **OHLC-Konsistenz** — `high >= max(open, close)`, `low <= min(open, close)`
4. **Null/NaN-Werte** — Unvollständige Zeilen
5. **Zero-Volume** — Verdächtig viele Bars ohne Volumen
6. **Zeitreihen-Kontinuität** — Monoton steigende Timestamps

**CLI:**
```bash
python -m data_tools.validate                    # Alles prüfen
python -m data_tools.validate --symbol AAPL      # Einzelnes Symbol
python -m data_tools.validate --report ~/data/validation_report.json
```

### 1.5 Catalog-Erweiterung

**catalog.json erweitern** um Quellen-Metadaten und Validierungs-Status:

```json
{
  "datasets": [
    {
      "symbol": "XAUUSD",
      "source": "dukascopy",
      "asset_class": "forex",
      "timeframes": ["15m", "1h"],
      "start": "2020-11-08",
      "end": "2026-02-07",
      "rows": {"15m": 123794, "1h": 31018},
      "path": "ohlcv/dukascopy/forex/XAUUSD/",
      "source_meta": {
        "url": "https://www.dukascopy.com/...",
        "downloaded_at": "2026-02-24T14:00:00Z",
        "license": "free",
        "update_frequency": "manual"
      },
      "validation": {
        "last_checked": "2026-03-15T10:00:00Z",
        "status": "ok",
        "gaps": 0,
        "duplicates": 0,
        "warnings": []
      },
      "storage": "local"
    }
  ]
}
```

**Neue Felder:**
- `source_meta` — Woher kommen die Daten, wann heruntergeladen, Update-Frequenz
- `validation` — Letzter Validierungs-Lauf, Status, Probleme
- `storage` — `"local"` | `"cached"` | `"remote"` (vorbereitet für Priorität 3: LRU-Cache)

### 1.6 Aufgabenliste

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 1 | **Strategie-Triage** — Alle ~100 externen + ~28 eigenen Strategien sichten, kategorisieren (vielversprechend/vielleicht/müll), Top 10-20 identifizieren. Output: `strategy_triage.md` | L | — |
| 2 | **Daten-Bedarf ermitteln** — Aus der Triage: welche Symbole, Timeframes, Asset-Klassen brauchen die Top-Strategien? Fehlende Daten identifizieren. | S | 1 |
| 3 | **Downloader-Interface** — `data_tools/downloaders/base.py`: ABC mit `download()`, `list_available()`, Parquet-Output | S | — |
| 4 | **Histdata-Downloader** — Forex Minute-Daten (EURUSD, GBPUSD, USDJPY, XAUUSD, etc.) | M | 3 |
| 5 | **Dolthub-Downloader** — Dolt-Repos klonen, SQL-Query, CSV-Export → Parquet | M | 3 |
| 6 | **HuggingFace-Downloader** — HF Datasets API, Parquet-Download | S | 3 |
| 7 | **cTrader-Downloader portieren** — Code aus `ML_tick_data_prepare` übernehmen, an Interface anpassen | S | 3 |
| 8 | **Data-Validierung** — `data_tools/validate.py`: Gap-Detection, Duplikate, OHLC-Konsistenz, CLI | M | — |
| 9 | **Catalog-Erweiterung** — `catalog_builder.py` erweitern um `source_meta`, `validation`, `storage` Felder | S | 8 |
| 10 | **Daten herunterladen** — Top-Strategie-Daten tatsächlich laden, validieren, in ~/data/ ablegen | M | 2, 4–7, 8 |

### 1.7 Reihenfolge

```
1 (Triage) ──→ 2 (Daten-Bedarf)
                    └──→ 10 (Download ausführen)
3 (Downloader IF) ──→ 4 (Histdata)
                  ├──→ 5 (Dolthub)
                  ├──→ 6 (HuggingFace)
                  └──→ 7 (cTrader)
8 (Validierung)  ──→ 9 (Catalog-Erweiterung)
                 └──→ 10 (Download ausführen)
```

---

## Priorität 2: PyneCore Adapter

### 2.1 PyneCore-Recherche

**Was ist PyneCore?**
- Python-Framework von PyneSys (GitHub: `PyneSys/pynecore`, ~99 Stars, aktiv maintained)
- Nutzt **AST-Transformationen** um Python-Code wie Pine Script verhalten zu lassen
- Kann Pine Script (.pine) **kompilieren** in Python (.py) — benötigt **PyneSys API-Key** (kostenlos?)
- Oder: Strategien direkt in Python schreiben mit Pine-ähnlicher API
- Eigenes Datenformat: `.ohlcv` (muss aus CSV/Parquet konvertiert werden)
- **Pine Script v6** Support

**Unterstützte Pine-Features (verifiziert in Docs):**
- `ta.rsi()`, `ta.sma()`, `ta.ema()`, `ta.atr()`, `ta.bb()` (Bollinger Bands)
- `ta.crossover()`, `ta.crossunder()`
- `strategy.entry()`, `strategy.exit()`, `strategy.close()`
- `input.int()`, `input.float()`, `input.string()`
- `plot()`, `hline()`, `color`
- Stop-Loss, Take-Profit, Trailing Stop via `strategy.exit()`
- Pyramiding, Commission-Settings
- `strategy.percent_of_equity`, `strategy.cash` Position Sizing

**Offene Fragen (zu klären bei Integration):**
- Wie extrahiert man Trade-Ergebnisse programmatisch (nicht nur CLI-Output)?
- Unterstützt es `request.security()` (Multi-Timeframe)?
- Wie verhält sich die API-Key-Abhängigkeit? Kompilierung = einmalig oder bei jedem Run?
- Performance bei vielen Bars (100k+)?
- Welche Pine-Features fehlen (Arrays, Maps, Custom Types, etc.)?

**Bewertung:** PyneCore sieht **vielversprechend** aus für die ~28 Pine-Strategien im Indikatoren-Repo. Die AST-Transformation ist ein cleverer Ansatz. Risiko: API-Key-Abhängigkeit für Kompilierung könnte ein Showstopper sein falls kostenpflichtig oder instabil.

### 2.2 Integration als Engine-Adapter

```
runner/engines/
├── base.py              # existiert
├── vectorbt_adapter.py  # existiert
├── lean_adapter.py      # existiert
└── pynecore_adapter.py  # NEU
```

**PyneCore-Adapter Architektur:**

```python
class PyneCoreAdapter(EngineAdapter):
    """Runs Pine Script strategies via PyneCore."""

    name = "pynecore"

    def run(self, config: BacktestConfig, strategy_path: str) -> BacktestResult:
        # 1. Daten vorbereiten: Parquet → .ohlcv (PyneCore-Format)
        ohlcv_path = self._convert_data(config)

        # 2. Strategie ausführen
        #    a) .pine Datei → erst kompilieren (pyne compile)
        #    b) .py Datei → direkt ausführen (pyne run)
        raw_result = self._execute(strategy_path, ohlcv_path)

        # 3. PyneCore-Output → Universal Trade Format
        return self._to_universal(raw_result, config)
```

**Daten-Konvertierung:**
- PyneCore nutzt ein eigenes `.ohlcv`-Format
- `DataConverter` Klasse in PyneCore kann CSV → .ohlcv konvertieren
- Wir brauchen: Parquet → CSV (temporär) → .ohlcv, oder direkt Parquet → .ohlcv
- Temporäre .ohlcv-Dateien in `/tmp/pynecore/` mit Cleanup

**Pine-Strategien testen:**
- Indikatoren-Repo klonen: `git clone github.com/itsjustFred/Indikatoren`
- Pine-Strategien identifizieren (nicht alle sind Strategien, manche sind nur Indikatoren)
- Mit PyneCore kompilieren und gegen vorhandene OHLCV-Daten laufen lassen
- Ergebnisse ins Universal Format konvertieren und im Viewer anzeigen

### 2.3 Aufgabenliste

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 11 | **PyneCore installieren + evaluieren** — pip install, API-Key besorgen, Test mit einfacher SMA-Strategie. Klären: API-Key-Kosten, Trade-Output-Format, Performance, Feature-Gaps | M | — |
| 12 | **Daten-Konverter** — Parquet → .ohlcv Konvertierung (PyneCore DataConverter oder eigenes Script) | S | 11 |
| 13 | **PyneCore Engine-Adapter** — `runner/engines/pynecore_adapter.py`: Implementiert EngineAdapter Interface, führt .pine/.py Strategien aus, konvertiert Output → Universal Format | L | 11, 12 |
| 14 | **Indikatoren-Repo klonen + testen** — Repo klonen, Pine-Strategien identifizieren (vs. reine Indikatoren), Top 5 mit PyneCore testen | M | 13 |
| 15 | **Pine → Universal Converter** — Falls nötig: Script das TradingView-Strategy-Export (CSV) ins Universal Format konvertiert (Fallback wenn PyneCore nicht alle Strategien unterstützt) | S | — |
| 16 | **Dokumentation** — `docs/pynecore_guide.md`: Wie man Pine-Strategien einbindet, Datenformat, Limitationen, Troubleshooting | S | 13, 14 |

### 2.4 Reihenfolge

```
11 (Evaluierung) ──→ 12 (Daten-Konverter) ──→ 13 (Adapter)
                                              └──→ 14 (Strategien testen)
                                              └──→ 16 (Doku)
15 (TV-Export Converter) — parallel, unabhängig
```

---

## Priorität 3: LRU-Cache für große Datasets

### 3.1 Problem

- Große Remote-Datasets (HuggingFace, Dolthub) passen nicht komplett auf die 100 GB Disk
- Aktuell: alles lokal oder gar nicht — kein Mittelweg
- Ziel: On-Demand Download, automatisches Eviction wenn Disk voll

### 3.2 Architektur

```
data_tools/
├── cache_manager.py        # NEU: Cache-Logik
├── catalog_builder.py      # Erweitert um storage-Status
└── downloaders/
    └── ...                 # Downloader nutzen Cache-Manager
```

**Cache-Manager:**

```python
class CacheManager:
    """LRU Cache für Remote-Datasets."""

    def __init__(self, cache_dir: Path, max_size_gb: float = 50.0):
        self.cache_dir = cache_dir        # ~/data/cache/
        self.max_size_gb = max_size_gb
        self.index_path = cache_dir / "cache_index.json"

    def get(self, dataset_key: str) -> Path | None:
        """Gibt lokalen Pfad zurück wenn cached, sonst None."""

    def fetch(self, dataset_key: str, downloader: Downloader) -> Path:
        """Download + Cache. Evicted älteste Einträge wenn nötig."""

    def evict_lru(self, needed_bytes: int):
        """Löscht die am längsten nicht genutzten Datasets."""

    def status(self) -> dict:
        """Cache-Statistiken: Größe, Einträge, Hit-Rate."""
```

**Cache-Index (`~/data/cache/cache_index.json`):**

```json
{
  "entries": [
    {
      "key": "huggingface/tick_data/BTCUSDT/1m",
      "path": "cache/hf_btcusdt_1m.parquet",
      "size_bytes": 524288000,
      "downloaded_at": "2026-03-20T10:00:00Z",
      "last_accessed": "2026-03-25T14:30:00Z",
      "source_url": "https://huggingface.co/datasets/...",
      "checksum": "sha256:abc123..."
    }
  ],
  "total_size_bytes": 2147483648,
  "max_size_bytes": 53687091200
}
```

**Catalog-Integration:**
- `storage: "local"` — Dauerhaft in `~/data/ohlcv/`, wird nie evicted
- `storage: "cached"` — In `~/data/cache/`, kann evicted werden
- `storage: "remote"` — Nur in catalog.json registriert, nicht lokal vorhanden

**Config (`~/.trading-platform/config.yaml` oder ENV):**
```yaml
cache:
  max_size_gb: 50
  cache_dir: ~/data/cache/
  eviction_policy: lru
```

### 3.3 Aufgabenliste

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 17 | **Cache-Manager** — `data_tools/cache_manager.py`: LRU-Logik, Index-File, Eviction, Size-Tracking | M | — |
| 18 | **Cache-Dir Setup** — `~/data/cache/` anlegen, Config für max_size_gb (ENV oder config.yaml) | S | 17 |
| 19 | **Downloader-Integration** — Downloader nutzen `CacheManager.fetch()` statt direktem Schreiben nach `~/data/ohlcv/` für große Remote-Datasets | S | 3, 17 |
| 20 | **Catalog-Erweiterung** — `storage` Feld in catalog.json (`local`/`cached`/`remote`), Catalog-Builder erkennt Cache-Einträge | S | 9, 17 |
| 21 | **Cache-CLI** — `python -m data_tools.cache status`, `python -m data_tools.cache evict --size 10gb`, `python -m data_tools.cache clear` | S | 17 |

### 3.4 Reihenfolge

```
17 (Cache-Manager) ──→ 18 (Setup)
                   ├──→ 19 (Downloader-Integration)
                   ├──→ 20 (Catalog-Erweiterung)
                   └──→ 21 (Cache-CLI)
```

---

## Priorität 4: Alerts + Auto-Restart

### 4.1 Notification-Service

**`shared/notifications.py`** — Leichtgewichtiger Notification-Service:

```python
class Notifier:
    """Sendet Notifications über verschiedene Kanäle."""

    def __init__(self, channels: list[NotificationChannel]):
        self.channels = channels

    def send(self, event: Event):
        for channel in self.channels:
            channel.send(event)

class TelegramChannel(NotificationChannel):
    def __init__(self, bot_token: str, chat_id: str): ...
    def send(self, event: Event): ...

class DiscordChannel(NotificationChannel):
    def __init__(self, webhook_url: str): ...
    def send(self, event: Event): ...
```

**Events:**

| Event | Trigger | Nachricht |
|-------|---------|-----------|
| `backtest_complete` | Runner CLI nach Backtest | "✅ Backtest `{strategy}` fertig: {trades} Trades, Sharpe {sharpe}" |
| `backtest_failed` | Runner CLI bei Error | "❌ Backtest `{strategy}` fehlgeschlagen: {error}" |
| `bot_trade` | stockkit Bot nach Trade | "📈 {symbol} {direction} Entry@{price} — {strategy}" |
| `bot_error` | stockkit Bot bei Fehler | "⚠️ Bot `{bot}` Fehler: {error}" |
| `health_check_fail` | Health-Check Script | "🔴 Service `{service}` antwortet nicht" |
| `risk_alert` | Risk-Service (Prio 5) | "🛑 Drawdown-Limit erreicht: {drawdown}%" |

**Config (`.env`):**
```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
DISCORD_WEBHOOK_URL=...
NOTIFICATION_CHANNELS=telegram,discord
```

### 4.2 Systemd Services

**Drei Services:**

```ini
# /etc/systemd/system/stockkit-ramjet.service
[Unit]
Description=StockKit Ram Jet Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Desktop/stockkit_ai_bot
ExecStart=/home/ubuntu/Desktop/stockkit_ai_bot/.venv/bin/python -m src.bots.ram_jet.main
Restart=always
RestartSec=30
EnvironmentFile=/home/ubuntu/Desktop/stockkit_ai_bot/.env

[Install]
WantedBy=multi-user.target
```

Analog für: `stockkit-rocketfuel.service`, `trading-viewer.service`

**Auto-Restart:**
- `Restart=always` + `RestartSec=30` — startet nach 30s neu bei Crash
- `WantedBy=multi-user.target` — startet beim Server-Boot

### 4.3 Health-Checks

**`scripts/health_check.py`:**

```python
SERVICES = [
    {"name": "viewer", "check": "http://localhost:5556/api/backtests", "type": "http"},
    {"name": "stockkit-ramjet", "check": "systemctl is-active stockkit-ramjet", "type": "systemd"},
    {"name": "stockkit-rocketfuel", "check": "systemctl is-active stockkit-rocketfuel", "type": "systemd"},
]
```

- Prüft periodisch (Cronjob alle 5 Minuten)
- Sendet Notification bei Ausfall
- Schreibt Status nach `~/logs/health.log`

### 4.4 Aufgabenliste

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 22 | **Notification-Modul** — `shared/notifications.py`: Telegram + Discord Channels, Event-Klassen, Config aus .env | M | — |
| 23 | **Runner-Integration** — Runner CLI sendet `backtest_complete` / `backtest_failed` Events nach jedem Run | S | 22 |
| 24 | **Systemd Service Files** — `stockkit-ramjet.service`, `stockkit-rocketfuel.service`, `trading-viewer.service` erstellen und enablen | M | — |
| 25 | **Auto-Restart testen** — Services starten, manuell killen, prüfen ob sie wiederkommen. Reboot-Test. | S | 24 |
| 26 | **Health-Check Script** — `scripts/health_check.py`: Prüft HTTP + Systemd Services, sendet Notifications bei Ausfall | S | 22, 24 |
| 27 | **Cronjob einrichten** — Health-Check alle 5 Minuten via crontab | S | 26 |

### 4.5 Reihenfolge

```
22 (Notification-Modul) ──→ 23 (Runner-Integration)
                        └──→ 26 (Health-Check) ──→ 27 (Cronjob)
24 (Systemd Services)   ──→ 25 (Testen)
                        └──→ 26 (Health-Check)
```

---

## Priorität 5: Risk-Management

### 5.1 Architektur

**Risk-Service als eigener Prozess** — sitzt zwischen Signal-Quelle und Order-Execution:

```
Discord Signal → stockkit Bot → [Risk-Service] → Alpaca API
                                      ↑
                               Risk-Regeln prüfen
                               Portfolio-Status laden
```

**Implementierung:** `risk/` Modul im Monorepo:

```
risk/
├── __init__.py
├── service.py          # Risk-Service (Unix Socket oder HTTP)
├── rules.py            # Risk-Regeln (Position Limits, Drawdown, Korrelation)
├── portfolio.py        # Portfolio-State aus Alpaca API lesen
└── config.py           # Risk-Config
```

### 5.2 Risk-Regeln

```python
@dataclass
class RiskConfig:
    max_position_size_usd: float = 5000.0       # Max $ pro Position
    max_total_positions: int = 10                # Max gleichzeitige Positionen
    max_daily_drawdown_pct: float = 3.0          # Pausiert bei 3% Tages-Drawdown
    max_total_drawdown_pct: float = 10.0         # Pausiert bei 10% Gesamt-Drawdown
    correlation_threshold: float = 0.7           # Warnt bei Korrelation > 0.7
    max_correlated_positions: int = 3            # Max Positionen im gleichen Sektor/Korrelationscluster

class RiskCheck:
    def check_order(self, order: OrderRequest, portfolio: Portfolio) -> RiskResult:
        """
        Returns:
            RiskResult(approved=True/False, reason="...", warnings=[...])
        """
```

**Checks:**
1. **Position Limit** — `order.value <= max_position_size_usd`
2. **Position Count** — `len(portfolio.open_positions) < max_total_positions`
3. **Drawdown Kill-Switch** — Tages-PnL und Gesamt-PnL gegen Limits prüfen
4. **Korrelations-Check** — Neues Symbol gegen bestehende Positionen prüfen (Sektor-basiert als Proxy, echte Korrelation wäre teurer)

### 5.3 Integration mit stockkit

**Option A (empfohlen): Library-Call**
- stockkit importiert `risk.rules.RiskCheck` direkt
- Kein separater Prozess, weniger Komplexität
- Wird vor jedem `submit_order()` aufgerufen

**Option B: Separater Service**
- Risk-Service läuft als eigener Prozess (HTTP auf localhost:9000)
- stockkit fragt vor Order-Submission: `POST /api/check {"symbol": "CRWD", "value": 5000}`
- Vorteil: Unabhängig vom Bot-Prozess, kann zentrales Dashboard haben
- Nachteil: Mehr Infrastruktur, Single Point of Failure

**Empfehlung:** Start mit **Option A** (Library), später optional zu Option B upgraden wenn mehrere Bots laufen.

### 5.4 Aufgabenliste

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 28 | **Risk-Config** — `risk/config.py`: Dataclass mit allen Limits, ladbar aus .env oder config.yaml | S | — |
| 29 | **Portfolio-State** — `risk/portfolio.py`: Liest offene Positionen + PnL aus Alpaca API (oder aus stockkit bot_state JSON) | M | — |
| 30 | **Risk-Regeln** — `risk/rules.py`: Position Limits, Drawdown Kill-Switch, Korrelations-Check (Sektor-basiert) | M | 28, 29 |
| 31 | **Risk-Service API** — `risk/service.py`: Einfacher HTTP-Server oder Library-Interface, `check_order()` → approved/rejected + reason | M | 30 |
| 32 | **stockkit Integration** — Guide + Code-Snippet wie stockkit den Risk-Check einbaut (vor `submit_order()`) | S | 31 |
| 33 | **Drawdown-Monitor** — Laufender Check: liest PnL periodisch, sendet Alert + pausiert Bots bei Limit | M | 22, 30 |

### 5.5 Reihenfolge

```
28 (Config)     ──→ 30 (Regeln)  ──→ 31 (Service)  ──→ 32 (stockkit Integration)
29 (Portfolio)  ──┘              └──→ 33 (Drawdown-Monitor)
```

---

## Priorität 6: Docker

### 6.1 Container-Strategie

**Drei Container:**

| Container | Basis-Image | Ports | Volumes |
|-----------|-------------|-------|---------|
| `trading-viewer` | python:3.12-slim | 5556 | ~/data/ (readonly) |
| `trading-runner` | python:3.12-slim | — | ~/data/ (read-write) |
| `trading-risk` | python:3.12-slim | 9000 | — |

**Shared:**
- `~/data/` als Volume gemounted (wie seit Phase 1 geplant)
- `.env` Files für Secrets
- `~/logs/` als Log-Volume

### 6.2 Dateien

```
docker/
├── Dockerfile.viewer
├── Dockerfile.runner
├── Dockerfile.risk
└── docker-compose.yml
```

**docker-compose.yml:**

```yaml
services:
  viewer:
    build:
      context: .
      dockerfile: docker/Dockerfile.viewer
    ports:
      - "5556:5556"
    volumes:
      - ~/data:/data:ro
      - ~/logs:/logs
    env_file: .env
    restart: unless-stopped

  runner:
    build:
      context: .
      dockerfile: docker/Dockerfile.runner
    volumes:
      - ~/data:/data
      - ~/logs:/logs
    env_file: .env

  risk:
    build:
      context: .
      dockerfile: docker/Dockerfile.risk
    ports:
      - "9000:9000"
    env_file: .env
    restart: unless-stopped
```

### 6.3 Aufgabenliste

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 34 | **Dockerfile.viewer** — Multi-Stage Build, pip install, Copy viewer/ + shared/ | S | — |
| 35 | **Dockerfile.runner** — Wie Viewer, plus runner/ + strategies/ + data_tools/ | S | — |
| 36 | **Dockerfile.risk** — Wie Viewer, plus risk/ | S | 31 |
| 37 | **docker-compose.yml** — Alle drei Services, Volumes, Ports, Env-Files, Restart-Policy | M | 34, 35, 36 |
| 38 | **Testen** — Build, run, Viewer erreichbar, Runner kann Backtest ausführen, Daten-Volume funktioniert | M | 37 |
| 39 | **Deployment-Doku** — `docs/docker_guide.md`: Build, Run, Logs, Update-Prozess | S | 38 |

### 6.4 Reihenfolge

```
34 (Dockerfile.viewer) ──┐
35 (Dockerfile.runner)  ──┼──→ 37 (docker-compose) ──→ 38 (Testen) ──→ 39 (Doku)
36 (Dockerfile.risk)   ──┘
```

---

## Priorität 7: ML & Advanced

### 7.1 Features

Diese Features sind Nice-to-have und werden nur angefangen wenn Priorität 1-6 stabil laufen.

| Feature | Beschreibung | Komplexität |
|---------|-------------|-------------|
| **ML-Strategie-Integration** | `trading_ml` Repo als Engine-Adapter anbinden. ML-Modelle generieren Signale → Universal Trade Format. Feature-Store in `~/data/features/` (Parquet). | L |
| **Parameter-Sensitivity** | Runner CLI: `--sweep param=start:end:step`. Erzeugt N Backtests, Multi-Result-JSON. Viewer: Heatmap für Parameter-Empfindlichkeit. | M |
| **Walk-Forward Testing** | Rolling-Window Backtests: Train auf Window N, Test auf Window N+1. Re-Optimierung der Parameter pro Window. Output: Performance-Stabilität über Zeit. | L |
| **Live-Performance Dashboard** | Viewer erweitern: Offene Positionen, Tages-PnL, Equity in Echtzeit. Liest stockkit `bot_state_*.json`. WebSocket oder Polling. | M |
| **Deklaratives Strategie-Format** | YAML → Entry/Exit-Regeln. Parser konvertiert zu vectorbt-Signalen. Senkt Einstiegshürde für einfache Strategien massiv. | M |

### 7.2 Aufgabenliste

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 40 | **ML Engine-Adapter** — `runner/engines/ml_adapter.py`: trading_ml als Adapter, Feature-Loading, Prediction → Signale → Trades | L | 13 (Adapter-Interface) |
| 41 | **Parameter-Sweep CLI** — `python -m runner.cli run --sweep rsi_period=5:20:1` → N Backtests, aggregiertes Ergebnis | M | — |
| 42 | **Sweep-Viewer** — Heatmap / Tabelle im Viewer für Parameter-Sensitivity Ergebnisse | M | 41 |
| 43 | **Walk-Forward Engine** — Rolling-Window Logik, Re-Optimierung, Stability-Report | L | 41 |
| 44 | **Live-Dashboard** — Viewer-Erweiterung: Real-time Bot-Status, offene Positionen, PnL | M | — |
| 45 | **YAML-Strategie-Parser** — YAML → vectorbt Signale, mit ta-lib / pandas_ta für Indikatoren | M | — |

### 7.3 Reihenfolge

```
40 (ML-Adapter)         — unabhängig, braucht trading_ml Repo
41 (Sweep CLI) ──→ 42 (Sweep-Viewer)
              └──→ 43 (Walk-Forward)
44 (Live-Dashboard)     — unabhängig
45 (YAML-Parser)        — unabhängig
```

---

## Gesamtübersicht: Abhängigkeiten zwischen Prioritäten

```
Prio 1 (Strategien + Daten)
├──→ Prio 2 (PyneCore) — braucht Daten + Strategien-Liste aus Triage
├──→ Prio 3 (LRU-Cache) — baut auf Downloader + Catalog auf
└──→ Prio 5 (Risk) — braucht Verständnis der gehandelten Strategien

Prio 4 (Alerts + Auto-Restart) — unabhängig, kann parallel zu Prio 1-3

Prio 5 (Risk) ──→ Prio 6 (Docker) — Risk-Service als Container

Prio 7 (ML & Advanced) — unabhängig, nach Stabilisierung von 1-6
```

---

## Zusammenfassung

| Priorität | Tasks | Komplexität | Kern-Deliverables |
|-----------|-------|-------------|-------------------|
| **1: Strategien + Daten** | 10 Tasks (1L, 4M, 5S) | **Hauptarbeit** | Triage-Dokument, 4 Downloader, Validierung, Catalog v2 |
| **2: PyneCore** | 6 Tasks (1L, 2M, 3S) | Mittel | Engine-Adapter, Pine-Strategien lauffähig |
| **3: LRU-Cache** | 5 Tasks (1M, 4S) | Klein-Mittel | Cache-Manager, On-Demand Download, Eviction |
| **4: Alerts + Restart** | 6 Tasks (2M, 4S) | Mittel | Telegram/Discord, Systemd, Health-Checks |
| **5: Risk-Management** | 6 Tasks (3M, 3S) | Mittel | Position Limits, Drawdown Kill-Switch, Korrelation |
| **6: Docker** | 6 Tasks (2M, 4S) | Mittel | 3 Container, docker-compose, Deployment |
| **7: ML & Advanced** | 6 Tasks (2L, 4M) | Groß (optional) | ML-Adapter, Sweep, Walk-Forward, YAML |
| **Gesamt** | **45 Tasks** | | |
