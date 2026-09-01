import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Download,
  RefreshCw,
  Search,
  Table2,
  AlertTriangle,
} from "lucide-react";

import { getMatrixGrid } from "../api/client";
import { Badge } from "../components/Badge";
import type { Trade } from "../api/types";

type MatrixStatus = string;

interface BlockedStrategy {
  strategy: string;
  status: MatrixStatus;
  ok?: number | null;
  failed?: number | null;
  requested?: number | null;
  trades?: number | null;
  blocked_reason?: string | null;
  updated_at?: string | null;
}

interface StrategySummary {
  strategy: string;
  status: MatrixStatus;
  ok: number;
  failed: number;
  skipped_existing: number;
  requested: number;
  trades: number;
  count: number;
  total_trades: number;
  total_pnl?: number | null;
  avg_sharpe?: number | null;
  best_sharpe?: number | null;
  best_sharpe_combo?: { symbol: string; timeframe: string } | null;
  avg_cagr?: number | null;
  best_cagr?: number | null;
  best_cagr_combo?: { symbol: string; timeframe: string } | null;
  avg_psr?: number | null;
  best_psr?: number | null;
  best_psr_combo?: { symbol: string; timeframe: string } | null;
  avg_max_drawdown_pct?: number | null;
  worst_max_drawdown_pct?: number | null;
  worst_drawdown_combo?: { symbol: string; timeframe: string } | null;
  avg_net_profit_pct?: number | null;
  best_net_profit_pct?: number | null;
  best_profit_combo?: { symbol: string; timeframe: string } | null;
  completed_at?: string | null;
  updated_at?: string | null;
  matrix_csv?: string | null;
}

interface MatrixRow {
  strategy: string;
  status: MatrixStatus;
  symbol: string;
  timeframe: string;
  run_id: string;
  backtest_id?: string | null;
  backtest_url?: string | null;
  trades_url?: string | null;
  trade_data_available?: boolean | null;
  trades?: Trade[];
  trade_sample_count?: number;
  output?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_minutes?: number | null;
  total_trades: number;
  total_pnl?: number | null;
  net_profit_pct?: number | null;
  sharpe?: number | null;
  cagr?: number | null;
  max_drawdown_pct?: number | null;
  win_rate?: number | null;
  psr?: number | null;
  daily_return_count?: number | null;
  returncode?: number | null;
  matrix_csv?: string | null;
}

interface MatrixGridOverview {
  generated_at: string;
  source: string;
  report_started_at?: string | null;
  report_updated_at?: string | null;
  counts: {
    strategies_done: number;
    strategies_blocked: number;
    rows: number;
    ok_rows: number;
    failed_rows: number;
    symbols: string[];
  };
  strategy_summaries: StrategySummary[];
  blocked_strategies: BlockedStrategy[];
  rows: MatrixRow[];
}

type SortKey =
  | "strategy"
  | "symbol"
  | "timeframe"
  | "sharpe"
  | "cagr"
  | "max_drawdown_pct"
  | "psr"
  | "total_trades"
  | "net_profit_pct"
  | "win_rate"
  | "total_pnl";

type SortDirection = "asc" | "desc";
type ViewMode = "table" | "grid";
type GridMetric =
  | "sharpe"
  | "psr"
  | "net_profit_pct"
  | "max_drawdown_pct"
  | "total_trades";

const GRID_METRIC_OPTIONS: Array<{ value: GridMetric; label: string }> = [
  { value: "sharpe", label: "Sharpe" },
  { value: "psr", label: "Probabilistic Sharpe Ratio" },
  { value: "net_profit_pct", label: "Net Profit %" },
  { value: "max_drawdown_pct", label: "Max Drawdown" },
  { value: "total_trades", label: "Trades" },
];

const TIMEFRAME_ORDER: Record<string, number> = {
  "1m": 1,
  "5m": 2,
  "15m": 3,
  "1h": 4,
  "4h": 5,
  "1d": 6,
};

const PAGE_SIZE = 100;

function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "N/A";
  return new Intl.NumberFormat("de-DE", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

function formatPctFraction(
  value: number | null | undefined,
  digits = 2,
): string {
  if (value == null || !Number.isFinite(value)) return "N/A";
  const signed = value >= 0 ? "+" : "";
  return `${signed}${(value * 100).toFixed(digits)}%`;
}

function formatPctPoints(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "N/A";
  const signed = value >= 0 ? "+" : "";
  return `${signed}${value.toFixed(digits)}%`;
}

function formatMetric(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "N/A";
  return value.toFixed(digits);
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "N/A";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("de-DE");
}

function statusColor(
  status: MatrixStatus,
): "green" | "red" | "yellow" | "blue" | "purple" | "muted" {
  if (status === "ok") return "green";
  if (status === "failed") return "red";
  if (status === "blocked" || status === "missing_grid_summary")
    return "yellow";
  return "muted";
}

function sharpeColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value))
    return "var(--color-text-muted)";
  if (value >= 1) return "#3fb950";
  if (value >= 0) return "#d29922";
  return "#f85149";
}

function psrColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value))
    return "var(--color-text-muted)";
  if (value >= 0.95) return "#3fb950";
  if (value >= 0.5) return "#d29922";
  return "#f85149";
}

function cagrColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value))
    return "var(--color-text-muted)";
  return value >= 0 ? "#3fb950" : "#f85149";
}

function drawdownColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value))
    return "var(--color-text-muted)";
  if (value >= -0.05) return "#3fb950";
  if (value >= -0.15) return "#d29922";
  return "#f85149";
}

function gridMetricColor(
  value: number | null | undefined,
  metric: GridMetric,
): string {
  if (metric === "max_drawdown_pct") return drawdownColor(value);
  if (metric === "psr") return psrColor(value);
  if (metric === "net_profit_pct") return cagrColor(value);
  if (metric === "sharpe") return sharpeColor(value);
  return value == null || !Number.isFinite(value)
    ? "var(--color-text-muted)"
    : "#3fb950";
}

function formatGridMetric(
  value: number | null | undefined,
  metric: GridMetric,
): string {
  if (metric === "net_profit_pct") return formatPctPoints(value, 2);
  if (metric === "max_drawdown_pct") return formatPctPoints(value, 2);
  if (metric === "total_trades") return formatNumber(value, 0);
  if (metric === "psr") return formatMetric(value, 3);
  return formatMetric(value, 2);
}

function selectedGridMetricValue(
  row: MatrixRow,
  metric: GridMetric,
): number | null | undefined {
  if (metric === "psr") return row.psr;
  if (metric === "net_profit_pct") return row.net_profit_pct;
  if (metric === "max_drawdown_pct") return row.max_drawdown_pct;
  if (metric === "total_trades") return row.total_trades;
  return row.sharpe;
}

function uniqueSorted(values: string[]): string[] {
  return Array.from(new Set(values)).sort((a, b) => a.localeCompare(b));
}

