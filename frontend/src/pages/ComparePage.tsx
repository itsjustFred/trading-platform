import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { compareBacktests } from "../api/client";
import type { BacktestsCompareResponse } from "../api/types";

const COLORS = ["#58a6ff", "#3fb950"];

type MetricConfig = {
  key: keyof BacktestsCompareResponse["backtest1"]["stats"];
  label: string;
  better: "higher" | "lower";
  format: "number" | "percent" | "currency" | "integer";
};

// The /api/backtests/compare payload serves every percent-like metric here in
// percent points (see _backtest_compare_payload in viewer/main.py), so no
// per-metric scale is needed on this page.
const METRICS: MetricConfig[] = [
  { key: "total_return", label: "Total Return", better: "higher", format: "percent" },
  { key: "net_profit", label: "Net Profit", better: "higher", format: "currency" },
  { key: "sharpe_ratio", label: "Sharpe (higher = better)", better: "higher", format: "number" },
  { key: "psr", label: "PSR (higher = better)", better: "higher", format: "percent" },
  { key: "p_value", label: "p-Value (lower = better)", better: "lower", format: "number" },
  { key: "cagr", label: "CAGR (higher = better)", better: "higher", format: "percent" },
  { key: "max_drawdown", label: "Max DD (lower = better)", better: "lower", format: "percent" },
  { key: "win_rate", label: "Win Rate (higher = better)", better: "higher", format: "percent" },
  { key: "trade_count", label: "Trade Count", better: "higher", format: "integer" },
];

function formatMetric(value: number, format: MetricConfig["format"]): string {
  if (!Number.isFinite(value)) return "—";
  if (format === "currency") {
    return `${value >= 0 ? "+" : "-"}$${Math.abs(value).toLocaleString("en-US", {
      maximumFractionDigits: 2,
    })}`;
  }
  if (format === "percent") {
    return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
  }
  if (format === "integer") {
    return `${value >= 0 ? "+" : ""}${Math.trunc(value)}`;
  }
  return value.toFixed(3);
}

