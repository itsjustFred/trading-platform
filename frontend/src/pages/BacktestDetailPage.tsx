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
  deleteBacktest,
} from "../api/client";
import { StatCard } from "../components/StatCard";
import { TradeTable, type TradeTableRow } from "../components/TradeTable";
import { sharpeTone } from "../lib/statFormat";
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
import { requireMetricScale } from "../api/metricScale";
import { formatPValue, pValueColor } from "../utils/stats";
import { useToast } from "../components/toastContext";

const BENCHMARK_SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "XAUUSD"];
const TRADE_PAGE_SIZE = 100;

/** Accepts the untyped equity payloads the API returns and coerces them to numbers. */
function normalizeEquityPoints(points: unknown): EquityPoint[] {
  if (!Array.isArray(points)) return [];
  const normalized = points
    .map((point: unknown) => {
      const record = (point ?? {}) as { time?: unknown; equity?: unknown };
      const time =
        typeof record.time === "number" ? record.time : Number(record.time);
      const equity =
        typeof record.equity === "number" ? record.equity : Number(record.equity);
      if (!Number.isFinite(time) || !Number.isFinite(equity)) return null;
      return { time, equity };
    })
    .filter((point): point is EquityPoint => point !== null)
    .sort((left, right) => left.time - right.time);

  const deduped: EquityPoint[] = [];
  for (const point of normalized) {
    const last = deduped.at(-1);
    if (last?.time === point.time) {
      deduped[deduped.length - 1] = point;
      continue;
    }
    deduped.push(point);
  }
  return deduped;
}

interface CapmSectionProps {
  loading: boolean;
  error: string;
  data: BacktestCapmResponse | null;
}

function CapmSection({ loading, error, data }: Readonly<CapmSectionProps>) {
  if (loading) {
    return (
      <div style={{ color: "var(--color-text-muted)", padding: "14px 0" }}>
        Loading CAPM analysis...
      </div>
    );
  }
  if (error) {
    return (
      <div style={{ color: "var(--color-red)", padding: "14px 0" }}>
        Error: {error}
      </div>
    );
  }
  if (!data) {
    return (
      <div style={{ color: "var(--color-text-muted)", padding: "14px 0" }}>
        No CAPM data available.
      </div>
    );
  }
  return (
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
          value={data.alpha * 100}
          format="percent"
          color={data.alpha >= 0 ? "#3fb950" : "#f85149"}
        />
        <StatCard label="Beta" value={data.beta.toFixed(2)} color="#58a6ff" />
        <StatCard
          label="R²"
          value={data.r_squared.toFixed(2)}
          color="#c9d1d9"
        />
        <StatCard
          label="P-Value"
          value={data.p_value.toFixed(3)}
          color={data.p_value < 0.05 ? "#3fb950" : "#f85149"}
        />
      </div>
      <CapmScatterPlot capmData={data} />
    </>
  );
}

