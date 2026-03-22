import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, CandlestickChart, Waves } from "lucide-react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { getLiveBalances, getLiveTrades } from "../api/client";
import type { LiveBalanceRecord, LiveTradeSlippage } from "../api/types";
import { DataTable } from "../components/DataTable";

const LIVE_DETAIL_POLL_MS = 12000;

export function LiveDetailPage() {
  const { accountId: rawAccountId } = useParams<{ accountId: string }>();
  const navigate = useNavigate();
  const accountId = decodeURIComponent(rawAccountId ?? "");

  const [balances, setBalances] = useState<LiveBalanceRecord[]>([]);
  const [trades, setTrades] = useState<LiveTradeSlippage[]>([]);
  const [deploymentIds, setDeploymentIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!accountId) {
      setError("Missing account id");
      setLoading(false);
      return;
    }

    let active = true;
    let intervalId: number | undefined;

    const load = async () => {
      try {
        const [balancesPayload, tradesPayload] = await Promise.all([
          getLiveBalances(accountId),
          getLiveTrades(accountId),
        ]);
        if (!active) return;
        setBalances(balancesPayload.series);
        setTrades(tradesPayload.trades);
        setDeploymentIds(tradesPayload.deployment_ids);
        setError("");
      } catch (issue: unknown) {
        if (!active) return;
        const message =
          issue instanceof Error ? issue.message : "Failed to load live account details";
        setError(message);
      } finally {
        if (active) setLoading(false);
      }
    };

    load();
    intervalId = window.setInterval(load, LIVE_DETAIL_POLL_MS);

    return () => {
      active = false;
      if (intervalId) window.clearInterval(intervalId);
    };
  }, [accountId]);

  const chartData = useMemo(
    () =>
      balances.map((item) => {
        const date = item.time ? new Date(item.time * 1000) : new Date(item.timestamp);
        return {
          ...item,
          timeLabel: date.toLocaleString("de-DE", {
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          }),
        };
      }),
    [balances],
  );

  const latestEquity = balances.length > 0 ? balances[balances.length - 1].equity : 0;
  const latestBalance = balances.length > 0 ? balances[balances.length - 1].balance : 0;
  const firstEquity = balances.length > 0 ? balances[0].equity : 0;
  const equityDelta = latestEquity - firstEquity;
  const equityDeltaPct = firstEquity !== 0 ? (equityDelta / firstEquity) * 100 : 0;

  const tradeColumns = [
    { key: "symbol", label: "Symbol", align: "center" as const, width: "80px" },
    {
      key: "action",
      label: "Action",
      align: "center" as const,
      width: "80px",
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
      label: "bps",
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
      key: "signal_timestamp",
      label: "Signal Time",
      align: "center" as const,
      render: (row: LiveTradeSlippage) => row.signal_timestamp.replace("T", " ").slice(0, 19),
    },
    {
      key: "execution_timestamp",
      label: "Fill Time",
      align: "center" as const,
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
              <p style={{ margin: "3px 0 0", color: "var(--color-text-muted)", fontSize: "0.84rem" }}>
                Equity curve and slippage details from broker executions.
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

        <div
          style={{
            marginTop: "14px",
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
            gap: "10px",
          }}
        >
          <MetricChip icon={<Waves size={14} />} label="Equity" value={formatUsd(latestEquity)} />
          <MetricChip
            icon={<CandlestickChart size={14} />}
            label="Balance"
            value={formatUsd(latestBalance)}
          />
          <MetricChip
            icon={<Waves size={14} />}
            label="PnL"
            value={`${formatSigned(equityDelta)} (${formatSigned(equityDeltaPct, 2)}%)`}
            valueColor={equityDelta >= 0 ? "#86efac" : "#fca5a5"}
          />
          <MetricChip icon={<CandlestickChart size={14} />} label="Trades" value={String(trades.length)} />
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
        {chartData.length === 0 ? (
          <div style={{ color: "var(--color-text-muted)", padding: "42px 0", textAlign: "center" }}>
            No balance data yet.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={340}>
            <LineChart data={chartData} margin={{ top: 8, right: 16, left: 4, bottom: 8 }}>
              <CartesianGrid stroke="rgba(139,148,158,0.22)" strokeDasharray="4 4" />
              <XAxis dataKey="timeLabel" minTickGap={28} stroke="var(--color-text-muted)" />
              <YAxis
                stroke="var(--color-text-muted)"
                tickFormatter={(value: number) => formatUsd(value)}
                width={84}
              />
              <Tooltip
                contentStyle={{
                  border: "1px solid var(--color-border)",
                  background: "var(--color-surface2)",
                  borderRadius: "8px",
                }}
                labelStyle={{ color: "var(--color-text-muted)" }}
                formatter={(value, name) => {
                  const rawValue = Array.isArray(value) ? value[0] : value;
                  const numeric = Number(rawValue ?? 0);
                  return [formatUsd(Number.isFinite(numeric) ? numeric : 0), String(name ?? "")];
                }}
              />
              <Legend />
              <Line
                type="monotone"
                dataKey="equity"
                stroke="#38bdf8"
                strokeWidth={2.5}
                dot={false}
                name="Equity"
              />
              <Line
                type="monotone"
                dataKey="balance"
                stroke="#22c55e"
                strokeWidth={2}
                dot={false}
                name="Balance"
              />
            </LineChart>
          </ResponsiveContainer>
        )}
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

function MetricChip({
  icon,
  label,
  value,
  valueColor,
}: {
  icon: React.ReactNode;
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
      <div style={{ display: "flex", alignItems: "center", gap: "7px", color: "var(--color-text-muted)" }}>
        {icon}
        <span style={{ fontSize: "0.76rem", textTransform: "uppercase", letterSpacing: "0.06em" }}>
          {label}
        </span>
      </div>
      <div style={{ marginTop: "6px", fontSize: "1.02rem", fontWeight: 600, color: valueColor ?? "#e2e8f0" }}>
        {value}
      </div>
    </div>
  );
}

function formatUsd(value: number): string {
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatSigned(value: number, digits = 2): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}`;
}
