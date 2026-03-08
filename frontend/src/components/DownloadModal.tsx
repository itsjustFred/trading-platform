import { useEffect, useMemo, useState } from "react";
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
import { LiveTerminal } from "./LiveTerminal";
import { Modal } from "./Modal";

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
  defaultTimeframes = ["d1"],
}: DownloadModalProps) {
  const normalizedDefaultTimeframes = useMemo(
    () =>
      (defaultTimeframes.length > 0 ? defaultTimeframes : ["d1"]).map(
        normalizeGenesisTimeframe,
      ),
    [defaultTimeframes],
  );
  const [source, setSource] = useState(defaultSource);
  const [symbols, setSymbols] = useState(defaultSymbols);
  const [selectedTimeframes, setSelectedTimeframes] = useState<string[]>(
    normalizedDefaultTimeframes,
  );
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [priceType, setPriceType] = useState<"bid" | "ask">("bid");
  const [mergeTick, setMergeTick] = useState(false);
  const [mergeS1, setMergeS1] = useState(false);
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState("idle");
  const [completionNotified, setCompletionNotified] = useState(false);
  const [dukascopyGenesis, setDukascopyGenesis] = useState<
    Record<string, Record<string, string>>
  >({});

  const isDukascopy = source === "dukascopy";
  const isStreaming = jobId !== null;
  const defaultTimeframesKey = normalizedDefaultTimeframes.join("|");
  const parsedSymbols = useMemo(
    () =>
      symbols
        .split(",")
        .map((symbol) => symbol.trim().toUpperCase())
        .filter(Boolean),
    [symbols],
  );

  const dukascopyMinStart = useMemo(() => {
    if (parsedSymbols.length === 0) {
      return "";
    }

    const requestedTimeframes = selectedTimeframes.map(
      normalizeGenesisTimeframe,
    );
    const knownDates: string[] = [];

    for (const symbol of parsedSymbols) {
      const symbolGenesis = dukascopyGenesis[symbol];
      if (!symbolGenesis) {
        return "";
      }

      for (const timeframe of requestedTimeframes) {
        const genesis = symbolGenesis[timeframe] ?? symbolGenesis.default;
        if (!genesis) {
          return "";
        }
        knownDates.push(genesis);
      }
    }

    if (knownDates.length === 0) {
      return "";
    }
    return knownDates.reduce((latest, current) =>
      current > latest ? current : latest,
    );
  }, [dukascopyGenesis, parsedSymbols, selectedTimeframes]);

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
    setError("");
    setJobId(null);
    setJobStatus("idle");
    setCompletionNotified(false);
  }, [
    defaultSource,
    defaultSymbols,
    defaultTimeframesKey,
    normalizedDefaultTimeframes,
    open,
  ]);

  useEffect(() => {
    if (!open || !isDukascopy || isStreaming) {
      return;
    }

    let cancelled = false;
    getDukascopyGenesis(parsedSymbols, selectedTimeframes)
      .then((res) => {
        if (!cancelled) {
          setDukascopyGenesis(res.genesis || {});
        }
      })
      .catch(() => {
        if (!cancelled) {
          setDukascopyGenesis({});
        }
      });

    return () => {
      cancelled = true;
    };
  }, [isDukascopy, isStreaming, open, parsedSymbols, selectedTimeframes]);

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
        `Explicit start date must be on or after ${dukascopyMinStart} for the selected instruments and timeframes.`,
      );
      return;
    }

    try {
      const res = await downloadData({
        source,
        symbols,
        timeframes: selectedTimeframes.join(","),
        start: startDate || undefined,
        end: endDate || undefined,
        price_type: isDukascopy ? priceType : undefined,
        merge_tick: isDukascopy ? mergeTick : undefined,
        merge_s1: isDukascopy ? mergeS1 : undefined,
      });
      setJobId(res.task_id);
      setJobStatus(res.status || "starting");
      setCompletionNotified(false);
      onTaskStarted?.(res.task_id);
    } catch (err: any) {
      setError(err.message || "Failed to start download.");
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
                    <option value="yfinance">Yahoo Finance</option>
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

                  {isDukascopy && (
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
                        {(["bid", "ask"] as const).map((option) => {
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

            {isDukascopy && dukascopyMinStart && (
              <div
                style={{
                  padding: "12px 14px",
                  borderRadius: "14px",
                  background: "rgba(37, 99, 235, 0.12)",
                  border: "1px solid rgba(96, 165, 250, 0.18)",
                  fontSize: "0.82rem",
                  color: "#bfdbfe",
                }}
              >
                Explicit Start Date must be on or after{" "}
                <strong>{dukascopyMinStart}</strong> for the selected
                instruments and timeframes.
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
                earliest available date for each timeframe.
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
                The modal will switch into a live terminal as soon as the task
                is queued.
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

function MergeOptionCard({
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
}
