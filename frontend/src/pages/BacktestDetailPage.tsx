import { useState, useEffect, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  getBacktestInfo,
  getBacktestStats,
  getBacktestTrades,
  getBacktestEquity,
  getBacktestCapm,
  getBenchmark,
  renameBacktest,
} from "../api/client";
import { StatCard } from "../components/StatCard";
import { DataTable } from "../components/DataTable";
import { RenameModal } from "../components/RenameModal";
import { OptimizeModal } from "../components/OptimizeModal";
import { ArrowLeft, ArrowUpRight, PencilLine, Target } from "lucide-react";
import {
  createChart,
  AreaSeries,
  LineSeries,
  HistogramSeries,
  ColorType,
  CrosshairMode,
} from "lightweight-charts";
import type { IChartApi, Time } from "lightweight-charts";
import {
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type {
  BacktestCapmResponse,
  BacktestInfo,
  BacktestStats,
  EquityPoint,
  Trade,
} from "../api/types";
import { formatPValue, pValueColor } from "../utils/stats";

const BENCHMARK_SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "XAUUSD"];

export function BacktestDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [info, setInfo] = useState<BacktestInfo | null>(null);
  const [stats, setStats] = useState<BacktestStats | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [benchmarkSymbol, setBenchmarkSymbol] = useState("");
  const [benchmarkData, setBenchmarkData] = useState<any[]>([]);
  const [capmData, setCapmData] = useState<BacktestCapmResponse | null>(null);
  const [capmLoading, setCapmLoading] = useState(false);
  const [capmError, setCapmError] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showRenameModal, setShowRenameModal] = useState(false);
  const [showOptimizeModal, setShowOptimizeModal] = useState(false);
  const [savingRename, setSavingRename] = useState(false);
  const [renameError, setRenameError] = useState("");

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    Promise.all([
      getBacktestInfo(id),
      getBacktestStats(id),
      getBacktestTrades(id),
      getBacktestEquity(id),
    ])
      .then(([i, s, t, e]) => {
        setInfo(i);
        setStats(s);
        setTrades(t);
        setEquity(e);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, [id]);

  // Load benchmark data when symbol changes
  useEffect(() => {
    if (!benchmarkSymbol || !info) {
      setBenchmarkData([]);
      return;
    }
    const start = info?.timeframe?.start?.slice(0, 10) || "";
    const end = info?.timeframe?.end?.slice(0, 10) || "";
    getBenchmark(benchmarkSymbol, start, end)
      .then((data: any) => setBenchmarkData(data.equity || []))
      .catch(() => setBenchmarkData([]));
  }, [benchmarkSymbol, info]);

  useEffect(() => {
    if (!id || !info) {
      setCapmData(null);
      return;
    }
    setCapmLoading(true);
    setCapmError("");
    getBacktestCapm(id, benchmarkSymbol || undefined)
      .then((payload) => {
        setCapmData(payload);
      })
      .catch((capmIssue: unknown) => {
        const message =
          capmIssue instanceof Error && capmIssue.message
            ? capmIssue.message
            : "Could not load CAPM data";
        setCapmError(message);
        setCapmData(null);
      })
      .finally(() => {
        setCapmLoading(false);
      });
  }, [id, info, benchmarkSymbol]);

  const handleRename = async (nextTitle: string) => {
    if (!id) return;
    setSavingRename(true);
    setRenameError("");
    try {
      const updated = await renameBacktest(id, nextTitle);
      setInfo(updated);
      setShowRenameModal(false);
    } catch (renameIssue: any) {
      setRenameError(renameIssue.message || "Could not rename backtest");
    } finally {
      setSavingRename(false);
    }
  };

  if (loading)
    return (
      <div style={{ padding: "20px", color: "var(--color-text-muted)" }}>
        Loading...
      </div>
    );
  if (error)
    return (
      <div style={{ padding: "20px", color: "var(--color-red)" }}>
        Error: {error}
      </div>
    );

  // Trade stats
  const wins = trades.filter((t) => t.pnl > 0);
  const losses = trades.filter((t) => t.pnl <= 0);
  const totalPnl = trades.reduce((s, t) => s + (t.pnl || 0), 0);
  const avgPnl = trades.length > 0 ? totalPnl / trades.length : 0;
  const bestTrade =
    trades.length > 0 ? Math.max(...trades.map((t) => t.pnl || 0)) : 0;
  const worstTrade =
    trades.length > 0 ? Math.min(...trades.map((t) => t.pnl || 0)) : 0;

  const tradeColumns = [
    { key: "nr", label: "#", width: "40px", align: "center" as const },
    { key: "symbol", label: "Symbol", width: "70px", align: "center" as const },
    {
      key: "direction",
      label: "Side",
      width: "55px",
      align: "center" as const,
      render: (r: any) => (
        <span
          style={{
            color: r.direction === "LONG" ? "#3fb950" : "#f85149",
            fontWeight: 600,
          }}
        >
          {r.direction}
        </span>
      ),
    },
    {
      key: "entry_time_iso",
      label: "Entry",
      align: "center" as const,
      render: (r: any) => (r.entry_time_iso || "").slice(0, 19),
    },
    {
      key: "entry_price",
      label: "Entry $",
      align: "right" as const,
      render: (r: any) => `$${r.entry_price?.toFixed(2)}`,
    },
    {
      key: "exit_time_iso",
      label: "Exit",
      align: "center" as const,
      render: (r: any) => (r.exit_time_iso || "").slice(0, 19),
    },
    {
      key: "exit_price",
      label: "Exit $",
      align: "right" as const,
      render: (r: any) => `$${r.exit_price?.toFixed(2)}`,
    },
    {
      key: "quantity",
      label: "Qty",
      align: "right" as const,
      render: (r: any) => r.quantity?.toFixed(2),
    },
    {
      key: "pnl",
      label: "P&L",
      align: "right" as const,
      render: (r: any) => (
        <span
          style={{ color: r.pnl >= 0 ? "#3fb950" : "#f85149", fontWeight: 600 }}
        >
          ${r.pnl?.toLocaleString("en-US", { maximumFractionDigits: 2 })}
        </span>
      ),
    },
    {
      key: "pnl_pct",
      label: "P&L %",
      align: "right" as const,
      render: (r: any) =>
        r.pnl_pct != null ? (
          <span style={{ color: r.pnl_pct >= 0 ? "#3fb950" : "#f85149" }}>
            {r.pnl_pct?.toFixed(2)}%
          </span>
        ) : (
          "—"
        ),
    },
    { key: "holding_bars", label: "Bars", align: "right" as const },
  ];

  return (
    <div style={{ padding: "24px" }}>
      {/* Header */}
      <div
        style={{
          background:
            "radial-gradient(circle at top right, rgba(88,166,255,0.12), transparent 38%), var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "12px",
          padding: "24px",
          marginBottom: "20px",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          flexWrap: "wrap",
          gap: "16px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <button
            onClick={() => navigate(-1)}
            style={{
              background: "none",
              border: "none",
              color: "var(--color-text-muted)",
              cursor: "pointer",
              padding: "4px",
            }}
          >
            <ArrowLeft size={20} />
          </button>
          <div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "10px",
                flexWrap: "wrap",
              }}
            >
              <h1
                style={{
                  fontSize: "1.6rem",
                  fontWeight: 700,
                  letterSpacing: "-0.03em",
                  margin: 0,
                }}
              >
                {info?.title || info?.strategy_display_name || info?.strategy || id}
              </h1>
              <button
                type="button"
                onClick={() => setShowRenameModal(true)}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  width: "34px",
                  height: "34px",
                  borderRadius: "999px",
                  border: "1px solid rgba(88,166,255,0.18)",
                  background: "rgba(88,166,255,0.08)",
                  color: "#58a6ff",
                  cursor: "pointer",
                }}
                aria-label="Rename backtest"
              >
                <PencilLine size={15} />
              </button>
              {info?.strategy_display_name &&
              info?.title &&
              info.title !== info.strategy_display_name ? (
                <span
                  style={{
                    background: "var(--color-surface2)",
                    border: "1px solid var(--color-border)",
                    padding: "3px 10px",
                    borderRadius: "999px",
                    fontSize: "0.72rem",
                    color: "var(--color-text-muted)",
                  }}
                >
                  Strategy: {info.strategy_display_name}
                </span>
              ) : null}
            </div>
            <div
              style={{
                color: "var(--color-text-muted)",
                fontSize: "0.85rem",
                marginTop: "4px",
              }}
            >
              <span style={{ marginRight: "16px" }}>🔧 {info?.engine}</span>
              <span style={{ marginRight: "16px" }}>
                📈 {info?.symbols?.join(", ")}
              </span>
              <span style={{ marginRight: "16px" }}>
                ⏱ {info?.data_timeframe}
              </span>
              <span style={{ marginRight: "16px" }}>
                📅 {info?.timeframe?.start?.slice(0, 10)} →{" "}
                {info?.timeframe?.end?.slice(0, 10)}
              </span>
              <span>
                💰 ${(info?.initial_capital || 100000).toLocaleString()}
              </span>
            </div>
            {/* Strategy Parameters */}
            {info?.parameters && Object.keys(info.parameters).length > 0 && (
              <div
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  gap: "6px",
                  marginTop: "10px",
                  alignItems: "center",
                }}
              >
                <span
                  style={{
                    fontSize: "0.7rem",
                    color: "var(--color-text-muted)",
                    textTransform: "uppercase",
                    letterSpacing: "0.05em",
                    marginRight: "2px",
                  }}
                >
                  Params:
                </span>
                {Object.entries(info.parameters).map(([key, val]) => (
                  <span
                    key={key}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "4px",
                      background: "rgba(88,166,255,0.08)",
                      border: "1px solid rgba(88,166,255,0.18)",
                      borderRadius: "6px",
                      padding: "2px 8px",
                      fontSize: "0.72rem",
                      fontFamily: "monospace",
                      color: "#c9d1d9",
                    }}
                  >
                    <span style={{ color: "var(--color-text-muted)" }}>{key}</span>
                    <span style={{ color: "rgba(88,166,255,0.7)" }}>:</span>
                    <span style={{ color: "#e6edf3" }}>{String(val)}</span>
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
        {info?.strategy_slug || info?.strategy ? (
          <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
            <button
              type="button"
              onClick={() => setShowOptimizeModal(true)}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "8px",
                background:
                  "linear-gradient(135deg, rgba(14,165,233,0.18), rgba(245,158,11,0.18))",
                color: "#f8fafc",
                border: "1px solid rgba(56,189,248,0.22)",
                borderRadius: "10px",
                padding: "10px 14px",
                cursor: "pointer",
                boxShadow: "0 16px 36px rgba(14,165,233,0.12)",
              }}
            >
              <Target size={15} />
              Optimize
            </button>
            <button
              type="button"
              onClick={() =>
                navigate(
                  `/strategies/${encodeURIComponent(info?.strategy_slug || info?.strategy || "")}`,
                )
              }
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "8px",
                background:
                  "linear-gradient(135deg, rgba(88,166,255,0.18), rgba(124,58,237,0.16))",
                color: "#c9d1d9",
                border: "1px solid rgba(88,166,255,0.2)",
                borderRadius: "10px",
                padding: "10px 14px",
                cursor: "pointer",
              }}
            >
              View Strategy Details
              <ArrowUpRight size={15} />
            </button>
          </div>
        ) : null}
      </div>

      {/* Stats Grid */}
      {stats && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
            gap: "12px",
            marginBottom: "20px",
          }}
        >
          <StatCard
            label="Net Profit"
            value={stats.net_profit}
            format="currency"
          />
          <StatCard
            label="Net Profit %"
            value={stats.net_profit_pct}
            format="percent"
          />
          <StatCard label="CAGR" value={stats.cagr} format="percent" />
          <StatCard
            label="Sharpe Ratio"
            value={stats.sharpe?.toFixed(3)}
            color={
              stats.sharpe > 1
                ? "#3fb950"
                : stats.sharpe > 0
                  ? "#d29922"
                  : "#f85149"
            }
          />
          <StatCard
            label="Sortino Ratio"
            value={stats.sortino?.toFixed(3)}
            color={stats.sortino > 1 ? "#3fb950" : "#d29922"}
          />
          <StatCard
            label="Max Drawdown"
            value={stats.max_drawdown_pct}
            format="percent"
            color="#f85149"
          />
          <StatCard
            label="Win Rate"
            value={stats.win_rate}
            format="percent"
            color={stats.win_rate > 50 ? "#3fb950" : "#f85149"}
          />
          <StatCard
            label="Profit Factor"
            value={stats.profit_factor?.toFixed(2)}
            color={stats.profit_factor > 1 ? "#3fb950" : "#f85149"}
          />
          <StatCard
            label="Calmar"
            value={stats.calmar?.toFixed(2)}
            color={stats.calmar > 1 ? "#3fb950" : "#d29922"}
          />
          <StatCard
            label="PSR"
            value={
              stats.psr != null ? `${(stats.psr * 100).toFixed(1)}%` : "N/A"
            }
            color={
              (stats.psr ?? 0) > 0.95
                ? "#3fb950"
                : (stats.psr ?? 0) > 0.5
                  ? "#d29922"
                  : "#f85149"
            }
          />
          <StatCard
            label="P-Value"
            value={formatPValue(stats.p_value)}
            color={pValueColor(stats.p_value)}
          />
          <StatCard
            label="Expectancy"
            value={stats.expectancy}
            format="currency"
          />
          <StatCard
            label="Total Trades"
            value={stats.total_trades}
            format="number"
            color="#58a6ff"
          />
          <StatCard
            label="Avg Bars Held"
            value={stats.avg_bars_held?.toFixed(1)}
            color="#c9d1d9"
          />
        </div>
      )}

      {/* Equity Curve */}
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "20px",
          marginBottom: "20px",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "16px",
          }}
        >
          <h2 style={{ fontSize: "1rem", fontWeight: 600 }}>Equity Curve</h2>
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <label
              style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}
            >
              Benchmark:
            </label>
            <select
              value={benchmarkSymbol}
              onChange={(e) => setBenchmarkSymbol(e.target.value)}
              style={{
                background: "var(--color-surface2)",
                color: "var(--color-text)",
                border: "1px solid var(--color-border)",
                borderRadius: "4px",
                padding: "4px 8px",
                fontSize: "0.78rem",
                cursor: "pointer",
              }}
            >
              <option value="">None</option>
              {BENCHMARK_SYMBOLS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
        </div>
        {equity.length > 0 ? (
          <EquityChart
            data={equity}
            benchmark={benchmarkData}
            benchmarkSymbol={benchmarkSymbol}
          />
        ) : (
          <div
            style={{
              height: "300px",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--color-text-muted)",
            }}
          >
            No equity data
          </div>
        )}
      </div>

      {/* CAPM Analysis */}
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "20px",
          marginBottom: "20px",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "14px",
            gap: "10px",
            flexWrap: "wrap",
          }}
        >
          <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>CAPM Analysis</h2>
          <span style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>
            Benchmark: {capmData?.benchmark_symbol || benchmarkSymbol || info?.symbols?.[0] || "N/A"}
          </span>
        </div>

        {capmLoading ? (
          <div style={{ color: "var(--color-text-muted)", padding: "14px 0" }}>
            Loading CAPM analysis...
          </div>
        ) : capmError ? (
          <div style={{ color: "var(--color-red)", padding: "14px 0" }}>Error: {capmError}</div>
        ) : capmData ? (
          <>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
                gap: "12px",
                marginBottom: "16px",
              }}
            >
              <StatCard
                label="Alpha (ann.)"
                value={capmData.alpha * 100}
                format="percent"
                color={capmData.alpha >= 0 ? "#3fb950" : "#f85149"}
              />
              <StatCard label="Beta" value={capmData.beta.toFixed(2)} color="#58a6ff" />
              <StatCard label="R²" value={capmData.r_squared.toFixed(2)} color="#c9d1d9" />
              <StatCard
                label="P-Value"
                value={capmData.p_value.toFixed(3)}
                color={capmData.p_value < 0.05 ? "#3fb950" : "#f85149"}
              />
            </div>
            <CapmScatterPlot capmData={capmData} />
          </>
        ) : (
          <div style={{ color: "var(--color-text-muted)", padding: "14px 0" }}>
            No CAPM data available.
          </div>
        )}
      </div>

      {/* Trade Summary Bar */}
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "20px",
          marginBottom: "20px",
        }}
      >
        <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "16px" }}>
          Trade Statistics
        </h2>
        <div
          style={{
            display: "flex",
            gap: "20px",
            flexWrap: "wrap",
            marginBottom: "14px",
          }}
        >
          <SummaryItem label="Total Trades" value={String(trades.length)} />
          <SummaryItem
            label="Long / Short"
            value={`${trades.filter((t) => t.direction === "LONG").length} / ${trades.filter((t) => t.direction === "SHORT").length}`}
          />
          <SummaryItem
            label="Win / Loss"
            value={`${wins.length} / ${losses.length}`}
            color={`#3fb950 / #f85149`}
          />
          <SummaryItem
            label="Total P&L"
            value={`$${totalPnl.toLocaleString("en-US", { maximumFractionDigits: 2 })}`}
            color={totalPnl >= 0 ? "#3fb950" : "#f85149"}
          />
          <SummaryItem
            label="Avg P&L"
            value={`$${avgPnl.toLocaleString("en-US", { maximumFractionDigits: 2 })}`}
            color={avgPnl >= 0 ? "#3fb950" : "#f85149"}
          />
          <SummaryItem
            label="Best Trade"
            value={`$${bestTrade.toLocaleString("en-US", { maximumFractionDigits: 2 })}`}
            color="#3fb950"
          />
          <SummaryItem
            label="Worst Trade"
            value={`$${worstTrade.toLocaleString("en-US", { maximumFractionDigits: 2 })}`}
            color="#f85149"
          />
        </div>
      </div>

      {/* Trade Table */}
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "20px",
        }}
      >
        <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "16px" }}>
          Trades ({trades.length})
        </h2>
        <DataTable
          columns={tradeColumns}
          data={trades}
          onRowClick={(row) => navigate(`/backtests/${id}/trade/${row.nr}`)}
        />
      </div>

      <RenameModal
        open={showRenameModal}
        title="Rename Backtest"
        label="Backtest title"
        value={info?.title || info?.strategy_display_name || info?.strategy || id || ""}
        helperText={
          renameError ||
          "This updates the backtest's display title stored in meta.notes without changing its ID."
        }
        saving={savingRename}
        onClose={() => {
          setShowRenameModal(false);
          setRenameError("");
        }}
        onSave={handleRename}
      />
      <OptimizeModal
        open={showOptimizeModal}
        onClose={() => setShowOptimizeModal(false)}
        backtestId={id || info?.id || ""}
        backtestTitle={info?.title || info?.strategy_display_name || info?.strategy || id || ""}
        parameters={info?.parameters || {}}
      />
    </div>
  );
}

