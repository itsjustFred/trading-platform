import { useCallback, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { ArrowRight, RefreshCw, RotateCcw } from "lucide-react";

import { resampleData } from "../api/client";
import { isTerminalTaskStatus } from "../lib/taskStatus";
import { LiveTerminal } from "./LiveTerminal";
import { Modal } from "./Modal";
import { renderTaskStatusIcon } from "./taskStatusIcon";

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
}: Readonly<ResampleModalProps>) {
  const [sourcePath, setSourcePath] = useState(defaultSourcePath);
  const [targetTimeframes, setTargetTimeframes] = useState(
    defaultTargetTimeframes,
  );
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState("idle");
  // Job whose terminal status has already been reported upstream. A ref, not
  // state: the dedupe has to be exact at the instant the callback runs, and a
  // state copy read through the closure can still hold the previous value when
  // two status transitions land in one batch - which would report the same job
  // to the parent twice. Keeping it out of state also means a status line does
  // not re-render this modal; the callback's identity is held by useCallback.
  const notifiedJobIdRef = useRef<string | null>(null);
  const [wasOpen, setWasOpen] = useState(open);

  // Reopening starts a fresh form; adjusting during render beats a
  // setState-in-effect cascade. `notifiedJobIdRef` is deliberately not cleared
  // here - writing a ref during render is unsafe, and it does not need clearing:
  // the dedupe compares it against the *current* job id, which is null until a
  // new job is queued and then a fresh id.
  if (wasOpen !== open) {
    setWasOpen(open);
    if (open) {
      setSourcePath(defaultSourcePath);
      setTargetTimeframes(defaultTargetTimeframes);
      setError("");
      setJobId(null);
      setJobStatus("idle");
    }
  }

  // The task feed reports each status transition once, so the terminal status is
  // forwarded from the callback rather than mirrored by an effect. Every
  // terminal status closes the stream, but only a clean finish or a failure is
  // reported upstream, so a partial run is never counted as a failure.
  //
  // Memoised on the two values it reads, so a status line arriving does not hand
  // the terminal a new callback identity for a job that has not changed.
  const handleStatusChange = useCallback(
    (status: string) => {
      setJobStatus(status);
      if (
        !jobId ||
        notifiedJobIdRef.current === jobId ||
        !isTerminalTaskStatus(status)
      ) {
        return;
      }
      notifiedJobIdRef.current = jobId;
      if (status === "completed" || status === "failed") {
        onTaskFinished?.(status, jobId);
      }
    },
    [jobId, onTaskFinished],
  );

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
    notifiedJobIdRef.current = null;
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
      notifiedJobIdRef.current = null;
      onTaskStarted?.(res.task_id);
    } catch (err: unknown) {
      setError(
        err instanceof Error && err.message
          ? err.message
          : "Failed to start resample task.",
      );
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
                Keep this open if you want to tail the job output to completion.
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
