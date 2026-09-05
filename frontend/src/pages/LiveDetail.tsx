import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import {
  AreaSeries,
  ColorType,
  createChart,
  CrosshairMode,
  LineSeries,
} from "lightweight-charts";
import type { IChartApi, Time } from "lightweight-charts";

import {
  getLiveBalances,
  getLiveStats,
  getLiveTrades,
  getSignalProcessors,
  runBacktestSignals,
} from "../api/client";
import { liveWinRateToPoints } from "../api/metricScale";
import type {
  LiveAccountStats,
  LiveBalanceRecord,
  SignalProcessorOption,
  LiveTradeSlippage,
} from "../api/types";
import { TradeTable, type TradeTableRow } from "../components/TradeTable";
import { Modal } from "../components/Modal";
import { useToast } from "../components/ToastProvider";

const LIVE_DETAIL_POLL_MS = 30000;
const DEFAULT_REPLAY_TIMEFRAME = "1h";

type ReplayFormState = {
  start_date: string;
  end_date: string;
  timeframe: string;
  signal_processor: string;
  auto_download: boolean;
};

type LiveDetailStats = {
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  totalPnl: number;
  maxDrawdown: number;
  currentBalance: number;
  currentEquity: number;
};

export function LiveDetailPage() {
  const { accountId: rawAccountId } = useParams<{ accountId: string }>();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const accountId = decodeURIComponent(rawAccountId ?? "");

  const [balances, setBalances] = useState<LiveBalanceRecord[]>([]);
  const [trades, setTrades] = useState<LiveTradeSlippage[]>([]);
  const [deploymentIds, setDeploymentIds] = useState<string[]>([]);
  const [stats, setStats] = useState<LiveDetailStats>({
    totalTrades: 0,
    winningTrades: 0,
    losingTrades: 0,
    winRate: 0,
    totalPnl: 0,
    maxDrawdown: 0,
    currentBalance: 0,
    currentEquity: 0,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [replayModalOpen, setReplayModalOpen] = useState(false);
  const [isStartingReplay, setIsStartingReplay] = useState(false);
  const [signalProcessors, setSignalProcessors] = useState<SignalProcessorOption[]>([]);
  const [signalProcessorsLoading, setSignalProcessorsLoading] = useState(false);
  // Tracks the last modal-open transition that already kicked off a fetch, so the
  // modal fetches processors once per open instead of in an infinite loop.
  const processorsFetchedForOpenRef = useRef(false);
  const [replayForm, setReplayForm] = useState<ReplayFormState>(() => {
    const today = new Date();
    const endDate = today.toISOString().slice(0, 10);
    const startDate = new Date(today.getTime() - (30 * 24 * 60 * 60 * 1000)).toISOString().slice(0, 10);
    return {
      start_date: startDate,
      end_date: endDate,
      timeframe: DEFAULT_REPLAY_TIMEFRAME,
      signal_processor: "",
      auto_download: true,
    };
  });

  useEffect(() => {
    if (!accountId) {
      setError("Missing account id");
      setLoading(false);
      return;
    }

    let ignore = false;

    const load = async () => {
      try {
        const [balancesPayload, tradesPayload] = await Promise.all([
          getLiveBalances(accountId),
          getLiveTrades(accountId),
        ]);
        if (ignore) return;

        const sortedTrades = [...tradesPayload.trades].sort(
          (a, b) => b.execution_time - a.execution_time,
        );
        setBalances(balancesPayload.series);
        setTrades(sortedTrades);
        setDeploymentIds(tradesPayload.deployment_ids);

        try {
          const statsPayload = await getLiveStats(accountId);
          if (ignore) return;
          setStats(normalizeStats(statsPayload, balancesPayload.series, sortedTrades));
        } catch {
          if (ignore) return;
          setStats(buildFallbackStats(balancesPayload.series, sortedTrades));
        }

        setError("");
      } catch (issue: unknown) {
        if (ignore) return;
        const message =
          issue instanceof Error ? issue.message : "Failed to load live account details";
        setError(message);
      } finally {
        if (!ignore) {
          setLoading(false);
        }
      }
    };

    void load();
    const intervalId = window.setInterval(() => {
      void load();
    }, LIVE_DETAIL_POLL_MS);

    return () => {
      ignore = true;
      window.clearInterval(intervalId);
    };
  }, [accountId]);

  useEffect(() => {
    if (!replayModalOpen) {
      // Allow the next open transition to refetch (processors can appear meanwhile).
      processorsFetchedForOpenRef.current = false;
      return;
    }
    if (processorsFetchedForOpenRef.current) {
      return;
    }
    processorsFetchedForOpenRef.current = true;
    setSignalProcessorsLoading(true);
    getSignalProcessors()
      .then((payload) => setSignalProcessors(payload))
      .catch(() => setSignalProcessors([]))
      .finally(() => setSignalProcessorsLoading(false));
  }, [replayModalOpen]);

  const equitySeries = useMemo(
    () =>
      balances
        .map((item) => ({
          time: item.time as Time,
          equity: asFiniteNumber(item.equity, 0),
          balance: asFiniteNumber(item.balance, 0),
        }))
        .filter((item) => Number.isFinite(item.time)),
    [balances],
  );

  const tradeRows = trades.map<TradeTableRow>((row, index) => {
    const metadata = row.signal_metadata as Record<string, unknown>;
    const metadataPnl = Number(metadata?.pnl ?? NaN);
    const metadataPnlPct = Number(metadata?.pnl_pct ?? NaN);
    const direction = String(row.action || "").toUpperCase();
    const directionFactor = direction === "SELL" ? -1 : 1;
    const slippagePnl = -asFiniteNumber(row.slippage, 0) * asFiniteNumber(row.filled_qty, 0) * directionFactor;
    const resolvedPnl = Number.isFinite(metadataPnl) ? metadataPnl : slippagePnl;

    const durationFromMeta = String(
      metadata?.duration ?? metadata?.holding_time ?? metadata?.bars_held ?? "",
    ).trim();

    return {
      id: row.execution_id || row.signal_id || `${row.symbol}-${row.execution_time}-${index}`,
      time: row.execution_timestamp || row.signal_timestamp || "",
      symbol: row.symbol,
      side: direction || "BUY",
      size: Number.isFinite(row.position_size) ? row.position_size : row.filled_qty,
      entryPrice: row.theoretical_price,
      exitPrice: row.actual_price,
      pnl: resolvedPnl,
      pnlPct: Number.isFinite(metadataPnlPct) ? metadataPnlPct : null,
      exitReason:
        row.exit_reason ||
        String(metadata?.exit_reason ?? metadata?.reason ?? "").trim(),
      duration: durationFromMeta || null,
    };
  });

  const handleStartReplay = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!accountId) {
      showToast("Missing account id for replay.", "warning");
      return;
    }
    if (!replayForm.start_date || !replayForm.end_date || replayForm.start_date > replayForm.end_date) {
      showToast("Please choose a valid start/end date range.", "warning");
      return;
    }
    setIsStartingReplay(true);
    try {
      const response = await runBacktestSignals({
        signal_source: "discord",
        signal_processor: replayForm.signal_processor || undefined,
        account_id: accountId,
        start_date: replayForm.start_date,
        end_date: replayForm.end_date,
        timeframe: replayForm.timeframe || DEFAULT_REPLAY_TIMEFRAME,
        auto_download: replayForm.auto_download,
      });
      const taskId = response.task_id || response.job_id;
      setReplayModalOpen(false);
      showToast(`Signal replay started (task ${taskId}).`, "success");
      navigate("/backtests");
    } catch (issue: unknown) {
      const message = issue instanceof Error ? issue.message : "Failed to start signal replay";
      showToast(message, "error");
    } finally {
      setIsStartingReplay(false);
    }
  };

  if (loading) {
    return (
      <div style={{ padding: "24px", color: "var(--color-text-muted)" }}>
        Loading live account details...
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ padding: "24px", color: "var(--color-red)" }}>
        Error: {error}
      </div>
    );
  }

  return (
    <div style={{ padding: "24px" }}>
      <Modal
        open={replayModalOpen}
        onClose={() => setReplayModalOpen(false)}
        title="Backtest Signals"
        width="520px"
      >
        <form onSubmit={handleStartReplay} style={{ display: "grid", gap: "12px" }}>
          <label style={fieldLabelStyle}>
            Start Date
            <input
              type="date"
              value={replayForm.start_date}
              onChange={(event) =>
                setReplayForm((current) => ({ ...current, start_date: event.target.value }))
              }
              style={fieldInputStyle}
              required
            />
          </label>
          <label style={fieldLabelStyle}>
            End Date
            <input
              type="date"
              value={replayForm.end_date}
              onChange={(event) =>
                setReplayForm((current) => ({ ...current, end_date: event.target.value }))
              }
              style={fieldInputStyle}
              required
            />
          </label>
          <label style={fieldLabelStyle}>
            Signal Processor (optional)
            <select
              value={replayForm.signal_processor}
              onChange={(event) =>
                setReplayForm((current) => ({ ...current, signal_processor: event.target.value }))
              }
              style={fieldInputStyle}
            >
              <option value="">
                {signalProcessorsLoading ? "Loading processors..." : "No processor override"}
              </option>
              {signalProcessors.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label style={fieldLabelStyle}>
            Replay Timeframe
            <select
              value={replayForm.timeframe}
              onChange={(event) =>
                setReplayForm((current) => ({ ...current, timeframe: event.target.value }))
              }
              style={fieldInputStyle}
            >
              {["1m", "5m", "15m", "30m", "1h", "4h", "1d"].map((tf) => (
                <option key={tf} value={tf}>
                  {tf}
                </option>
              ))}
            </select>
          </label>
          <label style={{ ...fieldLabelStyle, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            Auto-download missing OHLCV
            <input
              type="checkbox"
              checked={replayForm.auto_download}
              onChange={(event) =>
                setReplayForm((current) => ({ ...current, auto_download: event.target.checked }))
              }
            />
          </label>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "4px" }}>
            <button type="button" onClick={() => setReplayModalOpen(false)} style={ghostButtonStyle} disabled={isStartingReplay}>
              Cancel
            </button>
            <button type="submit" style={primaryButtonStyle} disabled={isStartingReplay}>
              {isStartingReplay ? "Starting..." : "Start Replay"}
            </button>
          </div>
        </form>
      </Modal>

      <div
        style={{
          background:
            "linear-gradient(140deg, rgba(14, 165, 233, 0.14), rgba(59, 130, 246, 0.08) 45%, rgba(22, 27, 34, 0.95))",
          border: "1px solid rgba(59, 130, 246, 0.24)",
          borderRadius: "14px",
          padding: "20px",
          marginBottom: "18px",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "12px",
            flexWrap: "wrap",
          }}
        >
            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
              <Link
                to="/live"
              style={{
                border: "1px solid rgba(59, 130, 246, 0.3)",
                background: "rgba(59, 130, 246, 0.1)",
                color: "#bfdbfe",
                width: "34px",
                height: "34px",
                borderRadius: "10px",
                cursor: "pointer",
              }}
              title="Back to Live Hub"
            >
                <ArrowLeft size={16} />
              </Link>
              <button
                onClick={() => setReplayModalOpen(true)}
                style={{
                  border: "1px solid rgba(168, 85, 247, 0.45)",
                  background: "rgba(168, 85, 247, 0.16)",
                  color: "#e9d5ff",
                  height: "34px",
                  borderRadius: "10px",
                  cursor: "pointer",
                  padding: "0 12px",
                  fontSize: "0.8rem",
                  fontWeight: 600,
                }}
                title="Replay Discord signals as backtest"
              >
                Backtest Signals
              </button>
              <Link
                to={"/live/" + encodeURIComponent(accountId) + "/viewer"}
                style={{
                  border: "1px solid rgba(59, 130, 246, 0.38)",
                  background: "rgba(59, 130, 246, 0.14)",
                  color: "#bfdbfe",
                  height: "34px",
                  borderRadius: "10px",
                  cursor: "pointer",
                  padding: "0 12px",
                  fontSize: "0.8rem",
                  fontWeight: 600,
                  textDecoration: "none",
                  display: "inline-flex",
                  alignItems: "center",
                }}
                title="Open live viewer"
              >
                Open Live Viewer
              </Link>
              <div>
                <h1 style={{ margin: 0, fontSize: "1.4rem", fontWeight: 700 }}>
                  Live Account {accountId}
              </h1>
              <p
                style={{
                  margin: "3px 0 0",
                  color: "var(--color-text-muted)",
                  fontSize: "0.84rem",
                }}
              >
                Live equity performance and execution logs (auto-refresh 30s).
              </p>
            </div>
          </div>
          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
            {deploymentIds.map((deploymentId) => (
              <span
                key={deploymentId}
                style={{
                  border: "1px solid var(--color-border)",
                  borderRadius: "999px",
                  padding: "4px 10px",
                  fontSize: "0.73rem",
                  color: "var(--color-text-muted)",
                }}
              >
                {deploymentId}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "12px",
          padding: "18px",
          marginBottom: "16px",
        }}
      >
        <h2 style={{ margin: "0 0 12px", fontSize: "1rem", fontWeight: 600 }}>Equity Chart</h2>
        {equitySeries.length < 2 ? (
          <div style={{ color: "var(--color-text-muted)", padding: "42px 0", textAlign: "center" }}>
            No balance data yet.
          </div>
        ) : (
          <LiveEquityChart
            equitySeries={equitySeries.map((item) => ({ time: item.time, value: item.equity }))}
            balanceSeries={equitySeries.map((item) => ({ time: item.time, value: item.balance }))}
          />
        )}
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
          gap: "10px",
          marginBottom: "16px",
        }}
      >
        <MetricCard label="Total Trades" value={String(stats.totalTrades)} />
        <MetricCard label="Winning" value={String(stats.winningTrades)} valueColor="#86efac" />
        <MetricCard label="Losing" value={String(stats.losingTrades)} valueColor="#fecaca" />
        <MetricCard label="Win Rate" value={formatPercent(stats.winRate)} />
        <MetricCard
          label="Total PnL"
          value={formatSignedUsd(stats.totalPnl)}
          valueColor={stats.totalPnl >= 0 ? "#86efac" : "#fecaca"}
        />
        <MetricCard label="Current Balance" value={formatUsd(stats.currentBalance)} />
        <MetricCard label="Current Equity" value={formatUsd(stats.currentEquity)} />
        <MetricCard label="Max Drawdown" value={formatPercent(stats.maxDrawdown)} valueColor="#fca5a5" />
      </div>

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "12px",
          padding: "18px",
        }}
      >
        <h2 style={{ margin: "0 0 12px", fontSize: "1rem", fontWeight: 600 }}>
          Trades & Slippage ({trades.length})
        </h2>
        {trades.length === 0 ? (
          <div style={{ color: "var(--color-text-muted)", padding: "42px 0", textAlign: "center" }}>
            No fills captured yet.
          </div>
        ) : (
          <TradeTable trades={tradeRows} />
        )}
      </div>
    </div>
  );
}

