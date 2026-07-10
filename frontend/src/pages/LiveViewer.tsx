import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Radio } from "lucide-react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  CrosshairMode,
  createSeriesMarkers,
  HistogramSeries,
  LineStyle,
} from "lightweight-charts";
import type {
  IChartApi,
  IPriceLine,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  Time,
  UTCTimestamp,
} from "lightweight-charts";

import { getLiveAccounts, getLiveDeployments, getLiveTrades } from "../api/client";
import { useLivePositions, useLiveQuotes, useLiveSignalLog } from "../api/queries";
import {
  useLiveExecutionsStream,
  useLivePositionsStream,
  useLiveSignalsStream,
} from "../hooks/liveStreams";
import type {
  LiveAccount,
  LiveDeploymentConfig,
  LivePosition,
  LiveQuoteBar,
  LiveSignalLog,
  LiveTradeSlippage,
} from "../api/types";
import { TradeTable, type TradeTableRow } from "../components/TradeTable";

type AccountHeaderStats = {
  balance: number;
  equity: number;
  totalPnl: number;
  dailyPnl: number;
  dailyPnlPct: number;
};

type ViewerTrade = {
  id: string;
  symbol: string;
  side: "BUY" | "SELL";
  size: number;
  entry: number;
  exit: number | null;
  pnl: number | null;
  exitReason: string;
  time: string;
  executionTimeUnix: number;
  signalTimeUnix: number;
};

type TradeMarker = {
  id: string;
  time: Time;
  position: "aboveBar" | "belowBar";
  color: string;
  shape: "arrowUp" | "arrowDown";
  text: string;
};

const LIVE_VIEWER_FALLBACK_POLL_MS = 30_000;
const DEFAULT_BARS_LIMIT = 500;

function resolveChartLocale(): string {
  if (typeof navigator === "undefined") return "en-US";
  const language = String(navigator.language || "").trim();
  return language && language !== "undefined" ? language : "en-US";
}