function SummaryItem({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div
      style={{
        background: "var(--color-surface2)",
        border: "1px solid var(--color-border)",
        borderRadius: "6px",
        padding: "8px 14px",
        fontSize: "0.8rem",
      }}
    >
      <div
        style={{
          color: "var(--color-text-muted)",
          fontSize: "0.7rem",
          textTransform: "uppercase",
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontWeight: 700,
          fontSize: "1rem",
          fontVariantNumeric: "tabular-nums",
          color: color || "var(--color-text)",
        }}
      >
        {value}
      </div>
    </div>
  );
}

function EquityChart({
  data,
  benchmark = [],
  benchmarkSymbol = "",
}: {
  data: { time: number; equity: number }[];
  benchmark?: { time: number; equity: number }[];
  benchmarkSymbol?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const [tooltip, setTooltip] = useState<{
    x: number;
    y: number;
    time: string;
    equity: string;
    dd: string;
    ret: string;
  } | null>(null);

  useEffect(() => {
    if (!containerRef.current || !data.length) return;

    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
    }

    const container = containerRef.current;
    const w = container.clientWidth || 1100;
    const locale =
      typeof navigator !== "undefined" &&
      navigator.language &&
      navigator.language !== "undefined"
        ? navigator.language
        : "en-US";
    const chart = createChart(container, {
      width: w,
      height: 340,
      localization: { locale },
      layout: {
        background: { type: ColorType.Solid, color: "#0d1117" },
        textColor: "#8b949e",
      },
      grid: {
        vertLines: { color: "#1c2128" },
        horzLines: { color: "#1c2128" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { borderColor: "#30363d", timeVisible: false },
      rightPriceScale: { borderColor: "#30363d" },
    });
    chartRef.current = chart;

    const isProfit = data[data.length - 1].equity >= data[0].equity;
    const lineColor = isProfit ? "#3fb950" : "#f85149";

    // Main equity area series
    const equitySeries = chart.addSeries(AreaSeries, {
      lineColor,
      topColor: isProfit ? "rgba(63,185,80,0.25)" : "rgba(248,81,73,0.25)",
      bottomColor: "rgba(0,0,0,0)",
      lineWidth: 2,
      priceFormat: {
        type: "custom",
        formatter: (p: number) =>
          "$" + p.toLocaleString("en-US", { maximumFractionDigits: 0 }),
      },
    });
    equitySeries.setData(
      data.map((d) => ({ time: d.time as Time, value: d.equity })),
    );

    // Benchmark line
    if (benchmark.length > 1) {
      const bmScale = data[0].equity / benchmark[0].equity;
      const bmSeries = chart.addSeries(LineSeries, {
        color: "#58a6ff",
        lineWidth: 1,
        lineStyle: 2, // dashed
        title: benchmarkSymbol,
        priceFormat: {
          type: "custom",
          formatter: (p: number) =>
            "$" + p.toLocaleString("en-US", { maximumFractionDigits: 0 }),
        },
      });
      bmSeries.setData(
        benchmark.map((d) => ({
          time: d.time as Time,
          value: d.equity * bmScale,
        })),
      );
    }

    // Drawdown histogram (sub-pane)
    let peak = data[0].equity;
    const ddData = data.map((d) => {
      if (d.equity > peak) peak = d.equity;
      const dd = (d.equity - peak) / peak;
      return {
        time: d.time as Time,
        value: dd,
        color: dd < -0.1 ? "rgba(248,81,73,0.5)" : "rgba(248,81,73,0.25)",
      };
    });
    const ddSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: "dd",
      priceFormat: {
        type: "custom",
        formatter: (p: number) => (p * 100).toFixed(1) + "%",
      },
    });
    ddSeries.setData(ddData);
    chart
      .priceScale("dd")
      .applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    // Tooltip on crosshair move
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.point) {
        setTooltip(null);
        return;
      }
      const eqVal = param.seriesData.get(equitySeries);
      if (!eqVal || !("value" in eqVal)) {
        setTooltip(null);
        return;
      }
      const eq = (eqVal as any).value as number;
      const initial = data[0].equity;
      const ret = (((eq - initial) / initial) * 100).toFixed(2);
      // Find drawdown
      let p = initial;
      for (const d of data) {
        if (d.time > (param.time as number)) break;
        if (d.equity > p) p = d.equity;
      }
      const dd = (((eq - p) / p) * 100).toFixed(2);
      const dateStr = new Date((param.time as number) * 1000)
        .toISOString()
        .slice(0, 10);
      setTooltip({
        x: param.point.x,
        y: param.point.y,
        time: dateStr,
        equity: "$" + eq.toLocaleString("en-US", { maximumFractionDigits: 0 }),
        dd: dd + "%",
        ret: ret + "%",
      });
    });

    chart.timeScale().fitContent();

    const resizeObserver = new ResizeObserver(() => {
      if (container.clientWidth > 0) chart.resize(container.clientWidth, 340);
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, [data, benchmark, benchmarkSymbol]);

  return (
    <div style={{ position: "relative" }}>
      <div ref={containerRef} style={{ width: "100%", height: "340px" }} />
      {tooltip && (
        <div
          style={{
            position: "absolute",
            left: Math.min(
              tooltip.x + 16,
              (containerRef.current?.clientWidth || 800) - 180,
            ),
            top: Math.max(tooltip.y - 80, 4),
            background: "rgba(22,27,34,0.95)",
            border: "1px solid var(--color-border)",
            borderRadius: "6px",
            padding: "8px 12px",
            fontSize: "0.75rem",
            pointerEvents: "none",
            zIndex: 10,
            lineHeight: 1.6,
          }}
        >
          <div style={{ color: "#8b949e" }}>{tooltip.time}</div>
          <div>
            <span style={{ color: "#8b949e" }}>Equity: </span>
            <b>{tooltip.equity}</b>
          </div>
          <div>
            <span style={{ color: "#8b949e" }}>Return: </span>
            <span
              style={{
                color: parseFloat(tooltip.ret) >= 0 ? "#3fb950" : "#f85149",
              }}
            >
              {tooltip.ret}
            </span>
          </div>
          <div>
            <span style={{ color: "#8b949e" }}>Drawdown: </span>
            <span style={{ color: "#f85149" }}>{tooltip.dd}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function CapmScatterPlot({ capmData }: { capmData: BacktestCapmResponse }) {
  const points = capmData.scatter_data || [];
  if (!points.length) {
    return (
      <div style={{ color: "var(--color-text-muted)", padding: "10px 0" }}>
        No aligned daily returns available for CAPM scatter plot.
      </div>
    );
  }

  const xValues = points.map((point) => point.x);
  const minX = Math.min(...xValues);
  const maxX = Math.max(...xValues);
  const lineSegment: [{ x: number; y: number }, { x: number; y: number }] = [
    { x: minX, y: capmData.regression_line.slope * minX + capmData.regression_line.intercept },
    { x: maxX, y: capmData.regression_line.slope * maxX + capmData.regression_line.intercept },
  ];

  const formatReturn = (value: number | string) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) {
      return String(value);
    }
    return `${(numeric * 100).toFixed(2)}%`;
  };

  return (
    <div>
      <div style={{ fontSize: "0.8rem", color: "var(--color-text-muted)", marginBottom: "10px" }}>
        Beta = {capmData.beta.toFixed(2)} | Alpha = {(capmData.alpha * 100).toFixed(2)}% (ann.) | R² ={" "}
        {capmData.r_squared.toFixed(2)}
      </div>
      <div style={{ width: "100%", height: "340px" }}>
        <ResponsiveContainer width="100%" height="100%">
          <ScatterChart margin={{ top: 10, right: 20, bottom: 20, left: 8 }}>
            <CartesianGrid stroke="#30363d" strokeDasharray="3 3" />
            <XAxis
              type="number"
              dataKey="x"
              name="Market Daily Returns"
              label={{
                value: "Market Daily Returns",
                position: "insideBottom",
                offset: -10,
                fill: "#8b949e",
              }}
              tickFormatter={formatReturn}
              stroke="#8b949e"
              tick={{ fill: "#8b949e", fontSize: 12 }}
            />
            <YAxis
              type="number"
              dataKey="y"
              name="Strategy Daily Returns"
              label={{
                value: "Strategy Daily Returns",
                angle: -90,
                position: "insideLeft",
                fill: "#8b949e",
              }}
              tickFormatter={formatReturn}
              stroke="#8b949e"
              tick={{ fill: "#8b949e", fontSize: 12 }}
            />
            <Tooltip
              cursor={{ stroke: "#8b949e", strokeDasharray: "3 3" }}
              contentStyle={{
                background: "#161b22",
                border: "1px solid #30363d",
                borderRadius: "6px",
                color: "#c9d1d9",
              }}
            />
            <Scatter name="Daily Returns" data={points} fill="#58a6ff" />
            <ReferenceLine segment={lineSegment} stroke="#f85149" strokeWidth={2} />
          </ScatterChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
