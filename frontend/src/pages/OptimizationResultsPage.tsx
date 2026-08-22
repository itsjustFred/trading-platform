import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, ArrowUpRight, Gauge, Layers3, Target } from "lucide-react";

import { getBacktestInfo, getOptimization } from "../api/client";
import { DataTable } from "../components/DataTable";
import type { BacktestInfo, OptimizationDetail } from "../api/types";
import { classifyOptimizationOosEvidence } from "../lib/optimizationEvidence";

const COMPARISON_ROWS = [
  { key: "sharpe_ratio", label: "Sharpe" },
  { key: "total_pnl", label: "Net Profit" },
  { key: "cagr", label: "CAGR" },
  { key: "win_rate", label: "Win Rate" },
  { key: "max_drawdown_pct", label: "Max DD" },
  { key: "profit_factor", label: "Profit Factor" },
  { key: "total_trades", label: "Trades" },
];

function formatComparisonValue(key: string, value: number | string | boolean | null | undefined) {
  if (value == null) {
    return "—";
  }
  if (typeof value !== "number") {
    return String(value);
  }
  if (key === "total_pnl") {
    return `$${value.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
  }
  if (["cagr", "win_rate", "max_drawdown_pct"].includes(key)) {
    return `${(value * 100).toFixed(2)}%`;
  }
  if (key === "total_trades") {
    return value.toFixed(0);
  }
  return value.toFixed(3);
}

function formatMode(value: string | null | undefined) {
  const normalized = String(value || "parameter").replaceAll("_", " ");
  return normalized.replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatSymbols(symbols: string[] | null | undefined) {
  const cleaned = (symbols || []).map((symbol) => String(symbol || "").trim()).filter(Boolean);
  return cleaned.length ? cleaned.join(", ") : "—";
}

export function OptimizationResultsPage() {
  const { id } = useParams<{ id: string }>();
  const [optimization, setOptimization] = useState<OptimizationDetail | null>(null);
  const [backtestInfo, setBacktestInfo] = useState<BacktestInfo | null>(null);
  const [loadedId, setLoadedId] = useState<string | null>(null);
  const [error, setError] = useState("");

  const loading = loadedId !== id;

  useEffect(() => {
    if (!id) {
      return;
    }

    getOptimization(id)
      .then(async (payload) => {
        setError("");
        setOptimization(payload);
        setLoadedId(id);
        try {
          const info = await getBacktestInfo(payload.base_backtest_id);
          setBacktestInfo(info);
        } catch {
          setBacktestInfo(null);
        }
      })
      .catch((caughtError) => {
        setError(caughtError instanceof Error ? caughtError.message : "Could not load optimization.");
        setLoadedId(id);
      });
  }, [id]);

  const topTrialColumns = useMemo(() => {
    const parameterKeys = Array.from(
      new Set(optimization?.top_trials.flatMap((trial) => Object.keys(trial.params || {})) || []),
    );

    return [
      {
        key: "trial_id",
        label: "Trial",
        width: "90px",
        render: (row: Record<string, unknown>) => `#${row.trial_id}`,
      },
      {
        key: "metric_value",
        label: "Metric",
        render: (row: Record<string, unknown>) => {
          const value = typeof row.metric_value === "number" ? row.metric_value.toFixed(4) : "—";
          return <span style={{ color: "#7dd3fc", fontWeight: 700 }}>{value}</span>;
        },
      },
      {
        key: "symbols",
        label: "Symbols",
        render: (row: Record<string, unknown>) => {
          const symbols = Array.isArray(row.symbols) ? row.symbols.map(String) : [];
          return <span style={{ whiteSpace: "normal" }}>{formatSymbols(symbols)}</span>;
        },
      },
      ...parameterKeys.map((key) => ({
        key,
        label: key,
        render: (row: Record<string, unknown>) => String(row[key] ?? "—"),
      })),
      {
        key: "trade_count",
        label: "Trades",
      },
    ];
  }, [optimization]);

  const topTrialRows = useMemo(
    () =>
      (optimization?.top_trials || []).map((trial) => ({
        trial_id: trial.number,
        metric_value: trial.value,
        symbols: trial.symbols || [],
        trade_count: trial.trade_count,
        ...trial.params,
      })),
    [optimization],
  );

  if (loading) {
    return <div style={{ padding: "24px", color: "var(--color-text-muted)" }}>Loading optimization...</div>;
  }

  if (error || !optimization) {
    return (
      <div style={{ padding: "24px", color: "#fecdd3" }}>
        Error: {error || "Optimization not found."}
      </div>
    );
  }

  const oosEvidence = classifyOptimizationOosEvidence(
    optimization.target_metric,
    optimization.best_value,
    optimization.test_stats,
  );

  return (
    <div style={{ padding: "24px", display: "grid", gap: "18px" }}>
      <section
        style={{
          position: "relative",
          overflow: "hidden",
          borderRadius: "24px",
          border: "1px solid rgba(56, 189, 248, 0.18)",
          background:
            "radial-gradient(circle at top right, rgba(59, 130, 246, 0.18), transparent 30%), linear-gradient(135deg, rgba(15, 23, 42, 0.97), rgba(2, 6, 23, 0.98))",
          padding: "24px",
        }}
      >
        <div
          style={{
            position: "absolute",
            inset: "12px",
            borderRadius: "18px",
            border: "1px dashed rgba(148, 163, 184, 0.12)",
            pointerEvents: "none",
          }}
        />
        <div style={{ position: "relative", display: "grid", gap: "18px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: "16px", flexWrap: "wrap" }}>
            <div style={{ display: "grid", gap: "10px" }}>
              <Link
                to="/optimizations"
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "8px",
                  border: "none",
                  background: "transparent",
                  color: "var(--color-text-muted)",
                  cursor: "pointer",
                  padding: 0,
                  textDecoration: "none",
                }}
              >
                <ArrowLeft size={16} />
                Back to optimizations
              </Link>
              <div
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "8px",
                  padding: "6px 10px",
                  borderRadius: "999px",
                  background: "rgba(251, 191, 36, 0.12)",
                  color: "#fbbf24",
                  fontSize: "0.72rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.12em",
                }}
              >
                <Target size={12} />
                {optimization.target_metric}
              </div>
              <h1
                style={{
                  fontSize: "2rem",
                  lineHeight: 1,
                  fontFamily: '"Iowan Old Style", "Palatino Linotype", Georgia, serif',
                }}
              >
                {backtestInfo?.title || backtestInfo?.strategy_display_name || optimization.base_backtest_id}
              </h1>
              <div style={{ color: "var(--color-text-muted)", lineHeight: 1.6 }}>
                {optimization.n_trials} trials • {formatSymbols(optimization.best_symbols || optimization.symbols)} • {optimization.timeframe} • created {new Date(optimization.created_at).toLocaleString("en-US")}
              </div>
            </div>
            <div style={{ display: "flex", gap: "10px", flexWrap: "wrap", alignItems: "flex-start" }}>
              <Link
                to={`/backtests/${optimization.base_backtest_id}`}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "8px",
                  borderRadius: "999px",
                  border: "1px solid rgba(59, 130, 246, 0.2)",
                  background: "rgba(59, 130, 246, 0.12)",
                  color: "#bfdbfe",
                  padding: "12px 16px",
                  cursor: "pointer",
                  textDecoration: "none",
                }}
              >
                Open base backtest
                <ArrowUpRight size={15} />
              </Link>
            </div>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "12px" }}>
            {[
              { label: "In-sample selected score", value: formatComparisonValue(optimization.target_metric, optimization.best_value), tone: "#7dd3fc" },
              { label: "Mode", value: formatMode(optimization.optimization_mode), tone: "#c4b5fd" },
              { label: "Train split", value: `${Math.round(optimization.train_split_pct * 100)}%`, tone: "#fbbf24" },
              { label: "OOS window", value: `${optimization.test_period.start.slice(0, 10)} → ${optimization.test_period.end.slice(0, 10)}`, tone: "#86efac" },
            ].map((item) => (
              <div
                key={item.label}
                style={{
                  borderRadius: "18px",
                  border: "1px solid rgba(148, 163, 184, 0.16)",
                  background: "rgba(15, 23, 42, 0.84)",
                  padding: "14px 16px",
                }}
              >
                <div style={{ fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--color-text-muted)" }}>
                  {item.label}
                </div>
                <div style={{ marginTop: "8px", fontSize: "1.15rem", fontWeight: 700, color: item.tone }}>
                  {item.value}
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section
        data-oos-evidence-state={oosEvidence.state}
        style={{
          borderRadius: "18px",
          border: `1px solid ${oosEvidence.border}`,
          background: oosEvidence.background,
          padding: "16px 18px",
        }}
      >
        <div style={{ color: oosEvidence.tone, fontSize: "0.76rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em" }}>
          {oosEvidence.label}
        </div>
        <div style={{ marginTop: "7px", color: "#e2e8f0", lineHeight: 1.55 }}>
          {oosEvidence.message}
        </div>
      </section>

      <section
        style={{
          borderRadius: "22px",
          border: "1px solid rgba(148, 163, 184, 0.16)",
          background: "linear-gradient(180deg, rgba(15, 23, 42, 0.96), rgba(2, 6, 23, 0.94))",
          padding: "18px",
        }}
      >
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: "14px" }}>
          {[
            { label: "Base symbols", value: formatSymbols(optimization.symbols), tone: "#93c5fd" },
            { label: "Best symbols", value: formatSymbols(optimization.best_symbols || optimization.symbols), tone: "#86efac" },
            {
              label: "Candidate universes",
              value: `${optimization.symbol_universes?.length || 0}`,
              tone: "#fbbf24",
            },
          ].map((item) => (
            <div
              key={item.label}
              style={{
                borderRadius: "16px",
                border: "1px solid rgba(148, 163, 184, 0.12)",
                background: "rgba(15, 23, 42, 0.78)",
                padding: "14px",
                minWidth: 0,
              }}
            >
              <div style={{ fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.12em", color: item.tone }}>
                {item.label}
              </div>
              <div style={{ marginTop: "8px", color: "#e2e8f0", lineHeight: 1.5, wordBreak: "break-word" }}>
                {item.value}
              </div>
            </div>
          ))}
        </div>
        {(optimization.symbol_universes?.length || 0) > 0 ? (
          <div style={{ marginTop: "14px", display: "flex", flexWrap: "wrap", gap: "8px" }}>
            {(optimization.symbol_universes || []).slice(0, 12).map((universe, index) => (
              <span
                key={`${index}-${universe.join("-")}`}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  padding: "8px 10px",
                  borderRadius: "999px",
                  border: "1px solid rgba(148, 163, 184, 0.14)",
                  background: "rgba(148, 163, 184, 0.1)",
                  color: "#cbd5e1",
                  fontSize: "0.78rem",
                }}
              >
                {formatSymbols(universe)}
              </span>
            ))}
          </div>
        ) : null}
      </section>

      <section
        style={{
          borderRadius: "22px",
          border: "1px solid rgba(148, 163, 184, 0.16)",
          background: "linear-gradient(180deg, rgba(15, 23, 42, 0.96), rgba(2, 6, 23, 0.94))",
          padding: "18px",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", gap: "12px", alignItems: "center", marginBottom: "14px", flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.14em", color: "#93c5fd" }}>
              In-sample selection
            </div>
            <div style={{ marginTop: "4px", fontSize: "1.02rem", fontWeight: 700 }}>
              In-sample selected parameters
            </div>
          </div>
          <div style={{ display: "inline-flex", alignItems: "center", gap: "8px", color: "#f8fafc" }}>
            <Gauge size={16} />
            {Object.keys(optimization.best_params || {}).length} optimized inputs
          </div>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "10px" }}>
          {Object.entries(optimization.best_params || {}).map(([key, value]) => (
            <div
              key={key}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "8px",
                borderRadius: "999px",
                padding: "10px 14px",
                border: "1px solid rgba(251, 191, 36, 0.16)",
                background: "rgba(251, 191, 36, 0.08)",
                fontFamily: '"SFMono-Regular", "Cascadia Code", ui-monospace, monospace',
              }}
            >
              <span style={{ color: "#fbbf24" }}>{key}</span>
              <span>{String(value)}</span>
            </div>
          ))}
        </div>
      </section>

      <section
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
          gap: "14px",
        }}
      >
        {[
          { title: "In-Sample", tone: "#7dd3fc", stats: optimization.train_stats },
          { title: "Out-of-Sample", tone: "#86efac", stats: optimization.test_stats },
        ].map((panel) => (
          <div
            key={panel.title}
            style={{
              borderRadius: "22px",
              border: "1px solid rgba(148, 163, 184, 0.16)",
              background: "linear-gradient(180deg, rgba(15, 23, 42, 0.96), rgba(2, 6, 23, 0.94))",
              padding: "18px",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <Layers3 size={16} color={panel.tone} />
              <div style={{ fontSize: "1rem", fontWeight: 700 }}>{panel.title}</div>
            </div>
            <div style={{ marginTop: "14px", display: "grid", gap: "10px" }}>
              {COMPARISON_ROWS.map((row) => (
                <div
                  key={row.key}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    gap: "12px",
                    borderRadius: "16px",
                    border: "1px solid rgba(148, 163, 184, 0.12)",
                    background: "rgba(15, 23, 42, 0.78)",
                    padding: "12px 14px",
                  }}
                >
                  <span style={{ color: "var(--color-text-muted)" }}>{row.label}</span>
                  <span style={{ fontWeight: 700, color: panel.tone }}>
                    {formatComparisonValue(row.key, panel.stats?.[row.key])}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </section>

      <section
        style={{
          borderRadius: "22px",
          border: "1px solid rgba(148, 163, 184, 0.16)",
          background: "linear-gradient(180deg, rgba(15, 23, 42, 0.96), rgba(2, 6, 23, 0.94))",
          padding: "18px",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", gap: "12px", alignItems: "center", marginBottom: "16px", flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.14em", color: "#93c5fd" }}>
              Ranked leaderboard
            </div>
            <div style={{ marginTop: "4px", fontSize: "1.02rem", fontWeight: 700 }}>
              Top 10 trials
            </div>
          </div>
          <div style={{ color: "var(--color-text-muted)", fontSize: "0.82rem" }}>
            Click column headers to sort.
          </div>
        </div>
        <DataTable columns={topTrialColumns} data={topTrialRows} maxHeight="460px" />
      </section>
    </div>
  );
}
