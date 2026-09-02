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
  IndicatorPoint,
  IndicatorSeriesView,
  MissingOHLCVDetail,
  OHLCVBar,
  Trade,
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
import { TradingViewChart } from "../components/TradingViewChart";

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
  if (value == null || typeof value === "number") {
    return String(value);
  }
  return JSON.stringify(value);
}

/** Renders "P&L $x (y%)" without nested template literals. */
/** Sign/label-aware color for the trade summary rows. */
function summaryRowColor(label: string, trade: Trade): string {
  if (label === "Direction") {
    return trade.direction === "LONG" ? "#3fb950" : "#f85149";
  }
  if (label === "P&L") {
    return trade.pnl >= 0 ? "#3fb950" : "#f85149";
  }
  return "var(--color-text)";
}

function formatTradePnlSummary(trade: Trade): string {
  const amount =
    trade.pnl?.toLocaleString("en-US", { maximumFractionDigits: 2 }) ?? "—";
  const percent = trade.pnl_pct != null ? `${trade.pnl_pct.toFixed(2)}%` : "—";
  return `$${amount} (${percent})`;
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
    parts.push(
      `Size: ${size.toLocaleString("en-US", { maximumFractionDigits: 4 })} units`,
    );
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
  return Array.from(barsByTime.values()).sort(
    (left, right) => left.time - right.time,
  );
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
  return Array.from(pointsByTime.values()).sort(
    (left, right) => left.time - right.time,
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

function indicatorIsPanel(indicator: IndicatorSeriesView): boolean {
  const type = String(indicator.type || "").toLowerCase();
  const pane = String(indicator.pane || "").toLowerCase();
  const name = String(indicator.name || "").toLowerCase();
  const panelNames = [
    "adx",
    "atr",
    "macd",
    "momentum",
    "osc",
    "rsi",
    "stoch",
    "volume",
    "vwap deviation",
  ];
  return (
    type === "panel" ||
    pane === "panel" ||
    pane === "separate" ||
    type.startsWith("panel-") ||
    panelNames.some((token) => name === token || name.includes(token)) ||
    /(^|[\s_-])([+-]?di|di[+-]?)([\s_-]|$)/.test(name) ||
    /^(plusdi|minusdi|diplus|diminus|pdi|mdi)$/.test(name)
  );
}

interface MissingOhlcvPanelProps {
  missing: BacktestReplayResponse["missing_ohlcv"][string];
  downloadStatus: string;
  downloadTaskId: string | null;
  downloadError: string;
  onDownload: () => void;
  onReload: () => void;
}

function MissingOhlcvPanel({
  missing,
  downloadStatus,
  downloadTaskId,
  downloadError,
  onDownload,
  onReload,
}: Readonly<MissingOhlcvPanelProps>) {
  const alternatives = missing.alternative_sources?.length
    ? ` (alternatives: ${missing.alternative_sources.join(", ")})`
    : "";
  return (
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
          Missing OHLCV data for {missing.symbol} {missing.timeframe}
        </div>
        <div style={{ color: "var(--color-text-muted)", fontSize: "0.9rem" }}>
          Source suggestion: <strong>{missing.suggested_source}</strong>
          {alternatives}
        </div>

        <div
          style={{
            display: "flex",
            gap: "10px",
            marginTop: "16px",
            flexWrap: "wrap",
          }}
        >
          <button
            type="button"
            data-testid="missing-data-download-btn"
            onClick={onDownload}
            disabled={
              downloadStatus === "starting" || downloadStatus === "running"
            }
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
            {sourceButtonLabel(missing.suggested_source)}
          </button>

          <button
            type="button"
            onClick={onReload}
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

        <div
          style={{
            marginTop: "12px",
            fontSize: "0.82rem",
            color: "var(--color-text-muted)",
          }}
        >
          {downloadTaskId
            ? `Download task ${downloadTaskId} status: ${downloadStatus}`
            : "No automatic download is started. Use the button above to trigger a download."}
        </div>
        {downloadError ? (
          <div
            style={{ marginTop: "8px", color: "#f85149", fontSize: "0.82rem" }}
          >
            {downloadError}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** Result of resolving a requested trade number against loaded trades. */
type ResolvedTradeNr = { nr: number; changed: boolean };

/** Clamp a requested trade number to one that exists in the replay payload. */
function resolveRequestedTradeNr(
  trades: Trade[],
  requestedNr: number | undefined,
  fallback: number,
): ResolvedTradeNr {
  const requested = Number.isFinite(requestedNr)
    ? Number(requestedNr)
    : fallback;
  const tradeNumbers = trades.map((trade) => trade.nr);
  const resolvedNr = tradeNumbers.includes(requested)
    ? requested
    : (tradeNumbers[0] ?? 1);
  return { nr: resolvedNr, changed: resolvedNr !== requested };
}

/** Error text for a failed replay load, with the missing-OHLCV detail. */
function describeReplayLoadError(err: unknown): string {
  if (err instanceof ApiError && isMissingOHLCVDetail(err.detail)) {
    return err.detail.message;
  }
  return err instanceof Error ? err.message : "Could not load replay data";
}

export function TradeChartPage() {
  const { id, nr } = useParams<{ id: string; nr: string }>();
  const navigate = useNavigate();

  const [info, setInfo] = useState<BacktestInfo | null>(null);
  const [replay, setReplay] = useState<BacktestReplayResponse | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [currentNr, setCurrentNr] = useState(Number.parseInt(nr || "1", 10));
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [isPlaying, setIsPlaying] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const [downloadTaskId, setDownloadTaskId] = useState<string | null>(null);
  const [downloadStatus, setDownloadStatus] = useState<string>("idle");
  const [downloadError, setDownloadError] = useState<string>("");

  const currentNrRef = useRef(currentNr);
  const viewportRef = useRef<HTMLDivElement>(null);
  const shouldAutoFocusTradeRef = useRef(true);

  const loadReplay = useCallback(
    async (requestedNr?: number) => {
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
        const { nr: resolvedNr } = resolveRequestedTradeNr(
          replayPayload.trades || [],
          requestedNr,
          currentNrRef.current,
        );
        setCurrentNr(resolvedNr);
        if (resolvedNr !== requestedNr) {
          window.history.replaceState(
            window.history.state,
            "",
            `/backtests/${id}/trade/${resolvedNr}`,
          );
        }
      } catch (err: unknown) {
        setLoadError(describeReplayLoadError(err));
        setReplay(null);
        setTrades([]);
      } finally {
        setIsLoading(false);
      }
    },
    [id],
  );

  useEffect(() => {
    const parsed = Number.parseInt(nr || "1", 10);
    shouldAutoFocusTradeRef.current = true;
    loadReplay(Number.isNaN(parsed) ? undefined : parsed);
  }, [id, nr, loadReplay]);

  useEffect(() => {
    const parsed = Number.parseInt(nr || "1", 10);
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
    return () =>
      document.removeEventListener("fullscreenchange", onFullscreenChange);
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

  const symbolOHLCV = useMemo(
    () => sanitizeOHLCVBars(rawSymbolOHLCV),
    [rawSymbolOHLCV],
  );
  const symbolTimeSet = useMemo(
    () => new Set(symbolOHLCV.map((bar) => bar.time)),
    [symbolOHLCV],
  );

  const missingData = useMemo(
    () => replay?.missing_ohlcv?.[currentSymbol] || null,
    [replay, currentSymbol],
  );

  const sanitizedIndicators = useMemo(() => {
    return (replay?.indicators || [])
      .filter((indicator) => Array.isArray(indicator.data))
      .map((indicator) => ({
        ...indicator,
        data: sanitizeIndicatorPoints(indicator.data, symbolTimeSet),
      }))
      .filter((indicator) => indicator.data.length > 0);
  }, [replay, symbolTimeSet]);

  const chartIndicators = useMemo(
    () =>
      sanitizedIndicators.map((indicator) => ({
        name: indicator.name,
        data: indicator.data.map((point) => ({
          time: point.time,
          value: point.value,
        })),
        color: indicator.style?.color || indicator.color,
        panel: indicatorIsPanel(indicator) ? "separate" : "main",
      })),
    [sanitizedIndicators],
  );

  const hasSeparateIndicators = useMemo(
    () => chartIndicators.some((indicator) => indicator.panel === "separate"),
    [chartIndicators],
  );

  const chartVolumeData = useMemo(
    () =>
      symbolOHLCV.map((bar) => ({
        time: bar.time,
        value: bar.volume || 0,
        color:
          bar.close >= bar.open
            ? "rgba(63,185,80,0.22)"
            : "rgba(248,81,73,0.22)",
      })),
    [symbolOHLCV],
  );

  const chartTrades = useMemo(
    () =>
      symbolTrades.map((trade) => ({
        id: trade.nr,
        nr: trade.nr,
        symbol: trade.symbol,
        direction: trade.direction,
        entry_time: trade.entry_time,
        entry_price: trade.entry_price,
        quantity: trade.quantity,
        position_size: trade.position_size,
        exit_reason: trade.exit_reason,
        exit_time: trade.exit_time,
        exit_price: trade.exit_price,
        exits: (trade.exits || []).map((exit) => ({
          time: exit.time,
          price: exit.price,
          pnl: exit.pnl,
          reason: exit.reason,
        })),
        pnl: trade.pnl,
      })),
    [symbolTrades],
  );

  const chartFocusTime = useMemo(() => {
    if (!currentTrade || !shouldAutoFocusTradeRef.current) {
      return null;
    }
    return currentTrade.entry_time;
  }, [currentTrade]);

  useEffect(() => {
    if (!currentTrade || !shouldAutoFocusTradeRef.current) {
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      shouldAutoFocusTradeRef.current = false;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [currentTrade]);

  const chartHeight = useMemo(() => {
    if (isFullscreen) {
      return Math.max(560, Math.floor(window.innerHeight * 0.74));
    }
    return hasSeparateIndicators ? 640 : 540;
  }, [isFullscreen, hasSeparateIndicators]);

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
            err instanceof Error
              ? err.message
              : "Could not read download task status",
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
    const source =
      request?.source || missingData.suggested_source || "dukascopy";
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
      setDownloadError(
        err instanceof Error ? err.message : "Could not start download",
      );
    }
  };

  let chartBody: React.ReactElement;
  if (symbolOHLCV.length > 0) {
    chartBody = (
      <TradingViewChart
        ohlcData={symbolOHLCV}
        volumeData={chartVolumeData}
        trades={chartTrades}
        indicators={chartIndicators}
        height={chartHeight}
        dataTestId="trade-chart-canvas"
        activeTradeId={currentTrade?.nr ?? null}
        focusTradeTime={chartFocusTime}
        showToolbar
        isFullscreen={isFullscreen}
        onToggleFullscreen={toggleFullscreen}
      />
    );
  } else if (missingData) {
    chartBody = (
      <MissingOhlcvPanel
        missing={missingData}
        downloadStatus={downloadStatus}
        downloadTaskId={downloadTaskId}
        downloadError={downloadError}
        onDownload={triggerMissingDataDownload}
        onReload={() => loadReplay(currentNrRef.current)}
      />
    );
  } else {
    chartBody = (
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
    );
  }

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
          type="button"
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
          type="button"
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
            alignItems: "flex-start",
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
                  value: formatTradePnlSummary(currentTrade),
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
                      color: summaryRowColor(row.label, currentTrade),
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
                maxHeight: "420px",
                overflowY: "auto",
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
                    <div
                      style={{
                        fontSize: "0.82rem",
                        fontWeight: 600,
                        marginBottom: "8px",
                      }}
                    >
                      {riskMathNarrative}
                    </div>
                  ) : null}
                  <div
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: "6px",
                    }}
                  >
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
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: "8px",
                  }}
                >
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
        {chartBody}

      </div>

      {tradeNumbers.length ? (
        <div
          style={{
            justifyContent: "center",
            marginTop: "12px",
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
            type="button"
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
            type="button"
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
            {isPlaying ? <Pause size={14} /> : <Play size={14} />}[
            {isPlaying ? "Pause" : "Play"}]
          </button>

          <button
            type="button"
            onClick={goToNextTrade}
            disabled={
              tradeNumbers.indexOf(currentNr) >= tradeNumbers.length - 1
            }
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
