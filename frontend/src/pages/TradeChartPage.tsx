import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  getBacktestTrades,
  getBacktestTrade,
  getBacktestInfo,
  downloadData,
  getDataTask,
  ApiError,
} from "../api/client";
import type {
  BacktestInfo,
  ChartBoxDrawing,
  ChartLineDrawing,
  ChartLineWithBreaksDrawing,
  ChartMarkerDrawing,
  IndicatorSeriesView,
  MissingOHLCVDetail,
  Trade,
  TradeChartResponse,
  TradeDrawing,
} from "../api/types";
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

function isMarkerDrawing(drawing: TradeDrawing): drawing is ChartMarkerDrawing {
  return drawing.type === "marker";
}

function isLineDrawing(drawing: TradeDrawing): drawing is ChartLineDrawing {
  return drawing.type === "line";
}

function isLineWithBreaksDrawing(
  drawing: TradeDrawing,
): drawing is ChartLineWithBreaksDrawing {
  return drawing.type === "line_with_breaks";
}

function isBoxDrawing(drawing: TradeDrawing): drawing is ChartBoxDrawing {
  return drawing.type === "box";
}

function normalizeMarkerPosition(value: unknown): "aboveBar" | "belowBar" | "inBar" {
  if (value === "belowBar" || value === "inBar") {
    return value;
  }
  return "aboveBar";
}

function normalizeMarkerShape(
  value: unknown,
): "arrowUp" | "arrowDown" | "circle" | "square" {
  if (
    value === "arrowUp" ||
    value === "arrowDown" ||
    value === "circle" ||
    value === "square"
  ) {
    return value;
  }
  return "circle";
}

function resolveLineStyle(value: unknown): number {
  if (value === "dotted") {
    return LineStyle.Dotted;
  }
  if (value === "dashed") {
    return LineStyle.Dashed;
  }
  return LineStyle.Solid;
}

function resolveLineWidth(value: unknown): 1 | 2 | 3 | 4 {
  const numeric = Number(value);
  if (numeric >= 4) {
    return 4;
  }
  if (numeric >= 3) {
    return 3;
  }
  if (numeric >= 2) {
    return 2;
  }
  return 1;
}

function drawingFallsWithinWindow(
  drawing: TradeDrawing,
  startTime: number,
  endTime: number,
): boolean {
  if (isMarkerDrawing(drawing)) {
    return drawing.time >= startTime && drawing.time <= endTime;
  }
  if (isLineWithBreaksDrawing(drawing)) {
    const first = drawing.data?.[0]?.time;
    const last = drawing.data?.[drawing.data.length - 1]?.time;
    if (typeof first !== "number" || typeof last !== "number") {
      return false;
    }
    return last >= startTime && first <= endTime;
  }
  return drawing.end_time >= startTime && drawing.start_time <= endTime;
}

function isMissingOHLCVDetail(value: unknown): value is MissingOHLCVDetail {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    candidate.code === "MISSING_OHLCV" &&
    typeof candidate.symbol === "string" &&
    typeof candidate.timeframe === "string"
  );
}

function sourceButtonLabel(source: string): string {
  if (source === "dukascopy") {
    return "Download from Dukascopy";
  }
  if (source === "yfinance") {
    return "Download from Yahoo Finance";
  }
  if (source === "dolthub") {
    return "Sync from Dolthub";
  }
  return "Download data";
}

