import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  getBacktestTrades,
  getBacktestTrade,
  getBacktestInfo,
} from "../api/client";
import { ArrowLeft, ChevronLeft, ChevronRight } from "lucide-react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  LineStyle,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createSeriesMarkers,
} from "lightweight-charts";
import type { IChartApi, CandlestickData, Time } from "lightweight-charts";

function formatTradeMetaValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }

  if (typeof value === "number") {
    return Number.isInteger(value)
      ? value.toLocaleString("en-US")
      : value.toLocaleString("en-US", { maximumFractionDigits: 4 });
  }

  if (typeof value === "boolean") {
    return value ? "True" : "False";
  }

  if (Array.isArray(value)) {
    return value.map(formatTradeMetaValue).join(", ");
  }

  if (typeof value === "object") {
    return JSON.stringify(value);
  }

  return String(value);
}

export function TradeChartPage() {
  const { id, nr } = useParams<{ id: string; nr: string }>();
  const navigate = useNavigate();
  const [trades, setTrades] = useState<any[]>([]);
  const [_info, setInfo] = useState<any>(null);
  const [tradeData, setTradeData] = useState<any>(null);
  const [currentNr, setCurrentNr] = useState(parseInt(nr || "1"));
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!id) return;
    Promise.all([getBacktestTrades(id), getBacktestInfo(id)])
      .then(([t, i]) => {
        setTrades(t);
        setInfo(i);
      })
      .catch(() => {});
  }, [id]);

  useEffect(() => {
    setCurrentNr(parseInt(nr || "1"));
  }, [nr]);

  // Load OHLCV data for current trade
  useEffect(() => {
    if (!id || !currentNr) return;
    getBacktestTrade(id, currentNr)
      .then(setTradeData)
      .catch(() => setTradeData(null));
  }, [id, currentNr]);

  // Render chart when data arrives
  useEffect(() => {
    if (!chartContainerRef.current || !tradeData?.ohlcv?.length) return;

    // Clean up previous chart
    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
    }

    const container = chartContainerRef.current;
    const w = container.clientWidth || 1100;
    const h = 460;
    const locale =
      typeof navigator.language === "string" &&
      navigator.language !== "undefined"
        ? navigator.language
        : "en-US";
    const chart = createChart(container, {
      width: w,
      height: h,
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
      timeScale: { borderColor: "#30363d", timeVisible: true },
      rightPriceScale: { borderColor: "#30363d" },
    });
    chartRef.current = chart;

    // Candlestick series
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#3fb950",
      downColor: "#f85149",
      wickUpColor: "#3fb950",
      wickDownColor: "#f85149",
      borderVisible: false,
    });

    const ohlcv: CandlestickData[] = tradeData.ohlcv.map((bar: any) => ({
      time: bar.time as Time,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));
    candleSeries.setData(ohlcv);

    // Volume series
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });
    volumeSeries.setData(
      tradeData.ohlcv.map((bar: any) => ({
        time: bar.time as Time,
        value: bar.volume || 0,
        color:
          bar.close >= bar.open ? "rgba(63,185,80,0.2)" : "rgba(248,81,73,0.2)",
      })),
    );

    // Trade markers
    const trade = tradeData.trade;
    if (trade) {
      const markers: any[] = [];
      // Entry marker
      markers.push({
        time: trade.entry_time as Time,
        position: trade.direction === "LONG" ? "belowBar" : "aboveBar",
        color: trade.direction === "LONG" ? "#3fb950" : "#f85149",
        shape: trade.direction === "LONG" ? "arrowUp" : "arrowDown",
        text: `${trade.direction} @ ${trade.entry_price?.toFixed(2)}`,
      });
      // Exit marker
      markers.push({
        time: trade.exit_time as Time,
        position: trade.direction === "LONG" ? "aboveBar" : "belowBar",
        color: trade.pnl >= 0 ? "#3fb950" : "#f85149",
        shape: "circle",
        text: `Exit @ ${trade.exit_price?.toFixed(2)} (${trade.pnl >= 0 ? "+" : ""}$${trade.pnl?.toFixed(0)})`,
      });
      // Entry/exit horizontal lines
      candleSeries.createPriceLine({
        price: trade.entry_price,
        color: "#58a6ff",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "Entry",
      });
      candleSeries.createPriceLine({
        price: trade.exit_price,
        color: trade.pnl >= 0 ? "#3fb950" : "#f85149",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "Exit",
      });
      createSeriesMarkers(
        candleSeries,
        markers.sort((a, b) => (a.time as number) - (b.time as number)),
      );
    }

    // Indicator overlays
    if (tradeData.indicators && Array.isArray(tradeData.indicators)) {
      // Overlay indicators (on main chart)
      tradeData.indicators
        .filter(
          (ind: any) =>
            (ind.type || ind.pane) === "overlay" && ind.data?.length,
        )
        .forEach((ind: any) => {
          const lineSeries = chart.addSeries(LineSeries, {
            color: ind.style?.color || ind.color || "#58a6ff",
            lineWidth: 1,
            lineStyle: ind.style?.lineStyle ?? ind.lineStyle ?? 0,
            title: ind.name,
          });
          lineSeries.setData(
            ind.data
              .filter((d: any) => d.value != null && !isNaN(d.value))
              .map((d: any) => ({ time: d.time as Time, value: d.value })),
          );
        });

      // Panel indicators (separate price scale)
      tradeData.indicators
        .filter(
          (ind: any) => (ind.type || ind.pane) === "panel" && ind.data?.length,
        )
        .forEach((ind: any, idx: number) => {
          const scaleId = `panel-${idx}`;
          const lineSeries = chart.addSeries(LineSeries, {
            color: ind.style?.color || ind.color || "#a78bfa",
            lineWidth: 1,
            title: ind.name,
            priceScaleId: scaleId,
          });
          lineSeries.setData(
            ind.data
              .filter((d: any) => d.value != null && !isNaN(d.value))
              .map((d: any) => ({ time: d.time as Time, value: d.value })),
          );
          chart
            .priceScale(scaleId)
            .applyOptions({
              scaleMargins: { top: 0.85 + idx * 0.05, bottom: 0 },
            });
        });
    }

    chart.timeScale().fitContent();

    // Force resize to ensure canvas pixel buffer matches container
    requestAnimationFrame(() => {
      if (container.clientWidth > 0) {
        chart.resize(container.clientWidth, h);
      }
    });

    // Resize observer for responsive
    const resizeObserver = new ResizeObserver(() => {
      if (container.clientWidth > 0) {
        chart.resize(container.clientWidth, h);
      }
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, [tradeData]);

  // Keyboard navigation
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "ArrowLeft" && currentNr > 1) {
        navigate(`/backtests/${id}/trade/${currentNr - 1}`, { replace: true });
      } else if (e.key === "ArrowRight" && currentNr < trades.length) {
        navigate(`/backtests/${id}/trade/${currentNr + 1}`, { replace: true });
      } else if (e.key === "Escape") {
        navigate(`/backtests/${id}`);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [currentNr, trades.length, id, navigate]);

  const currentTrade =
    tradeData?.trade ?? trades.find((t) => t.nr === currentNr);
  const tradeContextEntries = useMemo(() => {
    const meta = currentTrade?.meta;
    if (!meta || typeof meta !== "object") {
      return [];
    }

    return Object.entries(meta as Record<string, unknown>)
      .filter(([, value]) => value !== undefined && value !== null)
      .sort(([left], [right]) => left.localeCompare(right));
  }, [currentTrade]);

  return (
    <div style={{ padding: "24px" }}>
      {/* Toolbar */}
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "12px 16px",
          marginBottom: "16px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <button
          onClick={() => navigate(`/backtests/${id}`)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: "6px",
            background: "none",
            border: "none",
            color: "#58a6ff",
            cursor: "pointer",
            fontSize: "0.82rem",
          }}
        >
          <ArrowLeft size={16} /> Back to Report
        </button>

        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <button
            onClick={() =>
              currentNr > 1 &&
              navigate(`/backtests/${id}/trade/${currentNr - 1}`, {
                replace: true,
              })
            }
            disabled={currentNr <= 1}
            style={{
              background: "none",
              border: "none",
              color: currentNr <= 1 ? "#333" : "var(--color-text-muted)",
              cursor: "pointer",
            }}
          >
            <ChevronLeft size={18} />
          </button>
          <span
            style={{ fontSize: "0.82rem", color: "var(--color-text-muted)" }}
          >
            Trade {currentNr} / {trades.length}
          </span>
          <button
            onClick={() =>
              currentNr < trades.length &&
              navigate(`/backtests/${id}/trade/${currentNr + 1}`, {
                replace: true,
              })
            }
            disabled={currentNr >= trades.length}
            style={{
              background: "none",
              border: "none",
              color:
                currentNr >= trades.length ? "#333" : "var(--color-text-muted)",
              cursor: "pointer",
            }}
          >
            <ChevronRight size={18} />
          </button>
        </div>

        <div style={{ fontSize: "0.75rem", color: "var(--color-text-muted)" }}>
          ← → navigate • Esc back
        </div>
      </div>

      {/* Trade Info */}
      {currentTrade && (
        <div
          style={{
            display: "flex",
            gap: "16px",
            flexWrap: "wrap",
            marginBottom: "16px",
            alignItems: "stretch",
          }}
        >
          <div
            style={{
              flex: "2 1 560px",
              background: "var(--color-surface)",
              border: "1px solid var(--color-border)",
              borderRadius: "8px",
              padding: "20px",
            }}
          >
            <div style={{ display: "flex", gap: "20px", flexWrap: "wrap" }}>
              <div
                style={{
                  background: "var(--color-surface2)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "6px",
                  padding: "8px 14px",
                }}
              >
                <div
                  style={{
                    color: "var(--color-text-muted)",
                    fontSize: "0.7rem",
                    textTransform: "uppercase",
                  }}
                >
                  Direction
                </div>
                <div
                  style={{
                    fontWeight: 700,
                    color:
                      currentTrade.direction === "LONG" ? "#3fb950" : "#f85149",
                  }}
                >
                  {currentTrade.direction}
                </div>
              </div>
              <div
                style={{
                  background: "var(--color-surface2)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "6px",
                  padding: "8px 14px",
                }}
              >
                <div
                  style={{
                    color: "var(--color-text-muted)",
                    fontSize: "0.7rem",
                    textTransform: "uppercase",
                  }}
                >
                  Symbol
                </div>
                <div style={{ fontWeight: 700 }}>{currentTrade.symbol}</div>
              </div>
              <div
                style={{
                  background: "var(--color-surface2)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "6px",
                  padding: "8px 14px",
                }}
              >
                <div
                  style={{
                    color: "var(--color-text-muted)",
                    fontSize: "0.7rem",
                    textTransform: "uppercase",
                  }}
                >
                  Entry
                </div>
                <div style={{ fontWeight: 700 }}>
                  ${currentTrade.entry_price?.toFixed(2)} @{" "}
                  {(currentTrade.entry_time_iso || "").slice(0, 16)}
                </div>
              </div>
              <div
                style={{
                  background: "var(--color-surface2)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "6px",
                  padding: "8px 14px",
                }}
              >
                <div
                  style={{
                    color: "var(--color-text-muted)",
                    fontSize: "0.7rem",
                    textTransform: "uppercase",
                  }}
                >
                  Exit
                </div>
                <div style={{ fontWeight: 700 }}>
                  ${currentTrade.exit_price?.toFixed(2)} @{" "}
                  {(currentTrade.exit_time_iso || "").slice(0, 16)}
                </div>
              </div>
              <div
                style={{
                  background: "var(--color-surface2)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "6px",
                  padding: "8px 14px",
                }}
              >
                <div
                  style={{
                    color: "var(--color-text-muted)",
                    fontSize: "0.7rem",
                    textTransform: "uppercase",
                  }}
                >
                  P&L
                </div>
                <div
                  style={{
                    fontWeight: 700,
                    color: currentTrade.pnl >= 0 ? "#3fb950" : "#f85149",
                  }}
                >
                  $
                  {currentTrade.pnl?.toLocaleString("en-US", {
                    maximumFractionDigits: 2,
                  })}{" "}
                  ({currentTrade.pnl_pct?.toFixed(2)}%)
                </div>
              </div>
            </div>
          </div>

          {tradeContextEntries.length > 0 && (
            <div
              style={{
                flex: "1 1 320px",
                background: "var(--color-surface)",
                border: "1px solid var(--color-border)",
                borderRadius: "8px",
                padding: "20px",
              }}
            >
              <div
                style={{
                  color: "var(--color-text-muted)",
                  fontSize: "0.72rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.08em",
                  marginBottom: "10px",
                }}
              >
                Trade Context
              </div>
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: "10px",
                }}
              >
                {tradeContextEntries.map(([key, value]) => (
                  <div
                    key={key}
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      gap: "12px",
                      paddingBottom: "10px",
                      borderBottom: "1px solid var(--color-border)",
                    }}
                  >
                    <div
                      style={{
                        color: "var(--color-text-muted)",
                        fontSize: "0.82rem",
                        wordBreak: "break-word",
                      }}
                    >
                      {key}
                    </div>
                    <div
                      style={{
                        fontWeight: 600,
                        textAlign: "right",
                        wordBreak: "break-word",
                      }}
                    >
                      {formatTradeMetaValue(value)}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Chart area */}
      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          overflow: "hidden",
        }}
      >
        {tradeData?.ohlcv?.length ? (
          <div
            ref={chartContainerRef}
            style={{ width: "100%", height: "460px" }}
          />
        ) : (
          <div
            style={{
              height: "460px",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--color-text-muted)",
            }}
          >
            {tradeData === null
              ? "Loading chart data..."
              : "No OHLCV data available"}
          </div>
        )}
      </div>
    </div>
  );
}
