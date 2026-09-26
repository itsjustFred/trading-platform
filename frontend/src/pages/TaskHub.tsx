import { memo, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { Link } from "react-router-dom";
import { Activity, ArrowRight, Pause, RefreshCw, Square } from "lucide-react";

import {
  cancelDataTask,
  listTasks,
  pauseDataTask,
  restartDataTask,
} from "../api/client";
import type { DataTaskStatusResponse } from "../api/types";
import { useToast } from "../components/ToastProvider";
import {
  isActiveTaskStatus,
  isFailedTaskStatus,
  isTerminalTaskStatus,
  normalizeTaskStatus,
} from "../lib/taskStatus";

const TASK_POLL_MS = 3000;
const TERMINAL_TASK_LIMIT = 100;

function formatDuration(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.floor(seconds);
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m > 0) return `${m}m ${rem}s`;
  return `${rem}s`;
}

function taskPercent(task: DataTaskStatusResponse) {
  const primary = task.progress_percent;
  const fallback = task.progress?.percent;
  let value = 0;
  if (typeof primary === "number") {
    value = primary;
  } else if (typeof fallback === "number") {
    value = fallback;
  }
  return Math.max(0, Math.min(100, value));
}

function taskMessage(task: DataTaskStatusResponse) {
  return (
    task.progress_message?.trim() ||
    task.progress?.current?.trim() ||
    task.description?.trim() ||
    "Waiting for task output..."
  );
}

function categoryIcon(category?: DataTaskStatusResponse["category"]) {
  if (category === "backtest") return "📊";
  if (category === "optimization") return "⚡";
  if (category === "resample") return "📊";
  return "📥";
}

function byNewest(left: DataTaskStatusResponse, right: DataTaskStatusResponse) {
  const leftStamp =
    Date.parse(
      left.completed_at ||
        left.finished_at ||
        left.updated_at ||
        left.created_at ||
        "",
    ) || 0;
  const rightStamp =
    Date.parse(
      right.completed_at ||
        right.finished_at ||
        right.updated_at ||
        right.created_at ||
        "",
    ) || 0;
  return rightStamp - leftStamp;
}

function cardStyle(): CSSProperties {
  return {
    borderRadius: "16px",
    border: "1px solid rgba(148, 163, 184, 0.18)",
    background:
      "linear-gradient(180deg, rgba(15, 23, 42, 0.94), rgba(2, 6, 23, 0.94))",
    padding: "16px",
  };
}

