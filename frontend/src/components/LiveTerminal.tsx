import { useEffect, useMemo, useRef, useState } from "react";
import { TerminalSquare } from "lucide-react";

import { buildWebSocketUrl, useWebSocket, type WebSocketStatus } from "../hooks/useWebSocket";

interface LiveTerminalProps {
  jobId: string | null;
  onStatusChange?: (status: string) => void;
  emptyMessage?: string;
}

/** Log text parsed out of the task feed: `[task:<id>] status=<word>`. */
const TASK_STATUS_LINE = /\[task:[^\]]+\]\s+status=(\w+)/i;
const TERMINAL_TASK_STATUSES = new Set(["completed", "failed", "cancelled"]);

function deriveFeedStatus(
  wsStatus: WebSocketStatus,
  taskStatus: string | null,
): string {
  if (taskStatus && TERMINAL_TASK_STATUSES.has(taskStatus)) {
    return taskStatus;
  }
  switch (wsStatus) {
    case "connected":
      return "connected";
    case "connecting":
    case "reconnecting":
      return "connecting";
    case "disconnected":
    case "error":
      return "disconnected";
    default:
      return taskStatus ?? "idle";
  }
}

export function LiveTerminal({
  jobId,
  onStatusChange,
  emptyMessage = "Waiting for log output...",
}: LiveTerminalProps) {
  // Raw log text; reset when the job changes so a feed never shows another job's output.
  const [logs, setLogs] = useState("");
  // Terminal status parsed from the feed itself (completed/failed/cancelled); the
  // connection status is derived from the shared hook instead of mirrored in state.
  const [taskStatus, setTaskStatus] = useState<string | null>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const onStatusChangeRef = useRef(onStatusChange);
  const reportedStatusRef = useRef<string | null>(null);

  useEffect(() => {
    onStatusChangeRef.current = onStatusChange;
  }, [onStatusChange]);

  const wsUrl = useMemo(
    () => (jobId ? buildWebSocketUrl(`/ws/tasks/${jobId}`) : null),
    [jobId],
  );

  // The server closes the socket once the task reaches a terminal status; without
  // this gate the hook would reconnect forever and onOpen's reset would flap the
  // reported status between "completed" and "connecting".
  const taskFinished = taskStatus != null && TERMINAL_TASK_STATUSES.has(taskStatus);

  const stream = useWebSocket<string>(wsUrl, {
    enabled: Boolean(wsUrl) && !taskFinished,
    messageFormat: "text",
    // The task feed is plain text: the server never sends ping/pong frames and
    // stays silent while a job is idle, so the JSON-feed heartbeat would tear
    // the socket down and replay the log from position 0 on every reconnect.
    heartbeatIntervalMs: 0,
    onOpen: () => {
      // Fresh connection: the server replays the log from position 0, so start
      // from a clean slate to avoid duplicated lines.
      setLogs("");
      setTaskStatus(null);
    },
    onMessage: (line) => {
      setLogs((prev) => (prev ? `${prev}\n${line}` : line));

      const statusMatch = line.match(TASK_STATUS_LINE);
      if (statusMatch?.[1]) {
        setTaskStatus(statusMatch[1].toLowerCase());
      }
    },
  });

  const status = deriveFeedStatus(stream.status, taskStatus);

  // Report status transitions to the parent (stable callers only; ref-guarded).
  useEffect(() => {
    if (reportedStatusRef.current === status) {
      return;
    }
    reportedStatusRef.current = status;
    onStatusChangeRef.current?.(status);
  }, [status]);

  // Runs on mount, when the job is cleared (null), and whenever the job id
  // changes. A direct switch A→B must reset the terminal state: a stale
  // terminal status from job A would keep the finished-gate closed and the
  // feed for job B would never be opened.
  useEffect(() => {
    // Deferred: clearing during the same commit must not retain previous output.
    const task = window.setTimeout(() => {
      setLogs("");
      setTaskStatus(null);
    }, 0);
    return () => {
      window.clearTimeout(task);
    };
  }, [jobId]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) {
      return;
    }
    viewport.scrollTop = viewport.scrollHeight;
  }, [logs]);

  const statusTone = TERMINAL_STATUS_TONES[status] ?? "#67e8f9";

  return (
    <div
      style={{
        borderRadius: "16px",
        border: "1px solid rgba(74, 222, 128, 0.16)",
        background:
          "linear-gradient(180deg, rgba(2, 8, 23, 0.96) 0%, rgba(3, 7, 18, 0.98) 100%)",
        boxShadow:
          "0 24px 70px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(134, 239, 172, 0.06)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "12px",
          padding: "12px 16px",
          borderBottom: "1px solid rgba(74, 222, 128, 0.14)",
          background:
            "linear-gradient(90deg, rgba(21, 128, 61, 0.16) 0%, rgba(2, 8, 23, 0.6) 100%)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              width: "32px",
              height: "32px",
              borderRadius: "10px",
              background: "rgba(22, 163, 74, 0.16)",
              color: "#86efac",
            }}
          >
            <TerminalSquare size={16} />
          </div>
          <div>
            <div
              style={{
                fontSize: "0.72rem",
                letterSpacing: "0.16em",
                textTransform: "uppercase",
                color: "#86efac",
              }}
            >
              Live Task Feed
            </div>
            <div
              style={{
                fontSize: "0.76rem",
                color: "rgba(167, 243, 208, 0.82)",
              }}
            >
              job {jobId ?? "n/a"}
            </div>
          </div>
        </div>
        <div
          style={{
            fontFamily:
              "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            fontSize: "0.74rem",
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: statusTone,
          }}
        >
          {status}
        </div>
      </div>

      <div
        ref={viewportRef}
        className="font-mono text-green-400 bg-gray-900 p-4 rounded overflow-y-auto max-h-96"
        style={{
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          minHeight: "220px",
          backgroundImage:
            "linear-gradient(180deg, rgba(16, 185, 129, 0.04) 0%, rgba(0, 0, 0, 0) 100%), repeating-linear-gradient(180deg, rgba(255, 255, 255, 0.03) 0, rgba(255, 255, 255, 0.03) 1px, transparent 1px, transparent 24px)",
        }}
      >
        {jobId ? logs || emptyMessage : emptyMessage}
      </div>
    </div>
  );
}
