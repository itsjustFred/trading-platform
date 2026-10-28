import { useState } from "react";
import type { CSSProperties } from "react";
import { ArrowRight, RotateCcw, Sparkles } from "lucide-react";

import { harvestData } from "../api/client";
import { isTerminalTaskStatus } from "../lib/taskStatus";
import { LiveTerminal } from "./LiveTerminal";
import { Modal } from "./Modal";
import { renderTaskStatusIcon } from "./taskStatusIcon";

interface SmartHarvestModalProps {
  open: boolean;
  onClose: () => void;
  onTaskStarted?: (id: string) => void;
  onTaskFinished?: (status: "completed" | "failed", id: string) => void;
  defaultSource?: string;
  defaultSymbol?: string;
  defaultAssetClass?: string;
  defaultStartDate?: string;
}

export function SmartHarvestModal({
  open,
  onClose,
  onTaskStarted,
  onTaskFinished,
  defaultSource = "dukascopy",
  defaultSymbol = "",
  defaultAssetClass = "",
  defaultStartDate = "",
}: Readonly<SmartHarvestModalProps>) {
  const [source, setSource] = useState(defaultSource || "dukascopy");
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [assetClass, setAssetClass] = useState(defaultAssetClass);
  const [startDate, setStartDate] = useState(defaultStartDate);
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState("idle");
  // Job whose terminal status has already been reported upstream.
  const [notifiedJobId, setNotifiedJobId] = useState<string | null>(null);
  const [wasOpen, setWasOpen] = useState(open);

  // Reopening starts a fresh form; adjusting during render beats a
  // setState-in-effect cascade.
  if (wasOpen !== open) {
    setWasOpen(open);
    if (open) {
      setSource(defaultSource || "dukascopy");
      setSymbol(defaultSymbol);
      setAssetClass(defaultAssetClass);
      setStartDate(defaultStartDate);
      setError("");
      setJobId(null);
      setJobStatus("idle");
      setNotifiedJobId(null);
    }
  }

  // The task feed reports each status transition once, so the terminal status is
  // forwarded from the callback rather than mirrored by an effect. Every
  // terminal status closes the stream, but only a clean finish or a failure is
  // reported upstream, so a partial run is never counted as a failure.
  const handleStatusChange = (status: string) => {
    setJobStatus(status);
    if (!jobId || notifiedJobId === jobId || !isTerminalTaskStatus(status)) {
      return;
    }
    setNotifiedJobId(jobId);
    if (status === "completed" || status === "failed") {
      onTaskFinished?.(status, jobId);
    }
  };

  const inputStyle: CSSProperties = {
    background: "rgba(15, 23, 42, 0.75)",
    border: "1px solid rgba(148, 163, 184, 0.18)",
    color: "var(--color-text)",
    padding: "11px 12px",
    borderRadius: "12px",
    width: "100%",
  };

  const resetStream = () => {
    setJobId(null);
    setJobStatus("idle");
    setNotifiedJobId(null);
    setError("");
  };

  const handleSubmit = async () => {
    setError("");
    const normalizedSymbol = symbol.trim().toUpperCase();
    const normalizedSource = source.trim().toLowerCase();
    if (!normalizedSymbol) {
      setError("Please provide a symbol (e.g. XAUUSD).");
      return;
    }

    try {
      const res = await harvestData({
        source: normalizedSource || "dukascopy",
        symbol: normalizedSymbol,
        asset_class: assetClass.trim() || undefined,
        start: startDate || undefined,
      });
      setJobId(res.task_id);
      setJobStatus(res.status || "starting");
      setNotifiedJobId(null);
      onTaskStarted?.(res.task_id);
    } catch (err: unknown) {
      setError(
        err instanceof Error && err.message
          ? err.message
          : "Failed to start Smart Harvest task.",
      );
    }
  };

  const statusIcon = renderTaskStatusIcon(jobStatus);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Smart Harvest (Auto-Tune)"
      width={jobId ? "860px" : "740px"}
    >
      <div style={{ display: "grid", gap: "18px" }}>
        {!jobId ? (
          <>
            <div
              style={{
                display: "grid",
                gap: "14px",
                padding: "18px",
                borderRadius: "18px",
                border: "1px solid rgba(56, 189, 248, 0.14)",
                background:
                  "linear-gradient(135deg, rgba(15, 23, 42, 0.86), rgba(17, 24, 39, 0.94))",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: "12px",
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
                    Adaptive tuner
                  </div>
                  <div style={{ fontSize: "1.18rem", fontWeight: 700 }}>
                    Auto-tuned Dukascopy harvesting with live telemetry
                  </div>
                </div>
                <Sparkles size={20} color="#7dd3fc" />
              </div>

              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: "12px",
                }}
              >
                <div style={{ display: "grid", gap: "8px" }}>
                  <label
                    htmlFor="smart-harvest-source"
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
                    id="smart-harvest-source"
                    value={source}
                    onChange={(event) => setSource(event.target.value)}
                    style={inputStyle}
                  >
                    <option value="dukascopy">dukascopy</option>
                  </select>
                </div>

                <div style={{ display: "grid", gap: "8px" }}>
                  <label
                    htmlFor="smart-harvest-symbol"
                    style={{
                      fontSize: "0.76rem",
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    Symbol
                  </label>
                  <input
                    id="smart-harvest-symbol"
                    value={symbol}
                    onChange={(event) =>
                      setSymbol(event.target.value.toUpperCase())
                    }
                    placeholder="XAUUSD"
                    style={inputStyle}
                  />
                </div>
              </div>

              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: "12px",
                }}
              >
                <div style={{ display: "grid", gap: "8px" }}>
                  <label
                    htmlFor="smart-harvest-asset-class"
                    style={{
                      fontSize: "0.76rem",
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    Asset Class
                  </label>
                  <input
                    id="smart-harvest-asset-class"
                    value={assetClass}
                    onChange={(event) =>
                      setAssetClass(event.target.value.toLowerCase())
                    }
                    placeholder="metals / forex / indices ..."
                    style={inputStyle}
                  />
                </div>

                <div style={{ display: "grid", gap: "8px" }}>
                  <label
                    htmlFor="smart-harvest-start-date"
                    style={{
                      fontSize: "0.76rem",
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    Start Date (optional)
                  </label>
                  <input
                    id="smart-harvest-start-date"
                    type="date"
                    value={startDate}
                    onChange={(event) => setStartDate(event.target.value)}
                    style={inputStyle}
                  />
                </div>
              </div>
            </div>

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
                Streams PID tuner logs live (batch size, delay, throughput,
                penalties).
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
                    background: "linear-gradient(135deg, #0f766e, #0891b2)",
                    color: "#ecfeff",
                    cursor: "pointer",
                    fontWeight: 700,
                  }}
                >
                  <Sparkles size={16} /> Start Smart Harvest
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
                    Smart Harvest stream
                  </div>
                  <div style={{ fontSize: "1.05rem", fontWeight: 700 }}>
                    Harvester job is {jobStatus}
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
                Auto-tuning output is streamed from `task_&lt;job_id&gt;.log`.
              </div>
            </div>

            <LiveTerminal jobId={jobId} onStatusChange={handleStatusChange} />

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
                Keep this modal open to watch tuning cycles and final
                persistence status.
              </div>
              <div style={{ display: "flex", gap: "10px" }}>
                {isTerminalTaskStatus(jobStatus) && (
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
                    <RotateCcw size={15} /> Run another
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
                      "linear-gradient(135deg, rgba(15, 118, 110, 0.9), rgba(8, 145, 178, 0.9))",
                    color: "#ecfeff",
                    cursor: "pointer",
                    fontWeight: 700,
                  }}
                >
                  {isTerminalTaskStatus(jobStatus)
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
