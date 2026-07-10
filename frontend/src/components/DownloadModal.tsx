import { memo, useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import {
  ArrowRight,
  CheckCircle2,
  Download,
  Loader2,
  RotateCcw,
  XCircle,
} from "lucide-react";

import { downloadData, getDukascopyGenesis } from "../api/client";
import type { DukascopyGenesisResponse } from "../api/types";
import { LiveTerminal } from "./LiveTerminal";
import { Modal } from "./Modal";
import { useToast } from "./ToastProvider";

const TIMEFRAME_OPTIONS = [
  "tick",
  "s1",
  "m1",
  "m5",
  "m15",
  "m30",
  "h1",
  "h4",
  "d1",
  "mn1",
];
const DEFAULT_DOWNLOAD_TIMEFRAMES = ["d1"];

const DUKASCOPY_INSTRUMENTS = [
  "EURUSD",
  "GBPUSD",
  "USDJPY",
  "AUDUSD",
  "USDCAD",
  "USDCHF",
  "NZDUSD",
  "EURGBP",
  "EURJPY",
  "GBPJPY",
  "EURCHF",
  "EURCAD",
  "EURAUD",
  "GBPCAD",
  "GBPCHF",
  "AUDJPY",
  "AUDCAD",
  "AUDCHF",
  "AUDNZD",
  "CADJPY",
  "CHFJPY",
  "NZDJPY",
  "NZDCAD",
  "NZDCHF",
  "CADCHF",
  "XAUUSD",
  "XAGUSD",
  "US30",
  "US500",
  "USTEC",
  "USA30IDXUSD",
  "USA500IDXUSD",
  "USATECHIDXUSD",
  "DEUIDXEUR",
  "GBRIDXGBP",
  "AUSIDXAUD",
  "FRAIDXEUR",
  "ESPIDXEUR",
  "HKGIDXHKD",
  "EUSIDXEUR",
  "CHEIDXCHF",
  "JPNIDXJPY",
  "UKOIL",
  "USOIL",
  "BRENTCMDUSD",
  "LIGHTCMDUSD",
  "DOLLARIDXUSD",
  "VOLIDXUSD",
];

function normalizeGenesisTimeframe(timeframe: string): string {
  const normalized = timeframe.trim().toLowerCase();
  const aliases: Record<string, string> = {
    sec: "s1",
    "1m": "m1",
    "5m": "m5",
    "15m": "m15",
    "30m": "m30",
    "1h": "h1",
    "4h": "h4",
    "1d": "d1",
  };
  return aliases[normalized] ?? normalized;
}

type DukascopyCoverageMap = DukascopyGenesisResponse["coverage"];

interface DukascopyTimeframeCoverageRow {
  timeframe: string;
  available: boolean;
  availableCount: number;
  symbolCount: number;
  start: string | null;
  missingSymbols: string[];
}

function formatMissingSymbols(symbols: string[]): string {
  if (symbols.length <= 3) {
    return symbols.join(", ");
  }
  return `${symbols.slice(0, 3).join(", ")} +${symbols.length - 3} more`;
}

function formatCoverageTimeframe(timeframe: string): string {
  const labels: Record<string, string> = {
    tick: "tick",
    s1: "1s",
    m1: "1m",
    m5: "5m",
    m15: "15m",
    m30: "30m",
    h1: "1h",
    h4: "4h",
    d1: "1d",
    mn1: "1mn",
  };
  return labels[timeframe] ?? timeframe;
}

interface DownloadModalProps {
  open: boolean;
  onClose: () => void;
  onTaskStarted?: (id: string) => void;
  onTaskFinished?: (status: "completed" | "failed", id: string) => void;
  defaultSource?: string;
  defaultSymbols?: string;
  defaultTimeframes?: string[];
}

export function DownloadModal({
  open,
  onClose,
  onTaskStarted,
  onTaskFinished,
  defaultSource = "dukascopy",
  defaultSymbols = "",
  defaultTimeframes = DEFAULT_DOWNLOAD_TIMEFRAMES,
}: DownloadModalProps) {
  const { showToast } = useToast();
  const defaultTimeframesKey = (
    defaultTimeframes.length > 0 ? defaultTimeframes : DEFAULT_DOWNLOAD_TIMEFRAMES
  )
    .map(normalizeGenesisTimeframe)
    .join("|");
  const normalizedDefaultTimeframes = useMemo(
    () => defaultTimeframesKey.split("|").filter(Boolean),
    [defaultTimeframesKey],
  );
  const [source, setSource] = useState(defaultSource);
  const [symbols, setSymbols] = useState(defaultSymbols);
  const [selectedTimeframes, setSelectedTimeframes] = useState<string[]>(
    normalizedDefaultTimeframes,
  );
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [priceType, setPriceType] = useState<"bid" | "ask" | "both">("bid");
  const [mergeTick, setMergeTick] = useState(false);
  const [mergeS1, setMergeS1] = useState(false);
  const [showAdvancedSettings, setShowAdvancedSettings] = useState(false);
  const [batchSize, setBatchSize] = useState(10);
  const [pauseMs, setPauseMs] = useState(1000);
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState("idle");
  const [completionNotified, setCompletionNotified] = useState(false);
  const [dukascopyCoverage, setDukascopyCoverage] =
    useState<DukascopyCoverageMap>({});
  const [dukascopyCoverageLoaded, setDukascopyCoverageLoaded] = useState(false);
  const [dukascopyCoverageError, setDukascopyCoverageError] = useState("");

  const isDukascopy = source === "dukascopy";
  const isJForex = source === "jforex";
  const isDukascopyOrJForex = isDukascopy || isJForex;
  const isStreaming = jobId !== null;
  const parsedSymbols = useMemo(
    () =>
      symbols
        .split(",")
        .map((symbol) => symbol.trim().toUpperCase())
        .filter(Boolean),
    [symbols],
  );
  const requestedTimeframes = useMemo(() => {
    const ordered: string[] = [];
    const seen = new Set<string>();
    for (const timeframe of selectedTimeframes) {
      const normalized = normalizeGenesisTimeframe(timeframe);
      if (seen.has(normalized)) {
        continue;
      }
      seen.add(normalized);
      ordered.push(normalized);
    }
    return ordered;
  }, [selectedTimeframes]);

  const dukascopyMinStart = useMemo(() => {
    if (parsedSymbols.length === 0 || requestedTimeframes.length === 0) {
      return "";
    }

    const knownDates: string[] = [];

    for (const symbol of parsedSymbols) {
      const symbolCoverage = dukascopyCoverage[symbol];
      if (!symbolCoverage) {
        continue;
      }

      for (const timeframe of requestedTimeframes) {
        const coverage = symbolCoverage[timeframe];
        if (!coverage?.available || !coverage.start) {
          continue;
        }
        knownDates.push(coverage.start);
      }
    }

    if (knownDates.length === 0) {
      return "";
    }
    return knownDates.reduce((latest, current) =>
      current > latest ? current : latest,
    );
  }, [dukascopyCoverage, parsedSymbols, requestedTimeframes]);

  const dukascopyTimeframeCoverage = useMemo<DukascopyTimeframeCoverageRow[]>(
    () => {
      if (
        !isDukascopy ||
        parsedSymbols.length === 0 ||
        requestedTimeframes.length === 0
      ) {
        return [];
      }

      return requestedTimeframes.map((timeframe) => {
        const entries = parsedSymbols.map((symbol) => {
          const coverage = dukascopyCoverage[symbol]?.[timeframe];
          return {
            symbol,
            available: Boolean(coverage?.available && coverage?.start),
            start: coverage?.start ?? null,
          };
        });

        const availableEntries = entries.filter((item) => item.available);
        const starts = availableEntries
          .map((item) => item.start)
          .filter((value): value is string => Boolean(value));
        const latestStart =
          starts.length > 0
            ? starts.reduce((latest, current) =>
                current > latest ? current : latest,
              )
            : null;

        return {
          timeframe,
          available:
            entries.length > 0 && availableEntries.length === entries.length,
          availableCount: availableEntries.length,
          symbolCount: entries.length,
          start: latestStart,
          missingSymbols: entries
            .filter((item) => !item.available)
            .map((item) => item.symbol),
        };
      });
    },
    [dukascopyCoverage, isDukascopy, parsedSymbols, requestedTimeframes],
  );

  useEffect(() => {
    if (!open) {
      return;
    }
    setSource(defaultSource);
    setSymbols(defaultSymbols);
    setSelectedTimeframes(normalizedDefaultTimeframes);
    setStartDate("");
    setEndDate("");
    setPriceType("bid");
    setMergeTick(false);
    setMergeS1(false);
    setShowAdvancedSettings(false);
    setBatchSize(10);
    setPauseMs(1000);
    setError("");
    setJobId(null);
    setJobStatus("idle");
    setCompletionNotified(false);
    setDukascopyCoverage({});
    setDukascopyCoverageLoaded(false);
    setDukascopyCoverageError("");
  }, [
    defaultSource,
    defaultSymbols,
    defaultTimeframesKey,
    open,
  ]);

  useEffect(() => {
    if (!open || !isDukascopy || isStreaming) {
      return;
    }
    if (parsedSymbols.length === 0 || requestedTimeframes.length === 0) {
      setDukascopyCoverage({});
      setDukascopyCoverageLoaded(false);
      setDukascopyCoverageError("");
      return;
    }

    let cancelled = false;
    setDukascopyCoverageLoaded(false);
    setDukascopyCoverageError("");
    getDukascopyGenesis(parsedSymbols, selectedTimeframes)
      .then((res) => {
        if (!cancelled) {
          setDukascopyCoverage(res.coverage || {});
          setDukascopyCoverageLoaded(true);
          setDukascopyCoverageError("");
        }
      })
      .catch(() => {
        if (!cancelled) {
          setDukascopyCoverage({});
          setDukascopyCoverageLoaded(true);
          setDukascopyCoverageError(
            "Could not load Dukascopy coverage right now. Start-date guardrails may be less strict.",
          );
        }
      });

    return () => {
      cancelled = true;
    };
  }, [
    isDukascopy,
    isStreaming,
    open,
    parsedSymbols,
    requestedTimeframes,
    selectedTimeframes,
  ]);

  useEffect(() => {
    if (!jobId || completionNotified) {
      return;
    }
    if (jobStatus === "completed" || jobStatus === "failed") {
      onTaskFinished?.(jobStatus, jobId);
      setCompletionNotified(true);
    }
  }, [completionNotified, jobId, jobStatus, onTaskFinished]);

  const inputStyle: CSSProperties = {
    background: "rgba(15, 23, 42, 0.75)",
    border: "1px solid rgba(148, 163, 184, 0.18)",
    color: "var(--color-text)",
    padding: "11px 12px",
    borderRadius: "12px",
    width: "100%",
  };

  const tfBtn = (active: boolean): CSSProperties => ({
    border: `1px solid ${active ? "rgba(96, 165, 250, 0.5)" : "rgba(148, 163, 184, 0.18)"}`,
    background: active
      ? "linear-gradient(135deg, rgba(59, 130, 246, 0.22), rgba(14, 165, 233, 0.08))"
      : "rgba(15, 23, 42, 0.56)",
    color: active ? "#dbeafe" : "var(--color-text-muted)",
    borderRadius: "999px",
    padding: "6px 11px",
    fontSize: "0.74rem",
    cursor: "pointer",
    fontWeight: 700,
    letterSpacing: "0.04em",
  });

  const coverageBadgeStyle = (available: boolean): CSSProperties => ({
    display: "inline-flex",
    alignItems: "center",
    gap: "6px",
    padding: "6px 10px",
    borderRadius: "999px",
    border: `1px solid ${
      available ? "rgba(34, 197, 94, 0.28)" : "rgba(148, 163, 184, 0.18)"
    }`,
    background: available
      ? "rgba(34, 197, 94, 0.12)"
      : "rgba(15, 23, 42, 0.72)",
    color: available ? "#bbf7d0" : "var(--color-text-muted)",
    fontSize: "0.74rem",
    fontWeight: 700,
    whiteSpace: "nowrap",
  });

  const toggleTimeframe = (tf: string) => {
    setSelectedTimeframes((prev) =>
      prev.includes(tf) ? prev.filter((x) => x !== tf) : [...prev, tf],
    );
  };

  const hasTickSelected = selectedTimeframes.includes("tick");
  const hasS1Selected = selectedTimeframes.includes("s1");

  const resetStream = () => {
    setJobId(null);
    setJobStatus("idle");
    setCompletionNotified(false);
    setError("");
  };

  const handleSubmit = async () => {
    setError("");

    if (!symbols.trim() && source !== "dolthub") {
      setError("Please enter at least one symbol or instrument.");
      return;
    }

    if (selectedTimeframes.length === 0 && source !== "dolthub") {
      setError("Select at least one timeframe.");
      return;
    }

    if (
      isDukascopy &&
      dukascopyMinStart &&
      startDate &&
      startDate < dukascopyMinStart
    ) {
      setError(
        `Explicit start date must be on or after ${dukascopyMinStart} for all selected instruments and timeframes with Dukascopy coverage.`,
      );
      return;
    }

    if (isDukascopy && (!Number.isFinite(batchSize) || batchSize < 1)) {
      setError("Batch Size must be at least 1.");
      return;
    }

    if (isDukascopy && (!Number.isFinite(pauseMs) || pauseMs < 0)) {
      setError("Pause between batches must be 0 ms or greater.");
      return;
    }

    try {
      const res = await downloadData({
        source,
        symbols,
        timeframes: selectedTimeframes.join(","),
        start: startDate || undefined,
        end: endDate || undefined,
        // Preferred new field
        offer_side: isDukascopyOrJForex ? priceType : undefined,
        // Legacy compatibility
        price_type: isDukascopyOrJForex ? priceType : undefined,
        merge_tick: isDukascopy ? mergeTick : undefined,
        merge_s1: isDukascopy ? mergeS1 : undefined,
        batch_size: isDukascopy ? batchSize : undefined,
        pause_ms: isDukascopy ? pauseMs : undefined,
      });
      onTaskStarted?.(res.task_id);
      showToast(
        "Download started. Check Task Manager in the top right.",
        "success",
      );
      onClose();
    } catch (err: unknown) {
      setError(err instanceof Error && err.message ? err.message : "Failed to start download.");
    }
  };

  const statusIcon =
    jobStatus === "completed" ? (
      <CheckCircle2 size={18} color="#86efac" />
    ) : jobStatus === "failed" ? (
      <XCircle size={18} color="#fda4af" />
    ) : (
      <Loader2 size={18} className="animate-spin" color="#67e8f9" />
    );

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Download Data"
      width={isStreaming ? "860px" : "760px"}
    >
      <div style={{ display: "grid", gap: "18px" }}>
        {!isStreaming ? (
          <>
            <div
              style={{
                display: "grid",
                gap: "14px",
                padding: "18px",
                borderRadius: "18px",
                border: "1px solid rgba(96, 165, 250, 0.14)",
                background:
                  "linear-gradient(135deg, rgba(15, 23, 42, 0.86), rgba(17, 24, 39, 0.94))",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  gap: "12px",
                  alignItems: "center",
                }}
              >
                <div>
                  <div
                    style={{
                      fontSize: "0.75rem",
                      textTransform: "uppercase",
                      letterSpacing: "0.18em",
                      color: "#7dd3fc",
                    }}
                  >
                    Remote ingestion
                  </div>
                  <div style={{ fontSize: "1.18rem", fontWeight: 700 }}>
                    Queue a new market data import
                  </div>
                </div>
                <div
                  style={{
                    fontSize: "0.78rem",
                    color: "var(--color-text-muted)",
                    maxWidth: "220px",
                    textAlign: "right",
                  }}
                >
                  Pick a source, define the symbol scope, then stream the
                  backend logs inline.
                </div>
              </div>

              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1.1fr 1fr",
                  gap: "14px",
                }}
              >
                <div style={{ display: "grid", gap: "8px" }}>
                  <label
                    style={{
                      fontSize: "0.76rem",
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    Source
                  </label>
                  <select
                    value={source}
                    onChange={(e) => setSource(e.target.value)}
                    style={inputStyle}
                  >
                    <option value="dukascopy">Dukascopy</option>
                    <option value="jforex">JForex</option>
                    <option value="yfinance">Yahoo Finance</option>
                    <option value="huggingface">HuggingFace (OHLCV-1m)</option>
                    <option value="alpaca">Alpaca</option>
                    <option value="dolthub">Dolthub (Update All)</option>
                  </select>
                </div>
                <div style={{ display: "grid", gap: "8px" }}>
                  <label
                    style={{
                      fontSize: "0.76rem",
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    {isDukascopy ? "Instrument set" : "Symbols"}
                  </label>
                  <input
                    value={symbols}
                    onChange={(e) => setSymbols(e.target.value)}
                    list={isDukascopy ? "dukascopy-instruments" : undefined}
                    placeholder={
                      source === "dolthub"
                        ? "No symbol required"
                        : isDukascopy
                          ? "EURUSD, XAUUSD, US500"
                          : "AAPL, MSFT"
                    }
                    style={inputStyle}
                    disabled={source === "dolthub"}
                  />
                  {isDukascopy && (
                    <datalist id="dukascopy-instruments">
                      {DUKASCOPY_INSTRUMENTS.map((inst) => (
                        <option key={inst} value={inst} />
                      ))}
                    </datalist>
                  )}
                </div>
              </div>

              {source !== "dolthub" && (
                <>
                  <div style={{ display: "grid", gap: "8px" }}>
                    <label
                      style={{
                        fontSize: "0.76rem",
                        letterSpacing: "0.08em",
                        textTransform: "uppercase",
                        color: "var(--color-text-muted)",
                      }}
                    >
                      Timeframes
                    </label>
                    <div
                      style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}
                    >
                      {TIMEFRAME_OPTIONS.map((tf) => (
                        <button
                          key={tf}
                          type="button"
                          onClick={() => toggleTimeframe(tf)}
                          style={tfBtn(selectedTimeframes.includes(tf))}
                        >
                          {tf}
                        </button>
                      ))}
                    </div>
                  </div>

                  {isDukascopyOrJForex && (
                    <div style={{ display: "grid", gap: "8px" }}>
                      <label
                        style={{
                          fontSize: "0.76rem",
                          letterSpacing: "0.08em",
                          textTransform: "uppercase",
                          color: "var(--color-text-muted)",
                        }}
                      >
                        Price type
                      </label>
                      <div
                        style={{
                          display: "flex",
                          gap: "10px",
                          flexWrap: "wrap",
                        }}
                      >
                        {(["bid", "ask", "both"] as const).map((option) => {
                          const active = priceType === option;
                          return (
                            <button
                              key={option}
                              type="button"
                              onClick={() => setPriceType(option)}
                              style={{
                                ...tfBtn(active),
                                padding: "9px 14px",
                                textTransform: "uppercase",
                              }}
                            >
                              {option}
                            </button>
                          );
                        })}
                      </div>
                      <div
                        style={{
                          display: "grid",
                          gap: "10px",
                          marginTop: "2px",
                        }}
                      >
                        <MergeOptionCard
                          checked={mergeTick}
                          disabled={!hasTickSelected}
                          label="Merge Bid & Ask for Tick data"
                          hint={
                            hasTickSelected
                              ? "When active, tick downloads ignore Price Type and store bid + ask together."
                              : "Select the tick timeframe to enable merged tick downloads."
                          }
                          onChange={setMergeTick}
                        />
                        <MergeOptionCard
                          checked={mergeS1}
                          disabled={!hasS1Selected}
                          label="Merge Bid & Ask for 1-Second (s1) data"
                          hint={
                            hasS1Selected
                              ? "When active, s1 downloads ignore Price Type and store bid + ask together."
                              : "Select the s1 timeframe to enable merged 1-second downloads."
                          }
                          onChange={setMergeS1}
                        />
                      </div>
                    </div>
                  )}

                  {isDukascopy && (
                    <div
                      style={{
                        display: "grid",
                        gap: "12px",
                        padding: "14px",
                        borderRadius: "16px",
                        border: "1px solid rgba(148, 163, 184, 0.16)",
                        background: "rgba(15, 23, 42, 0.56)",
                      }}
                    >
                      <button
                        type="button"
                        onClick={() =>
                          setShowAdvancedSettings((current) => !current)
                        }
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          alignItems: "center",
                          gap: "12px",
                          border: "none",
                          background: "transparent",
                          color: "var(--color-text)",
                          padding: 0,
                          cursor: "pointer",
                          textAlign: "left",
                        }}
                      >
                        <div>
                          <div
                            style={{
                              fontSize: "0.76rem",
                              letterSpacing: "0.08em",
                              textTransform: "uppercase",
                              color: "var(--color-text-muted)",
                            }}
                          >
                            Advanced settings
                          </div>
                          <div
                            style={{
                              marginTop: "4px",
                              fontSize: "0.84rem",
                              color: "var(--color-text-muted)",
                            }}
                          >
                            Control Dukascopy batch concurrency and pacing.
                          </div>
                        </div>
                        <div
                          style={{
                            fontSize: "0.82rem",
                            color: "#7dd3fc",
                            fontWeight: 700,
                          }}
                        >
                          {showAdvancedSettings ? "Hide" : "Show"}
                        </div>
                      </button>

                      {showAdvancedSettings && (
                        <div
                          style={{
                            display: "grid",
                            gridTemplateColumns: "1fr 1fr",
                            gap: "14px",
                          }}
                        >
                          <div style={{ display: "grid", gap: "8px" }}>
                            <label
                              style={{
                                fontSize: "0.76rem",
                                letterSpacing: "0.08em",
                                textTransform: "uppercase",
                                color: "var(--color-text-muted)",
                              }}
                            >
                              Batch Size
                            </label>
                            <input
                              type="number"
                              min={1}
                              value={batchSize}
                              onChange={(event) =>
                                setBatchSize(
                                  Number.isFinite(event.target.valueAsNumber)
                                    ? Math.max(1, Math.floor(event.target.valueAsNumber))
                                    : 1,
                                )
                              }
                              style={inputStyle}
                            />
                            <div
                              style={{
                                fontSize: "0.76rem",
                                color: "var(--color-text-muted)",
                              }}
                            >
                              Maximum URLs fetched in one batch.
                            </div>
                          </div>

                          <div style={{ display: "grid", gap: "8px" }}>
                            <label
                              style={{
                                fontSize: "0.76rem",
                                letterSpacing: "0.08em",
                                textTransform: "uppercase",
                                color: "var(--color-text-muted)",
                              }}
                            >
                              Pause between batches (ms)
                            </label>
                            <input
                              type="number"
                              min={0}
                              step={100}
                              value={pauseMs}
                              onChange={(event) =>
                                setPauseMs(
                                  Number.isFinite(event.target.valueAsNumber)
                                    ? Math.max(0, Math.floor(event.target.valueAsNumber))
                                    : 0,
                                )
                              }
                              style={inputStyle}
                            />
                            <div
                              style={{
                                fontSize: "0.76rem",
                                color: "var(--color-text-muted)",
                              }}
                            >
                              Applied after every batch; the downloader doubles
                              it automatically when rate-limit signals appear.
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  )}

                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr",
                      gap: "14px",
                    }}
                  >
                    <div style={{ display: "grid", gap: "8px" }}>
                      <label
                        style={{
                          fontSize: "0.76rem",
                          letterSpacing: "0.08em",
                          textTransform: "uppercase",
                          color: "var(--color-text-muted)",
                        }}
                      >
                        Start date
                      </label>
                      <input
                        type="date"
                        value={startDate}
                        min={
                          isDukascopy && dukascopyMinStart
                            ? dukascopyMinStart
                            : undefined
                        }
                        onChange={(e) => setStartDate(e.target.value)}
                        style={inputStyle}
                      />
                    </div>
                    <div style={{ display: "grid", gap: "8px" }}>
                      <label
                        style={{
                          fontSize: "0.76rem",
                          letterSpacing: "0.08em",
                          textTransform: "uppercase",
                          color: "var(--color-text-muted)",
                        }}
                      >
                        End date
                      </label>
                      <input
                        type="date"
                        value={endDate}
                        onChange={(e) => setEndDate(e.target.value)}
                        style={inputStyle}
                      />
                    </div>
                  </div>
                </>
              )}
            </div>

            {isDukascopy &&
              parsedSymbols.length > 0 &&
              requestedTimeframes.length > 0 && (
                <div
                  style={{
                    display: "grid",
                    gap: "10px",
                    padding: "12px",
                    borderRadius: "14px",
                    background: "rgba(15, 23, 42, 0.56)",
                    border: "1px solid rgba(148, 163, 184, 0.16)",
                  }}
                >
                  <div
                    style={{
                      fontSize: "0.76rem",
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    Dukascopy coverage by timeframe
                  </div>
                  {!dukascopyCoverageLoaded ? (
                    <div
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: "8px",
                        fontSize: "0.82rem",
                        color: "var(--color-text-muted)",
                      }}
                    >
                      <Loader2 size={14} className="animate-spin" />
                      Loading timeframe coverage…
                    </div>
                  ) : dukascopyCoverageError ? (
                    <div style={{ fontSize: "0.82rem", color: "#fca5a5" }}>
                      {dukascopyCoverageError}
                    </div>
                  ) : dukascopyTimeframeCoverage.length === 0 ? (
                    <div
                      style={{
                        fontSize: "0.8rem",
                        color: "var(--color-text-muted)",
                      }}
                    >
                      Enter one or more instruments and select at least one
                      timeframe to inspect coverage.
                    </div>
                  ) : (
                    <div style={{ display: "grid", gap: "10px" }}>
                      {dukascopyTimeframeCoverage.map((row) => (
                        <div
                          key={row.timeframe}
                          style={{
                            padding: "12px",
                            borderRadius: "14px",
                            border: "1px solid rgba(148, 163, 184, 0.12)",
                            background: "rgba(2, 6, 23, 0.32)",
                            display: "grid",
                            gap: "10px",
                          }}
                        >
                          <div
                            style={{
                              display: "flex",
                              justifyContent: "space-between",
                              gap: "10px",
                              alignItems: "center",
                              flexWrap: "wrap",
                            }}
                          >
                            <div
                              style={{
                                display: "inline-flex",
                                alignItems: "center",
                                gap: "8px",
                                fontWeight: 800,
                                color: "#e2e8f0",
                              }}
                            >
                              <span
                                style={{
                                  ...coverageBadgeStyle(row.available),
                                  textTransform: "uppercase",
                                  letterSpacing: "0.08em",
                                }}
                              >
                                {formatCoverageTimeframe(row.timeframe)}
                              </span>
                              <span style={{ fontSize: "0.8rem" }}>
                                {row.availableCount}/{row.symbolCount} available
                              </span>
                            </div>
                            <div
                              style={{
                                fontSize: "0.78rem",
                                color: "var(--color-text-muted)",
                              }}
                            >
                              {row.start
                                ? `Available since ${row.start}`
                                : "Not found"}
                            </div>
                          </div>

                          <div
                            style={{
                              display: "flex",
                              flexWrap: "wrap",
                              gap: "8px",
                            }}
                          >
                            {parsedSymbols.map((symbol) => {
                              const coverage = dukascopyCoverage[symbol]?.[
                                row.timeframe
                              ];
                              const available = Boolean(
                                coverage?.available && coverage?.start,
                              );
                              return (
                                <span
                                  key={`${symbol}-${row.timeframe}`}
                                  style={coverageBadgeStyle(available)}
                                >
                                  {symbol} ·{" "}
                                  {available ? coverage?.start : "Not found"}
                                </span>
                              );
                            })}
                          </div>

                          {!row.available && row.missingSymbols.length > 0 && (
                            <div
                              style={{
                                fontSize: "0.74rem",
                                color: "var(--color-text-muted)",
                              }}
                              title={row.missingSymbols.join(", ")}
                            >
                              Missing: {formatMissingSymbols(row.missingSymbols)}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  {dukascopyMinStart ? (
                    <div
                      style={{
                        fontSize: "0.78rem",
                        color: "#bfdbfe",
                      }}
                    >
                      Explicit Start Date must be on or after{" "}
                      <strong>{dukascopyMinStart}</strong> for selected
                      timeframe entries with known Dukascopy coverage.
                    </div>
                  ) : (
                    <div
                      style={{
                        fontSize: "0.78rem",
                        color: "var(--color-text-muted)",
                      }}
                    >
                      No known Dukascopy start dates for the selected
                      instrument/timeframe scope.
                    </div>
                  )}
                </div>
              )}
            {isDukascopy && !startDate && (
              <div
                style={{
                  fontSize: "0.78rem",
                  color: "var(--color-text-muted)",
                }}
              >
                If Start Date is empty, the download will start from the
                earliest known date for each timeframe (or provider default when
                coverage is unknown).
              </div>
            )}
            {error && (
              <div style={{ color: "#fca5a5", fontSize: "0.82rem" }}>
                {error}
              </div>
            )}

            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: "12px",
              }}
            >
              <div
                style={{ fontSize: "0.8rem", color: "var(--color-text-muted)" }}
              >
                Starting the job will hand it off to the global Task Manager.
              </div>
              <div style={{ display: "flex", gap: "10px" }}>
                <button
                  type="button"
                  onClick={onClose}
                  style={{
                    padding: "10px 14px",
                    borderRadius: "12px",
                    border: "1px solid var(--color-border)",
                    background: "transparent",
                    color: "var(--color-text)",
                    cursor: "pointer",
                  }}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleSubmit}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "10px",
                    padding: "10px 16px",
                    borderRadius: "12px",
                    border: "none",
                    background: "linear-gradient(135deg, #2563eb, #0ea5e9)",
                    color: "#eff6ff",
                    cursor: "pointer",
                    fontWeight: 700,
                  }}
                >
                  <Download size={16} /> Start download
                </button>
              </div>
            </div>
          </>
        ) : (
          <>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: "12px",
              }}
            >
              <div
                style={{ display: "flex", alignItems: "center", gap: "10px" }}
              >
                {statusIcon}
                <div>
                  <div
                    style={{
                      fontSize: "0.82rem",
                      textTransform: "uppercase",
                      letterSpacing: "0.16em",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    Task stream
                  </div>
                  <div style={{ fontSize: "1.05rem", fontWeight: 700 }}>
                    Download task is {jobStatus}
                  </div>
                </div>
              </div>
              <div
                style={{
                  fontSize: "0.8rem",
                  color: "var(--color-text-muted)",
                  textAlign: "right",
                }}
              >
                Inputs are locked while the backend job is active.
              </div>
            </div>

            <LiveTerminal jobId={jobId} onStatusChange={setJobStatus} />

            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: "12px",
              }}
            >
              <div
                style={{ fontSize: "0.8rem", color: "var(--color-text-muted)" }}
              >
                You can keep this window open to tail the logs or close it and
                continue elsewhere.
              </div>
              <div style={{ display: "flex", gap: "10px" }}>
                {(jobStatus === "completed" || jobStatus === "failed") && (
                  <button
                    type="button"
                    onClick={resetStream}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "8px",
                      padding: "10px 14px",
                      borderRadius: "12px",
                      border: "1px solid var(--color-border)",
                      background: "transparent",
                      color: "var(--color-text)",
                      cursor: "pointer",
                    }}
                  >
                    <RotateCcw size={15} /> Queue another
                  </button>
                )}
                <button
                  type="button"
                  onClick={onClose}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "8px",
                    padding: "10px 14px",
                    borderRadius: "12px",
                    border: "none",
                    background:
                      "linear-gradient(135deg, rgba(37, 99, 235, 0.9), rgba(14, 165, 233, 0.9))",
                    color: "#eff6ff",
                    cursor: "pointer",
                    fontWeight: 700,
                  }}
                >
                  {jobStatus === "completed" || jobStatus === "failed"
                    ? "Close terminal"
                    : "Hide terminal"}
                  <ArrowRight size={15} />
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

const MergeOptionCard = memo(function MergeOptionCard({
  checked,
  disabled,
  label,
  hint,
  onChange,
}: {
  checked: boolean;
  disabled: boolean;
  label: string;
  hint: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: "12px",
        padding: "14px 16px",
        borderRadius: "16px",
        border: `1px solid ${checked ? "rgba(96, 165, 250, 0.3)" : "rgba(148, 163, 184, 0.18)"}`,
        background: checked
          ? "rgba(37, 99, 235, 0.12)"
          : "rgba(15, 23, 42, 0.58)",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.58 : 1,
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        style={{ marginTop: "2px" }}
      />
      <div style={{ display: "grid", gap: "4px" }}>
        <div style={{ fontWeight: 700, color: "var(--color-text)" }}>
          {label}
        </div>
        <div
          style={{
            fontSize: "0.78rem",
            color: "var(--color-text-muted)",
            lineHeight: 1.6,
          }}
        >
          {hint}
        </div>
      </div>
    </label>
  );
});