function downloadJson(overview: MatrixGridOverview | null) {
  if (!overview) return;
  const blob = new Blob([JSON.stringify(overview, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "matrix-grid-overview.json";
  link.click();
  URL.revokeObjectURL(url);
}

export function MatrixGridPage() {
  const [overview, setOverview] = useState<MatrixGridOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [selectedStrategy, setSelectedStrategy] = useState("");
  const [symbolFilter, setSymbolFilter] = useState("");
  const [timeframeFilter, setTimeframeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [viewMode, setViewMode] = useState<ViewMode>("table");
  const [gridMetric, setGridMetric] = useState<GridMetric>("sharpe");
  const [sortKey, setSortKey] = useState<SortKey>("sharpe");
  const [sortDir, setSortDir] = useState<SortDirection>("desc");
  const [page, setPage] = useState(1);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getMatrixGrid("all", 3)
      .then((data) => {
        if (!cancelled) setOverview(data);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const rows = overview?.rows ?? [];
  const summaries = overview?.strategy_summaries ?? [];
  const blocked = overview?.blocked_strategies ?? [];

  const strategies = useMemo(
    () => uniqueSorted(rows.map((row) => row.strategy)),
    [rows],
  );
  const symbols = useMemo(
    () =>
      overview?.counts.symbols.length
        ? overview.counts.symbols
        : uniqueSorted(rows.map((row) => row.symbol)),
    [overview, rows],
  );
  const timeframes = useMemo(
    () => uniqueSorted(rows.map((row) => row.timeframe)),
    [rows],
  );
  const statuses = useMemo(
    () => uniqueSorted(rows.map((row) => row.status)),
    [rows],
  );

  useEffect(() => {
    if (!selectedStrategy && strategies.length > 0) {
      setSelectedStrategy(strategies[0]);
    }
  }, [selectedStrategy, strategies]);

  const selectedStrategyRows = useMemo(
    () =>
      selectedStrategy
        ? rows.filter((row) => row.strategy === selectedStrategy)
        : rows,
    [rows, selectedStrategy],
  );
  const selectedStrategySummary = summaries.find(
    (summary) => summary.strategy === selectedStrategy,
  );

  const filteredRows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return rows.filter((row) => {
      if (selectedStrategy && row.strategy !== selectedStrategy) return false;
      if (symbolFilter && row.symbol !== symbolFilter) return false;
      if (timeframeFilter && row.timeframe !== timeframeFilter) return false;
      if (statusFilter && row.status !== statusFilter) return false;
      if (!needle) return true;
      return [row.strategy, row.symbol, row.timeframe, row.run_id].some(
        (value) => value.toLowerCase().includes(needle),
      );
    });
  }, [
    rows,
    search,
    selectedStrategy,
    symbolFilter,
    timeframeFilter,
    statusFilter,
  ]);

  const sortedRows = useMemo(() => {
    const ordered = [...filteredRows];
    ordered.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      let cmp: number;
      if (sortKey === "timeframe") {
        cmp =
          (TIMEFRAME_ORDER[a.timeframe] ?? 99) -
          (TIMEFRAME_ORDER[b.timeframe] ?? 99);
      } else if (typeof av === "number" && typeof bv === "number") {
        cmp = av - bv;
      } else {
        cmp = String(av ?? "").localeCompare(String(bv ?? ""));
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return ordered;
  }, [filteredRows, sortDir, sortKey]);

  const totalPages = Math.max(1, Math.ceil(sortedRows.length / PAGE_SIZE));
  const pagedRows = useMemo(() => {
    const safePage = Math.min(page, totalPages);
    if (safePage !== page) setPage(safePage);
    const start = (safePage - 1) * PAGE_SIZE;
    return sortedRows.slice(start, start + PAGE_SIZE);
  }, [page, sortedRows, totalPages]);

  const topSharpe = useMemo(
    () =>
      [...selectedStrategyRows]
        .sort((a, b) => (b.sharpe ?? -Infinity) - (a.sharpe ?? -Infinity))
        .slice(0, 5),
    [selectedStrategyRows],
  );
  const topCagr = useMemo(
    () =>
      [...selectedStrategyRows]
        .sort((a, b) => (b.cagr ?? -Infinity) - (a.cagr ?? -Infinity))
        .slice(0, 5),
    [selectedStrategyRows],
  );
  const topPsr = useMemo(
    () =>
      [...selectedStrategyRows]
        .sort((a, b) => (b.psr ?? -Infinity) - (a.psr ?? -Infinity))
        .slice(0, 5),
    [selectedStrategyRows],
  );

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((dir) => (dir === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
      setPage(1);
    }
  };

  const resetFilters = () => {
    setSearch("");
    setSelectedStrategy("");
    setSymbolFilter("");
    setTimeframeFilter("");
    setStatusFilter("");
    setSortKey("sharpe");
    setSortDir("desc");
    setPage(1);
  };

  const sortLabel = (key: SortKey) => {
    if (sortKey !== key) {
      return "↕";
    }
    return sortDir === "asc" ? "▲" : "▼";
  };

  const summaryCards = [
    {
      label: "Strategien fertig",
      value: overview?.counts.strategies_done ?? 0,
      hint: `${blocked.length} blockiert`,
    },
    {
      label: "Ausgewählte Strategie",
      value: selectedStrategy || "Alle",
      hint: `${selectedStrategyRows.length} Combos`,
    },
    {
      label: "OK / Failed",
      value: `${overview?.counts.ok_rows ?? 0} / ${overview?.counts.failed_rows ?? 0}`,
      hint: `${selectedStrategySummary?.ok ?? selectedStrategyRows.filter((row) => row.status === "ok").length} OK in Auswahl`,
    },
    {
      label: "Best Sharpe",
      value: topSharpe[0] ? formatMetric(topSharpe[0].sharpe, 2) : "N/A",
      hint: topSharpe[0]
        ? `${topSharpe[0].symbol} · ${topSharpe[0].timeframe}`
        : "",
    },
    {
      label: "Best PSR",
      value: topPsr[0] ? formatMetric(topPsr[0].psr, 3) : "N/A",
      hint: topPsr[0] ? `${topPsr[0].symbol} · ${topPsr[0].timeframe}` : "",
    },
  ];

  return (
    <div style={{ padding: "0 24px 32px" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: 16,
          marginBottom: 18,
        }}
      >
        <div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              marginBottom: 8,
            }}
          >
            <Table2 size={22} style={{ color: "var(--color-blue)" }} />
            <h1 style={{ fontSize: "1.35rem", fontWeight: 700 }}>
              Matrix Backtest Übersicht
            </h1>
          </div>
          <p style={{ color: "var(--color-text-muted)", lineHeight: 1.5 }}>
            Sharpe, CAGR, Max DD, Win Rate, PSR und Trade-Metriken je Strategie
            × Asset × Timeframe. PSR ist als Approximation aus der
            Trade-PnL-Verteilung berechnet.
          </p>
        </div>
        <button
          type="button"
          onClick={() => downloadJson(overview)}
          className="inline-flex items-center gap-2 rounded border border-border bg-surface2 px-3 py-2 text-xs text-text hover:text-blue"
          disabled={!overview}
        >
          <Download size={14} />
          JSON export
        </button>
      </div>

      {loading && (
        <div className="rounded border border-border bg-surface p-6 text-text-muted">
          Lade Matrix-Ergebnisse…
        </div>
      )}
      {!loading && error && (
        <div className="rounded border border-red/30 bg-red/10 p-6 text-red">
          Fehler beim Laden: {error}
        </div>
      )}
      {!loading && !error && overview && (
        <>
          <div
            className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4"
            style={{ marginBottom: 18 }}
          >
            {summaryCards.map((card) => (
              <div
                key={card.label}
                className="rounded border border-border bg-surface p-4"
              >
                <div className="text-xs text-text-muted">{card.label}</div>
                <div className="mt-2 text-2xl font-semibold text-text">
                  {card.value}
                </div>
                <div className="mt-1 text-xs text-text-muted">{card.hint}</div>
              </div>
            ))}
          </div>

          <div
            className="rounded border border-border bg-surface p-4"
            style={{ marginBottom: 18 }}
          >
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 10,
                alignItems: "center",
                marginBottom: 12,
              }}
            >
              <div className="relative flex-1" style={{ minWidth: 220 }}>
                <Search
                  size={14}
                  style={{
                    position: "absolute",
                    left: 10,
                    top: "50%",
                    transform: "translateY(-50%)",
                    color: "var(--color-text-muted)",
                  }}
                />
                <input
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                    setPage(1);
                  }}
                  placeholder="Suche: Strategie, Asset, TF, Run-ID…"
                  className="w-full rounded border border-border bg-surface2 px-3 py-2 pl-8 text-sm text-text outline-none focus:border-blue"
                />
              </div>
              {[
                {
                  value: selectedStrategy,
                  onChange: setSelectedStrategy,
                  options: strategies,
                  label: "Strategie",
                },
                {
                  value: symbolFilter,
                  onChange: setSymbolFilter,
                  options: symbols,
                  label: "Asset",
                },
                {
                  value: timeframeFilter,
                  onChange: setTimeframeFilter,
                  options: timeframes,
                  label: "Timeframe",
                },
                {
                  value: statusFilter,
                  onChange: setStatusFilter,
                  options: statuses,
                  label: "Status",
                },
              ].map((filter) => (
                <select
                  key={filter.label}
                  value={filter.value}
                  onChange={(event) => {
                    filter.onChange(event.target.value);
                    setPage(1);
                  }}
                  className="rounded border border-border bg-surface2 px-3 py-2 text-sm text-text outline-none focus:border-blue"
                >
                  <option value="">{filter.label}: alle</option>
                  {filter.options.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              ))}
              <button
                onClick={resetFilters}
                className="rounded border border-border bg-surface2 px-3 py-2 text-xs text-text hover:text-blue"
              >
                Reset
              </button>
              <div className="inline-flex rounded border border-border bg-surface2 p-0.5">
                <button
                  type="button"
                  onClick={() => setViewMode("table")}
                  className={`rounded px-2 py-1 text-xs ${viewMode === "table" ? "bg-blue/15 text-blue" : "text-text-muted hover:text-text"}`}
                >
                  Tabelle
                </button>
                <button
                  type="button"
                  onClick={() => setViewMode("grid")}
                  className={`rounded px-2 py-1 text-xs ${viewMode === "grid" ? "bg-blue/15 text-blue" : "text-text-muted hover:text-text"}`}
                >
                  Grid
                </button>
              </div>
              {viewMode === "grid" ? (
                <select
                  value={gridMetric}
                  onChange={(event) =>
                    setGridMetric(event.target.value as GridMetric)
                  }
                  className="rounded border border-border bg-surface2 px-3 py-2 text-sm text-text outline-none focus:border-blue"
                >
                  {GRID_METRIC_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : null}
              <div className="text-xs text-text-muted">
                {filteredRows.length} von {rows.length} Rows
              </div>
            </div>
            <div className="text-xs text-text-muted">
              Quelle: {overview.source} · Report updated:{" "}
              {formatDateTime(overview.report_updated_at)} · Generated:{" "}
              {formatDateTime(overview.generated_at)}
            </div>
          </div>

          {blocked.length > 0 ? (
            <div className="mb-4 rounded border border-yellow/30 bg-yellow/10 p-3 text-sm text-yellow">
              <div className="mb-1 flex items-center gap-2 font-semibold">
                <AlertTriangle size={15} /> Blockierte Strategien
              </div>
              {blocked.map((item) => (
                <div key={item.strategy}>
                  <span className="font-medium">{item.strategy}</span>:{" "}
                  {item.blocked_reason ?? item.status}
                </div>
              ))}
            </div>
          ) : null}

          <div
            className="grid grid-cols-1 gap-4 xl:grid-cols-3"
            style={{ marginBottom: 18 }}
          >
            <TopRows title="Top Sharpe" rows={topSharpe} metric="sharpe" />
            <TopRows title="Top CAGR" rows={topCagr} metric="cagr" />
            <TopRows title="Top PSR" rows={topPsr} metric="psr" />
          </div>

          {viewMode === "grid" ? (
            <StrategyGrid
              rows={selectedStrategyRows}
              metric={gridMetric}
              onMetricChange={setGridMetric}
            />
          ) : null}

          {viewMode !== "grid" ? (
            <MatrixTableView
              summaries={summaries}
              filteredRows={filteredRows}
              pagedRows={pagedRows}
              page={page}
              totalPages={totalPages}
              sortKey={sortKey}
              sortDir={sortDir}
              handleSort={handleSort}
              sortLabel={sortLabel}
              onPageChange={(updater) => setPage((current) => updater(current))}
            />
          ) : null}
        </>
      )}
    </div>
  );
}

interface StrategyGridProps {
  rows: MatrixRow[];
  metric: GridMetric;
  onMetricChange: (metric: GridMetric) => void;
}

function StrategyGrid({
  rows,
  metric,
  onMetricChange,
}: Readonly<StrategyGridProps>) {
  const symbols = useMemo(
    () => uniqueSorted(rows.map((row) => row.symbol)),
    [rows],
  );
  const timeframes = useMemo(
    () =>
      uniqueSorted(rows.map((row) => row.timeframe)).sort(
        (a, b) => (TIMEFRAME_ORDER[a] ?? 99) - (TIMEFRAME_ORDER[b] ?? 99),
      ),
    [rows],
  );
  const cells = useMemo(() => {
    const map = new Map<string, MatrixRow>();
    rows.forEach((row) => map.set(`${row.symbol}\u0000${row.timeframe}`, row));
    return map;
  }, [rows]);

  if (rows.length === 0) {
    return (
      <div className="rounded border border-border bg-surface p-4 text-text-muted">
        Keine Rows für diese Strategie gefunden.
      </div>
    );
  }

  return (
    <div className="rounded border border-border bg-surface p-4">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-text">
            Asset × Timeframe Grid
          </h2>
          <p className="mt-1 text-xs text-text-muted">
            Pro Zelle: ausgewählte Metrik plus Sharpe, PSR, Trades und Win Rate
            als Kontext.
          </p>
        </div>
        <select
          value={metric}
          onChange={(event) => onMetricChange(event.target.value as GridMetric)}
          className="rounded border border-border bg-surface2 px-3 py-2 text-sm text-text outline-none focus:border-blue"
        >
          {GRID_METRIC_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
      <div className="overflow-x-auto">
        <table
          className="w-full"
          style={{
            borderCollapse: "separate",
            borderSpacing: "8px",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          <thead>
            <tr>
              <th className="px-2 py-2 text-left text-xs uppercase tracking-wide text-text-muted">
                Asset
              </th>
              {timeframes.map((timeframe) => (
                <th
                  key={timeframe}
                  className="px-2 py-2 text-center text-xs uppercase tracking-wide text-text-muted"
                >
                  {timeframe}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {symbols.map((symbol) => (
              <tr key={symbol}>
                <td className="px-2 py-2 text-sm font-medium text-text">
                  {symbol}
                </td>
                {timeframes.map((timeframe) => {
                  const row = cells.get(`${symbol}\u0000${timeframe}`);
                  return (
                    <td key={timeframe} className="px-2 py-2 align-top">
                      {row ? (
                        <StrategyGridCell row={row} metric={metric} />
                      ) : (
                        <EmptyGridCell label="Keine Daten" />
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

interface StrategyGridCellProps {
  row: MatrixRow;
  metric: GridMetric;
}

function StrategyGridCell({ row, metric }: StrategyGridCellProps) {
  const value = selectedGridMetricValue(row, metric);
  const content = (
    <>
      <div className="text-[0.68rem] uppercase text-text-muted">
        Sharpe {formatMetric(row.sharpe, 2)} · PSR {formatMetric(row.psr, 3)}
      </div>
      <div
        className="mt-1 text-lg font-semibold"
        style={{ color: gridMetricColor(value, metric) }}
      >
        {formatGridMetric(value, metric)}
      </div>
      <div className="mt-1 text-xs text-text-muted">
        {formatNumber(row.total_trades, 0)} Trades · Win{" "}
        {formatPctFraction(row.win_rate, 1)}
      </div>
      {row.backtest_id ? (
        <div className="mt-2 text-[0.68rem] text-blue">
          Backtest {row.backtest_id}
        </div>
      ) : null}
    </>
  );
  return row.backtest_id ? (
    <Link
      to={`/backtests/${encodeURIComponent(row.backtest_id)}`}
      className="block min-w-[150px] rounded border border-border bg-surface2 p-3 text-center hover:border-blue/70"
    >
      {content}
    </Link>
  ) : (
    <div className="min-w-[150px] rounded border border-border/70 bg-surface2 p-3 text-center">
      {content}
    </div>
  );
}

function EmptyGridCell({ label }: Readonly<{ label: string }>) {
  return (
    <div className="min-w-[150px] rounded-lg border border-border/40 bg-surface2 p-3 text-center text-xs text-text-muted">
      {label}
    </div>
  );
}

interface MatrixTableViewProps {
  summaries: StrategySummary[];
  filteredRows: MatrixRow[];
  pagedRows: MatrixRow[];
  page: number;
  totalPages: number;
  sortKey: SortKey;
  sortDir: SortDirection;
  handleSort: (key: SortKey) => void;
  sortLabel: (key: SortKey) => string;
  onPageChange: (updater: (page: number) => number) => void;
}

function MatrixTableView({
  summaries,
  filteredRows,
  pagedRows,
  page,
  totalPages,
  sortKey,
  sortDir,
  handleSort,
  sortLabel,
  onPageChange,
}: Readonly<MatrixTableViewProps>) {
  return (
    <>
      <div className="rounded border border-border bg-surface p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-base font-semibold text-text">
            Strategie-Summary
          </h2>
          <span className="text-xs text-text-muted">
            {summaries.length} Strategien
          </span>
        </div>
        <div className="overflow-x-auto">
          <table
            className="w-full"
            style={{
              fontSize: "0.78rem",
              borderCollapse: "collapse",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            <thead>
              <tr className="text-left text-text-muted">
                {[
                  "Strategie",
                  "OK",
                  "Trades",
                  "Ø Sharpe",
                  "Best Sharpe",
                  "Ø CAGR",
                  "Best CAGR",
                  "Ø PSR",
                  "Worst DD",
                  "Best Combo",
                ].map((head) => (
                  <th key={head} className="border-b border-border px-2 py-2">
                    {head}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {summaries.map((summary) => (
                <tr
                  key={summary.strategy}
                  className="border-b border-border/70 hover:bg-surface2"
                >
                  <td className="px-2 py-2 font-medium text-text">
                    {summary.strategy}
                  </td>
                  <td className="px-2 py-2 text-center">
                    {summary.ok}/{summary.requested}
                  </td>
                  <td className="px-2 py-2 text-center">
                    {formatNumber(summary.total_trades, 0)}
                  </td>
                  <td className="px-2 py-2 text-center">
                    {formatMetric(summary.avg_sharpe, 2)}
                  </td>
                  <td
                    className="px-2 py-2 text-center"
                    style={{ color: sharpeColor(summary.best_sharpe) }}
                  >
                    {formatMetric(summary.best_sharpe, 2)}
                  </td>
                  <td
                    className="px-2 py-2 text-center"
                    style={{ color: cagrColor(summary.avg_cagr) }}
                  >
                    {formatPctFraction(summary.avg_cagr, 2)}
                  </td>
                  <td
                    className="px-2 py-2 text-center"
                    style={{ color: cagrColor(summary.best_cagr) }}
                  >
                    {formatPctFraction(summary.best_cagr, 2)}
                  </td>
                  <td
                    className="px-2 py-2 text-center"
                    style={{ color: psrColor(summary.avg_psr) }}
                  >
                    {formatMetric(summary.avg_psr, 3)}
                  </td>
                  <td
                    className="px-2 py-2 text-center"
                    style={{
                      color: drawdownColor(summary.worst_max_drawdown_pct),
                    }}
                  >
                    {formatPctFraction(summary.worst_max_drawdown_pct, 2)}
                  </td>
                  <td className="px-2 py-2 text-text-muted">
                    {summary.best_sharpe_combo
                      ? `${summary.best_sharpe_combo.symbol} · ${summary.best_sharpe_combo.timeframe}`
                      : "N/A"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="mt-4 rounded border border-border bg-surface p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-base font-semibold text-text">
            Alle Asset/TF-Combos
          </h2>
          <span className="text-xs text-text-muted">
            Seite {page} / {totalPages} · {PAGE_SIZE} Rows pro Seite
          </span>
        </div>
        <div className="overflow-x-auto" style={{ maxHeight: "720px" }}>
          <table
            className="w-full"
            style={{
              fontSize: "0.74rem",
              borderCollapse: "collapse",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            <thead style={{ position: "sticky", top: 0, zIndex: 2 }}>
              <tr className="text-left text-text-muted">
                <SortableTh
                  label="Strategie"
                  sortKey="strategy"
                  current={sortKey}
                  dir={sortDir}
                  onSort={handleSort}
                  sortLabel={sortLabel}
                />
                <SortableTh
                  label="Asset"
                  sortKey="symbol"
                  current={sortKey}
                  dir={sortDir}
                  onSort={handleSort}
                  sortLabel={sortLabel}
                />
                <SortableTh
                  label="TF"
                  sortKey="timeframe"
                  current={sortKey}
                  dir={sortDir}
                  onSort={handleSort}
                  sortLabel={sortLabel}
                />
                <SortableTh
                  label="Trades"
                  sortKey="total_trades"
                  current={sortKey}
                  dir={sortDir}
                  onSort={handleSort}
                  sortLabel={sortLabel}
                  align="right"
                />
                <SortableTh
                  label="Total PnL"
                  sortKey="total_pnl"
                  current={sortKey}
                  dir={sortDir}
                  onSort={handleSort}
                  sortLabel={sortLabel}
                  align="right"
                />
                <SortableTh
                  label="Net %"
                  sortKey="net_profit_pct"
                  current={sortKey}
                  dir={sortDir}
                  onSort={handleSort}
                  sortLabel={sortLabel}
                  align="right"
                />
                <SortableTh
                  label="Sharpe"
                  sortKey="sharpe"
                  current={sortKey}
                  dir={sortDir}
                  onSort={handleSort}
                  sortLabel={sortLabel}
                  align="right"
                />
                <SortableTh
                  label="CAGR"
                  sortKey="cagr"
                  current={sortKey}
                  dir={sortDir}
                  onSort={handleSort}
                  sortLabel={sortLabel}
                  align="right"
                />
                <SortableTh
                  label="Max DD"
                  sortKey="max_drawdown_pct"
                  current={sortKey}
                  dir={sortDir}
                  onSort={handleSort}
                  sortLabel={sortLabel}
                  align="right"
                />
                <SortableTh
                  label="Win %"
                  sortKey="win_rate"
                  current={sortKey}
                  dir={sortDir}
                  onSort={handleSort}
                  sortLabel={sortLabel}
                  align="right"
                />
                <SortableTh
                  label="PSR"
                  sortKey="psr"
                  current={sortKey}
                  dir={sortDir}
                  onSort={handleSort}
                  sortLabel={sortLabel}
                  align="right"
                />
                <th className="border-b border-border px-2 py-2">Status</th>
                <th className="border-b border-border px-2 py-2">Run</th>
                <th className="border-b border-border px-2 py-2">Trades</th>
              </tr>
            </thead>
            <tbody>
              {pagedRows.map((row) => (
                <tr
                  key={row.run_id}
                  className="border-b border-border/70 hover:bg-surface2"
                >
                  <td className="px-2 py-2 font-medium text-text">
                    {row.strategy}
                  </td>
                  <td className="px-2 py-2 text-center">{row.symbol}</td>
                  <td className="px-2 py-2 text-center">{row.timeframe}</td>
                  <td className="px-2 py-2 text-right">
                    {formatNumber(row.total_trades, 0)}
                  </td>
                  <td
                    className="px-2 py-2 text-right"
                    style={{
                      color:
                        row.total_pnl != null && row.total_pnl >= 0
                          ? "#3fb950"
                          : "#f85149",
                    }}
                  >
                    {formatNumber(row.total_pnl, 0)}
                  </td>
                  <td
                    className="px-2 py-2 text-right"
                    style={{ color: cagrColor(row.net_profit_pct) }}
                  >
                    {formatPctPoints(row.net_profit_pct, 2)}
                  </td>
                  <td
                    className="px-2 py-2 text-right"
                    style={{ color: sharpeColor(row.sharpe) }}
                  >
                    {formatMetric(row.sharpe, 2)}
                  </td>
                  <td
                    className="px-2 py-2 text-right"
                    style={{ color: cagrColor(row.cagr) }}
                  >
                    {formatPctFraction(row.cagr, 2)}
                  </td>
                  <td
                    className="px-2 py-2 text-right"
                    style={{ color: drawdownColor(row.max_drawdown_pct) }}
                  >
                    {formatPctFraction(row.max_drawdown_pct, 2)}
                  </td>
                  <td className="px-2 py-2 text-right">
                    {formatPctFraction(row.win_rate, 1)}
                  </td>
                  <td
                    className="px-2 py-2 text-right"
                    style={{ color: psrColor(row.psr) }}
                  >
                    {formatMetric(row.psr, 3)}
                  </td>
                  <td className="px-2 py-2 text-center">
                    <Badge color={statusColor(row.status)}>{row.status}</Badge>
                  </td>
                  <td
                    className="px-2 py-2 max-w-[220px] truncate"
                    title={row.backtest_id || row.run_id}
                  >
                    {row.backtest_id ? (
                      <Link
                        className="text-blue hover:underline"
                        to={`/backtests/${encodeURIComponent(row.backtest_id)}`}
                      >
                        {row.backtest_id}
                      </Link>
                    ) : (
                      <span className="text-text-muted">{row.run_id}</span>
                    )}
                  </td>
                  <td className="px-2 py-2 text-xs text-text-muted">
                    {row.trades_url ? (
                      <a
                        className="text-blue hover:underline"
                        href={row.trades_url}
                      >
                        API
                      </a>
                    ) : null}
                    {row.trades && row.trades.length > 0 ? (
                      <span className="ml-2">
                        {row.trades
                          .slice(0, 2)
                          .map(
                            (trade) =>
                              `${trade.direction} ${formatNumber(trade.pnl, 0)}`,
                          )
                          .join(" · ")}
                      </span>
                    ) : (
                      <span className="ml-2">Keine Samples</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-3 flex items-center justify-between gap-3">
          <button
            type="button"
            onClick={() => onPageChange((current) => Math.max(1, current - 1))}
            disabled={page <= 1}
            className="rounded border border-border bg-surface2 px-3 py-2 text-xs text-text disabled:opacity-40"
          >
            Zurück
          </button>
          <div className="text-xs text-text-muted">
            {filteredRows.length} gefilterte Rows · {page} / {totalPages}
          </div>
          <button
            type="button"
            onClick={() =>
              onPageChange((current) => Math.min(totalPages, current + 1))
            }
            disabled={page >= totalPages}
            className="rounded border border-border bg-surface2 px-3 py-2 text-xs text-text disabled:opacity-40"
          >
            Weiter
          </button>
        </div>
      </div>
    </>
  );
}

interface SortableThProps {
  label: string;
  sortKey: SortKey;
  current: SortKey;
  dir: SortDirection;
  onSort: (key: SortKey) => void;
  sortLabel: (key: SortKey) => string;
  align?: "left" | "right" | "center";
}

function SortableTh({
  label,
  sortKey,
  current,
  dir,
  onSort,
  sortLabel,
  align = "left",
}: Readonly<SortableThProps>) {
  return (
    <th
      className="border-b border-border px-2 py-2"
      style={{ textAlign: align, cursor: "pointer", userSelect: "none" }}
      onClick={() => onSort(sortKey)}
    >
      {label}{" "}
      <span className="text-xs">
        {current === sortKey ? (dir === "asc" ? "▲" : "▼") : sortLabel(sortKey)}
      </span>
    </th>
  );
}

interface TopRowsProps {
  title: string;
  rows: MatrixRow[];
  metric: "sharpe" | "cagr" | "psr";
}

/** Picks the metric-specific color for top-row cards. */
function topRowColor(
  metric: "sharpe" | "cagr" | "psr",
  row: MatrixRow,
): string {
  if (metric === "cagr") {
    return cagrColor(row.cagr);
  }
  if (metric === "psr") {
    return psrColor(row.psr);
  }
  return sharpeColor(row.sharpe);
}

function TopRows({ title, rows, metric }: Readonly<TopRowsProps>) {
  return (
    <div className="rounded border border-border bg-surface p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-text">{title}</h3>
        <RefreshCw size={14} style={{ color: "var(--color-text-muted)" }} />
      </div>
      <div className="space-y-2">
        {rows.map((row) => (
          <div
            key={row.run_id}
            className="rounded border border-border/70 bg-surface2 p-3"
          >
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-medium text-text">
                  {row.strategy}
                </div>
                <div className="text-xs text-text-muted">
                  {row.symbol} · {row.timeframe}
                </div>
              </div>
              <div className="text-right">
                <div
                  className="text-lg font-semibold"
                  style={{ color: topRowColor(metric, row) }}
                >
                  {metric === "cagr"
                    ? formatPctFraction(row.cagr, 2)
                    : formatMetric(row[metric], 3)}
                </div>
                <div className="text-xs text-text-muted">
                  {formatNumber(row.total_trades, 0)} Trades
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