export function LiveViewerPage() {
  const { accountId: rawAccountId } = useParams<{ accountId: string }>();
  const queryClient = useQueryClient();
  const accountId = decodeURIComponent(rawAccountId ?? "");
  const positionsStream = useLivePositionsStream(accountId);
  const signalsStream = useLiveSignalsStream(accountId);
  const executionsStream = useLiveExecutionsStream(accountId);

  const [account, setAccount] = useState<LiveAccount | null>(null);
  const [deployments, setDeployments] = useState<LiveDeploymentConfig[]>([]);
  const [fills, setFills] = useState<LiveTradeSlippage[]>([]);
  const [selectedSymbol, setSelectedSymbol] = useState("");
  const [loadingHeader, setLoadingHeader] = useState(true);
  const [headerError, setHeaderError] = useState("");

  useEffect(() => {
    if (!accountId) {
      setLoadingHeader(false);
      setHeaderError("Missing account id");
      return;
    }

    let ignore = false;
    const load = async () => {
      try {
        const [accountsPayload, deploymentsPayload, tradesPayload] = await Promise.all([
          getLiveAccounts(),
          getLiveDeployments(),
          getLiveTrades(accountId),
        ]);
        if (ignore) return;

        const foundAccount = accountsPayload.find((entry) => entry.id === accountId) ?? null;
        const accountDeployments = deploymentsPayload.filter(
          (deployment) => deployment.account_id === accountId,
        );
        const sortedFills = sortLiveTrades(tradesPayload.trades);
        const symbols = uniqueSymbols(accountDeployments, sortedFills, [], []);

        setAccount(foundAccount);
        setDeployments(accountDeployments);
        setFills(sortedFills);
        setSelectedSymbol((current) => {
          if (current && symbols.includes(current)) return current;
          return symbols[0] ?? "";
        });
        setHeaderError("");
      } catch (issue: unknown) {
        if (ignore) return;
        const message =
          issue instanceof Error ? issue.message : "Failed to load live viewer context";
        setHeaderError(message);
      } finally {
        if (!ignore) {
          setLoadingHeader(false);
        }
      }
    };

    void load();
    return () => {
      ignore = true;
    };
  }, [accountId]);

  useEffect(() => {
    if (!accountId || !positionsStream.data) {
      return;
    }
    queryClient.setQueryData(["live-positions", accountId], positionsStream.data);
  }, [accountId, positionsStream.data, queryClient]);

  useEffect(() => {
    if (!accountId || !signalsStream.data) {
      return;
    }
    queryClient.setQueryData(["live-signal-log", accountId, 200], signalsStream.data);
  }, [accountId, queryClient, signalsStream.data]);

  useEffect(() => {
    if (!executionsStream.data) {
      return;
    }
    setFills(sortLiveTrades(executionsStream.data.trades));
  }, [executionsStream.data]);

  useEffect(() => {
    if (!accountId || executionsStream.isConnected) {
      return;
    }
    let cancelled = false;
    const refreshTrades = async () => {
      try {
        const payload = await getLiveTrades(accountId);
        if (cancelled) {
          return;
        }
        setFills(sortLiveTrades(payload.trades));
      } catch {
      }
    };
    void refreshTrades();
    const intervalId = window.setInterval(() => {
      void refreshTrades();
    }, LIVE_VIEWER_FALLBACK_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [accountId, executionsStream.isConnected]);

  const positionsFallbackPolling = Boolean(accountId) && !positionsStream.isConnected;
  const signalsFallbackPolling = Boolean(accountId) && !signalsStream.isConnected;

  const {
    data: quotePayload,
    isFetching: quoteFetching,
    error: quoteError,
  } = useLiveQuotes(selectedSymbol, {
    enabled: Boolean(selectedSymbol),
    retry: false,
  });
  const {
    data: positionsPayload,
    isFetching: positionsFetching,
  } = useLivePositions(accountId, {
    enabled: Boolean(accountId),
    retry: false,
    refetchInterval: positionsFallbackPolling ? LIVE_VIEWER_FALLBACK_POLL_MS : false,
    staleTime: positionsFallbackPolling ? LIVE_VIEWER_FALLBACK_POLL_MS : Number.POSITIVE_INFINITY,
  });
  const {
    data: signalsPayload,
    isFetching: signalsFetching,
  } = useLiveSignalLog(accountId, 200, {
    enabled: Boolean(accountId),
    retry: false,
    refetchInterval: signalsFallbackPolling ? LIVE_VIEWER_FALLBACK_POLL_MS : false,
    staleTime: signalsFallbackPolling ? LIVE_VIEWER_FALLBACK_POLL_MS : Number.POSITIVE_INFINITY,
  });

  const positions = positionsPayload?.positions ?? [];
  const signals = signalsPayload?.signals ?? [];
  const trades = useMemo(() => toTradeHistoryRows(fills), [fills]);
  const symbols = useMemo(
    () => uniqueSymbols(deployments, fills, positions, signals),
    [deployments, fills, positions, signals],
  );

  useEffect(() => {
    setSelectedSymbol((current) => {
      const normalizedCurrent = current.trim().toUpperCase();
      if (normalizedCurrent && symbols.includes(normalizedCurrent)) {
        return normalizedCurrent;
      }
      return symbols[0] ?? "";
    });
  }, [symbols]);

  const chartBars = useMemo(() => quotePayload?.bars ?? [], [quotePayload?.bars]);
  const quoteSymbol = quotePayload?.symbol ?? "";
  const fallbackSignalSymbol = useMemo(
    () => signals.find((signal) => signal.symbol.trim())?.symbol.trim().toUpperCase() ?? "",
    [signals],
  );
  const effectiveSymbol = selectedSymbol || quoteSymbol || fallbackSignalSymbol;
  const selectedSignals = useMemo(
    () =>
      signals
        .filter((signal) => signal.symbol.toUpperCase() === effectiveSymbol.toUpperCase())
        .slice(0, 60),
    [signals, effectiveSymbol],
  );
  const selectedTrades = useMemo(
    () => trades.filter((trade) => trade.symbol.toUpperCase() === effectiveSymbol.toUpperCase()),
    [trades, effectiveSymbol],
  );
  const selectedTradeRows = useMemo<TradeTableRow[]>(
    () =>
      selectedTrades.map((trade) => ({
        id: trade.id,
        time: trade.time,
        symbol: trade.symbol,
        side: trade.side,
        size: trade.size,
        entryPrice: trade.entry,
        exitPrice: trade.exit,
        pnl: trade.pnl,
        pnlPct: null,
        exitReason: trade.exitReason,
        duration: null,
      })),
    [selectedTrades],
  );

  const headerStats = useMemo<AccountHeaderStats>(() => {
    const currentBalance = account?.balance ?? 0;
    const currentEquity =
      positions.length > 0
        ? currentBalance + positions.reduce((sum, item) => sum + asFinite(item.unrealized_pnl, 0), 0)
        : currentBalance;
    const realizedPnl = trades.reduce((sum, row) => sum + asFinite(row.pnl, 0), 0);
    const unrealizedPnl = positions.reduce((sum, row) => sum + asFinite(row.unrealized_pnl, 0), 0);
    const totalPnl = realizedPnl + unrealizedPnl;

    const now = Date.now();
    const oneDayAgo = now - 24 * 60 * 60 * 1000;
    const dailyPnl = trades
      .filter((trade) => {
        const ts = Date.parse(trade.time);
        return Number.isFinite(ts) && ts >= oneDayAgo;
      })
      .reduce((sum, trade) => sum + asFinite(trade.pnl, 0), 0);
    const base = Math.max(1, currentBalance - dailyPnl);
    const dailyPnlPct = (dailyPnl / base) * 100;

    return {
      balance: currentBalance,
      equity: currentEquity,
      totalPnl,
      dailyPnl,
      dailyPnlPct,
    };
  }, [account, positions, trades]);

  if (loadingHeader) {
    return (
      <div style={{ padding: "24px", color: "var(--color-text-muted)" }}>
        Loading live viewer...
      </div>
    );
  }

  if (headerError) {
    return (
      <div style={{ padding: "24px", color: "var(--color-red)" }}>
        Error: {headerError}
      </div>
    );
  }

  const accountName = account?.name?.trim() || accountId;
  const accountBroker = account?.broker ?? "unknown";
  const isRealtimeConnected =
    positionsStream.isConnected && signalsStream.isConnected && executionsStream.isConnected;

  return (
    <div style={{ padding: "20px 24px 26px" }}>
      <section
        style={{
          borderRadius: "16px",
          border: "1px solid rgba(56, 189, 248, 0.24)",
          background:
            "radial-gradient(circle at 16% 20%, rgba(56, 189, 248, 0.17), transparent 46%), linear-gradient(145deg, rgba(13, 18, 28, 0.98), rgba(19, 28, 44, 0.94))",
          padding: "18px",
          marginBottom: "14px",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            gap: "14px",
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <Link
              to={"/live/" + encodeURIComponent(accountId)}
              style={iconButtonStyle}
              title="Back to account details"
            >
              <ArrowLeft size={15} />
            </Link>
            <div>
              <h1 style={{ margin: 0, fontSize: "1.35rem", fontWeight: 700 }}>
                {accountName}
              </h1>
              <p style={{ margin: "3px 0 0", color: "var(--color-text-muted)", fontSize: "0.84rem" }}>
                {accountBroker.toUpperCase()} · Live Viewer · Realtime via WebSocket{" "}
                {isRealtimeConnected ? "· connected" : "· fallback polling"}
              </p>
            </div>
          </div>
          <div style={{ display: "inline-flex", alignItems: "center", gap: "8px", color: "#7dd3fc" }}>
            <Radio size={14} />
            <span style={{ fontSize: "0.76rem", letterSpacing: "0.06em", textTransform: "uppercase" }}>
              LIVE
            </span>
          </div>
        </div>
        <div
          style={{
            marginTop: "14px",
            display: "grid",
            gridTemplateColumns: "repeat(5, minmax(0, 1fr))",
            gap: "8px",
          }}
        >
          <HeaderStatCard label="Balance" value={formatUsd(headerStats.balance)} />
          <HeaderStatCard label="Equity" value={formatUsd(headerStats.equity)} />
          <HeaderStatCard
            label="Daily P&L"
            value={`${formatSignedUsd(headerStats.dailyPnl)} (${formatSignedPct(headerStats.dailyPnlPct)})`}
            tone={headerStats.dailyPnl >= 0 ? "positive" : "negative"}
          />
          <HeaderStatCard
            label="Total P&L"
            value={formatSignedUsd(headerStats.totalPnl)}
            tone={headerStats.totalPnl >= 0 ? "positive" : "negative"}
          />
          <HeaderStatCard
            label="Open Positions"
            value={String(positions.length)}
            subtitle={positionsFetching ? "Refreshing..." : "Live"}
          />
        </div>
      </section>

      <section
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 7fr) minmax(320px, 3fr)",
          gap: "12px",
          marginBottom: "12px",
        }}
      >
        <div
          style={{
            border: "1px solid var(--color-border)",
            borderRadius: "14px",
            background: "linear-gradient(180deg, rgba(12, 16, 24, 0.96), rgba(11, 16, 26, 0.9))",
            padding: "12px",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: "10px",
              flexWrap: "wrap",
              marginBottom: "10px",
            }}
          >
            <h2 style={{ margin: 0, fontSize: "0.96rem", fontWeight: 600 }}>
              Live Chart
            </h2>
            <div style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
              <span style={{ fontSize: "0.76rem", color: "var(--color-text-muted)" }}>Symbol</span>
              <select
                value={effectiveSymbol}
                onChange={(event) => setSelectedSymbol(event.target.value)}
                style={symbolSelectStyle}
              >
                {symbols.length === 0 ? (
                  <option value="">No symbols</option>
                ) : (
                  symbols.map((symbol) => (
                    <option key={symbol} value={symbol}>
                      {symbol}
                    </option>
                  ))
                )}
              </select>
            </div>
          </div>
          <LiveCandlestickChart
            symbol={effectiveSymbol}
            bars={chartBars}
            signals={selectedSignals}
            trades={selectedTrades}
          />
          {quoteError ? (
            <div style={{ marginTop: "8px", color: "#fca5a5", fontSize: "0.76rem" }}>
              {(quoteError as Error).message || "No live quote bars available"}
            </div>
          ) : quoteFetching ? (
            <div style={{ marginTop: "8px", color: "var(--color-text-muted)", fontSize: "0.76rem" }}>
              Refreshing chart...
            </div>
          ) : null}
        </div>

        <div
          style={{
            border: "1px solid var(--color-border)",
            borderRadius: "14px",
            background: "linear-gradient(180deg, rgba(16, 22, 34, 0.96), rgba(13, 18, 28, 0.92))",
            padding: "12px",
          }}
        >
          <h2 style={{ margin: "2px 0 10px", fontSize: "0.95rem", fontWeight: 600 }}>
            Open Positions
          </h2>
          {positions.length === 0 ? (
            <EmptyState label="No open positions" />
          ) : (
            <div style={{ display: "grid", gap: "8px", maxHeight: "520px", overflowY: "auto", paddingRight: "2px" }}>
              {positions.map((position) => (
                <button
                  key={`${position.symbol}-${position.qty}`}
                  type="button"
                  onClick={() => setSelectedSymbol(position.symbol)}
                  style={positionCardStyle(position, effectiveSymbol)}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <strong style={{ fontSize: "0.9rem" }}>{position.symbol}</strong>
                    <span style={sideBadgeStyle(position.qty)}>
                      {position.qty >= 0 ? "LONG" : "SHORT"}
                    </span>
                  </div>
                  <div style={positionGridStyle}>
                    <MiniMetric label="Size" value={formatQty(position.qty)} />
                    <MiniMetric label="Entry" value={formatPrice(position.entry_price)} />
                    <MiniMetric label="Current" value={formatPrice(position.current_price)} />
                    <MiniMetric
                      label="P&L"
                      value={formatSignedUsd(position.unrealized_pnl)}
                      tone={position.unrealized_pnl >= 0 ? "positive" : "negative"}
                    />
                    <MiniMetric
                      label="P&L%"
                      value={formatSignedPct(calcPnlPercent(position))}
                      tone={position.unrealized_pnl >= 0 ? "positive" : "negative"}
                    />
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </section>

      <section
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 7fr) minmax(0, 3fr)",
          gap: "12px",
        }}
      >
        <div
          style={{
            border: "1px solid var(--color-border)",
            borderRadius: "14px",
            background: "var(--color-surface)",
            padding: "12px",
          }}
        >
          <h2 style={{ margin: "2px 0 10px", fontSize: "0.95rem", fontWeight: 600 }}>
            Signal Log
          </h2>
          {signals.length === 0 ? (
            <EmptyState label="No signal entries" />
          ) : (
            <SignalLogTable signals={signals} loading={signalsFetching} />
          )}
        </div>

        <div
          style={{
            border: "1px solid var(--color-border)",
            borderRadius: "14px",
            background: "var(--color-surface)",
            padding: "12px",
          }}
        >
          <h2 style={{ margin: "2px 0 10px", fontSize: "0.95rem", fontWeight: 600 }}>
            Trade History
          </h2>
          {selectedTrades.length === 0 ? (
            <EmptyState label="No completed trades" />
          ) : (
            <TradeTable trades={selectedTradeRows} />
          )}
        </div>
      </section>
    </div>
  );
}

function LiveCandlestickChart({
  symbol,
  bars,
  signals,
  trades,
}: {
  symbol: string;
  bars: LiveQuoteBar[];
  signals: LiveSignalLog[];
  trades: ViewerTrade[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick", Time> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram", Time> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);

  const normalizedBars = useMemo(
    () =>
      bars
        .slice(-DEFAULT_BARS_LIMIT)
        .map((bar) => ({
          time: toChartTime(bar.time),
          open: asFinite(bar.open, 0),
          high: asFinite(bar.high, 0),
          low: asFinite(bar.low, 0),
          close: asFinite(bar.close, 0),
          volume: asFinite(bar.volume, 0),
        }))
        .filter((bar) => bar.time !== null)
        .map((bar) => ({
          time: bar.time as UTCTimestamp,
          open: bar.open,
          high: bar.high,
          low: bar.low,
          close: bar.close,
          volume: bar.volume,
        })),
    [bars],
  );

  const markers = useMemo<TradeMarker[]>(() => {
    const out: TradeMarker[] = [];
    for (const signal of signals) {
      const t = toChartTime(signal.signal_time);
      if (t === null) continue;
      const side = String(signal.action || "").toLowerCase() === "sell" ? "SELL" : "BUY";
      const qty = Number(signal.signal_metadata?.qty ?? signal.filled_qty ?? 0);
      const price = signal.theoretical_price;
      out.push({
        id: `signal-${signal.signal_id}`,
        time: t,
        position: side === "BUY" ? "belowBar" : "aboveBar",
        color: side === "BUY" ? "#22c55e" : "#f87171",
        shape: side === "BUY" ? "arrowUp" : "arrowDown",
        text: `${side} ${Number.isFinite(qty) ? qty.toFixed(2) : "0.00"} @ ${formatPrice(price)}`,
      });
    }
    for (const trade of trades) {
      if (trade.exit === null) continue;
      const t = toChartTime(Number(trade.executionTimeUnix ?? trade.time));
      if (t === null) continue;
      out.push({
        id: `exit-${trade.id}`,
        time: t,
        position: trade.side === "BUY" ? "aboveBar" : "belowBar",
        color: "#eab308",
        shape: trade.side === "BUY" ? "arrowDown" : "arrowUp",
        text: `EXIT ${trade.size.toFixed(2)} @ ${formatPrice(trade.exit)} · ${trade.exitReason}`,
      });
    }
    return out.sort((left, right) => Number(left.time) - Number(right.time));
  }, [signals, trades]);

  useEffect(() => {
    if (!containerRef.current) return;
    const container = containerRef.current;
    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
    }

    const chart = createChart(container, {
      width: container.clientWidth || 960,
      height: 470,
      localization: { locale: resolveChartLocale() },
      layout: {
        background: { type: ColorType.Solid, color: "#0d1117" },
        textColor: "#a8b3c6",
      },
      grid: {
        vertLines: { color: "rgba(148, 163, 184, 0.08)" },
        horzLines: { color: "rgba(148, 163, 184, 0.08)" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "rgba(148, 163, 184, 0.25)" },
      timeScale: {
        borderColor: "rgba(148, 163, 184, 0.25)",
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#22c55e",
      downColor: "#ef4444",
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
      borderVisible: false,
      priceLineVisible: false,
    });
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      color: "rgba(56, 189, 248, 0.32)",
      priceScaleId: "",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: {
        top: 0.8,
        bottom: 0,
      },
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    markersRef.current = createSeriesMarkers(candleSeries, []);

    const resizeObserver = new ResizeObserver(() => {
      if (container.clientWidth > 0) {
        chart.resize(container.clientWidth, 470);
      }
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      markersRef.current = null;
      priceLinesRef.current = [];
    };
  }, []);

  useEffect(() => {
    const candle = candleSeriesRef.current;
    const volume = volumeSeriesRef.current;
    const chart = chartRef.current;
    if (!candle || !volume || !chart) return;

    candle.setData(
      normalizedBars.map((bar) => ({
        time: bar.time,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      })),
    );
    volume.setData(
      normalizedBars.map((bar) => ({
        time: bar.time,
        value: bar.volume,
        color: bar.close >= bar.open ? "rgba(34, 197, 94, 0.35)" : "rgba(239, 68, 68, 0.35)",
      })),
    );
    chart.timeScale().fitContent();
  }, [normalizedBars, symbol]);

  useEffect(() => {
    const markerPrimitive = markersRef.current;
    if (!markerPrimitive) return;
    markerPrimitive.setMarkers(
      markers.map((entry) => ({
        id: entry.id,
        time: entry.time,
        position: entry.position,
        color: entry.color,
        shape: entry.shape,
        text: entry.text,
      })),
    );
  }, [markers]);

  useEffect(() => {
    const candle = candleSeriesRef.current;
    if (!candle) return;
    for (const line of priceLinesRef.current) {
      candle.removePriceLine(line);
    }
    priceLinesRef.current = [];

    const latestSignal = signals
      .filter((entry) => entry.symbol.toUpperCase() === symbol.toUpperCase())
      .slice(0, 10);
    for (const signal of latestSignal) {
      const metadata = signal.signal_metadata ?? {};
      const slRaw = Number(metadata.stop_loss ?? metadata.sl_price ?? metadata.resolved_stop_loss);
      const tpRaw = Number(metadata.take_profit ?? metadata.tp_price);
      if (Number.isFinite(slRaw) && slRaw > 0) {
        priceLinesRef.current.push(
          candle.createPriceLine({
            price: slRaw,
            color: "#ef4444",
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: true,
            title: "SL",
          }),
        );
      }
      if (Number.isFinite(tpRaw) && tpRaw > 0) {
        priceLinesRef.current.push(
          candle.createPriceLine({
            price: tpRaw,
            color: "#22c55e",
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: true,
            title: "TP",
          }),
        );
      }
    }
  }, [signals, symbol]);

  return (
    <div style={{ position: "relative" }}>
      <div ref={containerRef} style={{ width: "100%", height: "470px" }} />
      {!symbol ? (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "grid",
            placeItems: "center",
            color: "var(--color-text-muted)",
            background: "linear-gradient(180deg, rgba(2, 6, 23, 0.48), rgba(2, 6, 23, 0.52))",
            borderRadius: "10px",
            pointerEvents: "none",
          }}
        >
          Select a symbol to stream live bars.
        </div>
      ) : null}
    </div>
  );
}

function SignalLogTable({ signals, loading }: { signals: LiveSignalLog[]; loading: boolean }) {
  return (
    <div style={{ maxHeight: "350px", overflowY: "auto", border: "1px solid var(--color-border)", borderRadius: "10px" }}>
      <table style={tableStyle}>
        <thead>
          <tr>
            <th style={thStyle}>Time</th>
            <th style={thStyle}>Source</th>
            <th style={thStyle}>Signal</th>
            <th style={thStyle}>Symbol</th>
            <th style={thStyle}>Status</th>
            <th style={thStyle}>Message</th>
          </tr>
        </thead>
        <tbody>
          {signals.slice(0, 200).map((signal) => (
            <tr key={signal.signal_id}>
              <td style={tdStyle}>{formatTableTime(signal.signal_timestamp)}</td>
              <td style={tdStyle}>{signal.source === "discord" ? "Discord" : "Internal"}</td>
              <td style={tdStyle}>{String(signal.action || "").toUpperCase()}</td>
              <td style={tdStyle}>{signal.symbol}</td>
              <td style={tdStyle}>
                <span style={statusBadgeStyle(signal.status)}>
                  {signal.status}
                  {String(signal.status || "").toLowerCase() === "waiting" && signal.waiting_countdown_seconds != null
                    ? ` (${formatCountdown(signal.waiting_countdown_seconds)})`
                    : ""}
                </span>
              </td>
              <td style={tdStyleMessage}>
                {signal.raw_message ||
                  String(signal.signal_metadata?.discord_content || signal.signal_metadata?.source || "—")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {loading ? (
        <div style={{ padding: "8px 10px", color: "var(--color-text-muted)", fontSize: "0.74rem" }}>
          Refreshing signal log...
        </div>
      ) : null}
    </div>
  );
}

const HeaderStatCard = memo(function HeaderStatCard({
  label,
  value,
  subtitle,
  tone = "neutral",
}: {
  label: string;
  value: string;
  subtitle?: string;
  tone?: "neutral" | "positive" | "negative";
}) {
  const toneColor = tone === "positive" ? "#86efac" : tone === "negative" ? "#fecaca" : "#dbeafe";
  return (
    <div
      style={{
        border: "1px solid rgba(148, 163, 184, 0.2)",
        borderRadius: "10px",
        background: "rgba(8, 14, 24, 0.54)",
        padding: "9px 10px",
      }}
    >
      <div style={{ fontSize: "0.68rem", color: "var(--color-text-muted)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
        {label}
      </div>
      <div style={{ marginTop: "5px", fontWeight: 700, fontSize: "0.96rem", color: toneColor }}>
        {value}
      </div>
      {subtitle ? (
        <div style={{ marginTop: "2px", fontSize: "0.7rem", color: "var(--color-text-muted)" }}>
          {subtitle}
        </div>
      ) : null}
    </div>
  );
});

const MiniMetric = memo(function MiniMetric({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "neutral" | "positive" | "negative";
}) {
  return (
    <div style={{ display: "grid", gap: "3px" }}>
      <span style={{ fontSize: "0.67rem", color: "var(--color-text-muted)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
        {label}
      </span>
      <span
        style={{
          fontSize: "0.82rem",
          color: tone === "positive" ? "#86efac" : tone === "negative" ? "#fecaca" : "#dbeafe",
          fontWeight: 600,
        }}
      >
        {value}
      </span>
    </div>
  );
});

const EmptyState = memo(function EmptyState({ label }: { label: string }) {
  return (
    <div
      style={{
        minHeight: "110px",
        border: "1px dashed var(--color-border)",
        borderRadius: "10px",
        display: "grid",
        placeItems: "center",
        color: "var(--color-text-muted)",
        fontSize: "0.83rem",
      }}
    >
      {label}
    </div>
  );
});

function sortLiveTrades(rows: LiveTradeSlippage[]): LiveTradeSlippage[] {
  return [...rows].sort((left, right) => right.execution_time - left.execution_time);
}

function toTradeHistoryRows(rows: LiveTradeSlippage[]): ViewerTrade[] {
  return rows.map((row) => {
    const entry = asFinite(row.theoretical_price, 0);
    const exit = Number.isFinite(row.actual_price) ? asFinite(row.actual_price, 0) : null;
    const side = String(row.action || "").toLowerCase() === "sell" ? "SELL" : "BUY";
    const pnl =
      exit == null
        ? null
        : (exit - entry) * asFinite(row.filled_qty, 0) * (side === "BUY" ? 1 : -1);
    return {
      id: `${row.execution_id || row.signal_id}`,
      symbol: row.symbol,
      side,
      size: asFinite(row.filled_qty, 0),
      entry,
      exit,
      pnl,
      exitReason: row.exit_reason || String(row.signal_metadata?.exit_reason || ""),
      time: row.execution_timestamp || row.signal_timestamp,
      executionTimeUnix: asFinite(row.execution_time, 0),
      signalTimeUnix: asFinite(row.signal_time, 0),
    };
  });
}

function uniqueSymbols(
  deployments: LiveDeploymentConfig[],
  fills: LiveTradeSlippage[],
  positions: LivePosition[],
  signals: LiveSignalLog[],
): string[] {
  const values = new Set<string>();
  for (const deployment of deployments) {
    for (const symbol of deployment.symbols) {
      const normalized = symbol.trim().toUpperCase();
      if (normalized && normalized !== "*") values.add(normalized);
    }
  }
  for (const fill of fills) {
    const normalized = fill.symbol.trim().toUpperCase();
    if (normalized && normalized !== "*") values.add(normalized);
  }
  for (const position of positions) {
    const normalized = position.symbol.trim().toUpperCase();
    if (normalized && normalized !== "*") values.add(normalized);
  }
  for (const signal of signals) {
    const normalized = signal.symbol.trim().toUpperCase();
    if (normalized && normalized !== "*") values.add(normalized);
  }
  return [...values].sort();
}

function positionCardStyle(position: LivePosition, selectedSymbol: string): React.CSSProperties {
  const active = position.symbol.toUpperCase() === selectedSymbol.toUpperCase();
  return {
    width: "100%",
    border: active ? "1px solid rgba(56, 189, 248, 0.55)" : "1px solid rgba(148, 163, 184, 0.24)",
    borderRadius: "10px",
    background: active ? "rgba(56, 189, 248, 0.12)" : "rgba(2, 6, 23, 0.38)",
    color: "var(--color-text)",
    padding: "8px 10px",
    cursor: "pointer",
    textAlign: "left",
    display: "grid",
    gap: "7px",
  };
}

const positionGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
  gap: "7px 10px",
};

function sideBadgeStyle(qty: number): React.CSSProperties {
  const positive = qty >= 0;
  return {
    borderRadius: "999px",
    padding: "2px 8px",
    border: positive
      ? "1px solid rgba(34, 197, 94, 0.45)"
      : "1px solid rgba(248, 113, 113, 0.45)",
    background: positive ? "rgba(34, 197, 94, 0.15)" : "rgba(248, 113, 113, 0.14)",
    color: positive ? "#86efac" : "#fecaca",
    fontSize: "0.7rem",
    letterSpacing: "0.04em",
    fontWeight: 700,
  };
}

const iconButtonStyle: React.CSSProperties = {
  border: "1px solid rgba(56, 189, 248, 0.3)",
  background: "rgba(56, 189, 248, 0.12)",
  color: "#bfdbfe",
  width: "34px",
  height: "34px",
  borderRadius: "10px",
  cursor: "pointer",
};

const symbolSelectStyle: React.CSSProperties = {
  borderRadius: "8px",
  border: "1px solid var(--color-border)",
  background: "rgba(2, 6, 23, 0.5)",
  color: "var(--color-text)",
  fontSize: "0.84rem",
  padding: "6px 9px",
};

const tableStyle: React.CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: "0.75rem",
  fontVariantNumeric: "tabular-nums",
};

const thStyle: React.CSSProperties = {
  position: "sticky",
  top: 0,
  textAlign: "left",
  background: "var(--color-surface2)",
  color: "var(--color-text-muted)",
  borderBottom: "1px solid var(--color-border)",
  fontWeight: 600,
  padding: "7px 8px",
  zIndex: 1,
};

const tdStyle: React.CSSProperties = {
  padding: "7px 8px",
  borderBottom: "1px solid rgba(148, 163, 184, 0.12)",
  whiteSpace: "nowrap",
  verticalAlign: "top",
};

const tdStyleMessage: React.CSSProperties = {
  ...tdStyle,
  whiteSpace: "normal",
  maxWidth: "480px",
  color: "#cbd5e1",
  lineHeight: 1.35,
};

function statusBadgeStyle(status: string): React.CSSProperties {
  const normalized = status.toLowerCase();
  if (normalized === "filled" || normalized === "executed") {
    return {
      borderRadius: "999px",
      padding: "2px 7px",
      border: "1px solid rgba(34, 197, 94, 0.45)",
      background: "rgba(34, 197, 94, 0.13)",
      color: "#86efac",
      fontSize: "0.68rem",
      fontWeight: 700,
    };
  }
  if (normalized === "failed") {
    return {
      borderRadius: "999px",
      padding: "2px 7px",
      border: "1px solid rgba(248, 113, 113, 0.45)",
      background: "rgba(248, 113, 113, 0.13)",
      color: "#fecaca",
      fontSize: "0.68rem",
      fontWeight: 700,
    };
  }
  if (normalized === "waiting") {
    return {
      borderRadius: "999px",
      padding: "2px 7px",
      border: "1px solid rgba(234, 179, 8, 0.45)",
      background: "rgba(234, 179, 8, 0.13)",
      color: "#fde68a",
      fontSize: "0.68rem",
      fontWeight: 700,
    };
  }
  return {
    borderRadius: "999px",
    padding: "2px 7px",
    border: "1px solid rgba(148, 163, 184, 0.45)",
    background: "rgba(148, 163, 184, 0.13)",
    color: "#cbd5e1",
    fontSize: "0.68rem",
    fontWeight: 700,
  };
}

function toChartTime(value: number): UTCTimestamp | null {
  if (!Number.isFinite(value) || value <= 0) {
    return null;
  }
  return Math.trunc(value) as UTCTimestamp;
}

function asFinite(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric;
  }
  return fallback;
}

function formatUsd(value: number): string {
  return `$${asFinite(value, 0).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatSignedUsd(value: number): string {
  const numeric = asFinite(value, 0);
  return `${numeric >= 0 ? "+" : "-"}$${Math.abs(numeric).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatPrice(value: number): string {
  return asFinite(value, 0).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 5,
  });
}

function formatSignedPct(value: number): string {
  const numeric = asFinite(value, 0);
  return `${numeric >= 0 ? "+" : ""}${numeric.toFixed(2)}%`;
}

function formatQty(value: number): string {
  return asFinite(value, 0).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

function formatTableTime(value: string): string {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return "—";
  return new Date(parsed).toISOString().slice(0, 19).replace("T", " ");
}

function calcPnlPercent(position: LivePosition): number {
  const entry = asFinite(position.entry_price, 0);
  const qty = Math.abs(asFinite(position.qty, 0));
  if (entry <= 0 || qty <= 0) return 0;
  const notional = entry * qty;
  if (notional <= 0) return 0;
  return (asFinite(position.unrealized_pnl, 0) / notional) * 100;
}

function formatCountdown(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}
