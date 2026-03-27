import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Database,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  Rocket,
  Square,
  Trash2,
  TriangleAlert,
  XCircle,
  Zap,
} from "lucide-react";

import {
  cancelDataTask,
  deleteDataTask,
  listDataTasks,
  pauseDataTask,
  restartDataTask,
  resumeDataTask,
} from "../api/client";
import type { DataTaskStatusResponse } from "../api/types";
import { useToast } from "./ToastProvider";

const FINAL_STATUSES = new Set(["completed", "failed", "cancelled", "interrupted"]);
const ACTIVE_STATUSES = new Set(["queued", "starting", "running", "paused"]);

function latestSnippet(task: DataTaskStatusResponse) {
  const progressLine = task.progress?.current?.trim();
  if (progressLine) {
    return progressLine;
  }
  const logLines = (task.log || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const logLine = logLines.length > 0 ? logLines[logLines.length - 1] : "";
  return logLine || task.description || "Waiting for task output...";
}

function sortTasksStable(tasks: DataTaskStatusResponse[]) {
  return [...tasks].sort((left, right) => {
    const leftStarted = Date.parse(left.started_at || left.created_at || "") || 0;
    const rightStarted = Date.parse(right.started_at || right.created_at || "") || 0;
    if (leftStarted !== rightStarted) {
      return rightStarted - leftStarted;
    }
    return (left.id || "").localeCompare(right.id || "");
  });
}

function formatDuration(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) {
    return "—";
  }
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rem = s % 60;
  if (h > 0) {
    return `${h}h ${m}m`;
  }
  if (m > 0) {
    return `${m}m ${rem}s`;
  }
  return `${rem}s`;
}

function categoryMeta(task: DataTaskStatusResponse) {
  const category = task.category || "download";
  switch (category) {
    case "backtest":
      return { icon: BarChart3, label: "Backtest", color: "#7dd3fc" };
    case "optimization":
      return { icon: Zap, label: "Optimization", color: "#facc15" };
    case "resample":
      return { icon: RefreshCw, label: "Resample", color: "#a78bfa" };
    case "live":
      return { icon: Rocket, label: "Live", color: "#34d399" };
    default:
      return { icon: Database, label: "Download", color: "#93c5fd" };
  }
}

function progressBarColor(status: string) {
  if (status === "paused" || status === "interrupted") {
    return "linear-gradient(90deg, rgba(251, 191, 36, 0.95), rgba(245, 158, 11, 0.82))";
  }
  if (status === "failed" || status === "cancelled") {
    return "linear-gradient(90deg, rgba(251, 113, 133, 0.95), rgba(244, 63, 94, 0.8))";
  }
  if (status === "completed") {
    return "linear-gradient(90deg, rgba(74, 222, 128, 0.95), rgba(16, 185, 129, 0.82))";
  }
  return "linear-gradient(90deg, rgba(96, 165, 250, 0.95), rgba(59, 130, 246, 0.8))";
}

function statusColor(status: string) {
  if (status === "paused") return "#fde68a";
  if (status === "failed" || status === "cancelled") return "#fecaca";
  if (status === "interrupted") return "#fde68a";
  if (status === "completed") return "#bbf7d0";
  if (status === "queued") return "#bfdbfe";
  return "#67e8f9";
}

function statusLabel(status: string) {
  if (status === "queued") return "Queued";
  if (status === "starting") return "Starting";
  if (status === "running") return "Running";
  if (status === "paused") return "Paused";
  if (status === "completed") return "Completed";
  if (status === "failed") return "Failed";
  if (status === "cancelled") return "Cancelled";
  if (status === "interrupted") return "Interrupted";
  return status;
}

function taskFooter(task: DataTaskStatusResponse) {
  const status = String(task.status || "").toLowerCase();
  const percent = task.progress?.percent ?? 0;
  if (status === "completed") {
    return `Completed in ${formatDuration(task.elapsed_seconds)}`;
  }
  if (status === "failed") {
    return `Failed after ${formatDuration(task.elapsed_seconds)}`;
  }
  if (status === "interrupted") {
    return `Interrupted after ${formatDuration(task.elapsed_seconds)}`;
  }
  if (status === "cancelled") {
    return `Cancelled after ${formatDuration(task.elapsed_seconds)}`;
  }
  const eta = formatDuration(task.eta_seconds);
  const elapsed = formatDuration(task.elapsed_seconds);
  return `${percent.toFixed(1)}% · ETA: ${eta} · Elapsed: ${elapsed}`;
}

