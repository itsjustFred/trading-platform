import { useEffect, useState } from "react";
import type { CSSProperties, ReactElement } from "react";
import {
  ArrowRight,
  CheckCircle2,
  Loader2,
  RefreshCw,
  RotateCcw,
  XCircle,
} from "lucide-react";

import { resampleData } from "../api/client";
import { LiveTerminal } from "./LiveTerminal";
import { Modal } from "./Modal";

const STATUS_ICON_COLORS = {
  ok: "#86efac",
  failed: "#fda4af",
  running: "#67e8f9",
} as const;

/** Shared task-status glyph used by the data-task modals. */
export function renderTaskStatusIcon(
  status: string | null | undefined,
  size = 18,
): ReactElement {
  if (status === "completed") {
    return <CheckCircle2 size={size} color={STATUS_ICON_COLORS.ok} />;
  }
  if (status === "failed") {
    return <XCircle size={size} color={STATUS_ICON_COLORS.failed} />;
  }
  return <Loader2 size={size} className="animate-spin" color={STATUS_ICON_COLORS.running} />;
}

interface ResampleModalProps {
  open: boolean;
  onClose: () => void;
  onTaskStarted?: (id: string) => void;
  onTaskFinished?: (status: "completed" | "failed", id: string) => void;
  defaultSourcePath?: string;
  defaultTargetTimeframes?: string;
}

export function ResampleModal({
  open,
  onClose,
  onTaskStarted,
  onTaskFinished,
  defaultSourcePath = "",
  defaultTargetTimeframes = "5m,15m,30m,1h,4h",
}: ResampleModalProps) {
  const [sourcePath, setSourcePath] = useState(defaultSourcePath);
  const [targetTimeframes, setTargetTimeframes] = useState(
    defaultTargetTimeframes,
  );
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState("idle");
  const [completionNotified, setCompletionNotified] = useState(false);

  useEffect(() => {
    if (!open) {
      return;
    }
    setSourcePath(defaultSourcePath);
    setTargetTimeframes(defaultTargetTimeframes);
    setError("");
    setJobId(null);
    setJobStatus("idle");
    setCompletionNotified(false);
  }, [defaultSourcePath, defaultTargetTimeframes, open]);

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
  };

  const handleSubmit = async () => {
    setError("");
    if (!sourcePath.trim()) {
      setError("Please provide the source parquet path to resample.");
      return;
    }

    try {
      const res = await resampleData({
        source_path: sourcePath,
        target_timeframes: targetTimeframes,
      });
      setJobId(res.task_id);
      setJobStatus(res.status || "starting");
      setCompletionNotified(false);
      onTaskStarted?.(res.task_id);
    } catch (err: any) {
      setError(err.message || "Failed to start resample task.");
    }
  };

  const statusIcon = renderTaskStatusIcon(jobStatus);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Resample Data"
      width={jobId ? "840px" : "720px"}
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
                    Timeframe synthesis
                  </div>
                  <div style={{ fontSize: "1.18rem", fontWeight: 700 }}>
                    Generate higher-level bars from raw parquet data
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
                  Feed a canonical source file, then stream the backend task
                  output directly inside the modal.
                </div>
              </div>

              <div style={{ display: "grid", gap: "8px" }}>
                <label
                  htmlFor="resample-source-path"
                  style={{
                    fontSize: "0.76rem",
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    color: "var(--color-text-muted)",
                  }}
                >
                  Source parquet path
                </label>
                <input
                  id="resample-source-path"
                  value={sourcePath}
                  onChange={(event) => setSourcePath(event.target.value)}
                  placeholder="/home/ubuntu/data/ohlcv/.../1m.parquet"
                  style={inputStyle}
                />
                <div
                  style={{
                    fontSize: "0.76rem",
                    color: "var(--color-text-muted)",
                  }}
                >
                  Use the highest-granularity file you already have for this
                  symbol.
                </div>
              </div>

              <div style={{ display: "grid", gap: "8px" }}>
                <label
                  htmlFor="resample-target-timeframes"
                  style={{
                    fontSize: "0.76rem",
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    color: "var(--color-text-muted)",
                  }}
                >
                  Target timeframes
                </label>
                <input
                  id="resample-target-timeframes"
                  value={targetTimeframes}
                  onChange={(event) => setTargetTimeframes(event.target.value)}
                  placeholder="5m,15m,30m,1h,4h"
                  style={inputStyle}
                />
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
                Once queued, the form collapses into a live terminal session.
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
                  <RefreshCw size={16} /> Start resample
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
                    Resample stream
                  </div>
                  <div style={{ fontSize: "1.05rem", fontWeight: 700 }}>
                    Resample job is {jobStatus}
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
                The terminal below follows the backend log file in real time.
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
                Keep this open if you want to tail the job output to completion.
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