function LiveEquityChart({
  equitySeries,
  balanceSeries,
}: {
  equitySeries: Array<{ time: Time; value: number }>;
  balanceSeries: Array<{ time: Time; value: number }>;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!containerRef.current || equitySeries.length < 2) {
      return;
    }

    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
    }

    const container = containerRef.current;
    const locale =
      typeof navigator !== "undefined" && navigator.language
        && navigator.language !== "undefined"
        ? navigator.language
        : "en-US";
    const chart = createChart(container, {
      width: container.clientWidth || 1000,
      height: 360,
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
      timeScale: { borderColor: "#30363d", timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: "#30363d" },
    });
    chartRef.current = chart;

    const start = equitySeries[0].value;
    const end = equitySeries[equitySeries.length - 1].value;
    const profit = end >= start;
    const lineColor = profit ? "#22c55e" : "#f87171";

    const equityChartSeries = chart.addSeries(AreaSeries, {
      lineColor,
      topColor: profit ? "rgba(34,197,94,0.24)" : "rgba(248,113,113,0.22)",
      bottomColor: "rgba(0,0,0,0)",
      lineWidth: 2,
      priceFormat: {
        type: "custom",
        formatter: (price: number) => formatUsd(price),
      },
      title: "Equity",
    });
    equityChartSeries.setData(equitySeries);

    if (balanceSeries.length > 0) {
      const balanceChartSeries = chart.addSeries(LineSeries, {
        color: "#38bdf8",
        lineWidth: 2,
        title: "Balance",
        priceFormat: {
          type: "custom",
          formatter: (price: number) => formatUsd(price),
        },
      });
      balanceChartSeries.setData(balanceSeries);
    }

    chart.timeScale().fitContent();

    const resizeObserver = new ResizeObserver(() => {
      if (container.clientWidth > 0) {
        chart.resize(container.clientWidth, 360);
      }
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, [equitySeries, balanceSeries]);

  return <div ref={containerRef} style={{ width: "100%", height: "360px" }} />;
}

