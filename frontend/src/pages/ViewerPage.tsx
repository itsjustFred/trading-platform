import { useCallback, useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createSeriesMarkers,
  createChart,
} from "lightweight-charts";
import type { IChartApi, Time } from "lightweight-charts";

import {
  getMarketOHLCV,
  getMarketTimeframes,
  getStrategies,
  getSymbols,
  getViewerStrategySignals,
} from "../api/client";
import type {
  ChartBoxDrawing,
  ChartLineDrawing,
  ChartMarkerDrawing,
  ChartTriangleDrawing,
  IndicatorSeriesView,
  OHLCVBar,
  StrategyMeta,
  TradeDrawing,
  ViewerStrategySignalsResponse,
} from "../api/types";

const DEFAULT_LIMIT = 120000;
const LOOKBACK_YEARS = 10;
const INITIAL_VISIBLE_BARS = 320;
const MAX_DRAWING_MARKERS = 1200;
const MAX_DRAWING_LINES = 180;
const MAX_DRAWING_BOXES = 100;
const MAX_DRAWING_TRIANGLES = 100;
const TIMEFRAME_RANK: Record<string, number> = {
  "1m": 1,
  "5m": 2,
  "15m": 3,
  "30m": 4,
  "1h": 5,
  "4h": 6,
  "1d": 7,
  "1w": 8,
};
const PREFERRED_TIMEFRAMES = ["1h", "4h", "1d", "15m", "5m", "1m"];

function sortTimeframes(values: string[]): string[] {
  return [...values].sort((left, right) => {
    const leftRank = TIMEFRAME_RANK[left] ?? 99;
    const rightRank = TIMEFRAME_RANK[right] ?? 99;
    if (leftRank === rightRank) {
      return left.localeCompare(right);
    }
    return leftRank - rightRank;
  });
}

function chooseDefaultTimeframe(values: string[]): string {
  for (const candidate of PREFERRED_TIMEFRAMES) {
    if (values.includes(candidate)) {
      return candidate;
    }
  }
  return values[0] ?? "1h";
}

function formatRange(value: string | null): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString();
}

function isoDateYearsAgo(years: number): string {
  const current = new Date();
  const target = new Date(current);
  target.setUTCFullYear(current.getUTCFullYear() - years);
  return target.toISOString().slice(0, 10);
}

function parseJumpDateInput(value: string): number | null {
  const normalized = value.trim();
  if (!normalized) {
    return null;
  }

  const dmyMatch = normalized.match(/^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$/);
  if (dmyMatch) {
    const day = Number(dmyMatch[1]);
    const month = Number(dmyMatch[2]);
    const year = Number(dmyMatch[3]);
    if (day >= 1 && day <= 31 && month >= 1 && month <= 12) {
      return Math.floor(Date.UTC(year, month - 1, day) / 1000);
    }
    return null;
  }

  const ymdMatch = normalized.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (ymdMatch) {
    const year = Number(ymdMatch[1]);
    const month = Number(ymdMatch[2]);
    const day = Number(ymdMatch[3]);
    if (day >= 1 && day <= 31 && month >= 1 && month <= 12) {
      return Math.floor(Date.UTC(year, month - 1, day) / 1000);
    }
    return null;
  }

  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }

  return Math.floor(parsed.getTime() / 1000);
}

function formatDdMmYyyy(value: Date): string {
  const day = String(value.getDate()).padStart(2, "0");
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const year = value.getFullYear();
  return `${day}.${month}.${year}`;
}

function formatIsoDate(value: Date): string {
  return value.toISOString().slice(0, 10);
}

function isMarkerDrawing(drawing: TradeDrawing): drawing is ChartMarkerDrawing {
  return drawing.type === "marker";
}

function isLineDrawing(drawing: TradeDrawing): drawing is ChartLineDrawing {
  return drawing.type === "line";
}

function isBoxDrawing(drawing: TradeDrawing): drawing is ChartBoxDrawing {
  return drawing.type === "box";
}

