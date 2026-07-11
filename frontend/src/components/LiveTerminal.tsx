import { useEffect, useRef, useState } from "react";
import { TerminalSquare } from "lucide-react";

interface LiveTerminalProps {
  jobId: string | null;
  onStatusChange?: (status: string) => void;
  emptyMessage?: string;
}

export function LiveTerminal({
  jobId,
  onStatusChange,
  emptyMessage = "Waiting for log output...",
}: LiveTerminalProps) {
  const [logs, setLogs] = useState("");
  const [status, setStatus] = useState("idle");
  const viewportRef = useRef<HTMLDivElement>(null);

  const onStatusChangeRef = useRef(onStatusChange);

  useEffect(() => {
    onStatusChangeRef.current = onStatusChange;
  }, [onStatusChange]);

  useEffect(() => {
    if (!jobId) {
      setLogs("");
      setStatus("idle");
      return;
    }

    setLogs("");
    setStatus("connecting");
    onStatusChangeRef.current?.("connecting");

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(
      `${protocol}//${window.location.host}/ws/tasks/${encodeURIComponent(jobId)}`,
    );

    ws.onopen = () => {
      setStatus("connected");
      onStatusChangeRef.current?.("connected");
    };

    ws.onmessage = (event) => {
      const line = String(event.data ?? "");
      setLogs((prev) => (prev ? `${prev}\n${line}` : line));

      const statusMatch = line.match(/\[task:[^\]]+\]\s+status=(\w+)/i);
      if (statusMatch?.[1]) {
        const nextStatus = statusMatch[1].toLowerCase();
        setStatus(nextStatus);
        onStatusChangeRef.current?.(nextStatus);
      }
    };

    ws.onerror = () => {
      setStatus("disconnected");
      onStatusChangeRef.current?.("disconnected");
    };

    ws.onclose = () => {
      setStatus((prev) =>
        prev === "completed" || prev === "failed" || prev === "cancelled"
          ? prev
          : "disconnected",
      );
      onStatusChangeRef.current?.("disconnected");
    };

    return () => {
      ws.close();
    };
  }, [jobId]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) {
      return;
    }
    viewport.scrollTop = viewport.scrollHeight;
  }, [logs]);

  const statusTone =
    status === "completed"
      ? "#86efac"
      : status === "failed" || status === "disconnected"
        ? "#fda4af"
        : "#67e8f9";

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