const MetricCard = memo(function MetricCard({
  label,
  value,
  valueColor,
}: {
  label: string;
  value: string;
  valueColor?: string;
}) {
  return (
    <div
      style={{
        border: "1px solid var(--color-border)",
        borderRadius: "10px",
        padding: "10px",
        background: "rgba(13, 17, 23, 0.45)",
      }}
    >
      <div
        style={{
          fontSize: "0.76rem",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          color: "var(--color-text-muted)",
        }}
      >
        {label}
      </div>
      <div
        style={{
          marginTop: "6px",
          fontSize: "1.02rem",
          fontWeight: 600,
          color: valueColor ?? "#e2e8f0",
        }}
      >
        {value}
      </div>
    </div>
  );
});

const fieldLabelStyle: React.CSSProperties = {
  display: "grid",
  gap: "6px",
  fontSize: "0.82rem",
  color: "var(--color-text-muted)",
};

const fieldInputStyle: React.CSSProperties = {
  width: "100%",
  borderRadius: "10px",
  border: "1px solid var(--color-border)",
  background: "rgba(2, 6, 23, 0.42)",
  color: "var(--color-text)",
  padding: "10px 12px",
  fontSize: "0.87rem",
  outline: "none",
};

const ghostButtonStyle: React.CSSProperties = {
  border: "1px solid var(--color-border)",
  background: "rgba(148, 163, 184, 0.08)",
  color: "var(--color-text)",
  borderRadius: "10px",
  padding: "8px 12px",
  cursor: "pointer",
};

