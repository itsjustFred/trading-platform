import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ApiError,
  downloadData,
  getBacktestInfo,
  getBacktestReplay,
  getDataTask,
} from "../api/client";
import type {
  BacktestInfo,
  BacktestReplayResponse,
  ChartBoxDrawing,
  ChartLineDrawing,
  ChartLineWithBreaksDrawing,
  ChartMarkerDrawing,
  IndicatorPoint,
  IndicatorSeriesView,
  MissingOHLCVDetail,
  OHLCVBar,
  Trade,
  TradeDrawing,
  TradeRiskMath,
} from "../api/types";
import {
  ArrowLeft,
  Maximize2,
  Minimize2,
  Pause,
  Play,
  SkipBack,
  SkipForward,
} from "lucide-react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  LineStyle,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  AreaSeries,
  createSeriesMarkers,
} from "lightweight-charts";
import type { CandlestickData, IChartApi, LogicalRange, Time } from "lightweight-charts";

type PanelGroup = {
  key: string;
  label: string;
  indicators: IndicatorSeriesView[];
};

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

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) {
      return numeric;
    }
  }
  return null;
}

function formatUsd(value: number, digits = 2): string {
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function formatPrice(value: number): string {
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  })}`;
}

function isTradeRiskMath(value: unknown): value is TradeRiskMath {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function buildRiskMathNarrative(riskMath: TradeRiskMath): string {
  const targetRisk = toFiniteNumber(riskMath.risk_target_usd);
  const size = toFiniteNumber(riskMath.position_size);
  const entry = toFiniteNumber(riskMath.entry_price);
  const stop = toFiniteNumber(riskMath.sl_price);
  const leverageCapped = riskMath.capped_by_max_leverage === true;
  const positionCapped = riskMath.capped_by_max_position === true;
  const maxLeverage = toFiniteNumber(riskMath.max_leverage);

  const parts: string[] = [];
  if (targetRisk !== null) {
    parts.push(`Target Risk: ${formatUsd(targetRisk)}`);
  }
  if (size !== null) {
    parts.push(`Size: ${size.toLocaleString("en-US", { maximumFractionDigits: 4 })} units`);
  }
  if (entry !== null && stop !== null) {
    parts.push(`Entry/SL: ${formatPrice(entry)} → ${formatPrice(stop)}`);
  }
  if (leverageCapped) {
    parts.push("Size capped by max leverage");
  } else if (positionCapped) {
    parts.push("Size capped by max position");
  }
  if (maxLeverage !== null) {
    parts.push(`Max leverage: ${maxLeverage.toFixed(2)}x`);
  }
  return parts.join(" | ");
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
  if (typeof value === "number") {
    return value;
  }
  return LineStyle.Solid;
}

function resolveLineWidth(value: unknown): 1 | 2 | 3 | 4 {
  const numeric = Number(value);
  if (numeric >= 4) return 4;
  if (numeric >= 3) return 3;
  if (numeric >= 2) return 2;
  return 1;
}

function normalizeUnixTime(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric) && numeric > 0) {
      return numeric;
    }
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) {
      return parsed / 1000;
    }
  }
  return null;
}

function sanitizeOHLCVBars(bars: OHLCVBar[]): OHLCVBar[] {
  const barsByTime = new Map<number, OHLCVBar>();
  bars.forEach((bar) => {
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
    const volume = Number(bar.volume);
    barsByTime.set(time, {
      time,
      open,
      high,
      low,
      close,
      volume: Number.isFinite(volume) ? volume : 0,
    });
  });
  return Array.from(barsByTime.values()).sort((left, right) => left.time - right.time);
}

function sanitizeIndicatorPoints(
  points: IndicatorPoint[],
  validTimes: Set<number> | null,
): IndicatorPoint[] {
  const pointsByTime = new Map<number, IndicatorPoint>();
  points.forEach((point) => {
    const time = normalizeUnixTime(point.time);
    if (time === null) {
      return;
    }
    if (validTimes && !validTimes.has(time)) {
      return;
    }
    const rawValue = point.value;
    let value: number | null = null;
    if (rawValue !== null && rawValue !== undefined) {
      const numeric = Number(rawValue);
      if (Number.isFinite(numeric)) {
        value = numeric;
      }
    }
    pointsByTime.set(time, { time, value });
  });
  return Array.from(pointsByTime.values()).sort((left, right) => left.time - right.time);
}

function toLineSeriesData(points: IndicatorPoint[]): Array<{ time: Time; value?: number }> {
  return points.map((point) => {
    if (point.value === null || Number.isNaN(point.value)) {
      return { time: point.time as Time };
    }
    return { time: point.time as Time, value: point.value };
  });
}

function pointsInRange(points: IndicatorPoint[], start: number, end: number): IndicatorPoint[] {
  return points.filter(
    (point) =>
      Number.isFinite(point.time) && point.time >= start && point.time <= end,
  );
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
  if (source === "dukascopy") return "Download from Dukascopy";
  if (source === "yfinance") return "Download from Yahoo Finance";
  if (source === "dolthub") return "Sync from Dolthub";
  return "Download data";
}

function inferPanelKey(indicator: IndicatorSeriesView): string {
  const stylePane =
    indicator.style && typeof indicator.style === "object"
      ? String((indicator.style as { pane?: unknown }).pane || "")
      : "";
  const explicitPane = String(indicator.pane || stylePane || "").trim().toLowerCase();
  if (explicitPane && explicitPane !== "panel" && explicitPane !== "overlay") {
    return explicitPane;
  }

  const name = indicator.name.toLowerCase();
  if (name.includes("macd") || name.includes("signal") || name.includes("hist")) {
    return "macd";
  }
  if (name.includes("rsi")) {
    return "rsi";
  }
  if (name.includes("atr")) {
    return "atr";
  }
  return "panel";
}

function inferPanelLabel(key: string): string {
  if (key === "macd") return "MACD";
  if (key === "rsi") return "RSI";
  if (key === "atr") return "ATR";
  if (key === "panel") return "Panel";
  return key.toUpperCase();
}

function indicatorIsPanel(indicator: IndicatorSeriesView): boolean {
  const type = String(indicator.type || "").toLowerCase();
  const pane = String(indicator.pane || "").toLowerCase();
  return type === "panel" || pane === "panel" || type.startsWith("panel-");
}

function findBarIndexByTime(ohlcv: OHLCVBar[], targetTime: number): number {
  if (!ohlcv.length) {
    return -1;
  }
  let low = 0;
  let high = ohlcv.length - 1;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const value = ohlcv[mid].time;
    if (value === targetTime) {
      return mid;
    }
    if (value < targetTime) {
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return Math.min(Math.max(low, 0), ohlcv.length - 1);
}

export function TradeChartPage() {
  const { id, nr } = useParams<{ id: string; nr: string }>();
  const navigate = useNavigate();

  const [info, setInfo] = useState<BacktestInfo | null>(null);
  const [replay, setReplay] = useState<BacktestReplayResponse | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [currentNr, setCurrentNr] = useState(parseInt(nr || "1", 10));
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [isPlaying, setIsPlaying] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const [downloadTaskId, setDownloadTaskId] = useState<string | null>(null);
  const [downloadStatus, setDownloadStatus] = useState<string>("idle");
  const [downloadError, setDownloadError] = useState<string>("");
  const currentNrRef = useRef(currentNr);

  const viewportRef = useRef<HTMLDivElement>(null);
  const mainChartContainerRef = useRef<HTMLDivElement>(null);
  const panelContainerRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const chartRefs = useRef<IChartApi[]>([]);
  const mainChartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<any>(null);
  const priceLineRefs = useRef<{ entry: any | null; exit: any | null }>({
    entry: null,
    exit: null,
  });
  const tradeHighlightSeriesRefs = useRef<any[]>([]);
  const boxOverlayRef = useRef<HTMLDivElement | null>(null);
  const rangeSyncSourceRef = useRef<IChartApi | null>(null);
  const rangeSyncFrameRef = useRef<number | null>(null);
  const shouldAutoFocusTradeRef = useRef(true);

  const loadReplay = useCallback(async (requestedNr?: number) => {
    if (!id) return;
    setIsLoading(true);
    setLoadError("");
    try {
      const [replayPayload, infoPayload] = await Promise.all([
        getBacktestReplay(id),
        getBacktestInfo(id),
      ]);
      setReplay(replayPayload);
      setTrades(replayPayload.trades || []);
      setInfo(infoPayload);

      const requested = Number.isFinite(requestedNr)
        ? Number(requestedNr)
        : currentNrRef.current;
      const tradeNumbers = (replayPayload.trades || []).map((trade) => trade.nr);
      const fallbackNr = tradeNumbers[0] ?? 1;
      const resolvedNr = tradeNumbers.includes(requested) ? requested : fallbackNr;
      setCurrentNr(resolvedNr);
      if (resolvedNr !== requested) {
        window.history.replaceState(
          window.history.state,
          "",
          `/backtests/${id}/trade/${resolvedNr}`,
        );
      }
    } catch (err: unknown) {
      if (err instanceof ApiError && isMissingOHLCVDetail(err.detail)) {
        setLoadError(err.detail.message);
      } else {
        setLoadError(err instanceof Error ? err.message : "Could not load replay data");
      }
      setReplay(null);
      setTrades([]);
    } finally {
      setIsLoading(false);
    }
  }, [id, navigate]);

  useEffect(() => {
    const parsed = parseInt(nr || "1", 10);
    shouldAutoFocusTradeRef.current = true;
    loadReplay(Number.isNaN(parsed) ? undefined : parsed);
  }, [id, loadReplay]);

  useEffect(() => {
    const parsed = parseInt(nr || "1", 10);
    if (!Number.isNaN(parsed)) {
      if (parsed !== currentNrRef.current) {
        shouldAutoFocusTradeRef.current = true;
      }
      setCurrentNr(parsed);
    }
  }, [nr]);

  useEffect(() => {
    currentNrRef.current = currentNr;
  }, [currentNr]);

  useEffect(() => {
    const onFullscreenChange = () => {
      setIsFullscreen(document.fullscreenElement === viewportRef.current);
    };
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", onFullscreenChange);
  }, []);

  const tradeNumbers = useMemo(
    () => trades.map((trade) => trade.nr).sort((left, right) => left - right),
    [trades],
  );

  const currentTrade = useMemo(
    () => trades.find((trade) => trade.nr === currentNr) || null,
    [trades, currentNr],
  );

  const currentSymbol = currentTrade?.symbol || replay?.primary_symbol || "";
  const symbolTrades = useMemo(
    () => trades.filter((trade) => trade.symbol === currentSymbol),
    [trades, currentSymbol],
  );
  const rawSymbolOHLCV = useMemo(
    () => replay?.ohlcv_by_symbol?.[currentSymbol] || [],
    [replay, currentSymbol],
  );
  const symbolOHLCV = useMemo(() => sanitizeOHLCVBars(rawSymbolOHLCV), [rawSymbolOHLCV]);
  const symbolTimeSet = useMemo(() => new Set(symbolOHLCV.map((bar) => bar.time)), [symbolOHLCV]);
  const missingData = useMemo(
    () => replay?.missing_ohlcv?.[currentSymbol] || null,
    [replay, currentSymbol],
  );

  const symbolDrawings = useMemo(() => {
    return (replay?.drawings || []).filter((drawing) => {
      const drawingSymbol = (drawing as { symbol?: string }).symbol;
      if (!drawingSymbol) return true;
      return drawingSymbol.toUpperCase() === currentSymbol.toUpperCase();
    });
  }, [replay, currentSymbol]);

  const [visibleStart, visibleEnd] = useMemo(() => {
    const first = symbolOHLCV[0]?.time ?? 0;
    const last = symbolOHLCV[symbolOHLCV.length - 1]?.time ?? 0;
    return [first, last];
  }, [symbolOHLCV]);

  const sanitizedIndicators = useMemo(() => {
    return (replay?.indicators || [])
      .filter((indicator) => Array.isArray(indicator.data))
      .map((indicator) => ({
        ...indicator,
        data: sanitizeIndicatorPoints(indicator.data, symbolTimeSet),
      }))
      .filter((indicator) => indicator.data.length > 0);
  }, [replay, symbolTimeSet]);

  const overlayIndicators = useMemo(() => {
    return sanitizedIndicators.filter((indicator) => {
      return !indicatorIsPanel(indicator) && Array.isArray(indicator.data);
    });
  }, [sanitizedIndicators]);

  const panelGroups = useMemo<PanelGroup[]>(() => {
    const groups = new Map<string, PanelGroup>();
    sanitizedIndicators
      .filter((indicator) => indicatorIsPanel(indicator) && Array.isArray(indicator.data))
      .forEach((indicator) => {
        const key = inferPanelKey(indicator);
        if (!groups.has(key)) {
          groups.set(key, { key, label: inferPanelLabel(key), indicators: [] });
        }
        groups.get(key)!.indicators.push(indicator);
      });
    return Array.from(groups.values());
  }, [sanitizedIndicators]);

  const chartHeights = useMemo(() => {
    if (!panelGroups.length) {
      return {
        mainHeight: isFullscreen ? Math.max(480, Math.floor(window.innerHeight * 0.58)) : 460,
        panelHeight: 0,
      };
    }
    const totalHeight = isFullscreen
      ? Math.max(620, Math.floor(window.innerHeight * 0.82))
      : 640;
    const mainHeight = Math.max(360, Math.floor(totalHeight * 0.7));
    const remainingHeight = Math.max(totalHeight - mainHeight, panelGroups.length * 96);
    const panelHeight = Math.max(96, Math.floor(remainingHeight / panelGroups.length));
    return { mainHeight, panelHeight };
  }, [isFullscreen, panelGroups.length]);

  const riskMath = useMemo(() => {
    const candidate = currentTrade?.meta?.risk_math;
    return isTradeRiskMath(candidate) ? candidate : null;
  }, [currentTrade]);

  const riskMathEntries = useMemo(() => {
    if (!riskMath) {
      return [];
    }
    return Object.entries(riskMath)
      .filter(([, value]) => value !== undefined && value !== null)
      .sort(([left], [right]) => left.localeCompare(right));
  }, [riskMath]);

  const riskMathNarrative = useMemo(() => {
    if (!riskMath) {
      return "";
    }
    return buildRiskMathNarrative(riskMath);
  }, [riskMath]);

  const tradeContextEntries = useMemo(() => {
    const meta = currentTrade?.meta;
    if (!meta || typeof meta !== "object") {
      return [];
    }
    return Object.entries(meta as Record<string, unknown>)
      .filter(([key]) => key !== "risk_math")
      .filter(([, value]) => value !== undefined && value !== null)
      .sort(([left], [right]) => left.localeCompare(right));
  }, [currentTrade]);

  const setPanelContainerRef = useCallback(
    (key: string) => (node: HTMLDivElement | null) => {
      panelContainerRefs.current[key] = node;
    },
    [],
  );

  const goToTradeNumber = useCallback(
    (tradeNr: number) => {
      if (!id) return;
      shouldAutoFocusTradeRef.current = true;
      setCurrentNr(tradeNr);
      window.history.replaceState(
        window.history.state,
        "",
        `/backtests/${id}/trade/${tradeNr}`,
      );
    },
    [id],
  );

  const goToPreviousTrade = useCallback((): boolean => {
    const idx = tradeNumbers.indexOf(currentNr);
    if (idx <= 0) {
      return false;
    }
    goToTradeNumber(tradeNumbers[idx - 1]);
    return true;
  }, [tradeNumbers, currentNr, goToTradeNumber]);

  const goToNextTrade = useCallback((): boolean => {
    const idx = tradeNumbers.indexOf(currentNr);
    if (idx < 0 || idx >= tradeNumbers.length - 1) {
      return false;
    }
    goToTradeNumber(tradeNumbers[idx + 1]);
    return true;
  }, [tradeNumbers, currentNr, goToTradeNumber]);

  const toggleFullscreen = useCallback(async () => {
    const target = viewportRef.current;
    if (!target) return;

    if (document.fullscreenElement === target) {
      await document.exitFullscreen();
      return;
    }
    if (!document.fullscreenElement) {
      await target.requestFullscreen();
    }
  }, []);

  useEffect(() => {
    if (!isPlaying) return;
    const timer = window.setInterval(() => {
      const moved = goToNextTrade();
      if (!moved) {
        setIsPlaying(false);
      }
    }, 1300);
    return () => window.clearInterval(timer);
  }, [isPlaying, goToNextTrade]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable);
      if (typing) return;

      if (event.key === "ArrowLeft") {
        event.preventDefault();
        goToPreviousTrade();
        return;
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        goToNextTrade();
        return;
      }
      if (event.code === "Space") {
        event.preventDefault();
        setIsPlaying((value) => !value);
        return;
      }
      if (event.key === "Escape") {
        setIsPlaying(false);
        if (document.fullscreenElement === viewportRef.current) {
          document.exitFullscreen().catch(() => undefined);
        } else if (id) {
          navigate(`/backtests/${id}`);
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [goToPreviousTrade, goToNextTrade, id, navigate]);

  useEffect(() => {
    if (!downloadTaskId) return;
    if (["completed", "failed", "cancelled"].includes(downloadStatus)) return;

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

  useEffect(() => {
    if (downloadStatus !== "completed") return;
    setDownloadTaskId(null);
    setDownloadStatus("idle");
    loadReplay(currentNrRef.current);
  }, [downloadStatus, loadReplay]);

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

  useEffect(() => {
    chartRefs.current.forEach((chart) => chart.remove());
    chartRefs.current = [];
    mainChartRef.current = null;
    candleSeriesRef.current = null;
    priceLineRefs.current = { entry: null, exit: null };
    tradeHighlightSeriesRefs.current = [];
    if (boxOverlayRef.current) {
      boxOverlayRef.current.remove();
      boxOverlayRef.current = null;
    }

    if (!mainChartContainerRef.current || !symbolOHLCV.length) {
      return;
    }

    const container = mainChartContainerRef.current;
    container.style.position = "relative";
    const locale =
      typeof navigator.language === "string" && navigator.language !== "undefined"
        ? navigator.language
        : "en-US";

    const width = container.clientWidth || 1100;
    const { mainHeight, panelHeight } = chartHeights;

    const chart = createChart(container, {
      width,
      height: mainHeight,
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
    mainChartRef.current = chart;
    chartRefs.current.push(chart);

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#3fb950",
      downColor: "#f85149",
      wickUpColor: "#3fb950",
      wickDownColor: "#f85149",
      borderVisible: false,
    });
    candleSeriesRef.current = candleSeries;

    const candleData: CandlestickData[] = symbolOHLCV.map((bar) => ({
      time: bar.time as Time,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));
    candleSeries.setData(candleData);

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });
    volumeSeries.setData(
      symbolOHLCV.map((bar) => ({
        time: bar.time as Time,
        value: bar.volume || 0,
        color:
          bar.close >= bar.open ? "rgba(63,185,80,0.2)" : "rgba(248,81,73,0.2)",
      })),
    );

    overlayIndicators.forEach((indicator) => {
      const points = pointsInRange(indicator.data, visibleStart, visibleEnd);
      if (!points.length) return;

      const lineSeries = chart.addSeries(LineSeries, {
        color: indicator.style?.color || indicator.color || "#58a6ff",
        lineWidth: resolveLineWidth(indicator.style?.width ?? 1),
        lineStyle: resolveLineStyle(indicator.style?.lineStyle ?? indicator.lineStyle),
        title: indicator.name,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      lineSeries.setData(toLineSeriesData(points));
    });

    symbolDrawings.filter(isLineDrawing).forEach((drawing, index) => {
      const startTime = normalizeUnixTime(drawing.start_time);
      const endTime = normalizeUnixTime(drawing.end_time);
      const startValue = Number(
        typeof drawing.start_value === "number" ? drawing.start_value : drawing.val1,
      );
      const endValue = Number(
        typeof drawing.end_value === "number" ? drawing.end_value : drawing.val2,
      );
      if (
        startTime === null ||
        endTime === null ||
        !symbolTimeSet.has(startTime) ||
        !symbolTimeSet.has(endTime) ||
        !Number.isFinite(startValue) ||
        !Number.isFinite(endValue)
      ) {
        return;
      }

      const lineSeries = chart.addSeries(LineSeries, {
        color: drawing.color || "#f59e0b",
        lineWidth: resolveLineWidth(drawing.lineWidth ?? 2),
        lineStyle: resolveLineStyle(drawing.lineStyle),
        title: drawing.title || `Line ${index + 1}`,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      lineSeries.setData([
        { time: startTime as Time, value: startValue },
        { time: endTime as Time, value: endValue },
      ]);
    });

    symbolDrawings
      .filter(isLineWithBreaksDrawing)
      .forEach((drawing, index) => {
        const pointsByTime = new Map<number, { time: number; value?: number }>();
        (drawing.data || []).forEach((point) => {
          const time = normalizeUnixTime(point.time);
          if (time === null || !symbolTimeSet.has(time)) {
            return;
          }
          if (point.value === null || Number.isNaN(point.value)) {
            pointsByTime.set(time, { time });
            return;
          }
          const value = Number(point.value);
          if (Number.isFinite(value)) {
            pointsByTime.set(time, { time, value });
          } else {
            pointsByTime.set(time, { time });
          }
        });
        const linePoints = Array.from(pointsByTime.values()).sort(
          (left, right) => left.time - right.time,
        );
        if (!linePoints.length) {
          return;
        }
        const lineSeries = chart.addSeries(LineSeries, {
          color: drawing.color || "#f59e0b",
          lineWidth: resolveLineWidth(drawing.lineWidth ?? 2),
          lineStyle: resolveLineStyle(drawing.lineStyle),
          title: drawing.title || `LineBreak ${index + 1}`,
          lastValueVisible: false,
          priceLineVisible: false,
        });
        lineSeries.setData(
          linePoints.map((point) =>
            point.value === undefined
              ? { time: point.time as Time }
              : { time: point.time as Time, value: point.value },
          ),
        );
      });

    const boxOverlayRoot = document.createElement("div");
    boxOverlayRoot.style.position = "absolute";
    boxOverlayRoot.style.left = "0";
    boxOverlayRoot.style.top = "0";
    boxOverlayRoot.style.width = "100%";
    boxOverlayRoot.style.height = `${mainHeight}px`;
    boxOverlayRoot.style.pointerEvents = "none";
    container.appendChild(boxOverlayRoot);
    boxOverlayRef.current = boxOverlayRoot;

    const renderBoxes = () => {
      boxOverlayRoot.replaceChildren();
      symbolDrawings.filter(isBoxDrawing).forEach((drawing) => {
        const startTime = normalizeUnixTime(drawing.start_time);
        const endTime = normalizeUnixTime(drawing.end_time);
        const low = Number(drawing.low);
        const high = Number(drawing.high);
        if (
          startTime === null ||
          endTime === null ||
          !symbolTimeSet.has(startTime) ||
          !symbolTimeSet.has(endTime) ||
          !Number.isFinite(low) ||
          !Number.isFinite(high)
        ) {
          return;
        }
        const left = chart.timeScale().timeToCoordinate(startTime as Time);
        const right = chart.timeScale().timeToCoordinate(endTime as Time);
        const top = candleSeries.priceToCoordinate(high);
        const bottom = candleSeries.priceToCoordinate(low);
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
    chart.timeScale().subscribeVisibleTimeRangeChange(renderBoxes);

    const panelCharts: IChartApi[] = [];
    panelGroups.forEach((group, groupIndex) => {
      const panelContainer = panelContainerRefs.current[group.key];
      if (!panelContainer) return;

      const panelChart = createChart(panelContainer, {
        width,
        height: panelHeight,
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
        timeScale: {
          borderColor: "#30363d",
          timeVisible: groupIndex === panelGroups.length - 1,
        },
        rightPriceScale: { borderColor: "#30363d" },
      });
      panelCharts.push(panelChart);
      chartRefs.current.push(panelChart);

      group.indicators.forEach((indicator) => {
        const points = pointsInRange(indicator.data, visibleStart, visibleEnd);
        if (!points.length) return;

        const lineSeries = panelChart.addSeries(LineSeries, {
          color: indicator.style?.color || indicator.color || "#a78bfa",
          lineWidth: resolveLineWidth(indicator.style?.width ?? 1),
          lineStyle: resolveLineStyle(indicator.style?.lineStyle ?? indicator.lineStyle),
          title: indicator.name,
          lastValueVisible: false,
          priceLineVisible: false,
        });
        lineSeries.setData(toLineSeriesData(points));
      });
      panelChart.priceScale("right").applyOptions({
        scaleMargins: { top: 0.1, bottom: 0.1 },
      });
    });

    const allCharts = [chart, ...panelCharts];
    if (rangeSyncFrameRef.current !== null) {
      window.cancelAnimationFrame(rangeSyncFrameRef.current);
      rangeSyncFrameRef.current = null;
    }
    rangeSyncSourceRef.current = null;
    const syncSubscriptions: Array<{ chart: IChartApi; handler: (range: LogicalRange | null) => void }> = [];
    const scheduleSyncRelease = () => {
      if (rangeSyncFrameRef.current !== null) {
        window.cancelAnimationFrame(rangeSyncFrameRef.current);
      }
      rangeSyncFrameRef.current = window.requestAnimationFrame(() => {
        rangeSyncSourceRef.current = null;
        rangeSyncFrameRef.current = null;
      });
    };
    allCharts.forEach((sourceChart, sourceIdx) => {
      const handler = (range: LogicalRange | null) => {
        if (!range) return;
        const activeSource = rangeSyncSourceRef.current;
        if (activeSource && activeSource !== sourceChart) {
          return;
        }
        rangeSyncSourceRef.current = sourceChart;
        allCharts.forEach((targetChart, targetIdx) => {
          if (targetIdx !== sourceIdx) {
            targetChart.timeScale().setVisibleLogicalRange(range);
          }
        });
        scheduleSyncRelease();
      };
      sourceChart.timeScale().subscribeVisibleLogicalRangeChange(handler);
      syncSubscriptions.push({ chart: sourceChart, handler });
    });

    chart.timeScale().fitContent();
    const initialRange = chart.timeScale().getVisibleLogicalRange();
    if (initialRange) {
      panelCharts.forEach((panelChart) => {
        panelChart.timeScale().setVisibleLogicalRange(initialRange);
      });
    }

    const handleResize = () => {
      const nextWidth = container.clientWidth || width;
      chart.resize(nextWidth, mainHeight);
      panelCharts.forEach((panelChart) => panelChart.resize(nextWidth, panelHeight));
      boxOverlayRoot.style.height = `${mainHeight}px`;
      renderBoxes();
    };
    const resizeObserver = new ResizeObserver(handleResize);
    resizeObserver.observe(container);
    window.addEventListener("resize", handleResize);

    return () => {
      resizeObserver.disconnect();
      window.removeEventListener("resize", handleResize);
      chart.timeScale().unsubscribeVisibleTimeRangeChange(renderBoxes);
      syncSubscriptions.forEach(({ chart: targetChart, handler }) => {
        targetChart.timeScale().unsubscribeVisibleLogicalRangeChange(handler);
      });
      if (rangeSyncFrameRef.current !== null) {
        window.cancelAnimationFrame(rangeSyncFrameRef.current);
        rangeSyncFrameRef.current = null;
      }
      rangeSyncSourceRef.current = null;
      chartRefs.current.forEach((targetChart) => targetChart.remove());
      chartRefs.current = [];
      mainChartRef.current = null;
      candleSeriesRef.current = null;
      priceLineRefs.current = { entry: null, exit: null };
      tradeHighlightSeriesRefs.current = [];
      if (boxOverlayRef.current) {
        boxOverlayRef.current.remove();
        boxOverlayRef.current = null;
      }
    };
  }, [
    symbolOHLCV,
    overlayIndicators,
    panelGroups,
    symbolDrawings,
    visibleStart,
    visibleEnd,
    isFullscreen,
    chartHeights,
  ]);

  useEffect(() => {
    const chart = mainChartRef.current;
    const candleSeries = candleSeriesRef.current;
    if (chart && tradeHighlightSeriesRefs.current.length) {
      tradeHighlightSeriesRefs.current.forEach((series) => chart.removeSeries(series));
      tradeHighlightSeriesRefs.current = [];
    }

    if (!chart || !candleSeries || !currentTrade || !symbolOHLCV.length) {
      return;
    }

    const markers: Array<{
      time: Time;
      position: "aboveBar" | "belowBar" | "inBar";
      color: string;
      shape: "arrowUp" | "arrowDown" | "circle" | "square";
      text?: string;
    }> = [];

    symbolTrades.forEach((trade) => {
      const entryTime = normalizeUnixTime(trade.entry_time);
      if (entryTime === null || !symbolTimeSet.has(entryTime)) {
        return;
      }
      markers.push({
        time: entryTime as Time,
        position: trade.direction === "LONG" ? "belowBar" : "aboveBar",
        color: trade.nr === currentNr ? "#58a6ff" : "rgba(88,166,255,0.55)",
        shape: trade.direction === "LONG" ? "arrowUp" : "arrowDown",
        text: trade.nr === currentNr ? `Trade #${trade.nr}` : undefined,
      });
    });

    (currentTrade.exits || []).forEach((exit, index) => {
      const exitTime = normalizeUnixTime(exit.time);
      if (exitTime === null || !symbolTimeSet.has(exitTime)) {
        return;
      }
      const isFinal = index === (currentTrade.exits?.length || 0) - 1;
      markers.push({
        time: exitTime as Time,
        position: currentTrade.direction === "LONG" ? "aboveBar" : "belowBar",
        color: isFinal
          ? currentTrade.pnl >= 0
            ? "#3fb950"
            : "#f85149"
          : "#f59e0b",
        shape: "circle",
        text: isFinal ? "Exit" : "Partial",
      });
    });

    symbolDrawings.filter(isMarkerDrawing).forEach((drawing) => {
      const markerTime = normalizeUnixTime(drawing.time);
      if (markerTime === null || !symbolTimeSet.has(markerTime)) {
        return;
      }
      markers.push({
        time: markerTime as Time,
        position: normalizeMarkerPosition(drawing.position),
        color: drawing.color || "#f59e0b",
        shape: normalizeMarkerShape(drawing.shape),
        text: drawing.text,
      });
    });

    const sortedMarkers = markers.sort((left, right) => {
      const leftTime = Number(left.time);
      const rightTime = Number(right.time);
      if (leftTime === rightTime) return 0;
      return leftTime < rightTime ? -1 : 1;
    });

    createSeriesMarkers(
      candleSeries,
      sortedMarkers,
    );

    if (priceLineRefs.current.entry) {
      candleSeries.removePriceLine(priceLineRefs.current.entry);
      priceLineRefs.current.entry = null;
    }
    if (priceLineRefs.current.exit) {
      candleSeries.removePriceLine(priceLineRefs.current.exit);
      priceLineRefs.current.exit = null;
    }

    priceLineRefs.current.entry = candleSeries.createPriceLine({
      price: currentTrade.entry_price,
      color: "#58a6ff",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: "Entry",
    });
    priceLineRefs.current.exit = candleSeries.createPriceLine({
      price: currentTrade.exit_price,
      color: currentTrade.pnl >= 0 ? "#3fb950" : "#f85149",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: "Exit",
    });

    const entryTime = normalizeUnixTime(currentTrade.entry_time);
    const exitPoints = (currentTrade.exits || [])
      .map((exit) => {
        const exitTime = normalizeUnixTime(exit.time);
        return {
          time: exitTime,
          price: Number(exit.price),
        };
      })
      .filter(
        (point): point is { time: number; price: number } =>
          point.time !== null && symbolTimeSet.has(point.time) && Number.isFinite(point.price),
      )
      .sort((left, right) => left.time - right.time);

    if (entryTime !== null && symbolTimeSet.has(entryTime) && exitPoints.length) {
      const lastExitTime = exitPoints[exitPoints.length - 1].time;
      const holdCeiling = symbolOHLCV.reduce(
        (maxValue, bar) =>
          bar.time >= entryTime && bar.time <= lastExitTime
            ? Math.max(maxValue, bar.high)
            : maxValue,
        Number.NEGATIVE_INFINITY,
      );

      if (Number.isFinite(holdCeiling)) {
        const holdSeries = chart.addSeries(AreaSeries, {
          lineColor: "rgba(0,0,0,0)",
          topColor: "rgba(0, 150, 255, 0.1)",
          bottomColor: "rgba(0, 150, 255, 0.1)",
          lineWidth: 1,
          lastValueVisible: false,
          priceLineVisible: false,
          crosshairMarkerVisible: false,
        });
        holdSeries.setData(
          symbolOHLCV.map((bar) =>
            bar.time >= entryTime && bar.time <= lastExitTime
              ? { time: bar.time as Time, value: holdCeiling }
              : { time: bar.time as Time },
          ),
        );
        tradeHighlightSeriesRefs.current.push(holdSeries);
      }

      exitPoints.forEach((exitPoint, index) => {
        const connector = chart.addSeries(LineSeries, {
          color: "rgba(247, 211, 84, 0.72)",
          lineWidth: index === exitPoints.length - 1 ? 2 : 1,
          lineStyle: LineStyle.Dashed,
          lastValueVisible: false,
          priceLineVisible: false,
          crosshairMarkerVisible: false,
        });
        connector.setData([
          { time: entryTime as Time, value: currentTrade.entry_price },
          { time: exitPoint.time as Time, value: exitPoint.price },
        ]);
        tradeHighlightSeriesRefs.current.push(connector);
      });
    }

    const entryIndex = findBarIndexByTime(symbolOHLCV, currentTrade.entry_time);
    if (entryIndex >= 0 && shouldAutoFocusTradeRef.current) {
      shouldAutoFocusTradeRef.current = false;
      const barsBefore = 120;
      const barsAfter = 80;
      chart.timeScale().setVisibleLogicalRange({
        from: Math.max(entryIndex - barsBefore, 0),
        to: Math.min(entryIndex + barsAfter, symbolOHLCV.length - 1),
      });
    }
  }, [currentTrade, currentNr, symbolOHLCV, symbolTrades, symbolDrawings, symbolTimeSet]);

  if (isLoading) {
    return (
      <div style={{ padding: "20px", color: "var(--color-text-muted)" }}>
        Loading replay data...
      </div>
    );
  }

  if (loadError) {
    return (
      <div style={{ padding: "20px", color: "var(--color-red)" }}>
        Error: {loadError}
      </div>
    );
  }

  return (
    <div
      ref={viewportRef}
      style={{
        padding: "24px",
        background: "var(--color-bg)",
        minHeight: "100%",
      }}
    >
      <div
        style={{
          background:
            "radial-gradient(circle at top right, rgba(88,166,255,0.12), transparent 38%), var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "12px",
          padding: "14px 16px",
          marginBottom: "16px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "12px",
          flexWrap: "wrap",
        }}
      >
        <button
          onClick={() => navigate(`/backtests/${id}`)}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "8px",
            background: "none",
            border: "none",
            color: "#58a6ff",
            cursor: "pointer",
            fontSize: "0.84rem",
          }}
        >
          <ArrowLeft size={16} />
          Back to Report
        </button>

        <div style={{ fontSize: "0.82rem", color: "var(--color-text-muted)" }}>
          Trade {currentNr} / {tradeNumbers.length} • {currentSymbol || "—"} •{" "}
          {replay?.timeframe || "—"}
        </div>

        <button
          onClick={toggleFullscreen}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
            border: "1px solid var(--color-border)",
            borderRadius: "8px",
            background: "var(--color-surface2)",
            color: "var(--color-text)",
            padding: "6px 10px",
            cursor: "pointer",
            fontSize: "0.78rem",
          }}
        >
          {isFullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          {isFullscreen ? "Exit Fullscreen" : "Fullscreen"}
        </button>
      </div>

      {currentTrade ? (
        <div
          style={{
            display: "flex",
            gap: "14px",
            flexWrap: "wrap",
            marginBottom: "16px",
          }}
        >
          <div
            style={{
              flex: "2 1 560px",
              background: "var(--color-surface)",
              border: "1px solid var(--color-border)",
              borderRadius: "10px",
              padding: "16px",
            }}
          >
            <div style={{ display: "flex", flexWrap: "wrap", gap: "10px" }}>
              {[
                { label: "Direction", value: currentTrade.direction },
                { label: "Symbol", value: currentTrade.symbol },
                {
                  label: "Entry",
                  value: `$${currentTrade.entry_price?.toFixed(2)} @ ${(currentTrade.entry_time_iso || "").slice(0, 16)}`,
                },
                {
                  label: "Exit",
                  value: `$${currentTrade.exit_price?.toFixed(2)} @ ${(currentTrade.exit_time_iso || "").slice(0, 16)}`,
                },
                {
                  label: "P&L",
                  value: `$${currentTrade.pnl?.toLocaleString("en-US", { maximumFractionDigits: 2 })} (${currentTrade.pnl_pct != null ? `${currentTrade.pnl_pct.toFixed(2)}%` : "—"})`,
                },
              ].map((row) => (
                <div
                  key={row.label}
                  style={{
                    background: "var(--color-surface2)",
                    border: "1px solid var(--color-border)",
                    borderRadius: "8px",
                    padding: "8px 12px",
                    minWidth: "140px",
                  }}
                >
                  <div
                    style={{
                      color: "var(--color-text-muted)",
                      fontSize: "0.7rem",
                      textTransform: "uppercase",
                      marginBottom: "3px",
                    }}
                  >
                    {row.label}
                  </div>
                  <div
                    style={{
                      fontWeight: 700,
                      color:
                        row.label === "Direction"
                          ? currentTrade.direction === "LONG"
                            ? "#3fb950"
                            : "#f85149"
                          : row.label === "P&L"
                            ? currentTrade.pnl >= 0
                              ? "#3fb950"
                              : "#f85149"
                            : "var(--color-text)",
                    }}
                  >
                    {row.value}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {riskMath || tradeContextEntries.length > 0 ? (
            <div
              style={{
                flex: "1 1 320px",
                background: "var(--color-surface)",
                border: "1px solid var(--color-border)",
                borderRadius: "10px",
                padding: "16px",
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
              {riskMath ? (
                <div
                  data-testid="risk-math-box"
                  style={{
                    marginBottom: tradeContextEntries.length ? "12px" : "0",
                    background: "rgba(0, 150, 255, 0.1)",
                    border: "1px solid rgba(88,166,255,0.38)",
                    borderRadius: "8px",
                    padding: "10px",
                  }}
                >
                  <div
                    style={{
                      color: "#79c0ff",
                      fontSize: "0.72rem",
                      textTransform: "uppercase",
                      letterSpacing: "0.08em",
                      marginBottom: "8px",
                      fontWeight: 700,
                    }}
                  >
                    Risk Math
                  </div>
                  {riskMathNarrative ? (
                    <div style={{ fontSize: "0.82rem", fontWeight: 600, marginBottom: "8px" }}>
                      {riskMathNarrative}
                    </div>
                  ) : null}
                  <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                    {riskMathEntries.map(([key, value]) => (
                      <div
                        key={key}
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          gap: "10px",
                          borderBottom: "1px solid rgba(88,166,255,0.2)",
                          paddingBottom: "6px",
                        }}
                      >
                        <div
                          style={{
                            color: "var(--color-text-muted)",
                            fontSize: "0.77rem",
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
                            fontSize: "0.8rem",
                          }}
                        >
                          {formatTradeMetaValue(value)}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}

              {tradeContextEntries.length > 0 ? (
                <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                  {tradeContextEntries.map(([key, value]) => (
                    <div
                      key={key}
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        gap: "10px",
                        borderBottom: "1px solid var(--color-border)",
                        paddingBottom: "8px",
                      }}
                    >
                      <div
                        style={{
                          color: "var(--color-text-muted)",
                          fontSize: "0.8rem",
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
                          fontSize: "0.82rem",
                        }}
                      >
                        {formatTradeMetaValue(value)}
                      </div>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      <div
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "10px",
          padding: "12px",
          overflow: "hidden",
        }}
      >
        {symbolOHLCV.length ? (
          <>
            <div
              ref={mainChartContainerRef}
              data-testid="trade-chart-canvas"
              style={{
                width: "100%",
                height: `${chartHeights.mainHeight}px`,
                borderRadius: "8px",
                overflow: "hidden",
              }}
            />

            {panelGroups.map((group) => (
              <div key={group.key} style={{ marginTop: "8px" }}>
                <div
                  style={{
                    fontSize: "0.68rem",
                    color: "var(--color-text-muted)",
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    marginBottom: "4px",
                    paddingLeft: "4px",
                  }}
                >
                  {group.label}
                </div>
                <div
                  ref={setPanelContainerRef(group.key)}
                  style={{
                    width: "100%",
                    height: `${chartHeights.panelHeight}px`,
                    border: "1px solid var(--color-border)",
                    borderRadius: "8px",
                    overflow: "hidden",
                  }}
                />
              </div>
            ))}
          </>
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
                width: "min(700px, 100%)",
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
                  onClick={() => loadReplay(currentNrRef.current)}
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
                  Reload replay
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
              minHeight: "460px",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--color-text-muted)",
            }}
          >
            No OHLCV data available for this symbol
          </div>
        )}
      </div>

      {tradeNumbers.length ? (
        <div
          style={{
            position: "fixed",
            left: "50%",
            bottom: "16px",
            transform: "translateX(-50%)",
            zIndex: 40,
            display: "flex",
            alignItems: "center",
            gap: "8px",
            padding: "8px",
            borderRadius: "999px",
            border: "1px solid rgba(88,166,255,0.22)",
            background: "rgba(13,17,23,0.82)",
            backdropFilter: "blur(10px)",
            boxShadow: "0 12px 40px rgba(0,0,0,0.35)",
          }}
        >
          <button
            onClick={goToPreviousTrade}
            disabled={tradeNumbers.indexOf(currentNr) <= 0}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "6px",
              border: "1px solid var(--color-border)",
              borderRadius: "999px",
              background: "var(--color-surface2)",
              color: "var(--color-text)",
              padding: "8px 12px",
              cursor: "pointer",
              fontSize: "0.78rem",
            }}
          >
            <SkipBack size={14} />
            [|&lt;] Previous Trade
          </button>

          <button
            onClick={() => setIsPlaying((value) => !value)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "6px",
              border: "1px solid rgba(88,166,255,0.38)",
              borderRadius: "999px",
              background: "rgba(88,166,255,0.14)",
              color: "#79c0ff",
              padding: "8px 12px",
              cursor: "pointer",
              fontSize: "0.78rem",
              fontWeight: 700,
            }}
          >
            {isPlaying ? <Pause size={14} /> : <Play size={14} />}
            [{isPlaying ? "Pause" : "Play"}]
          </button>

          <button
            onClick={goToNextTrade}
            disabled={tradeNumbers.indexOf(currentNr) >= tradeNumbers.length - 1}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "6px",
              border: "1px solid var(--color-border)",
              borderRadius: "999px",
              background: "var(--color-surface2)",
              color: "var(--color-text)",
              padding: "8px 12px",
              cursor: "pointer",
              fontSize: "0.78rem",
            }}
          >
            [&gt;|] Next Trade
            <SkipForward size={14} />
          </button>
        </div>
      ) : null}
    </div>
  );
}
