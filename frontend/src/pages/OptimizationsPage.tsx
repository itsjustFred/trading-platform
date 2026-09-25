import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, Target, Telescope } from "lucide-react";

import { getOptimizations } from "../api/client";
import type { OptimizationSummary } from "../api/types";
import { classifyOptimizationOosEvidence } from "../lib/optimizationEvidence";

function formatMetricValue(metric: string, value: number | string | boolean | null | undefined) {
  if (typeof value !== "number") {
    return value == null ? "—" : String(value);
  }
  if (["cagr", "win_rate", "max_drawdown_pct"].includes(metric)) {
    return `${(value * 100).toFixed(2)}%`;
  }
  if (metric === "total_pnl") {
    return `$${value.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
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

export function OptimizationsPage() {
  const [optimizations, setOptimizations] = useState<OptimizationSummary[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    getOptimizations()
      .then((payload) => {
        setOptimizations(payload);
        setLoading(false);
      })
      .catch((caughtError) => {
        setError(caughtError instanceof Error ? caughtError.message : "Could not load optimizations.");
        setLoading(false);
      });
  }, []);

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) {
      return optimizations;
    }
    return optimizations.filter((optimization) => {
      return [
        optimization.id,
        optimization.base_backtest_id,
        optimization.target_metric,
        optimization.optimization_mode || "",
        ...(optimization.symbols || []),
        ...(optimization.best_symbols || []),
        ...Object.keys(optimization.best_params || {}),
      ]
        .filter(Boolean)
        .some((value) => value.toLowerCase().includes(query));
    });
  }, [optimizations, search]);

  return (
    <div style={{ padding: "24px", display: "grid", gap: "18px" }}>
      <section
        style={{
          position: "relative",
          overflow: "hidden",
          borderRadius: "24px",
          border: "1px solid rgba(56, 189, 248, 0.18)",
          background:
            "radial-gradient(circle at top right, rgba(56, 189, 248, 0.2), transparent 30%), linear-gradient(135deg, rgba(15, 23, 42, 0.97), rgba(2, 6, 23, 0.98))",
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
        <div
          style={{
            position: "relative",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            gap: "20px",
            flexWrap: "wrap",
          }}
        >
          <div>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "8px",
                padding: "6px 10px",
                borderRadius: "999px",
                background: "rgba(56, 189, 248, 0.12)",
                color: "#7dd3fc",
                fontSize: "0.72rem",
                letterSpacing: "0.14em",
                textTransform: "uppercase",
              }}
            >
              <Telescope size={14} />
              Optimization archive
            </div>
            <h1
              style={{
                marginTop: "14px",
                fontSize: "2rem",
                lineHeight: 1,
                fontFamily: '"Iowan Old Style", "Palatino Linotype", Georgia, serif',
              }}
            >
              Sweep history, winner envelopes, and OOS verdicts
            </h1>
            <p
              style={{
                marginTop: "12px",
                maxWidth: "760px",
                color: "var(--color-text-muted)",
                lineHeight: 1.6,
              }}
            >
              Browse completed Optuna runs, inspect their winning settings, and jump straight into the train versus test comparison view.
            </p>
          </div>
          <div style={{ minWidth: "240px", width: "min(100%, 320px)" }}>
            <label
              htmlFor="optimizations-status-filter"
              style={{
                display: "block",
                marginBottom: "8px",
                fontSize: "0.72rem",
                textTransform: "uppercase",
                letterSpacing: "0.14em",
                color: "#93c5fd",
              }}
            >
              Search runs
            </label>
            <input
              id="optimizations-status-filter"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="base backtest, metric, parameter..."
              style={{
                width: "100%",
                borderRadius: "14px",
                border: "1px solid rgba(148, 163, 184, 0.18)",
                background: "rgba(15, 23, 42, 0.88)",
                color: "var(--color-text)",
                padding: "12px 14px",
                outline: "none",
              }}
            />
          </div>
        </div>
      </section>

      {loading && (
        <div style={{ color: "var(--color-text-muted)", padding: "16px 4px" }}>Loading optimizations...</div>
      )}
      {!loading && error && (
        <div style={{ color: "#fecdd3", padding: "16px 4px" }}>Error: {error}</div>
      )}
      {!loading && !error && filtered.length === 0 && (
        <div
          style={{
            borderRadius: "22px",
            border: "1px dashed rgba(148, 163, 184, 0.22)",
            background: "rgba(15, 23, 42, 0.5)",
            padding: "28px",
            color: "var(--color-text-muted)",
            textAlign: "center",
          }}
        >
          No optimization results found yet.
        </div>
      )}
      {!loading && !error && filtered.length > 0 && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
            gap: "14px",
          }}
        >
          {filtered.map((optimization) => {
            const bestMetric = formatMetricValue(
              optimization.target_metric === "net_profit" ? "total_pnl" : optimization.target_metric,
              optimization.best_value,
            );
            const oosEvidence = classifyOptimizationOosEvidence(
              optimization.target_metric,
              optimization.best_value,
              optimization.test_stats,
              optimization.oos_status ?? "unavailable",
            );
            return (
              <Link
                key={optimization.id}
                to={`/optimizations/${optimization.id}`}
                style={{
                  textAlign: "left",
                  borderRadius: "22px",
                  border: "1px solid rgba(148, 163, 184, 0.16)",
                  background:
                    "linear-gradient(180deg, rgba(15, 23, 42, 0.96), rgba(2, 6, 23, 0.94))",
                  padding: "18px",
                  color: "var(--color-text)",
                  cursor: "pointer",
                  display: "grid",
                  gap: "16px",
                  textDecoration: "none",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", gap: "12px", alignItems: "flex-start" }}>
                  <div>
                    <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
                      <div
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: "8px",
                          padding: "5px 10px",
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
                      <div
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          padding: "5px 10px",
                          borderRadius: "999px",
                          background: "rgba(124, 58, 237, 0.12)",
                          color: "#c4b5fd",
                          fontSize: "0.72rem",
                          textTransform: "uppercase",
                          letterSpacing: "0.12em",
                        }}
                      >
                        {formatMode(optimization.optimization_mode)}
                      </div>
                    </div>
                    <div style={{ marginTop: "12px", fontSize: "1.06rem", fontWeight: 700 }}>
                      {optimization.base_backtest_id}
                    </div>
                    <div style={{ marginTop: "6px", color: "var(--color-text-muted)", fontSize: "0.82rem" }}>
                      {optimization.n_trials} trials • split {Math.round(optimization.train_split_pct * 100)} / {Math.round((1 - optimization.train_split_pct) * 100)}
                    </div>
                    <div style={{ marginTop: "6px", color: "#cbd5e1", fontSize: "0.78rem", lineHeight: 1.45 }}>
                      {formatSymbols(optimization.best_symbols || optimization.symbols)}
                    </div>
                  </div>
                  <ArrowUpRight size={18} color="#93c5fd" />
                </div>

                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
                    gap: "12px",
                  }}
                >
                  <div
                    style={{
                      borderRadius: "16px",
                      border: "1px solid rgba(56, 189, 248, 0.16)",
                      background: "rgba(14, 116, 144, 0.1)",
                      padding: "14px",
                    }}
                  >
                    <div style={{ fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "#7dd3fc" }}>
                      In-sample selected score
                    </div>
                    <div style={{ marginTop: "8px", fontSize: "1.4rem", fontWeight: 700 }}>{bestMetric}</div>
                  </div>
                  <div
                    style={{
                      borderRadius: "16px",
                      border: "1px solid rgba(74, 222, 128, 0.16)",
                      background: "rgba(22, 101, 52, 0.14)",
                      padding: "14px",
                    }}
                  >
                    <div style={{ fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "#86efac" }}>
                      OOS Sharpe
                    </div>
                    <div style={{ marginTop: "8px", fontSize: "1.4rem", fontWeight: 700 }}>
                      {formatMetricValue("sharpe_ratio", optimization.test_stats?.sharpe_ratio)}
                    </div>
                  </div>
                </div>

                <div
                  data-oos-evidence-state={oosEvidence.state}
                  style={{
                    borderRadius: "14px",
                    border: `1px solid ${oosEvidence.border}`,
                    background: oosEvidence.background,
                    padding: "11px 12px",
                  }}
                >
                  <div style={{ color: oosEvidence.tone, fontSize: "0.72rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em" }}>
                    {oosEvidence.label}
                  </div>
                  <div style={{ marginTop: "5px", color: "#cbd5e1", fontSize: "0.76rem", lineHeight: 1.45 }}>
                    {oosEvidence.message}
                  </div>
                </div>

                <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
                  {Object.entries(optimization.best_params || {}).map(([name, value]) => (
                    <span
                      key={name}
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: "6px",
                        padding: "7px 10px",
                        borderRadius: "999px",
                        background: "rgba(148, 163, 184, 0.12)",
                        color: "#e2e8f0",
                        fontFamily: '"SFMono-Regular", "Cascadia Code", ui-monospace, monospace',
                        fontSize: "0.76rem",
                      }}
                    >
                      <span style={{ color: "#94a3b8" }}>{name}</span>
                      <span>{String(value)}</span>
                    </span>
                  ))}
                  {(optimization.symbol_universes?.length || 0) > 0 ? (
                    <span
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: "6px",
                        padding: "7px 10px",
                        borderRadius: "999px",
                        background: "rgba(124, 58, 237, 0.12)",
                        color: "#ddd6fe",
                        fontSize: "0.76rem",
                      }}
                    >
                      {optimization.symbol_universes?.length} universes
                    </span>
                  ) : null}
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