const primaryButtonStyle: React.CSSProperties = {
  border: "1px solid rgba(168, 85, 247, 0.45)",
  background: "rgba(168, 85, 247, 0.16)",
  color: "#e9d5ff",
  borderRadius: "10px",
  padding: "8px 12px",
  cursor: "pointer",
};

function normalizeStats(
  stats: LiveAccountStats,
  balances: LiveBalanceRecord[],
  trades: LiveTradeSlippage[],
): LiveDetailStats {
  const fallback = buildFallbackStats(balances, trades);
  return {
    totalTrades: asFiniteNumber(stats.total_trades, fallback.totalTrades),
    winningTrades: asFiniteNumber(stats.winning_trades, fallback.winningTrades),
    losingTrades: asFiniteNumber(stats.losing_trades, fallback.losingTrades),
    winRate: liveWinRateToPoints(stats.win_rate) ?? fallback.winRate,
    totalPnl: asFiniteNumber(stats.total_pnl, fallback.totalPnl),
    maxDrawdown: toDrawdownPercent(
      stats.max_drawdown_pct ?? stats.max_drawdown,
      fallback.maxDrawdown,
    ),
    currentBalance: asFiniteNumber(stats.current_balance, fallback.currentBalance),
    currentEquity: asFiniteNumber(stats.current_equity, fallback.currentEquity),
  };
}

