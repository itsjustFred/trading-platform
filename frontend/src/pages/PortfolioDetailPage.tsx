import { useState, useEffect, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { getPortfolioDetail } from "../api/client";
import { StatCard } from "../components/StatCard";
import { ArrowLeft } from "lucide-react";
import {
  createChart,
  AreaSeries,
  HistogramSeries,
  ColorType,
  CrosshairMode,
} from "lightweight-charts";
import type { IChartApi, Time } from "lightweight-charts";
import { formatPValue, pValueColor } from "../utils/stats";

export function PortfolioDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    getPortfolioDetail(id)
      .then(setDetail)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [id]);

  if (loading)
    return (
      <div style={{ padding: "20px", color: "var(--color-text-muted)" }}>
        Loading...
      </div>
    );
  if (!detail)
    return (
      <div style={{ padding: "20px", color: "var(--color-red)" }}>
        Portfolio not found
      </div>
    );

  const stats = detail.combined_stats || {};
  const equity = detail.combined_equity || [];
  const trades = detail.trades || [];
  const contributions = detail.strategy_contributions || [];

  return (
    <div style={{ padding: "24px" }}>
      {/* Header */}
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "24px",
          marginBottom: "20px",
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
            }}
          >
            <ArrowLeft size={20} />
          </button>
          <div>
            <h1 style={{ fontSize: "1.5rem", fontWeight: 600 }}>
              {detail.name || id}
            </h1>
            <div
              style={{
                color: "var(--color-text-muted)",
                fontSize: "0.82rem",
                marginTop: "4px",
              }}
            >
              {detail.strategies?.length || 0} strategies • $
              {(detail.account_capital || 100000).toLocaleString()} capital •{" "}
              {trades.length} trades
            </div>
          </div>
        </div>
      </div>

      {/* Stats */}
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
          value={stats.net_profit || 0}
          format="currency"
        />
        <StatCard
          label="Sharpe"
          value={(stats.sharpe || 0).toFixed(3)}
          color={stats.sharpe > 1 ? "#3fb950" : "#d29922"}
        />
        <StatCard
          label="CAGR"
          value={((stats.cagr || 0) * 100).toFixed(2)}
          format="percent"
        />
        <StatCard
          label="PSR"
          value={
            stats.psr != null ? `${(Number(stats.psr) * 100).toFixed(1)}%` : "N/A"
          }
          color={
            Number(stats.psr) > 0.95
              ? "#3fb950"
              : Number(stats.psr) > 0.5
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
          label="Max Drawdown"
          value={((stats.max_drawdown || 0) * 100).toFixed(2)}
          format="percent"
          color="#f85149"
        />
        <StatCard
          label="Total Trades"
          value={trades.length}
          format="number"
          color="#58a6ff"
        />
      </div>

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
        <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "16px" }}>
          Combined Equity Curve
        </h2>
        {equity.length > 1 ? (
          <PortfolioEquityChart data={equity} />
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
            No equity data available
          </div>
        )}
      </div>

      {/* Strategy Contributions */}
      {contributions.length > 0 && (
        <div
          style={{
            background: "var(--color-surface)",
            border: "1px solid var(--color-border)",
            borderRadius: "8px",
            padding: "20px",
            marginBottom: "20px",
          }}
        >
          <h2
            style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "16px" }}
          >
            Strategy Contributions
          </h2>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(250px, 1fr))",
              gap: "10px",
            }}
          >
            {contributions.map((c: any, i: number) => (
              <div
                key={i}
                style={{
                  background: "var(--color-surface2)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "6px",
                  padding: "12px",
                }}
              >
                <div style={{ fontWeight: 600, fontSize: "0.85rem" }}>
                  {c.strategy_name}
                </div>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    marginTop: "6px",
                    fontSize: "0.78rem",
                  }}
                >
                  <span
                    style={{
                      color: c.pnl_contribution >= 0 ? "#3fb950" : "#f85149",
                    }}
                  >
                    $
                    {c.pnl_contribution?.toLocaleString("en-US", {
                      maximumFractionDigits: 0,
                    })}
                  </span>
                  <span style={{ color: "var(--color-text-muted)" }}>
                    {c.pnl_contribution_pct?.toFixed(1)}%
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Strategies in portfolio */}
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "20px",
        }}
      >
        <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "16px" }}>
          Strategies ({detail.strategies?.length || 0})
        </h2>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(250px, 1fr))",
            gap: "10px",
          }}
        >
          {(detail.strategies || []).map((s: any, i: number) => (
            <div
              key={i}
              style={{
                background: "var(--color-surface2)",
                border: "1px solid var(--color-border)",
                borderRadius: "6px",
                padding: "12px",
              }}
            >
              <div style={{ fontWeight: 600, fontSize: "0.85rem" }}>
                {s.strategy_name || s.name}
              </div>
              <div
                style={{
                  fontSize: "0.72rem",
                  color: "var(--color-text-muted)",
                  marginTop: "4px",
                }}
              >
                Risk: {s.risk_per_trade_pct}% • Max pos: {s.max_positions}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function PortfolioEquityChart({
  data,
}: {
  data: { time: string; equity: number }[];
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
    const chart = createChart(container, {
      width: w,
      height: 340,
      localization: { locale: navigator.language || "en-US" },
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

    // Convert ISO time strings to unix timestamps
    const parsed = data.map((d) => ({
      time: (new Date(d.time).getTime() / 1000) as Time,
      equity: d.equity,
    }));

    const isProfit = parsed[parsed.length - 1].equity >= parsed[0].equity;
    const lineColor = isProfit ? "#3fb950" : "#f85149";

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
      parsed.map((d) => ({ time: d.time, value: d.equity })),
    );

    // Drawdown histogram
    let peak = parsed[0].equity;
    const ddData = parsed.map((d) => {
      if (d.equity > peak) peak = d.equity;
      const dd = (d.equity - peak) / peak;
      return {
        time: d.time,
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

    // Tooltip
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
      const initial = parsed[0].equity;
      const ret = (((eq - initial) / initial) * 100).toFixed(2);
      let p = initial;
      for (const d of parsed) {
        if ((d.time as number) > (param.time as number)) break;
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
  }, [data]);

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
