import { useState, useEffect } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { compareBacktests } from "../api/client";
import { ArrowLeft } from "lucide-react";

const COLORS = [
  "#58a6ff",
  "#3fb950",
  "#f0883e",
  "#bc8cff",
  "#f85149",
  "#79c0ff",
];

export function ComparePage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  const ids = params.get("ids")?.split(",") || [];

  useEffect(() => {
    if (ids.length < 2) return;
    compareBacktests(ids)
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [params]);

  if (loading)
    return (
      <div style={{ padding: "20px", color: "var(--color-text-muted)" }}>
        Loading comparison...
      </div>
    );
  if (!data)
    return (
      <div style={{ padding: "20px", color: "var(--color-red)" }}>
        Could not load comparison data
      </div>
    );

  const results = data.results || [];

  const statKeys = [
    { key: "net_profit", label: "Net Profit", fmt: "currency" as const },
    { key: "sharpe", label: "Sharpe", fmt: "number" as const },
    { key: "sortino", label: "Sortino", fmt: "number" as const },
    { key: "max_drawdown_pct", label: "Max DD %", fmt: "percent" as const },
    { key: "win_rate", label: "Win Rate %", fmt: "percent" as const },
    { key: "profit_factor", label: "Profit Factor", fmt: "number" as const },
    { key: "total_trades", label: "Trades", fmt: "number" as const },
    { key: "cagr", label: "CAGR", fmt: "percent" as const },
  ];

  return (
    <div style={{ padding: "24px" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "12px",
          marginBottom: "20px",
        }}
      >
        <button
          onClick={() => navigate(-1)}
          style={{
            background: "none",
            border: "none",
            color: "var(--color-text-muted)",
            cursor: "pointer",
          }}
        >
          <ArrowLeft size={20} />
        </button>
        <h1 style={{ fontSize: "1.5rem", fontWeight: 600 }}>
          Comparison ({results.length})
        </h1>
      </div>

      {/* Legend */}
      <div
        style={{
          display: "flex",
          gap: "16px",
          flexWrap: "wrap",
          marginBottom: "20px",
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "12px 16px",
        }}
      >
        {results.map((r: any, i: number) => (
          <div
            key={r.id}
            style={{ display: "flex", alignItems: "center", gap: "6px" }}
          >
            <div
              style={{
                width: "12px",
                height: "12px",
                borderRadius: "2px",
                background: COLORS[i % COLORS.length],
              }}
            />
            <span style={{ fontSize: "0.82rem" }}>
              {r.strategy || r.id?.slice(0, 12)}
            </span>
          </div>
        ))}
      </div>

      {/* Stats comparison table */}
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
          Statistics Comparison
        </h2>
        <div style={{ overflowX: "auto" }}>
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: "0.8rem",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            <thead>
              <tr>
                <th
                  style={{
                    padding: "8px",
                    textAlign: "left",
                    background: "var(--color-surface2)",
                    borderBottom: "2px solid var(--color-border)",
                    color: "var(--color-text-muted)",
                  }}
                >
                  Metric
                </th>
                {results.map((r: any, i: number) => (
                  <th
                    key={r.id}
                    style={{
                      padding: "8px",
                      textAlign: "right",
                      background: "var(--color-surface2)",
                      borderBottom: `2px solid ${COLORS[i % COLORS.length]}`,
                      color: COLORS[i % COLORS.length],
                    }}
                  >
                    {r.strategy || r.id?.slice(0, 12)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {statKeys.map((sk) => {
                const vals = results.map((r: any) => {
                  const s = r.stats || {};
                  return s[sk.key];
                });
                const best =
                  sk.key === "max_drawdown_pct"
                    ? Math.max(...vals.filter((v: any) => v != null))
                    : Math.max(...vals.filter((v: any) => v != null));
                return (
                  <tr key={sk.key}>
                    <td
                      style={{
                        padding: "6px 8px",
                        borderBottom: "1px solid var(--color-border)",
                        color: "var(--color-text-muted)",
                        fontWeight: 500,
                      }}
                    >
                      {sk.label}
                    </td>
                    {results.map((r: any, i: number) => {
                      const v = (r.stats || {})[sk.key];
                      const isBest =
                        v === best &&
                        vals.filter((x: any) => x === best).length === 1;
                      let display =
                        v != null
                          ? sk.fmt === "currency"
                            ? `$${v.toLocaleString("en-US", { maximumFractionDigits: 0 })}`
                            : sk.fmt === "percent"
                              ? `${(sk.key === "cagr" ? v * 100 : v).toFixed(2)}%`
                              : typeof v === "number"
                                ? v.toFixed(2)
                                : v
                          : "—";
                      return (
                        <td
                          key={r.id}
                          style={{
                            padding: "6px 8px",
                            textAlign: "right",
                            borderBottom: "1px solid var(--color-border)",
                            fontWeight: isBest ? 700 : 400,
                            color: isBest
                              ? COLORS[i % COLORS.length]
                              : "var(--color-text)",
                          }}
                        >
                          {display}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Equity curves placeholder */}
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "20px",
        }}
      >
        <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "16px" }}>
          Equity Curves
        </h2>
        <div
          style={{
            height: "350px",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--color-text-muted)",
          }}
        >
          Overlay equity chart will render here
        </div>
      </div>
    </div>
  );
}