export function BacktestDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [info, setInfo] = useState<BacktestInfo | null>(null);
  const [stats, setStats] = useState<BacktestStats | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [tradePage, setTradePage] = useState(1);
  const [hasMoreTrades, setHasMoreTrades] = useState(false);
  const [tradesLoading, setTradesLoading] = useState(false);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [benchmarkSymbol, setBenchmarkSymbol] = useState("");
  const [benchmarkData, setBenchmarkData] = useState<EquityPoint[]>([]);
  const [capmData, setCapmData] = useState<BacktestCapmResponse | null>(null);
  const [capmLoading, setCapmLoading] = useState(false);
  const [capmError, setCapmError] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showRenameModal, setShowRenameModal] = useState(false);
  const [showOptimizeModal, setShowOptimizeModal] = useState(false);
  const [savingRename, setSavingRename] = useState(false);
  const [renameError, setRenameError] = useState("");
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    setTradePage(1);
    Promise.all([
      getBacktestInfo(id),
      getBacktestStats(id),
      getBacktestEquity(id, { max_points: 4000, sampling: "minmax" }),
    ])
      .then(([i, s, e]) => {
        setInfo(i);
        setStats(s);
        setEquity(normalizeEquityPoints(e));
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message || String(err));
        setLoading(false);
      });
  }, [id]);

  useEffect(() => {
    if (!id) return;
    setTradesLoading(true);
    const offset = Math.max(0, (tradePage - 1) * TRADE_PAGE_SIZE);
    getBacktestTrades(id, { offset, limit: TRADE_PAGE_SIZE })
      .then((rows) => {
        setTrades(rows || []);
        setHasMoreTrades((rows || []).length >= TRADE_PAGE_SIZE);
      })
      .catch((err) => {
        setError(err.message || String(err));
      })
      .finally(() => {
        setTradesLoading(false);
      });
  }, [id, tradePage]);

  // Load benchmark data when symbol changes
  useEffect(() => {
    if (!benchmarkSymbol || !info) {
      setBenchmarkData([]);
      return;
    }
    const start = info?.timeframe?.start?.slice(0, 10) || "";
    const end = info?.timeframe?.end?.slice(0, 10) || "";
    getBenchmark(benchmarkSymbol, start, end)
      .then((data) =>
        setBenchmarkData(normalizeEquityPoints(data.equity || [])),
      )
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
      .catch((error_: unknown) => {
        const message =
          error_ instanceof Error && error_.message
            ? error_.message
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
      await renameBacktest(id, nextTitle);
      const refreshed = await getBacktestInfo(id);
      setInfo(refreshed);
      setShowRenameModal(false);
    } catch (renameIssue: unknown) {
      setRenameError(
        renameIssue instanceof Error && renameIssue.message
          ? renameIssue.message
          : "Could not rename backtest",
      );
    } finally {
      setSavingRename(false);
    }
  };

  const handleDelete = async () => {
    if (!id || deleting) return;
    const confirmed = window.confirm("Delete this backtest?");
    if (!confirmed) return;
    setDeleting(true);
    try {
      await deleteBacktest(id);
      showToast("Backtest deleted.", "success");
      navigate("/backtests", { replace: true });
    } catch (error_: unknown) {
      const message =
        error_ instanceof Error && error_.message
          ? error_.message
          : "Could not delete backtest";
      showToast(message, "error");
      setDeleting(false);
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

  // Whole-backtest figures come from the stats payload; the loaded trade
  // table is a single page (TRADE_PAGE_SIZE rows), so page-derived values
  // must be labeled as page-scoped, never presented as whole-backtest stats.
  const statsAreWholeBacktest =
    typeof stats?.winners === "number" &&
    typeof stats?.losers === "number" &&
    typeof stats?.total_trades === "number" &&
    (stats.total_trades ?? 0) <= trades.length;
  const totalTradesFromStats =
    typeof stats?.total_trades === "number" ? stats.total_trades : trades.length;
  const totalPnlFromStats =
    typeof stats?.net_profit === "number"
      ? stats.net_profit
      : trades.reduce((sum, trade) => sum + (trade.pnl || 0), 0);
  // Fallback only when the stats payload omits the average: net profit over
  // the reported trade count, else 0 when neither is usable.
  const canDeriveAvgPnl =
    typeof stats?.total_trades === "number" && stats.total_trades > 0;
  let avgPnlFromStats = 0;
  if (typeof stats?.avg_trade_pnl === "number") {
    avgPnlFromStats = stats.avg_trade_pnl;
  } else if (canDeriveAvgPnl) {
    avgPnlFromStats = totalPnlFromStats / (stats?.total_trades as number);
  }
  const wholeBacktestStats = {
    totalPnl: totalPnlFromStats,
    totalTrades: totalTradesFromStats,
    wins: typeof stats?.winners === "number" ? stats.winners : trades.filter((t) => t.pnl > 0).length,
    losses: typeof stats?.losers === "number" ? stats.losers : trades.filter((t) => t.pnl <= 0).length,
    avgPnl: avgPnlFromStats,
    bestTrade:
      typeof stats?.best_trade === "number" ? stats.best_trade : null,
    worstTrade:
      typeof stats?.worst_trade === "number" ? stats.worst_trade : null,
  };
  const tradeStats = {
    ...wholeBacktestStats,
    // Page-scoped observations from the loaded table only.
    longTrades: trades.filter((trade) => trade.direction === "LONG").length,
    shortTrades: trades.filter((trade) => trade.direction === "SHORT").length,
    pageTrades: trades.length,
    statsAreWholeBacktest,
  };

  const toNumberOrNull = (value: unknown): number | null => {
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim()) {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
    return null;
  };

  const tradeRows = trades.map<TradeTableRow>((trade) => {
    const duration =
      trade.duration?.trim() ||
      (Number.isFinite(trade.holding_bars) ? `${trade.holding_bars} bars` : "");
    const timestamp = trade.entry_time_iso || trade.exit_time_iso || "";
    const parsed = Date.parse(timestamp);
    const date = Number.isFinite(parsed)
      ? new Date(parsed).toISOString().slice(0, 10)
      : "";
    const time = Number.isFinite(parsed)
      ? new Date(parsed).toISOString().slice(11, 19)
      : timestamp;
    const tradeMeta = (
      trade.meta && typeof trade.meta === "object" ? trade.meta : {}
    ) as Record<string, unknown>;
    const signalTimestamp =
      typeof tradeMeta.signal_timestamp === "string"
        ? tradeMeta.signal_timestamp
        : "";
    let signalSource = "";
    if (typeof tradeMeta.signal_source === "string") {
      signalSource = tradeMeta.signal_source;
    } else if (typeof info?.parameters?.signal_source === "string") {
      signalSource = info.parameters.signal_source;
    }
    const fillLagFromMeta = toNumberOrNull(tradeMeta.fill_delay_seconds);
    const fillLagFromTimestamps = (() => {
      const signalTimeMs = Date.parse(signalTimestamp || "");
      const fillTimeMs = Date.parse(trade.entry_time_iso || "");
      if (!Number.isFinite(signalTimeMs) || !Number.isFinite(fillTimeMs)) {
        return null;
      }
      const diffSeconds = (fillTimeMs - signalTimeMs) / 1000;
      return Number.isFinite(diffSeconds)
        ? Math.max(0, Math.round(diffSeconds))
        : null;
    })();
    const signalStopLoss = toNumberOrNull(tradeMeta.signal_stop_loss);
    const signalTakeProfit = toNumberOrNull(tradeMeta.signal_take_profit);
    return {
      id: String(trade.nr),
      date,
      time,
      strategy:
        info?.title || info?.strategy_display_name || info?.strategy || "",
      symbol: trade.symbol,
      side: trade.direction,
      signalSource,
      signalTime: signalTimestamp,
      fillLagSec: fillLagFromMeta ?? fillLagFromTimestamps,
      size: trade.position_size ?? trade.quantity,
      entryPrice: trade.entry_price,
      exitPrice: trade.exit_price,
      processorStopLoss: signalStopLoss,
      processorTakeProfit: signalTakeProfit,
      pnl: trade.pnl,
      pnlPct: trade.pnl_pct,
      exitReason: trade.exit_reason ?? "",
      duration,
    };
  });

  const engine = String(info?.engine || "")
    .trim()
    .toLowerCase();
  const isSignalReplay =
    engine === "signal-replay" || engine === "signal_replay";

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
            type="button"
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
                {info?.title ||
                  info?.strategy_display_name ||
                  info?.strategy ||
                  id}
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
              {isSignalReplay ? (
                <span style={{ marginRight: "16px" }}>
                  📡{" "}
                  {String(info?.parameters?.signal_source || "signal-replay")}
                </span>
              ) : null}
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
                    <span style={{ color: "var(--color-text-muted)" }}>
                      {key}
                    </span>
                    <span style={{ color: "rgba(88,166,255,0.7)" }}>:</span>
                    <span style={{ color: "#e6edf3" }}>{String(val)}</span>
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
          <button
            type="button"
            onClick={() => void handleDelete()}
            disabled={deleting}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "8px",
              background: "rgba(127, 29, 29, 0.26)",
              color: "#fecaca",
              border: "1px solid rgba(248,113,113,0.3)",
              borderRadius: "10px",
              padding: "10px 14px",
              cursor: deleting ? "not-allowed" : "pointer",
              opacity: deleting ? 0.65 : 1,
            }}
          >
            {deleting ? "Deleting..." : "Delete"}
          </button>
          {info?.strategy_slug || info?.strategy ? (
            <>
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
            </>
          ) : null}
        </div>
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
            percentScale={requireMetricScale("backtest_stats", "net_profit_pct")}
          />
          <StatCard
            label="CAGR (higher = better)"
            value={stats.cagr}
            format="percent"
            percentScale={requireMetricScale("backtest_stats", "cagr")}
          />
          <StatCard
            label="Sharpe (higher = better)"
            value={stats.sharpe?.toFixed(3)}
            color={sharpeTone(stats.sharpe)}
          />
          <StatCard
            label="Sortino Ratio"
            value={stats.sortino?.toFixed(3)}
            color={stats.sortino > 1 ? "#3fb950" : "#d29922"}
          />
          <StatCard
            label="Max DD (lower = better)"
            value={stats.max_drawdown_pct}
            format="percent"
            percentScale={requireMetricScale("backtest_stats", "max_drawdown_pct")}
            color="#f85149"
          />
          <StatCard
            label="Win Rate (higher = better)"
            value={stats.win_rate}
            format="percent"
            percentScale={requireMetricScale("backtest_stats", "win_rate")}
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
            label="PSR (higher = better)"
            value={stats.psr}
            format="percent"
            percentScale={requireMetricScale("backtest_stats", "psr")}
            color={
              (stats.psr ?? 0) > 0.95
                ? "#3fb950"
                : (stats.psr ?? 0) > 0.5
                  ? "#d29922"
                  : "#f85149"
            }
          />
          <StatCard
            label="p-Value (lower = better)"
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
              htmlFor="equity-chart-mode"
              style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}
            >
              Benchmark:
            </label>
            <select
              id="equity-chart-mode"
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
          <h2 style={{ fontSize: "1rem", fontWeight: 600, margin: 0 }}>
            CAPM Analysis
          </h2>
          <span
            style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}
          >
            Benchmark:{" "}
            {capmData?.benchmark_symbol ||
              benchmarkSymbol ||
              info?.symbols?.[0] ||
              "N/A"}
          </span>
        </div>

        <CapmSection loading={capmLoading} error={capmError} data={capmData} />
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
          {!tradeStats.statsAreWholeBacktest && tradeStats.pageTrades < tradeStats.totalTrades ? (
            <span
              style={{
                marginLeft: "10px",
                fontSize: "0.72rem",
                fontWeight: 400,
                color: "var(--color-text-muted)",
              }}
            >
              (whole-backtest stats; long/short counts cover the {tradeStats.pageTrades} loaded trades of{" "}
              {tradeStats.totalTrades})
            </span>
          ) : null}
        </h2>
        <div
          style={{
            display: "flex",
            gap: "20px",
            flexWrap: "wrap",
            marginBottom: "14px",
          }}
        >
          <SummaryItem
            label="Total Trades"
            value={String(tradeStats.totalTrades)}
          />
          <SummaryItem
            label="Long / Short"
            value={`${tradeStats.longTrades} / ${tradeStats.shortTrades}`}
          />
          <SummaryItem
            label="Win / Loss"
            value={`${tradeStats.wins} / ${tradeStats.losses}`}
            color={`#3fb950 / #f85149`}
          />
          <SummaryItem
            label="Total P&L"
            value={`$${tradeStats.totalPnl.toLocaleString("en-US", { maximumFractionDigits: 2 })}`}
            color={tradeStats.totalPnl >= 0 ? "#3fb950" : "#f85149"}
          />
          <SummaryItem
            label="Avg P&L"
            value={`$${tradeStats.avgPnl.toLocaleString("en-US", { maximumFractionDigits: 2 })}`}
            color={tradeStats.avgPnl >= 0 ? "#3fb950" : "#f85149"}
          />
          <SummaryItem
            label="Best Trade"
            value={
              tradeStats.bestTrade != null
                ? `$${tradeStats.bestTrade.toLocaleString("en-US", { maximumFractionDigits: 2 })}`
                : "N/A"
            }
            color="#3fb950"
          />
          <SummaryItem
            label="Worst Trade"
            value={
              tradeStats.worstTrade != null
                ? `$${tradeStats.worstTrade.toLocaleString("en-US", { maximumFractionDigits: 2 })}`
                : "N/A"
            }
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
          Trades (Page {tradePage}, {trades.length} rows)
        </h2>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "12px",
            marginBottom: "14px",
          }}
        >
          <div
            style={{ color: "var(--color-text-muted)", fontSize: "0.85rem" }}
          >
            Loaded in pages of {TRADE_PAGE_SIZE} trades.
          </div>
          <div style={{ display: "flex", gap: "8px" }}>
            <button
              type="button"
              onClick={() =>
                setTradePage((current) => Math.max(1, current - 1))
              }
              disabled={tradePage <= 1 || tradesLoading}
              style={{
                borderRadius: "8px",
                border: "1px solid var(--color-border)",
                background: "var(--color-surface2)",
                color: "var(--color-text)",
                padding: "6px 10px",
                cursor:
                  tradePage <= 1 || tradesLoading ? "not-allowed" : "pointer",
              }}
            >
              Prev
            </button>
            <button
              type="button"
              onClick={() => setTradePage((current) => current + 1)}
              disabled={!hasMoreTrades || tradesLoading}
              style={{
                borderRadius: "8px",
                border: "1px solid var(--color-border)",
                background: "var(--color-surface2)",
                color: "var(--color-text)",
                padding: "6px 10px",
                cursor:
                  !hasMoreTrades || tradesLoading ? "not-allowed" : "pointer",
              }}
            >
              Next
            </button>
          </div>
        </div>
        {tradesLoading ? (
          <div
            style={{ marginBottom: "12px", color: "var(--color-text-muted)" }}
          >
            Loading trades...
          </div>
        ) : null}
        <TradeTable
          trades={tradeRows}
          showSignalColumns={isSignalReplay}
          onTradeClick={(row) => navigate(`/backtests/${id}/trade/${row.id}`)}
        />
      </div>

      <RenameModal
        open={showRenameModal}
        title="Rename Backtest"
        label="Backtest title"
        value={
          info?.title ||
          info?.strategy_display_name ||
          info?.strategy ||
          id ||
          ""
        }
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
        backtestTitle={
          info?.title ||
          info?.strategy_display_name ||
          info?.strategy ||
          id ||
          ""
        }
        parameters={info?.parameters || {}}
      />
    </div>
  );
}