export function TaskHubPage() {
  const { showToast } = useToast();
  const [tasks, setTasks] = useState<DataTaskStatusResponse[]>([]);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const statusRef = useRef<Record<string, string>>({});

  useEffect(() => {
    let active = true;
    let id = 0;

    const poll = async () => {
      try {
        const payload = await listTasks();
        if (!active) return;
        setTasks(payload.tasks || []);
      } catch {
        if (!active) return;
      } finally {
        if (active) setLoading(false);
      }
    };

    void poll();
    id = window.setInterval(() => {
      void poll();
    }, TASK_POLL_MS);

    return () => {
      active = false;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    const previous = statusRef.current;
    const next: Record<string, string> = {};
    for (const task of tasks) {
      const id = task.id || "";
      if (!id) continue;
      const status = normalizeTaskStatus(task.status);
      next[id] = status;
      if (!previous[id] || previous[id] === status) continue;
      if (status === "completed") {
        showToast(`${task.name || "Task"} completed`, "success");
      } else if (isFailedTaskStatus(status)) {
        showToast(
          `${task.name || "Task"} failed: ${task.error_message || task.error || taskMessage(task)}`,
          "error",
        );
      } else if (status === "cleanup_pending") {
        showToast(
          `${task.name || "Task"} cleanup is pending: ${task.progress_message || taskMessage(task)}`,
          "warning",
        );
      }
    }
    statusRef.current = next;
  }, [tasks, showToast]);

  const activeTasks = useMemo(
    () =>
      tasks
        .filter((task) => isActiveTaskStatus(task.status))
        .sort(byNewest),
    [tasks],
  );
  const terminalTasks = useMemo(
    () =>
      tasks
        .filter((task) => isTerminalTaskStatus(task.status))
        .sort(byNewest)
        .slice(0, TERMINAL_TASK_LIMIT),
    [tasks],
  );

  const summary = useMemo(() => {
    const counts = { running: 0, queued: 0, completed: 0, failed: 0 };
    for (const task of tasks) {
      const status = normalizeTaskStatus(task.status);
      if (status === "queued") counts.queued += 1;
      else if (isActiveTaskStatus(status)) counts.running += 1;
      if (status === "completed") counts.completed += 1;
      if (isFailedTaskStatus(status)) counts.failed += 1;
    }
    return counts;
  }, [tasks]);

  const runAction = async (
    taskId: string,
    action: () => Promise<DataTaskStatusResponse>,
    success: string,
  ) => {
    setBusy((current) => ({ ...current, [taskId]: true }));
    try {
      const updated = await action();
      setTasks((current) =>
        current.map((task) => (task.id === updated.id ? updated : task)),
      );
      showToast(success, "info");
    } catch (error: unknown) {
      const message =
        error instanceof Error ? error.message : "Task action failed.";
      showToast(message, "error");
    } finally {
      setBusy((current) => ({ ...current, [taskId]: false }));
    }
  };

  return (
    <div
      style={{
        minHeight: "100%",
        padding: "24px",
        display: "grid",
        gap: "18px",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "14px",
        }}
      >
        <div>
          <h1 style={{ fontSize: "1.6rem", fontWeight: 700 }}>Task Hub</h1>
          <div style={{ color: "var(--color-text-muted)" }}>
            Background tasks overview with progress, ETA and controls.
          </div>
        </div>
        <Link
          to="/data"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "8px",
            padding: "10px 12px",
            borderRadius: "10px",
            border: "1px solid rgba(148, 163, 184, 0.2)",
            color: "var(--color-text)",
            textDecoration: "none",
            background: "rgba(15, 23, 42, 0.85)",
          }}
        >
          Data Management
          <ArrowRight size={14} />
        </Link>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
          gap: "10px",
        }}
      >
        <SummaryCard label="Running" value={summary.running} />
        <SummaryCard label="Queued" value={summary.queued} />
        <SummaryCard label="Completed" value={summary.completed} />
        <SummaryCard label="Failed" value={summary.failed} failed />
      </div>

      <section style={{ display: "grid", gap: "10px" }}>
        <div style={{ fontWeight: 700, fontSize: "1.03rem" }}>Active Tasks</div>
        {loading && <div style={cardStyle()}>Loading tasks...</div>}
        {!loading && activeTasks.length === 0 && (
          <div style={cardStyle()}>No active tasks.</div>
        )}
        {!loading &&
          activeTasks.length > 0 &&
          activeTasks.map((task) => {
            const taskId = task.id || "";
            const status = normalizeTaskStatus(task.status);
            const percent = taskPercent(task);
            const isBusy = Boolean(taskId && busy[taskId]);
            return (
              <div
                key={task.id || `${task.name}-${task.created_at}`}
                style={cardStyle()}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: "12px",
                    alignItems: "start",
                  }}
                >
                  <div>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "8px",
                      }}
                    >
                      <span style={{ fontSize: "1.2rem" }}>
                        {categoryIcon(task.category)}
                      </span>
                      <strong>{task.name || "Task"}</strong>
                    </div>
                    <div
                      style={{
                        marginTop: "6px",
                        color: "var(--color-text-muted)",
                      }}
                    >
                      {percent.toFixed(0)}% · ETA:{" "}
                      {formatDuration(task.eta_seconds)}
                    </div>
                    {task.provider ? (
                      <div
                        style={{
                          marginTop: "6px",
                          color: "var(--color-text-muted)",
                          fontSize: "0.86rem",
                        }}
                      >
                        provider={task.provider}
                        {task.model ? ` · model=${task.model}` : ""}
                      </div>
                    ) : null}
                    <div
                      style={{
                        marginTop: "6px",
                        fontSize: "0.92rem",
                        color: "var(--color-text-muted)",
                      }}
                    >
                      {taskMessage(task)}
                    </div>
                  </div>

                  <div
                    style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}
                  >
                    {task.can_pause ? (
                      <button
                        type="button"
                        disabled={isBusy || !taskId}
                        onClick={() =>
                          taskId &&
                          void runAction(
                            taskId,
                            () => pauseDataTask(taskId),
                            "Task paused.",
                          )
                        }
                        style={buttonStyle("#fde68a")}
                      >
                        <Pause size={14} /> Pause
                      </button>
                    ) : null}
                    {task.can_cancel ? (
                      <button
                        type="button"
                        disabled={isBusy || !taskId}
                        onClick={() =>
                          taskId &&
                          void runAction(
                            taskId,
                            () => cancelDataTask(taskId),
                            "Task cancelled.",
                          )
                        }
                        style={buttonStyle("#fecaca")}
                      >
                        <Square size={14} /> Cancel
                      </button>
                    ) : null}
                    {!task.can_pause && !task.can_cancel ? (
                      <span
                        style={{
                          color: "var(--color-text-muted)",
                          fontSize: "0.8rem",
                        }}
                      >
                        {status}
                      </span>
                    ) : null}
                  </div>
                </div>

                <div
                  style={{
                    marginTop: "12px",
                    height: "10px",
                    borderRadius: "999px",
                    overflow: "hidden",
                    border: "1px solid rgba(148, 163, 184, 0.18)",
                    background: "rgba(15, 23, 42, 0.9)",
                  }}
                >
                  <div
                    style={{
                      width: `${percent}%`,
                      height: "100%",
                      background:
                        "linear-gradient(90deg, rgba(96, 165, 250, 0.95), rgba(59, 130, 246, 0.8))",
                      transition: "width 260ms ease",
                    }}
                  />
                </div>
              </div>
            );
          })}
      </section>

      <section style={{ display: "grid", gap: "10px" }}>
        <div style={{ fontWeight: 700, fontSize: "1.03rem" }}>
          Terminal Tasks
        </div>
        <div style={{ ...cardStyle(), padding: "10px 0" }}>
          {terminalTasks.length === 0 ? (
            <div
              style={{ padding: "6px 16px", color: "var(--color-text-muted)" }}
            >
              No terminal tasks yet.
            </div>
          ) : (
            terminalTasks.map((task) => {
              const status = normalizeTaskStatus(task.status);
              const isFailed = isFailedTaskStatus(status);
              const needsAttention = isFailed || status === "cleanup_pending";
              const taskId = task.id || "";
              const isBusy = Boolean(taskId && busy[taskId]);
              return (
                <div
                  key={`done-${task.id || task.name}`}
                  style={{
                    padding: "10px 16px",
                    borderTop: "1px solid rgba(148, 163, 184, 0.12)",
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    gap: "10px",
                    color: isFailed
                      ? "#fecaca"
                      : needsAttention
                        ? "#fde68a"
                        : "var(--color-text)",
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "8px",
                      }}
                    >
                      <span>{categoryIcon(task.category)}</span>
                      <span style={{ fontWeight: 600 }}>
                        {task.name || "Task"}
                      </span>
                      <span
                        style={{
                          color: isFailed
                            ? "#fecaca"
                            : needsAttention
                              ? "#fde68a"
                              : "var(--color-text-muted)",
                          fontSize: "0.82rem",
                        }}
                      >
                        {status}
                      </span>
                    </div>
                    <div
                      style={{
                        marginTop: "2px",
                        fontSize: "0.82rem",
                        color: isFailed
                          ? "#fecaca"
                          : needsAttention
                            ? "#fde68a"
                            : "var(--color-text-muted)",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        maxWidth: "70ch",
                      }}
                    >
                      {taskMessage(task)}
                    </div>
                    {task.provider ? (
                      <div
                        style={{
                          marginTop: "4px",
                          fontSize: "0.78rem",
                          color: "var(--color-text-muted)",
                          whiteSpace: "nowrap",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          maxWidth: "70ch",
                        }}
                      >
                        provider={task.provider}
                        {task.model ? ` · model=${task.model}` : ""}
                      </div>
                    ) : null}
                  </div>

                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "10px",
                      flexShrink: 0,
                    }}
                  >
                    <span
                      style={{
                        fontSize: "0.8rem",
                        color: "var(--color-text-muted)",
                      }}
                    >
                      {formatDuration(
                        task.duration_seconds ?? task.elapsed_seconds,
                      )}
                    </span>
                    {isFailed && task.can_restart ? (
                      <button
                        type="button"
                        disabled={!taskId || isBusy}
                        onClick={() =>
                          taskId &&
                          void runAction(
                            taskId,
                            () => restartDataTask(taskId),
                            "Retry requested.",
                          )
                        }
                        style={buttonStyle("#fde68a")}
                      >
                        <RefreshCw size={14} /> Retry
                      </button>
                    ) : null}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </section>
    </div>
  );
}

const SummaryCard = memo(function SummaryCard({
  label,
  value,
  failed = false,
}: {
  label: string;
  value: number;
  failed?: boolean;
}) {
  return (
    <div
      style={{
        borderRadius: "14px",
        border: failed
          ? "1px solid rgba(251, 113, 133, 0.25)"
          : "1px solid rgba(148, 163, 184, 0.18)",
        background: failed
          ? "linear-gradient(180deg, rgba(127, 29, 29, 0.22), rgba(2, 6, 23, 0.94))"
          : "linear-gradient(180deg, rgba(15, 23, 42, 0.92), rgba(2, 6, 23, 0.92))",
        padding: "12px 14px",
      }}
    >
      <div style={{ color: "var(--color-text-muted)", fontSize: "0.8rem" }}>
        {label}
      </div>
      <div
        style={{
          marginTop: "4px",
          fontWeight: 700,
          fontSize: "1.3rem",
          display: "flex",
          alignItems: "center",
          gap: "8px",
        }}
      >
        <Activity size={16} />
        {value}
      </div>
    </div>
  );
});

function buttonStyle(color: string): CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: "7px",
    padding: "7px 10px",
    borderRadius: "10px",
    border: "1px solid rgba(148, 163, 184, 0.2)",
    background: "rgba(15, 23, 42, 0.85)",
    color,
    cursor: "pointer",
    fontWeight: 700,
    fontSize: "0.78rem",
  };
}