export function TradeChartPage() {
  const { id, nr } = useParams<{ id: string; nr: string }>();
  const navigate = useNavigate();
  const [trades, setTrades] = useState<Trade[]>([]);
  const [info, setInfo] = useState<BacktestInfo | null>(null);
  const [tradeData, setTradeData] = useState<TradeChartResponse | null>(null);
  const [isLoadingTrade, setIsLoadingTrade] = useState(false);
  const [missingData, setMissingData] = useState<MissingOHLCVDetail | null>(null);
  const [downloadTaskId, setDownloadTaskId] = useState<string | null>(null);
  const [downloadStatus, setDownloadStatus] = useState<string>("idle");
  const [downloadError, setDownloadError] = useState<string>("");
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

    setIsLoadingTrade(true);
    setDownloadTaskId(null);
    setDownloadStatus("idle");
    setDownloadError("");
    getBacktestTrade(id, currentNr)
      .then((payload) => {
        setTradeData(payload);
        setMissingData(null);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && isMissingOHLCVDetail(err.detail)) {
          setMissingData(err.detail);
          setTradeData(null);
          return;
        }
        setMissingData(null);
        setTradeData(null);
      })
      .finally(() => setIsLoadingTrade(false));
  }, [id, currentNr]);

  useEffect(() => {
    if (!downloadTaskId) {
      return;
    }
    if (["completed", "failed", "cancelled"].includes(downloadStatus)) {
      return;
    }

    const interval = window.setInterval(() => {
      getDataTask(downloadTaskId)
        .then((task) => {
          const status = (task.status || "running").toLowerCase();
          setDownloadStatus(status);
          if (status === "failed") {
            setDownloadError(task.log || "Download failed");
          }
        })
        .catch((err: unknown) => {
          setDownloadStatus("failed");
          setDownloadError(
            err instanceof Error ? err.message : "Could not read download task status",
          );
        });
    }, 1500);

    return () => window.clearInterval(interval);
  }, [downloadTaskId, downloadStatus]);

  // Render chart when data arrives
  useEffect(() => {
    if (!chartContainerRef.current || !tradeData?.ohlcv?.length) return;

    // Clean up previous chart
    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
    }

    const container = chartContainerRef.current;
    container.style.position = "relative";
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

    const ohlcv: CandlestickData[] = tradeData.ohlcv.map((bar) => ({
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
      tradeData.ohlcv.map((bar) => ({
        time: bar.time as Time,
        value: bar.volume || 0,
        color:
          bar.close >= bar.open ? "rgba(63,185,80,0.2)" : "rgba(248,81,73,0.2)",
      })),
    );

    const visibleStartTime = tradeData.ohlcv[0]?.time ?? 0;
    const visibleEndTime = tradeData.ohlcv[tradeData.ohlcv.length - 1]?.time ?? 0;
    const visibleDrawings = (tradeData.drawings || []).filter((drawing) =>
      drawingFallsWithinWindow(drawing, visibleStartTime, visibleEndTime),
    );

    // Trade markers + custom drawings
    const trade = tradeData.trade;
    const markers: Array<{
      time: Time;
      position: "aboveBar" | "belowBar" | "inBar";
      color: string;
      shape: "arrowUp" | "arrowDown" | "circle" | "square";
      text?: string;
    }> = [];
    if (trade) {
      // Entry marker
      markers.push({
        time: trade.entry_time as Time,
        position: trade.direction === "LONG" ? "belowBar" : "aboveBar",
        color: trade.direction === "LONG" ? "#3fb950" : "#f85149",
        shape: trade.direction === "LONG" ? "arrowUp" : "arrowDown",
        text: `${trade.direction} @ ${trade.entry_price?.toFixed(2)}`,
      });

      if (Array.isArray(trade.exits) && trade.exits.length > 0) {
        trade.exits.forEach((exit, index) => {
          const finalExit = index === trade.exits!.length - 1;
          markers.push({
            time: exit.time as Time,
            position: trade.direction === "LONG" ? "aboveBar" : "belowBar",
            color: finalExit
              ? trade.pnl >= 0
                ? "#3fb950"
                : "#f85149"
              : "#f59e0b",
            shape: "circle",
            text: `${finalExit ? "Exit" : "Partial"} @ ${exit.price?.toFixed(2)}${exit.reason ? ` (${exit.reason})` : ""}`,
          });
        });
      } else {
        markers.push({
          time: trade.exit_time as Time,
          position: trade.direction === "LONG" ? "aboveBar" : "belowBar",
          color: trade.pnl >= 0 ? "#3fb950" : "#f85149",
          shape: "circle",
          text: `Exit @ ${trade.exit_price?.toFixed(2)} (${trade.pnl >= 0 ? "+" : ""}$${trade.pnl?.toFixed(0)})`,
        });
      }

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
    }

    visibleDrawings
      .filter(isMarkerDrawing)
      .forEach((drawing) => {
        markers.push({
          time: drawing.time as Time,
          position: normalizeMarkerPosition(drawing.position),
          color: drawing.color || "#f59e0b",
          shape: normalizeMarkerShape(drawing.shape),
          text: drawing.text,
        });
      });

    createSeriesMarkers(
      candleSeries,
      markers.sort((left, right) => Number(left.time) - Number(right.time)),
    );

    visibleDrawings.filter(isLineDrawing).forEach((drawing, index) => {
      const startValue =
        typeof drawing.start_value === "number"
          ? drawing.start_value
          : drawing.val1;
      const endValue =
        typeof drawing.end_value === "number"
          ? drawing.end_value
          : drawing.val2;
      if (typeof startValue !== "number" || typeof endValue !== "number") {
        return;
      }
      const lineSeries = chart.addSeries(LineSeries, {
        color: drawing.color || "#f59e0b",
        lineWidth: resolveLineWidth(drawing.lineWidth ?? 2),
        lineStyle: resolveLineStyle(drawing.lineStyle),
        title: drawing.title || `Drawing ${index + 1}`,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      lineSeries.setData([
        { time: drawing.start_time as Time, value: startValue },
        { time: drawing.end_time as Time, value: endValue },
      ]);
    });

    visibleDrawings.filter(isLineWithBreaksDrawing).forEach((drawing, index) => {
      const lineSeries = chart.addSeries(LineSeries, {
        color: drawing.color || "#f59e0b",
        lineWidth: resolveLineWidth(drawing.lineWidth ?? 2),
        lineStyle: resolveLineStyle(drawing.lineStyle),
        title: drawing.title || `LineBreak ${index + 1}`,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      lineSeries.setData(
        (drawing.data || []).map((point) => {
          if (point.value === null || Number.isNaN(point.value)) {
            return { time: point.time as Time };
          }
          return { time: point.time as Time, value: point.value };
        }),
      );
    });

    const boxOverlayRoot = document.createElement("div");
    boxOverlayRoot.style.position = "absolute";
    boxOverlayRoot.style.left = "0";
    boxOverlayRoot.style.top = "0";
    boxOverlayRoot.style.width = "100%";
    boxOverlayRoot.style.height = `${h}px`;
    boxOverlayRoot.style.pointerEvents = "none";
    container.appendChild(boxOverlayRoot);

    const renderBoxes = () => {
      boxOverlayRoot.replaceChildren();
      visibleDrawings.filter(isBoxDrawing).forEach((drawing) => {
        const left = chart.timeScale().timeToCoordinate(drawing.start_time as Time);
        const right = chart.timeScale().timeToCoordinate(drawing.end_time as Time);
        const top = candleSeries.priceToCoordinate(drawing.high);
        const bottom = candleSeries.priceToCoordinate(drawing.low);
        if (
          left === null ||
          right === null ||
          top === null ||
          bottom === null ||
          !Number.isFinite(left) ||
          !Number.isFinite(right) ||
          !Number.isFinite(top) ||
          !Number.isFinite(bottom)
        ) {
          return;
        }

        const box = document.createElement("div");
        box.style.position = "absolute";
        box.style.left = `${Math.min(left, right)}px`;
        box.style.width = `${Math.max(1, Math.abs(right - left))}px`;
        box.style.top = `${Math.min(top, bottom)}px`;
        box.style.height = `${Math.max(1, Math.abs(bottom - top))}px`;
        box.style.background = drawing.color || "rgba(88, 166, 255, 0.1)";
        box.style.border = `1px solid ${drawing.border_color || "#58a6ff"}`;
        box.style.boxSizing = "border-box";
        boxOverlayRoot.appendChild(box);
      });
    };
    renderBoxes();

    // Indicator overlays
    if (tradeData.indicators && Array.isArray(tradeData.indicators)) {
      // Overlay indicators (on main chart)
      tradeData.indicators
        .filter(
          (ind: IndicatorSeriesView) =>
            (ind.type || ind.pane) === "overlay" && ind.data?.length,
        )
        .forEach((ind: IndicatorSeriesView) => {
          const lineSeries = chart.addSeries(LineSeries, {
            color: ind.style?.color || ind.color || "#58a6ff",
            lineWidth: 1,
            lineStyle: ind.style?.lineStyle ?? ind.lineStyle ?? 0,
            title: ind.name,
          });
          lineSeries.setData(
            ind.data
              .filter((d) => d.value != null && !isNaN(d.value))
              .map((d) => ({ time: d.time as Time, value: d.value })),
          );
        });

      // Panel indicators (separate price scale)
      tradeData.indicators
        .filter(
          (ind: IndicatorSeriesView) =>
            (ind.type || ind.pane) === "panel" && ind.data?.length,
        )
        .forEach((ind: IndicatorSeriesView, idx: number) => {
          const scaleId = `panel-${idx}`;
          const lineSeries = chart.addSeries(LineSeries, {
            color: ind.style?.color || ind.color || "#a78bfa",
            lineWidth: 1,
            title: ind.name,
            priceScaleId: scaleId,
          });
          lineSeries.setData(
            ind.data
              .filter((d) => d.value != null && !isNaN(d.value))
              .map((d) => ({ time: d.time as Time, value: d.value })),
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
        renderBoxes();
      }
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      boxOverlayRoot.remove();
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

  const triggerMissingDataDownload = async () => {
    if (!missingData) {
      return;
    }
    const request = missingData.download_request;
    const source = request?.source || missingData.suggested_source || "dukascopy";
    const symbols = request?.symbols || missingData.symbol;
    const timeframes = request?.timeframes || missingData.timeframe;
    const start = request?.start || info?.timeframe?.start || "";
    const end = request?.end || info?.timeframe?.end || "";

    setDownloadStatus("starting");
    setDownloadError("");
    try {
      const started = await downloadData({
        source,
        symbols,
        timeframes,
        start,
        end,
      });
      setDownloadTaskId(started.task_id);
      setDownloadStatus(started.status || "running");
    } catch (err) {
      setDownloadStatus("failed");
      setDownloadError(err instanceof Error ? err.message : "Could not start download");
    }
  };

  const reloadCurrentTrade = () => {
    if (!id || !currentNr) {
      return;
    }
    setIsLoadingTrade(true);
    getBacktestTrade(id, currentNr)
      .then((payload) => {
        setTradeData(payload);
        setMissingData(null);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && isMissingOHLCVDetail(err.detail)) {
          setMissingData(err.detail);
          setTradeData(null);
          return;
        }
        setMissingData(null);
        setTradeData(null);
      })
      .finally(() => setIsLoadingTrade(false));
  };
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
        data-testid="trade-chart-area"
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
            data-testid="trade-chart-canvas"
            style={{ width: "100%", height: "460px" }}
          />
        ) : missingData ? (
          <div
            data-testid="missing-ohlcv"
            style={{
              minHeight: "460px",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              padding: "24px",
            }}
          >
            <div
              style={{
                width: "min(680px, 100%)",
                border: "1px solid var(--color-border)",
                borderRadius: "10px",
                background: "var(--color-surface2)",
                padding: "20px",
              }}
            >
              <div style={{ fontSize: "1rem", fontWeight: 700, marginBottom: "8px" }}>
                Missing OHLCV data for {missingData.symbol} {missingData.timeframe}
              </div>
              <div style={{ color: "var(--color-text-muted)", fontSize: "0.9rem" }}>
                Source suggestion: <strong>{missingData.suggested_source}</strong>
                {missingData.alternative_sources?.length
                  ? ` (alternatives: ${missingData.alternative_sources.join(", ")})`
                  : ""}
              </div>

              <div style={{ display: "flex", gap: "10px", marginTop: "16px", flexWrap: "wrap" }}>
                <button
                  data-testid="missing-data-download-btn"
                  onClick={triggerMissingDataDownload}
                  disabled={downloadStatus === "starting" || downloadStatus === "running"}
                  style={{
                    border: "1px solid #3fb950",
                    borderRadius: "8px",
                    background: "rgba(63, 185, 80, 0.12)",
                    color: "#3fb950",
                    padding: "8px 12px",
                    cursor: "pointer",
                    fontWeight: 600,
                  }}
                >
                  {sourceButtonLabel(missingData.suggested_source)}
                </button>

                <button
                  onClick={reloadCurrentTrade}
                  data-testid="missing-data-reload-btn"
                  style={{
                    border: "1px solid var(--color-border)",
                    borderRadius: "8px",
                    background: "var(--color-surface)",
                    color: "var(--color-text)",
                    padding: "8px 12px",
                    cursor: "pointer",
                  }}
                >
                  Reload chart
                </button>
              </div>

              <div style={{ marginTop: "12px", fontSize: "0.82rem", color: "var(--color-text-muted)" }}>
                {downloadTaskId
                  ? `Download task ${downloadTaskId} status: ${downloadStatus}`
                  : "No automatic download is started. Use the button above to trigger a download."}
              </div>
              {downloadError ? (
                <div style={{ marginTop: "8px", color: "#f85149", fontSize: "0.82rem" }}>
                  {downloadError}
                </div>
              ) : null}
            </div>
          </div>
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
            {isLoadingTrade
              ? "Loading chart data..."
              : "No OHLCV data available"}
          </div>
        )}
      </div>
    </div>
  );
}