function formatMetricRaw(value: number, format: MetricConfig["format"]): string {
  if (!Number.isFinite(value)) return "—";
  if (format === "currency") {
    return `$${value.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
  }
  if (format === "percent") {
    return `${value.toFixed(2)}%`;
  }
  if (format === "integer") {
    return Math.trunc(value).toLocaleString("en-US");
  }
  return value.toFixed(3);
}

function formatChartTime(unixSeconds: number): string {
  if (!Number.isFinite(unixSeconds)) return "";
  return new Date(unixSeconds * 1000).toISOString().slice(0, 10);
}

export function ComparePage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [data, setData] = useState<BacktestsCompareResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const ids = (params.get("ids") || "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);

  useEffect(() => {
    if (ids.length !== 2) {
      setLoading(false);
      setError("Select exactly 2 backtests to compare.");
      return;
    }
    setLoading(true);
    setError("");
    compareBacktests(ids)
      .then((payload) => {
        setData(payload);
      })
      .catch((issue: unknown) => {
        const message = issue instanceof Error ? issue.message : "Could not load comparison data";
        setError(message);
      })
      .finally(() => setLoading(false));
  }, [ids.join(",")]);

  const equityOverlayData = useMemo(() => {
    if (!data) return [];
    const byTime = new Map<number, { time: number; backtest1?: number; backtest2?: number }>();
    for (const point of data.backtest1.equity_curve || []) {
      const existing = byTime.get(point.time) || { time: point.time };
      existing.backtest1 = point.equity;
      byTime.set(point.time, existing);
    }
    for (const point of data.backtest2.equity_curve || []) {
      const existing = byTime.get(point.time) || { time: point.time };
      existing.backtest2 = point.equity;
      byTime.set(point.time, existing);
    }
    return [...byTime.values()]
      .sort((left, right) => left.time - right.time)
      .map((entry) => ({
        ...entry,
        label: formatChartTime(entry.time),
      }));
  }, [data]);

  const distributionData = useMemo(() => {
    if (!data) return [];
    return [
      {
        name: "Winning Trades",
        backtest1: data.backtest1.trade_distribution.winning_trades,
        backtest2: data.backtest2.trade_distribution.winning_trades,
      },
      {
        name: "Losing Trades",
        backtest1: data.backtest1.trade_distribution.losing_trades,
        backtest2: data.backtest2.trade_distribution.losing_trades,
      },
      {
        name: "Total Trades",
        backtest1: data.backtest1.trade_distribution.total_trades,
        backtest2: data.backtest2.trade_distribution.total_trades,
      },
    ];
  }, [data]);

  if (loading) {
    return <div style={{ padding: "20px", color: "var(--color-text-muted)" }}>Loading comparison...</div>;
  }
  if (error) {
    return <div style={{ padding: "20px", color: "var(--color-red)" }}>{error}</div>;
  }
  if (!data) {
    return <div style={{ padding: "20px", color: "var(--color-red)" }}>Could not load comparison data.</div>;
  }

  return (
    <div style={{ padding: "24px", display: "grid", gap: "18px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
        <button
          onClick={() => navigate(-1)}
          style={{
            background: "none",
            border: "none",
            color: "var(--color-text-muted)",
            cursor: "pointer",
          }}
          aria-label="Go back"
        >
          <ArrowLeft size={20} />
        </button>
        <h1 style={{ fontSize: "1.5rem", fontWeight: 600, margin: 0 }}>Backtest Comparison</h1>
      </div>

      <div
        style={{
          display: "grid",
          gap: "12px",
          gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
        }}
      >
        {[data.backtest1, data.backtest2].map((entry, index) => (
          <div
            key={entry.id}
            style={{
              background: "var(--color-surface)",
              border: `1px solid ${COLORS[index]}`,
              borderRadius: "8px",
              padding: "14px",
            }}
          >
            <div style={{ fontWeight: 700, marginBottom: "4px", color: COLORS[index] }}>{entry.name || entry.id}</div>
            <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>
              {entry.symbols.join(", ") || "N/A"} · {entry.timeframe || "N/A"} · {entry.engine}
            </div>
          </div>
        ))}
      </div>

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "16px",
        }}
      >
        <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "12px" }}>Side-by-Side Statistics</h2>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.8rem" }}>
            <thead>
              <tr>
                <th style={thStyle}>Metric</th>
                <th style={thStyle}>{data.backtest1.name}</th>
                <th style={thStyle}>{data.backtest2.name}</th>
                <th style={thStyle}>Delta (2 - 1)</th>
              </tr>
            </thead>
            <tbody>
              {METRICS.map((metric) => {
                const left = Number(data.backtest1.stats[metric.key] || 0);
                const right = Number(data.backtest2.stats[metric.key] || 0);
                const delta = right - left;
                const betterSecond = metric.better === "higher" ? right > left : right < left;
                return (
                  <tr key={metric.key}>
                    <td style={tdStyle}>{metric.label}</td>
                    <td style={tdStyle}>{formatMetricRaw(left, metric.format)}</td>
                    <td style={tdStyle}>{formatMetricRaw(right, metric.format)}</td>
                    <td style={{ ...tdStyle, color: betterSecond ? "#3fb950" : delta === 0 ? "#c9d1d9" : "#f85149" }}>
                      {formatMetric(delta, metric.format)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "16px",
        }}
      >
        <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "12px" }}>Equity Overlay</h2>
        <div style={{ width: "100%", height: "340px" }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={equityOverlayData}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
              <XAxis dataKey="label" minTickGap={30} stroke="#8b949e" tick={{ fontSize: 12 }} />
              <YAxis stroke="#8b949e" tick={{ fontSize: 12 }} />
              <Tooltip />
              <Legend />
              <Line type="monotone" dataKey="backtest1" stroke={COLORS[0]} dot={false} strokeWidth={2} name={data.backtest1.name} />
              <Line type="monotone" dataKey="backtest2" stroke={COLORS[1]} dot={false} strokeWidth={2} name={data.backtest2.name} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "16px",
        }}
      >
        <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "12px" }}>Trade Distribution</h2>
        <div style={{ width: "100%", height: "280px" }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={distributionData}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
              <XAxis dataKey="name" stroke="#8b949e" tick={{ fontSize: 12 }} />
              <YAxis stroke="#8b949e" tick={{ fontSize: 12 }} />
              <Tooltip />
              <Legend />
              <Bar dataKey="backtest1" fill={COLORS[0]} name={data.backtest1.name} radius={[4, 4, 0, 0]} />
              <Bar dataKey="backtest2" fill={COLORS[1]} name={data.backtest2.name} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}

const thStyle: React.CSSProperties = {
  padding: "8px",
  textAlign: "left",
  borderBottom: "1px solid var(--color-border)",
  color: "var(--color-text-muted)",
  whiteSpace: "nowrap",
};

const tdStyle: React.CSSProperties = {
  padding: "8px",
  borderBottom: "1px solid rgba(255,255,255,0.06)",
  whiteSpace: "nowrap",
};
