import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
} from "lightweight-charts";
import type {
  CandlestickData,
  HistogramData,
  IChartApi,
  IPriceLine,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  LineData,
  MouseEventParams,
  SeriesMarker,
  Time,
  UTCTimestamp,
  WhitespaceData,
} from "lightweight-charts";

type RangePreset = "50" | "150" | "300" | "all";

export type TradingViewChartTime = number | string;

export type TradingViewChartOHLC = {
  time: TradingViewChartTime;
  open: number;
  high: number;
  low: number;
  close: number;
};

export type TradingViewChartVolume = {
  time: TradingViewChartTime;
  value: number;
  color?: string;
};

export type TradingViewChartExit = {
  time: TradingViewChartTime;
  price: number;
  pnl?: number;
  reason?: string | null;
};

export type TradingViewChartTrade = {
  id?: string | number;
  nr?: number;
  symbol?: string;
  direction?: string;
  entry_time: TradingViewChartTime;
  entry_price: number;
  quantity?: number;
  position_size?: number | null;
  exit_reason?: string | null;
  exit_time?: TradingViewChartTime;
  exit_price?: number;
  exits?: TradingViewChartExit[];
  pnl?: number;
};

export type TradingViewChartIndicator = {
  name: string;
  data: Array<{ time: TradingViewChartTime; value: number | null }>;
  color?: string;
  panel?: string;
};

interface TradingViewChartProps {
  ohlcData: TradingViewChartOHLC[];
  volumeData?: TradingViewChartVolume[];
  trades: TradingViewChartTrade[];
  indicators: TradingViewChartIndicator[];
  height?: number;
  dataTestId?: string;
  activeTradeId?: string | number | null;
  focusTradeTime?: TradingViewChartTime | null;
  showToolbar?: boolean;
  isFullscreen?: boolean;
  onToggleFullscreen?: () => void;
}

type PreparedIndicator = {
  name: string;
  color: string;
  panel: "main" | "separate";
  data: Array<LineData<Time> | WhitespaceData<Time>>;
};

function normalizeUnixTime(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return Math.trunc(value);
  }
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric) && numeric > 0) {
      return Math.trunc(numeric);
    }
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) {
      return Math.trunc(parsed / 1000);
    }
  }
  return null;
}

function toChartTime(value: unknown): Time | null {
  const normalized = normalizeUnixTime(value);
  if (normalized === null) {
    return null;
  }
  return normalized as UTCTimestamp;
}

