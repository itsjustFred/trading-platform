import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import {
  ArrowRight,
  CheckCircle2,
  Loader2,
  RotateCcw,
  Sparkles,
  XCircle,
} from "lucide-react";

import { harvestData } from "../api/client";
import { LiveTerminal } from "./LiveTerminal";
import { Modal } from "./Modal";

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
}: SmartHarvestModalProps) {
  const [source, setSource] = useState(defaultSource || "dukascopy");
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [assetClass, setAssetClass] = useState(defaultAssetClass);
  const [startDate, setStartDate] = useState(defaultStartDate);
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState("idle");
  const [completionNotified, setCompletionNotified] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) {
      return;
    }
    setSource(defaultSource || "dukascopy");
    setSymbol(defaultSymbol);
    setAssetClass(defaultAssetClass);
    setStartDate(defaultStartDate);
    setError("");
    setJobId(null);
    setJobStatus("idle");
    setCompletionNotified(false);
    setSubmitting(false);
  }, [defaultAssetClass, defaultSource, defaultStartDate, defaultSymbol, open]);

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

  const resetStream = () => {
    setJobId(null);
    setJobStatus("idle");
    setCompletionNotified(false);
    setError("");
    setSubmitting(false);
  };

  const handleSubmit = async () => {
    if (submitting) {
      return;
    }
    setError("");
    const normalizedSymbol = symbol.trim().toUpperCase();
    const normalizedSource = source.trim().toLowerCase();
    if (!normalizedSymbol) {
      setError("Please provide a symbol (e.g. XAUUSD).");
      return;
    }

    try {
      setSubmitting(true);
      const res = await harvestData({
        source: normalizedSource || "dukascopy",
        symbol: normalizedSymbol,
        asset_class: assetClass.trim() || undefined,
        start: startDate || undefined,
      });
      setJobId(res.task_id);
      setJobStatus(res.status || "starting");
      setCompletionNotified(false);
      onTaskStarted?.(res.task_id);
    } catch (err: any) {
      setError(err.message || "Failed to start Smart Harvest task.");
    } finally {
      setSubmitting(false);
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

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
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
                    onChange={(event) => setSource(event.target.value)}
                    style={inputStyle}
                  >
                    <option value="dukascopy">dukascopy</option>
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
                    Symbol
                  </label>
                  <input
                    value={symbol}
                    onChange={(event) => setSymbol(event.target.value.toUpperCase())}
                    placeholder="XAUUSD"
                    style={inputStyle}
                  />
                </div>
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
                <div style={{ display: "grid", gap: "8px" }}>
                  <label
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
                    value={assetClass}
                    onChange={(event) => setAssetClass(event.target.value.toLowerCase())}
                    placeholder="metals / forex / indices ..."
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
                    Start Date (optional)
                  </label>
                  <input
                    type="date"
                    value={startDate}
                    onChange={(event) => setStartDate(event.target.value)}
                    style={inputStyle}
                  />
                </div>
              </div>
            </div>

            {error && <div style={{ color: "#fca5a5", fontSize: "0.82rem" }}>{error}</div>}

            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: "12px",
              }}
            >
              <div style={{ fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
                Streams PID tuner logs live (batch size, delay, throughput, penalties).
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
                  disabled={submitting}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "10px",
                    padding: "10px 16px",
                    borderRadius: "12px",
                    border: "none",
                    background: "linear-gradient(135deg, #0f766e, #0891b2)",
                    color: "#ecfeff",
                    cursor: submitting ? "wait" : "pointer",
                    fontWeight: 700,
                    opacity: submitting ? 0.72 : 1,
                  }}
                >
                  {submitting ? (
                    <Loader2 size={16} className="animate-spin" />
                  ) : (
                    <Sparkles size={16} />
                  )}
                  Start Smart Harvest
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
              <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
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

            <LiveTerminal jobId={jobId} onStatusChange={setJobStatus} />

            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: "12px",
              }}
            >
              <div style={{ fontSize: "0.8rem", color: "var(--color-text-muted)" }}>
                Keep this modal open to watch tuning cycles and final persistence status.
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