function isTriangleDrawing(drawing: TradeDrawing): drawing is ChartTriangleDrawing {
  return drawing.type === "triangle";
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

function estimateBarStepSeconds(ohlcv: OHLCVBar[]): number {
  if (ohlcv.length < 2) {
    return 60;
  }

  const deltas: number[] = [];
  const limit = Math.min(ohlcv.length - 1, 200);
  for (let i = 1; i <= limit; i += 1) {
    const delta = ohlcv[i].time - ohlcv[i - 1].time;
    if (delta > 0) {
      deltas.push(delta);
    }
  }

  if (!deltas.length) {
    return 60;
  }

  deltas.sort((a, b) => a - b);
  return deltas[Math.floor(deltas.length / 2)] || 60;
}

function drawingFallsWithinWindow(
  drawing: TradeDrawing,
  startTime: number,
  endTime: number,
): boolean {
  if (isMarkerDrawing(drawing)) {
    return drawing.time >= startTime && drawing.time <= endTime;
  }
  if (isLineDrawing(drawing) || isBoxDrawing(drawing)) {
    return drawing.end_time >= startTime && drawing.start_time <= endTime;
  }
  if (isTriangleDrawing(drawing)) {
    return drawing.x >= startTime && drawing.x <= endTime;
  }
  return false;
}

function boxPolyline(drawing: ChartBoxDrawing): Array<{ time: Time; value: number }> {
  return [
    { time: drawing.start_time as Time, value: drawing.top },
    { time: drawing.end_time as Time, value: drawing.top },
    { time: drawing.end_time as Time, value: drawing.bottom },
    { time: drawing.start_time as Time, value: drawing.bottom },
    { time: drawing.start_time as Time, value: drawing.top },
  ];
}

function trianglePolyline(
  drawing: ChartTriangleDrawing,
  barStepSeconds: number,
): Array<{ time: Time; value: number }> {
  const sizeToken = String(drawing.size || "md").toLowerCase();
  const sizeFactor = sizeToken === "sm" ? 0.75 : sizeToken === "lg" ? 1.35 : 1;
  const scale = Number.isFinite(Number(drawing.scale_override))
    ? Number(drawing.scale_override)
    : sizeFactor;
  const widthSeconds = Math.max(barStepSeconds * 4 * scale, 1);
  const height = Math.max(Math.abs(drawing.y) * 0.003 * scale, 0.0001);
  const rotationDeg = Number.isFinite(Number(drawing.rotation))
    ? Number(drawing.rotation)
    : 0;
  const theta = (rotationDeg * Math.PI) / 180;

  const points = [
    { x: 0, y: height },
    { x: -widthSeconds / 2, y: -height / 2 },
    { x: widthSeconds / 2, y: -height / 2 },
  ];

  const rotated = points.map((point) => {
    const rx = point.x * Math.cos(theta) - point.y * Math.sin(theta);
    const ry = point.x * Math.sin(theta) + point.y * Math.cos(theta);
    return {
      time: (drawing.x + rx) as Time,
      value: drawing.y + ry,
    };
  });

  return [...rotated, rotated[0]];
}

export function ViewerPage() {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const now = new Date();

  const [symbols, setSymbols] = useState<string[]>([]);
  const [selectedSymbol, setSelectedSymbol] = useState("");
  const [availableTimeframes, setAvailableTimeframes] = useState<string[]>([]);
  const [selectedTimeframe, setSelectedTimeframe] = useState("1h");
  const [jumpDateInput, setJumpDateInput] = useState(formatDdMmYyyy(now));
  const [jumpDatePickerValue, setJumpDatePickerValue] = useState(formatIsoDate(now));
  const [bars, setBars] = useState<OHLCVBar[]>([]);
  const [strategies, setStrategies] = useState<StrategyMeta[]>([]);
  const [selectedStrategy, setSelectedStrategy] = useState("");
  const [showStrategies, setShowStrategies] = useState(false);
  const [loadingStrategies, setLoadingStrategies] = useState(true);
  const [loadingStrategyOverlay, setLoadingStrategyOverlay] = useState(false);
  const [strategyOverlay, setStrategyOverlay] =
    useState<ViewerStrategySignalsResponse | null>(null);

  const [loadingSymbols, setLoadingSymbols] = useState(true);
  const [loadingChart, setLoadingChart] = useState(false);
  const [error, setError] = useState("");
  const [count, setCount] = useState(0);
  const [rangeStart, setRangeStart] = useState<string | null>(null);
  const [rangeEnd, setRangeEnd] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoadingSymbols(true);
    getSymbols()
      .then((payload) => {
        if (!active) return;
        const uniqueSymbols = Array.from(
          new Set(payload.symbols.map((symbol) => symbol.trim().toUpperCase())),
        )
          .filter(Boolean)
          .sort((left, right) => left.localeCompare(right));

        setSymbols(uniqueSymbols);
        setSelectedSymbol((current) => current || uniqueSymbols[0] || "");
      })
      .catch((loadError) => {
        if (!active) return;
        const message =
          loadError instanceof Error
            ? loadError.message
            : "Failed to load symbol catalog.";
        setError(message);
      })
      .finally(() => {
        if (!active) return;
        setLoadingSymbols(false);
      });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setLoadingStrategies(true);

    getStrategies()
      .then((payload) => {
        if (!active) return;
        const sorted = [...payload].sort((left, right) =>
          left.name.localeCompare(right.name),
        );
        setStrategies(sorted);
        setSelectedStrategy((current) => current || sorted[0]?.slug || "");
      })
      .catch(() => {
        if (!active) return;
        setStrategies([]);
        setSelectedStrategy("");
      })
      .finally(() => {
        if (!active) return;
        setLoadingStrategies(false);
      });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!selectedSymbol) {
      setAvailableTimeframes([]);
      return;
    }

    let active = true;
    getMarketTimeframes(selectedSymbol)
      .then((payload) => {
        if (!active) return;
        const sortedTimeframes = sortTimeframes(payload.timeframes || []);
        const fallbackTimeframes =
          sortedTimeframes.length > 0 ? sortedTimeframes : ["1h"];
        setAvailableTimeframes(fallbackTimeframes);
        setSelectedTimeframe((current) => {
          if (fallbackTimeframes.includes(current)) {
            return current;
          }
          return chooseDefaultTimeframe(fallbackTimeframes);
        });
      })
      .catch(() => {
        if (!active) return;
        setAvailableTimeframes(["1h"]);
        setSelectedTimeframe("1h");
      });

    return () => {
      active = false;
    };
  }, [selectedSymbol]);

  const loadMarketData = useCallback(async () => {
    if (!selectedSymbol || !selectedTimeframe) {
      return;
    }

    setLoadingChart(true);
    setError("");

    try {
      const response = await getMarketOHLCV({
        symbol: selectedSymbol,
        timeframe: selectedTimeframe,
        start: isoDateYearsAgo(LOOKBACK_YEARS),
        limit: DEFAULT_LIMIT,
      });

      setBars(response.ohlcv || []);
      setCount(response.count || 0);
      setRangeStart(response.start);
      setRangeEnd(response.end);
    } catch (loadError) {
      setBars([]);
      setCount(0);
      setRangeStart(null);
      setRangeEnd(null);
      const message =
        loadError instanceof Error
          ? loadError.message
          : "Failed to load OHLCV data.";
      setError(message);
    } finally {
      setLoadingChart(false);
    }
  }, [selectedSymbol, selectedTimeframe]);

  useEffect(() => {
    if (!selectedSymbol || !selectedTimeframe) {
      return;
    }
    void loadMarketData();
  }, [selectedSymbol, selectedTimeframe, loadMarketData]);

  useEffect(() => {
    if (!selectedStrategy || !selectedSymbol || !selectedTimeframe || !bars.length) {
      setLoadingStrategyOverlay(false);
      setStrategyOverlay(null);
      return;
    }

    let active = true;
    setLoadingStrategyOverlay(true);
    setStrategyOverlay(null);

    getViewerStrategySignals({
      strategy: selectedStrategy,
      symbol: selectedSymbol,
      timeframe: selectedTimeframe,
      start: isoDateYearsAgo(LOOKBACK_YEARS),
      limit: DEFAULT_LIMIT,
    })
      .then((payload) => {
        if (!active) {
          return;
        }
        setStrategyOverlay(payload);
      })
      .catch(() => {
        if (!active) {
          return;
        }
        setStrategyOverlay(null);
      })
      .finally(() => {
        if (!active) {
          return;
        }
        setLoadingStrategyOverlay(false);
      });

    return () => {
      active = false;
    };
  }, [selectedStrategy, selectedSymbol, selectedTimeframe, bars.length]);

  useEffect(() => {
    const container = chartContainerRef.current;
    if (!container) {
      return;
    }

    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
    }

    if (!bars.length) {
      return;
    }

    const chartHeight = 560;
    const chart = createChart(container, {
      width: container.clientWidth || 1080,
      height: chartHeight,
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

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#3fb950",
      downColor: "#f85149",
      wickUpColor: "#3fb950",
      wickDownColor: "#f85149",
      borderVisible: false,
    });

    candleSeries.setData(
      bars.map((bar) => ({
        time: bar.time as Time,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      })),
    );

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });

    volumeSeries.setData(
      bars.map((bar) => ({
        time: bar.time as Time,
        value: bar.volume || 0,
        color:
          bar.close >= bar.open ? "rgba(63,185,80,0.25)" : "rgba(248,81,73,0.25)",
      })),
    );

    const markers: Array<{
      time: Time;
      position: "aboveBar" | "belowBar" | "inBar";
      color: string;
      shape: "arrowUp" | "arrowDown" | "circle" | "square";
      text?: string;
    }> = [];

    if (strategyOverlay?.markers?.length) {
      strategyOverlay.markers.forEach((marker) => {
        markers.push({
          time: marker.time as Time,
          position: normalizeMarkerPosition(marker.position),
          color: marker.color || "#f59e0b",
          shape: normalizeMarkerShape(marker.shape),
          text: marker.text,
        });
      });
    }

    const visibleStartTime = bars[0]?.time ?? 0;
    const visibleEndTime = bars[bars.length - 1]?.time ?? 0;
    const sourceDrawings = (strategyOverlay?.drawings || []).filter((drawing) =>
      drawingFallsWithinWindow(drawing, visibleStartTime, visibleEndTime),
    );

    const markerDrawings = sourceDrawings
      .filter(isMarkerDrawing)
      .slice(0, MAX_DRAWING_MARKERS);
    const lineDrawings = sourceDrawings
      .filter(isLineDrawing)
      .slice(0, MAX_DRAWING_LINES);
    const boxDrawings = sourceDrawings
      .filter(isBoxDrawing)
      .slice(0, MAX_DRAWING_BOXES);
    const triangleDrawings = sourceDrawings
      .filter(isTriangleDrawing)
      .slice(0, MAX_DRAWING_TRIANGLES);
    const barStepSeconds = estimateBarStepSeconds(bars);

    markerDrawings.forEach((drawing) => {
      markers.push({
        time: drawing.time as Time,
        position: normalizeMarkerPosition(drawing.position),
        color: drawing.color || "#f59e0b",
        shape: normalizeMarkerShape(drawing.shape),
        text: drawing.text,
      });
    });

    lineDrawings.forEach((drawing, index) => {
      const lineSeries = chart.addSeries(LineSeries, {
        color: drawing.color || "#f59e0b",
        lineWidth: resolveLineWidth(drawing.lineWidth ?? 2),
        lineStyle: resolveLineStyle(drawing.lineStyle),
        title: drawing.title || `Strategy Line ${index + 1}`,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      lineSeries.setData([
        { time: drawing.start_time as Time, value: drawing.val1 },
        { time: drawing.end_time as Time, value: drawing.val2 },
      ]);
    });

    boxDrawings.forEach((drawing, index) => {
      const lineSeries = chart.addSeries(LineSeries, {
        color: drawing.color || "#d29922",
        lineWidth: resolveLineWidth(drawing.lineWidth ?? 1),
        lineStyle: resolveLineStyle(drawing.lineStyle),
        title: drawing.title || `Strategy Box ${index + 1}`,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      lineSeries.setData(boxPolyline(drawing));
    });

    triangleDrawings.forEach((drawing, index) => {
      const lineSeries = chart.addSeries(LineSeries, {
        color: drawing.color || "#f59e0b",
        lineWidth: 1,
        title: drawing.title || `Strategy Triangle ${index + 1}`,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      lineSeries.setData(trianglePolyline(drawing, barStepSeconds));
    });

    if (markers.length) {
      createSeriesMarkers(
        candleSeries,
        markers.sort((left, right) => Number(left.time) - Number(right.time)),
      );
    }

    if (strategyOverlay?.indicators?.length) {
      strategyOverlay.indicators
        .filter(
          (indicator: IndicatorSeriesView) =>
            (indicator.type || indicator.pane) === "overlay" && indicator.data?.length,
        )
        .forEach((indicator: IndicatorSeriesView) => {
          const lineSeries = chart.addSeries(LineSeries, {
            color: indicator.style?.color || indicator.color || "#58a6ff",
            lineWidth: 1,
            lineStyle: indicator.style?.lineStyle ?? indicator.lineStyle ?? 0,
            title: indicator.name,
          });
          lineSeries.setData(
            indicator.data
              .filter((point) => point.value != null && !Number.isNaN(point.value))
              .map((point) => ({ time: point.time as Time, value: point.value })),
          );
        });

      strategyOverlay.indicators
        .filter(
          (indicator: IndicatorSeriesView) =>
            (indicator.type || indicator.pane) === "panel" && indicator.data?.length,
        )
        .forEach((indicator: IndicatorSeriesView, index: number) => {
          const scaleId = `viewer-panel-${index}`;
          const lineSeries = chart.addSeries(LineSeries, {
            color: indicator.style?.color || indicator.color || "#a78bfa",
            lineWidth: 1,
            title: indicator.name,
            priceScaleId: scaleId,
          });
          lineSeries.setData(
            indicator.data
              .filter((point) => point.value != null && !Number.isNaN(point.value))
              .map((point) => ({ time: point.time as Time, value: point.value })),
          );
          chart.priceScale(scaleId).applyOptions({
            scaleMargins: { top: 0.85 + index * 0.05, bottom: 0 },
          });
        });
    }

    chart.timeScale().fitContent();
    if (bars.length > INITIAL_VISIBLE_BARS) {
      chart.timeScale().setVisibleLogicalRange({
        from: bars.length - INITIAL_VISIBLE_BARS,
        to: bars.length + 10,
      });
    }

    const resizeObserver = new ResizeObserver(() => {
      if (container.clientWidth > 0) {
        chart.resize(container.clientWidth, chartHeight);
      }
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, [bars, strategyOverlay]);

  const jumpToDate = useCallback(() => {
    const timestamp = parseJumpDateInput(jumpDateInput);
    if (!timestamp) {
      setError("Invalid date. Use dd.mm.yyyy or yyyy-mm-dd.");
      return;
    }
    if (!bars.length) {
      setError("No chart data loaded yet.");
      return;
    }

    let closestIndex = 0;
    let smallestDistance = Number.POSITIVE_INFINITY;
    for (let index = 0; index < bars.length; index += 1) {
      const distance = Math.abs(bars[index].time - timestamp);
      if (distance < smallestDistance) {
        smallestDistance = distance;
        closestIndex = index;
      }
    }

    const chart = chartRef.current;
    if (!chart) {
      setError("Chart is not ready yet.");
      return;
    }

    const halfWindow = Math.floor(INITIAL_VISIBLE_BARS / 2);
    chart.timeScale().setVisibleLogicalRange({
      from: Math.max(0, closestIndex - halfWindow),
      to: Math.min(bars.length - 1, closestIndex + halfWindow),
    });
    setError("");
  }, [bars, jumpDateInput]);

  const selectedStrategyName =
    strategies.find((strategy) => strategy.slug === selectedStrategy)?.name || "";

  return (
    <div style={{ padding: "24px" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "18px",
          flexWrap: "wrap",
          gap: "12px",
        }}
      >
        <div>
          <h1 style={{ fontSize: "1.5rem", fontWeight: 600, marginBottom: 0 }}>
            Viewer
          </h1>
        </div>
        <div
          style={{
            fontSize: "0.8rem",
            color: "var(--color-text-muted)",
            textAlign: "right",
            lineHeight: 1.4,
          }}
        >
          <div>{count.toLocaleString("en-US")} candles loaded</div>
          {selectedStrategyName ? (
            <div>
              {loadingStrategyOverlay
                ? "Loading strategy overlay..."
                : `Signals: ${strategyOverlay?.entry_count ?? 0} entries, ${strategyOverlay?.exit_count ?? 0} exits`}
            </div>
          ) : null}
          <div>
            {formatRange(rangeStart)} to {formatRange(rangeEnd)}
          </div>
        </div>
      </div>

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "14px",
          marginBottom: "16px",
          display: "flex",
          gap: "12px",
          flexWrap: "wrap",
          alignItems: "end",
        }}
      >
        <div style={{ minWidth: "220px", flex: "1 1 260px" }}>
          <div style={{ fontSize: "0.74rem", color: "var(--color-text-muted)", marginBottom: "6px" }}>
            Symbol
          </div>
          <input
            list="viewer-symbols"
            value={selectedSymbol}
            onChange={(event) => setSelectedSymbol(event.target.value.toUpperCase())}
            placeholder={loadingSymbols ? "Loading symbols..." : "Symbol"}
            style={{
              width: "100%",
              background: "var(--color-surface2)",
              color: "var(--color-text)",
              border: "1px solid var(--color-border)",
              borderRadius: "6px",
              padding: "9px 10px",
              fontSize: "0.85rem",
            }}
          />
          <datalist id="viewer-symbols">
            {symbols.map((symbol) => (
              <option key={symbol} value={symbol} />
            ))}
          </datalist>
        </div>

        <div style={{ minWidth: "240px", flex: "2 1 320px" }}>
          <div style={{ fontSize: "0.74rem", color: "var(--color-text-muted)", marginBottom: "6px" }}>
            Timeframe
          </div>
          <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
            {availableTimeframes.map((timeframe) => {
              const active = timeframe === selectedTimeframe;
              return (
                <button
                  key={timeframe}
                  onClick={() => setSelectedTimeframe(timeframe)}
                  style={{
                    border: active ? "1px solid rgba(88,166,255,0.55)" : "1px solid var(--color-border)",
                    background: active ? "rgba(88,166,255,0.16)" : "var(--color-surface2)",
                    color: active ? "#58a6ff" : "var(--color-text)",
                    borderRadius: "999px",
                    padding: "6px 12px",
                    fontSize: "0.78rem",
                    cursor: "pointer",
                  }}
                >
                  {timeframe}
                </button>
              );
            })}
          </div>
        </div>

        <div style={{ minWidth: "220px", flex: "1 1 260px" }}>
          <div style={{ fontSize: "0.74rem", color: "var(--color-text-muted)", marginBottom: "6px" }}>
            Date
          </div>
          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
            <input
              type="date"
              value={jumpDatePickerValue}
              onChange={(event) => {
                const value = event.target.value;
                setJumpDatePickerValue(value);
                if (!value) {
                  return;
                }
                const parsed = new Date(`${value}T00:00:00`);
                if (!Number.isNaN(parsed.getTime())) {
                  setJumpDateInput(formatDdMmYyyy(parsed));
                }
              }}
              style={{
                background: "var(--color-surface2)",
                color: "var(--color-text)",
                border: "1px solid var(--color-border)",
                borderRadius: "6px",
                padding: "9px 10px",
                fontSize: "0.82rem",
              }}
            />
            <input
              type="text"
              value={jumpDateInput}
              onChange={(event) => setJumpDateInput(event.target.value)}
              onBlur={() => {
                const ts = parseJumpDateInput(jumpDateInput);
                if (!ts) {
                  return;
                }
                setJumpDatePickerValue(formatIsoDate(new Date(ts * 1000)));
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  jumpToDate();
                }
              }}
              placeholder="dd.mm.yyyy"
              style={{
                minWidth: "130px",
                flex: "1 1 130px",
                background: "var(--color-surface2)",
                color: "var(--color-text)",
                border: "1px solid var(--color-border)",
                borderRadius: "6px",
                padding: "9px 10px",
                fontSize: "0.82rem",
              }}
            />
            <button
              onClick={jumpToDate}
              disabled={!bars.length}
              style={{
                border: "1px solid var(--color-border)",
                background: "var(--color-surface2)",
                color: "var(--color-text)",
                borderRadius: "6px",
                padding: "9px 12px",
                fontSize: "0.82rem",
                cursor: bars.length ? "pointer" : "not-allowed",
                opacity: bars.length ? 1 : 0.65,
              }}
            >
              Jump
            </button>
          </div>
        </div>

        <div style={{ minWidth: "190px" }}>
          <button
            onClick={() => setShowStrategies((current) => !current)}
            style={{
              border: "1px solid var(--color-border)",
              background: "var(--color-surface2)",
              color: "var(--color-text)",
              borderRadius: "6px",
              padding: "9px 12px",
              fontSize: "0.82rem",
              cursor: "pointer",
              minWidth: "130px",
            }}
          >
            Strategys
          </button>
          {showStrategies ? (
            <div
              style={{
                marginTop: "8px",
                padding: "8px",
                border: "1px solid var(--color-border)",
                borderRadius: "6px",
                background: "var(--color-surface2)",
                minWidth: "220px",
              }}
            >
              <select
                value={selectedStrategy}
                onChange={(event) => setSelectedStrategy(event.target.value)}
                disabled={loadingStrategies || !strategies.length}
                style={{
                  width: "100%",
                  background: "var(--color-surface)",
                  color: "var(--color-text)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "6px",
                  padding: "8px",
                  fontSize: "0.82rem",
                }}
              >
                {loadingStrategies ? (
                  <option value="">Loading strategies...</option>
                ) : null}
                {!loadingStrategies && !strategies.length ? (
                  <option value="">No strategies found</option>
                ) : null}
                {strategies.map((strategy) => (
                  <option key={strategy.slug} value={strategy.slug}>
                    {strategy.name}
                  </option>
                ))}
              </select>
              {selectedStrategyName ? (
                <div
                  style={{
                    marginTop: "6px",
                    fontSize: "0.75rem",
                    color: "var(--color-text-muted)",
                  }}
                >
                  Selected: {selectedStrategyName}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>

        <button
          onClick={() => void loadMarketData()}
          disabled={loadingChart || !selectedSymbol || !selectedTimeframe}
          style={{
            border: "none",
            background: loadingChart ? "#1f6feb" : "#58a6ff",
            color: "#ffffff",
            borderRadius: "6px",
            padding: "9px 14px",
            fontSize: "0.82rem",
            cursor: loadingChart ? "wait" : "pointer",
            minWidth: "108px",
            opacity: !selectedSymbol || !selectedTimeframe ? 0.65 : 1,
          }}
        >
          {loadingChart ? "Loading..." : "Reload"}
        </button>
      </div>

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          overflow: "hidden",
        }}
      >
        {error ? (
          <div
            style={{
              padding: "20px",
              color: "#f85149",
              borderBottom: "1px solid var(--color-border)",
              fontSize: "0.82rem",
            }}
          >
            {error}
          </div>
        ) : null}

        {bars.length > 0 ? (
          <div ref={chartContainerRef} style={{ width: "100%", height: "560px" }} />
        ) : (
          <div
            style={{
              width: "100%",
              height: "560px",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--color-text-muted)",
              fontSize: "0.9rem",
            }}
          >
            {loadingChart ? "Loading market candles..." : "Select a symbol and load chart data."}
          </div>
        )}
      </div>
    </div>
  );
}