function finishedAtLabel(task: DataTaskStatusResponse) {
  const raw = task.finished_at || task.updated_at;
  if (!raw) return "";
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleString("de-DE");
}

export function TaskManager() {
  const { showToast } = useToast();
  const [open, setOpen] = useState(false);
  const [showCompleted, setShowCompleted] = useState(false);
  const [tasks, setTasks] = useState<DataTaskStatusResponse[]>([]);
  const [busyTaskIds, setBusyTaskIds] = useState<string[]>([]);
  const lastStatusRef = useRef<Record<string, string>>({});

  useEffect(() => {
    let active = true;
    let intervalId = 0;

    const poll = async () => {
      try {
        const payload = await listDataTasks();
        if (!active) {
          return;
        }
        setTasks(payload.tasks || []);
      } catch {
        if (!active) {
          return;
        }
        setTasks((current) => current);
      }
    };

    void poll();
    intervalId = window.setInterval(() => {
      void poll();
    }, 1000);

    return () => {
      active = false;
      window.clearInterval(intervalId);
    };
  }, []);

  useEffect(() => {
    const previous = lastStatusRef.current;
    const next: Record<string, string> = {};
    for (const task of tasks) {
      const taskId = task.id || "";
      if (!taskId) continue;
      const status = String(task.status || "");
      next[taskId] = status;
      const previousStatus = previous[taskId];
      if (!previousStatus || previousStatus === status) continue;

      if (status === "completed") {
        showToast(
          `${task.name || categoryMeta(task).label} completed${
            task.progress?.completed ? ` - ${task.progress.completed} items` : ""
          }`,
          "success",
        );
      } else if (status === "failed") {
        const error = task.error || latestSnippet(task);
        showToast(`${task.name || categoryMeta(task).label} failed: ${error}`, "error");
      } else if (status === "interrupted") {
        showToast(`${task.name || categoryMeta(task).label} interrupted.`, "warning");
      }
    }
    lastStatusRef.current = next;
  }, [tasks, showToast]);

  const orderedTasks = useMemo(() => sortTasksStable(tasks), [tasks]);
  const activeTasks = useMemo(
    () => orderedTasks.filter((task) => ACTIVE_STATUSES.has(String(task.status || "").toLowerCase())),
    [orderedTasks],
  );
  const completedTasks = useMemo(
    () => orderedTasks.filter((task) => FINAL_STATUSES.has(String(task.status || "").toLowerCase())),
    [orderedTasks],
  );
  const failedTasks = useMemo(
    () => orderedTasks.filter((task) => ["failed", "interrupted"].includes(String(task.status || "").toLowerCase())),
    [orderedTasks],
  );

  const updateTask = (updatedTask: DataTaskStatusResponse) => {
    setTasks((current) => {
      const next = current.map((task) => (task.id === updatedTask.id ? updatedTask : task));
      return next.some((task) => task.id === updatedTask.id) ? next : [...next, updatedTask];
    });
  };

  const withBusyState = async (
    taskId: string,
    action: () => Promise<DataTaskStatusResponse>,
    successMessage: string,
  ) => {
    setBusyTaskIds((current) => [...current, taskId]);
    try {
      const updatedTask = await action();
      updateTask(updatedTask);
      showToast(successMessage, "info");
    } catch (error: any) {
      showToast(error.message || "Task action failed.", "error");
    } finally {
      setBusyTaskIds((current) => current.filter((id) => id !== taskId));
    }
  };

  const handleRemoveTask = async (taskId: string) => {
    setBusyTaskIds((current) => [...current, taskId]);
    try {
      await deleteDataTask(taskId);
      setTasks((current) => current.filter((task) => task.id !== taskId));
      showToast("Task removed.", "info");
    } catch (error: any) {
      showToast(error.message || "Failed to remove task.", "error");
    } finally {
      setBusyTaskIds((current) => current.filter((id) => id !== taskId));
    }
  };

  const runningCount = activeTasks.filter((task) => ["queued", "starting", "running"].includes(String(task.status || "").toLowerCase())).length;
  const summaryLabel =
    activeTasks.length > 0
      ? `${activeTasks.length} active task${activeTasks.length === 1 ? "" : "s"}`
      : orderedTasks.length > 0
        ? `${orderedTasks.length} recent task${orderedTasks.length === 1 ? "" : "s"}`
        : "No active tasks";

  return (
    <div style={{ display: "grid", gap: "12px", alignSelf: "start" }}>
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: "14px",
          width: "100%",
          padding: "14px 16px",
          borderRadius: "18px",
          border: "1px solid rgba(96, 165, 250, 0.18)",
          background:
            "linear-gradient(135deg, rgba(15, 23, 42, 0.94), rgba(17, 24, 39, 0.98))",
          color: "var(--color-text)",
          boxShadow: "0 16px 40px rgba(0, 0, 0, 0.18)",
          cursor: "pointer",
          textAlign: "left",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              width: "40px",
              height: "40px",
              borderRadius: "14px",
              background: "rgba(37, 99, 235, 0.16)",
              color: runningCount > 0 ? "#67e8f9" : "#cbd5f5",
              flexShrink: 0,
            }}
          >
            <Activity size={18} />
          </div>
          <div>
            <div
              style={{
                fontSize: "0.72rem",
                letterSpacing: "0.16em",
                textTransform: "uppercase",
                color: "var(--color-text-muted)",
              }}
            >
              Tasks ({runningCount} running)
            </div>
            <div style={{ marginTop: "4px", fontSize: "0.92rem", fontWeight: 700 }}>{summaryLabel}</div>
          </div>
        </div>

        <div
          style={{
            display: "grid",
            justifyItems: "end",
            gap: "4px",
            flexShrink: 0,
          }}
        >
          <div
            style={{
              fontSize: "0.8rem",
              fontWeight: 700,
              color: activeTasks.length > 0 ? "#7dd3fc" : "var(--color-text-muted)",
            }}
          >
            {activeTasks.length > 0 ? `${activeTasks.length} active` : "Idle"}
          </div>
          <div style={{ fontSize: "0.76rem", color: "var(--color-text-muted)" }}>
            {open ? "Hide queue" : "Show queue"}
          </div>
        </div>
      </button>

      {open && (
        <div
          style={{
            display: "grid",
            gap: "14px",
            borderRadius: "22px",
            border: "1px solid rgba(148, 163, 184, 0.16)",
            background:
              "linear-gradient(180deg, rgba(15, 23, 42, 0.97), rgba(2, 6, 23, 0.97))",
            boxShadow: "0 24px 60px rgba(0, 0, 0, 0.28)",
            padding: "16px",
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
                  fontSize: "0.72rem",
                  letterSpacing: "0.18em",
                  textTransform: "uppercase",
                  color: "#7dd3fc",
                }}
              >
                Task queue
              </div>
              <div style={{ fontSize: "1rem", fontWeight: 700 }}>Live activity with ETA</div>
            </div>
            <div
              style={{
                fontSize: "0.8rem",
                color: "var(--color-text-muted)",
                textAlign: "right",
              }}
            >
              Polling every 1s
            </div>
          </div>

          {failedTasks.length > 0 ? (
            <div
              style={{
                display: "grid",
                gap: "8px",
                borderRadius: "14px",
                border: "1px solid rgba(251, 113, 133, 0.24)",
                background: "rgba(127, 29, 29, 0.18)",
                padding: "10px 12px",
              }}
            >
              <div
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "6px",
                  color: "#fecaca",
                  fontSize: "0.8rem",
                  fontWeight: 700,
                }}
              >
                <AlertTriangle size={14} />
                Failed / Interrupted
              </div>
              {failedTasks.slice(0, 2).map((task) => (
                <div key={task.id || task.name} style={{ color: "#fecaca", fontSize: "0.76rem" }}>
                  {task.name || "Task"} · {latestSnippet(task)}
                </div>
              ))}
            </div>
          ) : null}

          <div style={{ display: "grid", gap: "10px" }}>
            {activeTasks.length === 0 ? (
              <div
                style={{
                  padding: "18px",
                  borderRadius: "18px",
                  border: "1px dashed rgba(148, 163, 184, 0.2)",
                  color: "var(--color-text-muted)",
                  textAlign: "center",
                }}
              >
                No active tasks.
              </div>
            ) : (
              activeTasks.map((task) => (
                <TaskRow
                  key={task.id || `${task.name}-${task.created_at}`}
                  task={task}
                  busy={Boolean(task.id && busyTaskIds.includes(task.id))}
                  withBusyState={withBusyState}
                  handleRemoveTask={handleRemoveTask}
                />
              ))
            )}
          </div>

          <div
            style={{
              borderTop: "1px solid rgba(148, 163, 184, 0.16)",
              paddingTop: "10px",
              display: "grid",
              gap: "10px",
            }}
          >
            <button
              type="button"
              onClick={() => setShowCompleted((current) => !current)}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "8px",
                borderRadius: "10px",
                border: "1px solid rgba(148, 163, 184, 0.2)",
                background: "rgba(15, 23, 42, 0.8)",
                color: "var(--color-text-muted)",
                padding: "8px 10px",
                fontSize: "0.78rem",
                cursor: "pointer",
                width: "fit-content",
              }}
            >
              {showCompleted ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              Completed ({completedTasks.length})
            </button>

            {showCompleted ? (
              <div style={{ display: "grid", gap: "8px" }}>
                {completedTasks.length === 0 ? (
                  <div style={{ color: "var(--color-text-muted)", fontSize: "0.76rem" }}>
                    No finished tasks yet.
                  </div>
                ) : (
                  completedTasks.map((task) => (
                    <TaskRow
                      key={`done-${task.id || task.name}`}
                      task={task}
                      busy={Boolean(task.id && busyTaskIds.includes(task.id))}
                      withBusyState={withBusyState}
                      handleRemoveTask={handleRemoveTask}
                      compact
                    />
                  ))
                )}
              </div>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}

function TaskRow({
  task,
  busy,
  withBusyState,
  handleRemoveTask,
  compact = false,
}: {
  task: DataTaskStatusResponse;
  busy: boolean;
  withBusyState: (
    taskId: string,
    action: () => Promise<DataTaskStatusResponse>,
    successMessage: string,
  ) => Promise<void>;
  handleRemoveTask: (taskId: string) => Promise<void>;
  compact?: boolean;
}) {
  const taskId = task.id || "";
  const status = String(task.status || "").toLowerCase();
  const percent = Math.max(0, Math.min(100, task.progress?.percent ?? 0));
  const canPause = Boolean(task.can_pause && taskId);
  const canResume = Boolean(task.can_resume && taskId);
  const canCancel = Boolean(task.can_cancel && taskId);
  const canRestart = Boolean(task.can_restart && taskId);
  const canRemove = Boolean(taskId && FINAL_STATUSES.has(status));
  const meta = categoryMeta(task);
  const CategoryIcon = meta.icon;

  const statusIcon =
    status === "completed" ? (
      <CheckCircle2 size={14} />
    ) : status === "failed" ? (
      <XCircle size={14} />
    ) : status === "interrupted" ? (
      <TriangleAlert size={14} />
    ) : (
      <Loader2 size={14} className="animate-spin" />
    );

  return (
    <div
      style={{
        display: "grid",
        gap: compact ? "8px" : "10px",
        padding: compact ? "10px" : "12px",
        borderRadius: "14px",
        border: `1px solid ${
          status === "failed"
            ? "rgba(251, 113, 133, 0.24)"
            : status === "interrupted"
              ? "rgba(251, 191, 36, 0.24)"
              : "rgba(148, 163, 184, 0.14)"
        }`,
        background:
          status === "failed"
            ? "linear-gradient(180deg, rgba(127, 29, 29, 0.22), rgba(2, 6, 23, 0.9))"
            : status === "interrupted"
              ? "linear-gradient(180deg, rgba(120, 53, 15, 0.22), rgba(2, 6, 23, 0.9))"
              : "linear-gradient(180deg, rgba(15, 23, 42, 0.88), rgba(2, 6, 23, 0.88))",
      }}
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: compact ? "1fr" : "auto 1fr auto",
          gap: "10px",
          alignItems: "start",
        }}
      >
        {!compact ? (
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              width: "28px",
              height: "28px",
              borderRadius: "10px",
              background: "rgba(30, 41, 59, 0.9)",
              color: meta.color,
            }}
            title={meta.label}
          >
            <CategoryIcon size={14} />
          </div>
        ) : null}

        <div style={{ minWidth: 0 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              flexWrap: "wrap",
            }}
          >
            <div style={{ fontSize: compact ? "0.84rem" : "0.9rem", fontWeight: 700 }}>
              {task.name || "Background task"}
            </div>
            <span
              style={{
                fontSize: "0.66rem",
                borderRadius: "999px",
                padding: "2px 7px",
                border: "1px solid rgba(148, 163, 184, 0.24)",
                color: "var(--color-text-muted)",
                textTransform: "uppercase",
                letterSpacing: "0.06em",
              }}
            >
              {meta.label}
            </span>
          </div>
          <div
            style={{
              marginTop: "3px",
              fontSize: compact ? "0.72rem" : "0.78rem",
              color: "var(--color-text-muted)",
              lineHeight: 1.45,
            }}
          >
            {latestSnippet(task)}
          </div>
        </div>

        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
            fontSize: "0.72rem",
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: statusColor(status),
          }}
        >
          {statusIcon}
          {statusLabel(status)}
        </div>
      </div>

      <div style={{ display: "grid", gap: "6px" }}>
        <div
          style={{
            position: "relative",
            height: compact ? "8px" : "10px",
            borderRadius: "999px",
            overflow: "hidden",
            background: "rgba(15, 23, 42, 0.92)",
            border: "1px solid rgba(148, 163, 184, 0.12)",
          }}
        >
          <div
            style={{
              width: `${percent}%`,
              height: "100%",
              background: progressBarColor(status),
              transition: "width 260ms ease",
            }}
          />
        </div>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            gap: "8px",
            alignItems: "center",
            flexWrap: "wrap",
            fontSize: "0.74rem",
            color: "var(--color-text-muted)",
          }}
        >
          <span>{taskFooter(task)}</span>
          {finishedAtLabel(task) ? <span>{finishedAtLabel(task)}</span> : null}
        </div>
      </div>

      {(canPause || canResume || canCancel || canRestart || canRemove) ? (
        <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
          {canPause ? (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void withBusyState(taskId, () => pauseDataTask(taskId), "Task paused.")
              }
              style={actionButtonStyle("#fde68a")}
            >
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Pause size={14} />}
              Pause
            </button>
          ) : null}

          {canResume ? (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void withBusyState(taskId, () => resumeDataTask(taskId), "Task resumed.")
              }
              style={actionButtonStyle("#bfdbfe")}
            >
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              Resume
            </button>
          ) : null}

          {canCancel ? (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void withBusyState(taskId, () => cancelDataTask(taskId), "Task cancelled.")
              }
              style={actionButtonStyle("#fecdd3")}
            >
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Square size={14} />}
              Cancel
            </button>
          ) : null}

          {canRestart ? (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void withBusyState(taskId, () => restartDataTask(taskId), status === "failed" ? "Retry requested." : "Restart requested.")
              }
              style={actionButtonStyle("#fde68a")}
            >
              {busy ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
              {status === "failed" ? "Retry" : "Restart"}
            </button>
          ) : null}

          {canRemove ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => void handleRemoveTask(taskId)}
              style={actionButtonStyle("#cbd5f5")}
            >
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
              Remove
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function actionButtonStyle(color: string): CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: "8px",
    padding: "7px 10px",
    borderRadius: "10px",
    border: "1px solid rgba(148, 163, 184, 0.16)",
    background: "rgba(15, 23, 42, 0.82)",
    color,
    cursor: "pointer",
    fontWeight: 700,
    fontSize: "0.74rem",
  };
}
