import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import {
  AreaSeries,
  ColorType,
  createChart,
  CrosshairMode,
  LineSeries,
} from "lightweight-charts";
import type { IChartApi, Time } from "lightweight-charts";

import { getLiveBalances, getLiveStats, getLiveTrades } from "../api/client";
import type {
  LiveAccountStats,
  LiveBalanceRecord,
  LiveTradeSlippage,
} from "../api/types";
import { DataTable } from "../components/DataTable";

const LIVE_DETAIL_POLL_MS = 30000;

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

  const tradeColumns = [
    { key: "symbol", label: "Symbol", align: "center" as const, width: "90px" },
    {
      key: "action",
      label: "Action",
      align: "center" as const,
      width: "84px",
      render: (row: LiveTradeSlippage) => (
        <span style={{ color: row.action === "buy" ? "#86efac" : "#fca5a5", fontWeight: 600 }}>
          {row.action.toUpperCase()}
        </span>
      ),
    },
    {
      key: "theoretical_price",
      label: "Theo Price",
      align: "right" as const,
      render: (row: LiveTradeSlippage) => formatUsd(row.theoretical_price),
    },
    {
      key: "actual_price",
      label: "Actual Price",
      align: "right" as const,
      render: (row: LiveTradeSlippage) => formatUsd(row.actual_price),
    },
    {
      key: "slippage",
      label: "Slippage",
      align: "right" as const,
      render: (row: LiveTradeSlippage) => (
        <span style={{ color: row.slippage <= 0 ? "#86efac" : "#fca5a5", fontWeight: 600 }}>
          {formatSigned(row.slippage)}
        </span>
      ),
    },
    {
      key: "slippage_bps",
      label: "Bps",
      align: "right" as const,
      render: (row: LiveTradeSlippage) => (
        <span style={{ color: row.slippage_bps <= 0 ? "#86efac" : "#fca5a5" }}>
          {formatSigned(row.slippage_bps, 2)}
        </span>
      ),
    },
    {
      key: "filled_qty",
      label: "Qty",
      align: "right" as const,
      render: (row: LiveTradeSlippage) => row.filled_qty.toFixed(4),
    },
    {
      key: "position_size",
      label: "Size",
      align: "right" as const,
      render: (row: LiveTradeSlippage) => {
        const directSize = row.position_size;
        const metadataSize = Number(
          (row.signal_metadata as Record<string, unknown>)?.qty ??
            (row.signal_metadata as Record<string, unknown>)?.position_size ??
            NaN,
        );
        const size =
          directSize != null && Number.isFinite(directSize) ? directSize : metadataSize;
        return Number.isFinite(size)
          ? size.toLocaleString("en-US", {
              minimumFractionDigits: 2,
              maximumFractionDigits: 4,
            })
          : "N/A";
      },
    },
    {
      key: "risk_amount",
      label: "Risk $",
      align: "right" as const,
      render: (row: LiveTradeSlippage) => {
        const directRisk = row.risk_amount;
        const metadataRisk = Number(
          (row.signal_metadata as Record<string, unknown>)?.risk_amount ?? NaN,
        );
        const value =
          directRisk != null && Number.isFinite(directRisk) ? directRisk : metadataRisk;
        return Number.isFinite(value)
          ? `$${value.toLocaleString("en-US", {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2,
            })}`
          : "N/A";
      },
    },
    {
      key: "risk_percent",
      label: "Risk %",
      align: "right" as const,
      render: (row: LiveTradeSlippage) => {
        const directPercent = row.risk_percent;
        const metadataPercent = Number(
          (row.signal_metadata as Record<string, unknown>)?.risk_percent ?? NaN,
        );
        const value =
          directPercent != null && Number.isFinite(directPercent)
            ? directPercent
            : metadataPercent;
        return Number.isFinite(value)
          ? `${(value * 100).toLocaleString("en-US", {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2,
            })}%`
          : "N/A";
      },
    },
    {
      key: "execution_timestamp",
      label: "Fill Time",
      align: "center" as const,
      width: "170px",
      render: (row: LiveTradeSlippage) =>
        row.execution_timestamp.replace("T", " ").slice(0, 19),
    },
  ];

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
            <button
              onClick={() => navigate("/live")}
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
            </button>
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
          <DataTable columns={tradeColumns} data={trades} maxHeight="520px" />
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

function MetricCard({
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
}

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
    winRate: toWinRatePercent(stats.win_rate, fallback.winRate),
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

function toWinRatePercent(value: unknown, fallback = 0): number {
  const numeric = asFiniteNumber(value, fallback);
  if (numeric > 0 && numeric <= 1) {
    return numeric * 100;
  }
  return numeric;
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

function formatSigned(value: number, digits = 2): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}`;
}