function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  })}`;
}

function formatPnl(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return `${value >= 0 ? "+" : ""}$${value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatLotSize(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "N/A";
  }
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

function normalizeExitReason(reason: unknown): string {
  const value = String(reason || "").trim().toLowerCase();
  if (!value) {
    return "Signal";
  }
  if (value === "tp" || value.includes("take_profit") || value.includes("take profit")) {
    return "TP";
  }
  if (value.includes("trailing")) {
    return "Trailing SL";
  }
  if (
    value === "sl" ||
    value.includes("stop_loss") ||
    value.includes("stop loss") ||
    value.includes("stopped")
  ) {
    return "SL";
  }
  return "Signal";
}

function formatMarkerTime(time: unknown): string {
  const normalized = normalizeUnixTime(time);
  if (normalized === null) {
    return "—";
  }
  return new Date(normalized * 1000).toISOString().slice(0, 16).replace("T", " ");
}

function resolveChartLocale(): string {
  if (typeof navigator === "undefined") {
    return "en-US";
  }
  const language = String(navigator.language || "").trim();
  return language && language !== "undefined" ? language : "en-US";
}

function findNearestBarIndex(times: number[], target: number): number {
  if (!times.length) {
    return -1;
  }
  let low = 0;
  let high = times.length - 1;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const value = times[mid];
    if (value === target) {
      return mid;
    }
    if (value < target) {
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  if (low <= 0) return 0;
  if (low >= times.length) return times.length - 1;
  const left = times[low - 1];
  const right = times[low];
  return Math.abs(target - left) <= Math.abs(right - target) ? low - 1 : low;
}

export function TradingViewChart({
  ohlcData,
  volumeData = [],
  trades,
  indicators,
  height = 560,
  dataTestId = "trade-chart-canvas",
  activeTradeId = null,
  focusTradeTime = null,
  showToolbar = true,
  isFullscreen = false,
  onToggleFullscreen,
}: TradingViewChartProps) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);

  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick", Time> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram", Time> | null>(null);
  const markersPrimitiveRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const indicatorSeriesRefs = useRef<ISeriesApi<"Line", Time>[]>([]);
  const separateIndicatorSeriesRefs = useRef<ISeriesApi<"Line", Time>[]>([]);
  const entryPriceLineRef = useRef<IPriceLine | null>(null);
  const exitPriceLineRef = useRef<IPriceLine | null>(null);
  const markerDetailsRef = useRef<Map<string, string>>(new Map());
  const lastFocusedTimeRef = useRef<number | null>(null);

  const [rangePreset, setRangePreset] = useState<RangePreset>("all");

  const normalizedCandles = useMemo<CandlestickData<Time>[]>(() => {
    const byTime = new Map<number, CandlestickData<Time>>();
    ohlcData.forEach((bar) => {
      const time = normalizeUnixTime(bar.time);
      const open = Number(bar.open);
      const high = Number(bar.high);
      const low = Number(bar.low);
      const close = Number(bar.close);
      if (
        time === null ||
        !Number.isFinite(open) ||
        !Number.isFinite(high) ||
        !Number.isFinite(low) ||
        !Number.isFinite(close)
      ) {
        return;
      }
      byTime.set(time, {
        time: time as UTCTimestamp,
        open,
        high,
        low,
        close,
      });
    });
    return Array.from(byTime.values()).sort((left, right) => Number(left.time) - Number(right.time));
  }, [ohlcData]);

  const candleTimes = useMemo(() => normalizedCandles.map((bar) => Number(bar.time)), [normalizedCandles]);
  const candleTimeSet = useMemo(() => new Set(candleTimes), [candleTimes]);

  const normalizedVolume = useMemo<HistogramData<Time>[]>(() => {
    const byTime = new Map<number, HistogramData<Time>>();
    volumeData.forEach((bar) => {
      const time = normalizeUnixTime(bar.time);
      const value = Number(bar.value);
      if (
        time === null ||
        !Number.isFinite(value) ||
        (candleTimeSet.size > 0 && !candleTimeSet.has(time))
      ) {
        return;
      }
      byTime.set(time, {
        time: time as UTCTimestamp,
        value,
        color: bar.color,
      });
    });
    return Array.from(byTime.values()).sort((left, right) => Number(left.time) - Number(right.time));
  }, [volumeData, candleTimeSet]);

  const preparedIndicators = useMemo<PreparedIndicator[]>(() => {
    return indicators
      .map((indicator) => {
        const panelLabel = String(indicator.panel || "main").toLowerCase();
        const panel: "main" | "separate" = panelLabel === "main" ? "main" : "separate";
        const pointsByTime = new Map<number, LineData<Time> | WhitespaceData<Time>>();
        indicator.data.forEach((point) => {
          const time = normalizeUnixTime(point.time);
          if (time === null || (candleTimeSet.size > 0 && !candleTimeSet.has(time))) {
            return;
          }
          if (point.value === null || point.value === undefined) {
            pointsByTime.set(time, { time: time as UTCTimestamp });
            return;
          }
          const numeric = Number(point.value);
          pointsByTime.set(
            time,
            Number.isFinite(numeric)
              ? { time: time as UTCTimestamp, value: numeric }
              : { time: time as UTCTimestamp },
          );
        });
        const data = Array.from(pointsByTime.values()).sort(
          (left, right) => Number(left.time) - Number(right.time),
        );
        return {
          name: indicator.name,
          color: indicator.color || "#79c0ff",
          panel,
          data,
        };
      })
      .filter((indicator) => indicator.data.length > 0);
  }, [indicators, candleTimeSet]);

  const activeTrade = useMemo<TradingViewChartTrade | null>(() => {
    if (!trades.length) {
      return null;
    }
    if (activeTradeId === null || activeTradeId === undefined) {
      return trades[0];
    }
    const matched = trades.find((trade, index) => {
      const tradeId = trade.id ?? trade.nr ?? index;
      return String(tradeId) === String(activeTradeId);
    });
    return matched || trades[0];
  }, [trades, activeTradeId]);

  const applyRangePreset = useCallback(
    (preset: RangePreset) => {
      const chart = chartRef.current;
      if (!chart || !normalizedCandles.length) {
        return;
      }
      if (preset === "all") {
        chart.timeScale().fitContent();
        return;
      }
      const bars = Number(preset);
      const to = normalizedCandles.length - 1;
      const from = Math.max(0, to - bars);
      chart.timeScale().setVisibleLogicalRange({ from, to });
    },
    [normalizedCandles.length],
  );

  const handleRangeSelect = useCallback(
    (preset: RangePreset) => {
      setRangePreset(preset);
      applyRangePreset(preset);
    },
    [applyRangePreset],
  );

  const handleScreenshot = useCallback(() => {
    const chart = chartRef.current;
    if (!chart) {
      return;
    }
    const canvas = chart.takeScreenshot(true, false);
    const link = document.createElement("a");
    link.download = `trade-chart-${Date.now()}.png`;
    link.href = canvas.toDataURL("image/png");
    link.click();
  }, []);

  useEffect(() => {
    if (!chartContainerRef.current) {
      return;
    }
    const container = chartContainerRef.current;
    const width = container.clientWidth || 960;
    const locale = resolveChartLocale();
    const chart = createChart(container, {
      width,
      height,
      localization: { locale },
      layout: {
        background: { type: ColorType.Solid, color: "#0d1117" },
        textColor: "#8b949e",
      },
      grid: {
        vertLines: { color: "#1f2630" },
        horzLines: { color: "#1f2630" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: {
          color: "rgba(121, 192, 255, 0.25)",
          width: 1,
        },
        horzLine: {
          color: "rgba(121, 192, 255, 0.22)",
          width: 1,
        },
      },
      rightPriceScale: {
        borderColor: "#30363d",
      },
      timeScale: {
        borderColor: "#30363d",
        timeVisible: true,
        rightOffset: 8,
      },
    });
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#3fb950",
      downColor: "#f85149",
      borderVisible: false,
      wickUpColor: "#3fb950",
      wickDownColor: "#f85149",
      priceLineVisible: false,
      lastValueVisible: false,
    });
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: "volume",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
      color: "rgba(88, 166, 255, 0.3)",
    });
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.74, bottom: 0 },
      borderVisible: false,
    });

    const markersPrimitive = createSeriesMarkers(candleSeries, []);

    const crosshairHandler = (param: MouseEventParams<Time>) => {
      const tooltip = tooltipRef.current;
      const wrapper = wrapperRef.current;
      if (!tooltip || !wrapper) {
        return;
      }
      const hoveredId =
        typeof param.hoveredObjectId === "string" ? param.hoveredObjectId : null;
      if (!hoveredId || !markerDetailsRef.current.has(hoveredId) || !param.point) {
        tooltip.style.opacity = "0";
        return;
      }
      tooltip.textContent = markerDetailsRef.current.get(hoveredId) || "";
      const left = Math.min(Math.max(param.point.x + 14, 8), Math.max(8, wrapper.clientWidth - 250));
      const top = Math.min(Math.max(param.point.y + 14, 8), Math.max(8, wrapper.clientHeight - 90));
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
      tooltip.style.opacity = "1";
    };
    chart.subscribeCrosshairMove(crosshairHandler);

    const handleResize = () => {
      const nextWidth = container.clientWidth || width;
      chart.resize(nextWidth, height);
    };
    const resizeObserver = new ResizeObserver(handleResize);
    resizeObserver.observe(container);

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    markersPrimitiveRef.current = markersPrimitive;

    const tooltipElement = tooltipRef.current;

    return () => {
      chart.unsubscribeCrosshairMove(crosshairHandler);
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      markersPrimitiveRef.current = null;
      indicatorSeriesRefs.current = [];
      separateIndicatorSeriesRefs.current = [];
      entryPriceLineRef.current = null;
      exitPriceLineRef.current = null;
      markerDetailsRef.current.clear();
      if (tooltipElement) {
        tooltipElement.style.opacity = "0";
      }
    };
  }, [height]);

  useEffect(() => {
    const chart = chartRef.current;
    const candleSeries = candleSeriesRef.current;
    const volumeSeries = volumeSeriesRef.current;
    if (!chart || !candleSeries || !volumeSeries) {
      return;
    }

    candleSeries.setData(normalizedCandles);
    volumeSeries.setData(normalizedVolume);

    indicatorSeriesRefs.current.forEach((series) => chart.removeSeries(series));
    separateIndicatorSeriesRefs.current.forEach((series) => chart.removeSeries(series));
    indicatorSeriesRefs.current = [];
    separateIndicatorSeriesRefs.current = [];

    preparedIndicators.forEach((indicator) => {
      const paneIndex = indicator.panel === "separate" ? 1 : 0;
      const lineSeries = chart.addSeries(
        LineSeries,
        {
          color: indicator.color,
          lineWidth: 2,
          lineStyle: LineStyle.Solid,
          title: indicator.name,
          lastValueVisible: false,
          priceLineVisible: false,
        },
        paneIndex,
      );
      lineSeries.setData(indicator.data);
      if (indicator.panel === "separate") {
        separateIndicatorSeriesRefs.current.push(lineSeries);
      } else {
        indicatorSeriesRefs.current.push(lineSeries);
      }
    });

    if (separateIndicatorSeriesRefs.current.length) {
      const panes = chart.panes();
      panes[0]?.setStretchFactor(0.72);
      panes[1]?.setStretchFactor(0.28);
    }

    applyRangePreset(rangePreset);
  }, [normalizedCandles, normalizedVolume, preparedIndicators, rangePreset, applyRangePreset]);

  useEffect(() => {
    const candleSeries = candleSeriesRef.current;
    const markersPrimitive = markersPrimitiveRef.current;
    if (!candleSeries || !markersPrimitive) {
      return;
    }

    const markers: SeriesMarker<Time>[] = [];
    const markerDetails = new Map<string, string>();

    trades.forEach((trade, index) => {
      const tradeKey = String(trade.id ?? trade.nr ?? index);
      const direction = String(trade.direction || "").toUpperCase();
      const isLong = direction !== "SHORT";
      const entryVerb = isLong ? "BUY" : "SELL";
      const lotSize = formatLotSize(trade.position_size ?? trade.quantity);
      const entryTime = toChartTime(trade.entry_time);
      if (entryTime !== null) {
        const entryId = `entry-${tradeKey}`;
        markers.push({
          id: entryId,
          time: entryTime,
          position: isLong ? "belowBar" : "aboveBar",
          color: "#3fb950",
          shape: isLong ? "arrowUp" : "arrowDown",
          text: `${entryVerb} ${lotSize} @ ${Number(trade.entry_price).toFixed(2)}`,
        });
        markerDetails.set(
          entryId,
          [
            `Trade #${trade.nr ?? tradeKey} ${direction || ""}`.trim(),
            `${entryVerb} ${lotSize} @ ${Number(trade.entry_price).toFixed(2)}`,
            `Entry: ${formatPrice(trade.entry_price)}`,
            `Time: ${formatMarkerTime(trade.entry_time)}`,
            `P&L: ${formatPnl(trade.pnl)}`,
          ].join("\n"),
        );
      }

      const exits =
        trade.exits && trade.exits.length
          ? trade.exits
          : trade.exit_time !== undefined && trade.exit_price !== undefined
            ? [{ time: trade.exit_time, price: trade.exit_price, pnl: trade.pnl }]
            : [];

      exits.forEach((exit, exitIndex) => {
        const exitTime = toChartTime(exit.time);
        if (exitTime === null) {
          return;
        }
        const isFinal = exitIndex === exits.length - 1;
        const pnlValue = exit.pnl ?? trade.pnl ?? 0;
        const exitColor = "#f85149";
        const exitId = `exit-${tradeKey}-${exitIndex}`;
        const reasonLabel = normalizeExitReason(exit.reason ?? trade.exit_reason);
        markers.push({
          id: exitId,
          time: exitTime,
          position: isLong ? "aboveBar" : "belowBar",
          color: exitColor,
          shape: isLong ? "arrowDown" : "arrowUp",
          text: `EXIT ${lotSize} @ ${Number(exit.price).toFixed(2)} · ${reasonLabel}`,
        });
        markerDetails.set(
          exitId,
          [
            `Trade #${trade.nr ?? tradeKey} ${isFinal ? "Exit" : "Partial Exit"}`,
            `EXIT ${lotSize} @ ${Number(exit.price).toFixed(2)} · ${reasonLabel}`,
            `Price: ${formatPrice(exit.price)}`,
            `Time: ${formatMarkerTime(exit.time)}`,
            `P&L: ${formatPnl(pnlValue)}`,
          ].join("\n"),
        );
      });
    });

    markers.sort((left, right) => Number(left.time) - Number(right.time));
    markersPrimitive.setMarkers(markers);
    markerDetailsRef.current = markerDetails;

    if (entryPriceLineRef.current) {
      candleSeries.removePriceLine(entryPriceLineRef.current);
      entryPriceLineRef.current = null;
    }
    if (exitPriceLineRef.current) {
      candleSeries.removePriceLine(exitPriceLineRef.current);
      exitPriceLineRef.current = null;
    }

    if (activeTrade) {
      entryPriceLineRef.current = candleSeries.createPriceLine({
        price: activeTrade.entry_price,
        color: "#58a6ff",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "Entry",
      });
      const fallbackExit = activeTrade.exits?.[activeTrade.exits.length - 1];
      const exitPrice = fallbackExit?.price ?? activeTrade.exit_price;
      if (exitPrice !== undefined && Number.isFinite(exitPrice)) {
        exitPriceLineRef.current = candleSeries.createPriceLine({
          price: exitPrice,
          color: (activeTrade.pnl ?? 0) >= 0 ? "#3fb950" : "#f85149",
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: "Exit",
        });
      }
    }
  }, [trades, activeTrade]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || focusTradeTime === null || focusTradeTime === undefined || !candleTimes.length) {
      return;
    }
    const focusTime = normalizeUnixTime(focusTradeTime);
    if (focusTime === null || focusTime === lastFocusedTimeRef.current) {
      return;
    }
    lastFocusedTimeRef.current = focusTime;
    const centerIndex = findNearestBarIndex(candleTimes, focusTime);
    if (centerIndex < 0) {
      return;
    }
    chart.timeScale().setVisibleLogicalRange({
      from: Math.max(centerIndex - 120, 0),
      to: Math.min(centerIndex + 80, candleTimes.length - 1),
    });
  }, [focusTradeTime, candleTimes]);

  return (
    <div ref={wrapperRef} style={{ width: "100%", position: "relative" }}>
      {showToolbar ? (
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "8px",
            flexWrap: "wrap",
            marginBottom: "8px",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
            {[
              { label: "50 bars", value: "50" as const },
              { label: "150 bars", value: "150" as const },
              { label: "300 bars", value: "300" as const },
              { label: "All", value: "all" as const },
            ].map((preset) => (
              <button
                key={preset.value}
                onClick={() => handleRangeSelect(preset.value)}
                style={{
                  border: `1px solid ${rangePreset === preset.value ? "rgba(88,166,255,0.45)" : "var(--color-border)"}`,
                  background:
                    rangePreset === preset.value ? "rgba(88,166,255,0.15)" : "var(--color-surface2)",
                  color: rangePreset === preset.value ? "#79c0ff" : "var(--color-text-muted)",
                  borderRadius: "8px",
                  padding: "6px 10px",
                  fontSize: "0.74rem",
                  cursor: "pointer",
                  fontWeight: 600,
                }}
              >
                {preset.label}
              </button>
            ))}
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
            <button
              onClick={handleScreenshot}
              style={{
                border: "1px solid var(--color-border)",
                borderRadius: "8px",
                background: "var(--color-surface2)",
                color: "var(--color-text)",
                padding: "6px 10px",
                fontSize: "0.74rem",
                cursor: "pointer",
                fontWeight: 600,
              }}
            >
              Screenshot
            </button>
            {onToggleFullscreen ? (
              <button
                onClick={onToggleFullscreen}
                style={{
                  border: "1px solid var(--color-border)",
                  borderRadius: "8px",
                  background: "var(--color-surface2)",
                  color: "var(--color-text)",
                  padding: "6px 10px",
                  fontSize: "0.74rem",
                  cursor: "pointer",
                  fontWeight: 600,
                }}
              >
                {isFullscreen ? "Exit Fullscreen" : "Fullscreen"}
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      <div
        ref={chartContainerRef}
        data-testid={dataTestId}
        style={{
          width: "100%",
          height: `${height}px`,
          border: "1px solid #30363d",
          borderRadius: "10px",
          overflow: "hidden",
          background: "#0d1117",
        }}
      />

      <div
        ref={tooltipRef}
        style={{
          position: "absolute",
          left: "8px",
          top: "8px",
          pointerEvents: "none",
          border: "1px solid rgba(88,166,255,0.4)",
          background: "rgba(13,17,23,0.95)",
          borderRadius: "8px",
          padding: "8px 10px",
          color: "#c9d1d9",
          fontSize: "0.72rem",
          lineHeight: 1.45,
          whiteSpace: "pre-line",
          maxWidth: "240px",
          opacity: 0,
          transition: "opacity 0.12s ease-out",
          boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
        }}
      />
    </div>
  );
}

export default TradingViewChart;