function SummaryItem({
  label,
  value,
  color,
}: Readonly<{
  label: string;
  value: string;
  color?: string;
}>) {
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
}: Readonly<{
  data: { time: number; equity: number }[];
  benchmark?: { time: number; equity: number }[];
  benchmarkSymbol?: string;
}>) {
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
  // Chart container width, tracked by the chart's ResizeObserver.
  const [containerWidth, setContainerWidth] = useState(0);

  useEffect(() => {
    const normalizedData = normalizeEquityPoints(data);
    const normalizedBenchmark = normalizeEquityPoints(benchmark);
    if (!containerRef.current || !normalizedData.length) return;

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
      timeScale: {
        borderColor: "#30363d",
        timeVisible: false,
        minBarSpacing: 0.1,
      },
      rightPriceScale: { borderColor: "#30363d", invertScale: false },
    });
    chartRef.current = chart;

    const isProfit =
      (normalizedData.at(-1)?.equity ?? 0) >= (normalizedData[0]?.equity ?? 0);
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
      normalizedData.map((d) => ({ time: d.time as Time, value: d.equity })),
    );

    // Benchmark line
    if (normalizedBenchmark.length > 1) {
      const bmScale = normalizedData[0].equity / normalizedBenchmark[0].equity;
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
        normalizedBenchmark.map((d) => ({
          time: d.time as Time,
          value: d.equity * bmScale,
        })),
      );
    }

    // Drawdown histogram (sub-pane)
    let peak = normalizedData[0].equity;
    const ddData = normalizedData.map((d) => {
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
      const eq = (eqVal as { value: unknown }).value as number;
      const initial = normalizedData[0].equity;
      const ret = (((eq - initial) / initial) * 100).toFixed(2);
      // Find drawdown
      let p = initial;
      for (const d of normalizedData) {
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
      if (container.clientWidth > 0) {
        chart.resize(container.clientWidth, 340);
        // Tracked in state so the tooltip can be placed during render.
        setContainerWidth(container.clientWidth);
      }
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
            left: Math.min(tooltip.x + 16, (containerWidth || 800) - 180),
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
                color:
                  Number.parseFloat(tooltip.ret) >= 0 ? "#3fb950" : "#f85149",
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

function CapmScatterPlot({
  capmData,
}: Readonly<{ capmData: BacktestCapmResponse }>) {
  const points = capmData.scatter_data || [];
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [chartWidth, setChartWidth] = useState(0);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const syncWidth = () => {
      const measured = Math.floor(container.clientWidth);
      setChartWidth((prev) => (prev !== measured ? measured : prev));
    };

    syncWidth();
    const observer = new ResizeObserver(syncWidth);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

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
    {
      x: minX,
      y:
        capmData.regression_line.slope * minX +
        capmData.regression_line.intercept,
    },
    {
      x: maxX,
      y:
        capmData.regression_line.slope * maxX +
        capmData.regression_line.intercept,
    },
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
      <div
        style={{
          fontSize: "0.8rem",
          color: "var(--color-text-muted)",
          marginBottom: "10px",
        }}
      >
        Beta = {capmData.beta.toFixed(2)} | Alpha ={" "}
        {(capmData.alpha * 100).toFixed(2)}% (ann.) | R² ={" "}
        {capmData.r_squared.toFixed(2)}
      </div>
      <div
        ref={containerRef}
        style={{ width: "100%", height: "340px", minWidth: 0 }}
      >
        {chartWidth > 0 ? (
          <ScatterChart
            width={chartWidth}
            height={340}
            margin={{ top: 10, right: 20, bottom: 20, left: 8 }}
          >
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
            <ReferenceLine
              segment={lineSegment}
              stroke="#f85149"
              strokeWidth={2}
            />
          </ScatterChart>
        ) : null}
      </div>
    </div>
  );
}