function buildFallbackStats(
  balances: LiveBalanceRecord[],
  trades: LiveTradeSlippage[],
): LiveDetailStats {
  const totalTrades = trades.length;
  const realized = calculateRealizedTradeStats(trades);
  const winRate = totalTrades > 0 ? (realized.winningTrades / totalTrades) * 100 : 0;
  const currentBalance =
    balances.length > 0
      ? asFiniteNumber(balances[balances.length - 1].balance, 0)
      : 0;
  const currentEquity =
    balances.length > 0
      ? asFiniteNumber(balances[balances.length - 1].equity, 0)
      : 0;
  return {
    totalTrades,
    winningTrades: realized.winningTrades,
    losingTrades: realized.losingTrades,
    winRate,
    totalPnl: realized.totalPnl,
    maxDrawdown: maxDrawdownFromBalances(balances),
    currentBalance,
    currentEquity,
  };
}

function calculateRealizedTradeStats(trades: LiveTradeSlippage[]): {
  totalPnl: number;
  winningTrades: number;
  losingTrades: number;
} {
  const positions = new Map<string, { qty: number; avg: number }>();
  let totalPnl = 0;
  let winningTrades = 0;
  let losingTrades = 0;

  const orderedTrades = [...trades].sort((a, b) => a.execution_time - b.execution_time);
  for (const trade of orderedTrades) {
    const action = String(trade.action || "").toLowerCase();
    const direction = action === "buy" ? 1 : action === "sell" ? -1 : 0;
    if (direction === 0) {
      continue;
    }

    const quantity = Math.abs(asFiniteNumber(trade.filled_qty, 0));
    const price = asFiniteNumber(trade.actual_price, 0);
    if (quantity <= 0 || price <= 0) {
      continue;
    }

    const symbol = String(trade.symbol || "UNKNOWN").toUpperCase();
    const delta = quantity * direction;
    const current = positions.get(symbol) ?? { qty: 0, avg: 0 };
    const currentQty = current.qty;
    const currentAvg = current.avg;

    if (currentQty === 0) {
      positions.set(symbol, { qty: delta, avg: price });
      continue;
    }

    if (currentQty * delta > 0) {
      const combinedQty = Math.abs(currentQty) + Math.abs(delta);
      const nextAvg =
        combinedQty > 0
          ? ((Math.abs(currentQty) * currentAvg) + (Math.abs(delta) * price)) / combinedQty
          : price;
      positions.set(symbol, { qty: currentQty + delta, avg: nextAvg });
      continue;
    }

    const closedQty = Math.min(Math.abs(currentQty), Math.abs(delta));
    if (closedQty > 0) {
      const sideSign = currentQty > 0 ? 1 : -1;
      const realizedPnl = (price - currentAvg) * closedQty * sideSign;
      totalPnl += realizedPnl;
      if (realizedPnl > 0) {
        winningTrades += 1;
      } else if (realizedPnl < 0) {
        losingTrades += 1;
      }
    }

    const newQty = currentQty + delta;
    if (newQty === 0) {
      positions.set(symbol, { qty: 0, avg: 0 });
    } else if (currentQty * newQty > 0) {
      positions.set(symbol, { qty: newQty, avg: currentAvg });
    } else {
      positions.set(symbol, { qty: newQty, avg: price });
    }
  }

  return { totalPnl, winningTrades, losingTrades };
}

function maxDrawdownFromBalances(balances: LiveBalanceRecord[]): number {
  if (balances.length < 2) {
    return 0;
  }
  let peak = asFiniteNumber(balances[0].balance, 0);
  let maxDrawdown = 0;
  for (const point of balances) {
    const balance = asFiniteNumber(point.balance, peak);
    if (balance > peak) {
      peak = balance;
      continue;
    }
    if (peak <= 0) {
      continue;
    }
    const drawdownPct = ((peak - balance) / peak) * 100;
    if (drawdownPct > maxDrawdown) {
      maxDrawdown = drawdownPct;
    }
  }
  return maxDrawdown;
}



function toDrawdownPercent(value: unknown, fallback = 0): number {
  const numeric = Math.abs(asFiniteNumber(value, fallback));
  if (numeric > 0 && numeric <= 1) {
    return numeric * 100;
  }
  return numeric;
}

function asFiniteNumber(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) {
      return numeric;
    }
  }
  return fallback;
}

function formatUsd(value: number): string {
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatSignedUsd(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatPercent(value: number): string {
  return `${value.toFixed(1)}%`;
}
