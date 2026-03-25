import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { Activity, Loader2, Pause, Play, Square, X } from "lucide-react";

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

function progressBarColor(status: string) {
  if (status === "paused") {
    return "linear-gradient(90deg, rgba(250, 204, 21, 0.95), rgba(251, 191, 36, 0.8))";
  }
  if (status === "failed" || status === "cancelled") {
    return "linear-gradient(90deg, rgba(251, 113, 133, 0.95), rgba(244, 63, 94, 0.8))";
  }
  if (status === "interrupted") {
    return "linear-gradient(90deg, rgba(251, 191, 36, 0.95), rgba(245, 158, 11, 0.8))";
  }
  return "linear-gradient(90deg, rgba(74, 222, 128, 0.95), rgba(16, 185, 129, 0.82))";
}

function statusColor(status: string) {
  if (status === "paused") {
    return "#fde68a";
  }
  if (status === "failed" || status === "cancelled") {
    return "#fecdd3";
  }
  if (status === "interrupted") {
    return "#fef08a";
  }
  if (status === "completed") {
    return "#bbf7d0";
  }
  if (status === "queued") {
    return "#bfdbfe";
  }
  return "#67e8f9";
}

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
    const createdComparison = (left.created_at || "").localeCompare(
      right.created_at || "",
    );
    if (createdComparison !== 0) {
      return createdComparison;
    }
    return (left.id || "").localeCompare(right.id || "");
  });
}

export function TaskManager() {
  const { showToast } = useToast();
  const [open, setOpen] = useState(false);
  const [tasks, setTasks] = useState<DataTaskStatusResponse[]>([]);
  const [busyTaskIds, setBusyTaskIds] = useState<string[]>([]);

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

  const orderedTasks = useMemo(() => sortTasksStable(tasks), [tasks]);

  const activeCount = useMemo(
    () => orderedTasks.filter((task) => !FINAL_STATUSES.has(task.status)).length,
    [orderedTasks],
  );

  const runningCount = useMemo(
    () =>
      orderedTasks.filter(
        (task) =>
          task.status === "queued" ||
          task.status === "starting" ||
          task.status === "running",
      ).length,
    [orderedTasks],
  );

  const updateTask = (updatedTask: DataTaskStatusResponse) => {
    setTasks((current) => {
      const next = current.map((task) =>
        task.id === updatedTask.id ? updatedTask : task,
      );
      return next.some((task) => task.id === updatedTask.id)
        ? next
        : [...next, updatedTask];
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

  const summaryLabel =
    activeCount > 0
      ? `${activeCount} active task${activeCount === 1 ? "" : "s"}`
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
              Background tasks
            </div>
            <div style={{ marginTop: "4px", fontSize: "0.92rem", fontWeight: 700 }}>
              {summaryLabel}
            </div>
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
              color: activeCount > 0 ? "#7dd3fc" : "var(--color-text-muted)",
            }}
          >
            {activeCount > 0 ? `${activeCount} live` : "Idle"}
          </div>
          <div style={{ fontSize: "0.76rem", color: "var(--color-text-muted)" }}>
            {runningCount > 0
              ? `${runningCount} running`
              : open
                ? "Hide queue"
                : "Show queue"}
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
              <div style={{ fontSize: "1rem", fontWeight: 700 }}>
                Stable order by start time
              </div>
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

          <div style={{ display: "grid", gap: "12px" }}>
            {orderedTasks.length === 0 ? (
              <div
                style={{
                  padding: "18px",
                  borderRadius: "18px",
                  border: "1px dashed rgba(148, 163, 184, 0.2)",
                  color: "var(--color-text-muted)",
                  textAlign: "center",
                }}
              >
                No recent tasks yet.
              </div>
            ) : (
              orderedTasks.map((task) => {
                const taskId = task.id || "";
                const busy = busyTaskIds.includes(taskId);
                const percent = task.progress?.percent ?? 0;
                const canPause = Boolean(task.can_pause && taskId);
                const canResume = Boolean(task.can_resume && taskId);
                const canCancel = Boolean(task.can_cancel && taskId);
                const canRestart = Boolean(task.can_restart && taskId);
                const canRemove = Boolean(
                  taskId && FINAL_STATUSES.has(task.status),
                );

                return (
                  <div
                    key={taskId || `${task.name}-${task.created_at}`}
                    style={{
                      display: "grid",
                      gap: "12px",
                      padding: "14px",
                      borderRadius: "18px",
                      border: "1px solid rgba(148, 163, 184, 0.14)",
                      background:
                        "linear-gradient(180deg, rgba(15, 23, 42, 0.88), rgba(2, 6, 23, 0.88))",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        gap: "12px",
                        alignItems: "flex-start",
                      }}
                    >
                      <div>
                        <div style={{ fontSize: "0.98rem", fontWeight: 700 }}>
                          {task.name || "Background task"}
                        </div>
                        <div
                          style={{
                            marginTop: "3px",
                            fontSize: "0.8rem",
                            color: "var(--color-text-muted)",
                            lineHeight: 1.5,
                          }}
                        >
                          {latestSnippet(task)}
                        </div>
                      </div>
                      <div
                        style={{
                          fontSize: "0.76rem",
                          textTransform: "uppercase",
                          letterSpacing: "0.12em",
                          color: statusColor(task.status),
                          whiteSpace: "nowrap",
                        }}
                      >
                        {task.status}
                      </div>
                    </div>

                    <div style={{ display: "grid", gap: "8px" }}>
                      <div
                        style={{
                          position: "relative",
                          height: "10px",
                          borderRadius: "999px",
                          overflow: "hidden",
                          background: "rgba(15, 23, 42, 0.92)",
                          border: "1px solid rgba(148, 163, 184, 0.12)",
                        }}
                      >
                        <div
                          style={{
                            width: `${Math.max(0, Math.min(100, percent))}%`,
                            height: "100%",
                            background: progressBarColor(task.status),
                            transition: "width 180ms ease",
                          }}
                        />
                      </div>
                      <div
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          gap: "12px",
                          fontSize: "0.8rem",
                          color: "var(--color-text-muted)",
                        }}
                      >
                        <span>{percent.toFixed(1)}%</span>
                        <span>
                          {task.progress?.completed ?? 0} ok /{" "}
                          {task.progress?.failed ?? 0} failed
                        </span>
                      </div>
                    </div>

                    {(canPause || canResume || canCancel || canRestart || canRemove) && (
                      <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
                        {canPause ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void withBusyState(
                                taskId,
                                () => pauseDataTask(taskId),
                                "Task paused.",
                              )
                            }
                            style={actionButtonStyle("#fde68a")}
                          >
                            {busy ? (
                              <Loader2 size={14} className="animate-spin" />
                            ) : (
                              <Pause size={14} />
                            )}
                            Pause
                          </button>
                        ) : null}

                        {canResume ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void withBusyState(
                                taskId,
                                () => resumeDataTask(taskId),
                                "Task resumed.",
                              )
                            }
                            style={actionButtonStyle("#bfdbfe")}
                          >
                            {busy ? (
                              <Loader2 size={14} className="animate-spin" />
                            ) : (
                              <Play size={14} />
                            )}
                            Resume
                          </button>
                        ) : null}

                        {canCancel ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void withBusyState(
                                taskId,
                                () => cancelDataTask(taskId),
                                "Task cancelled.",
                              )
                            }
                            style={actionButtonStyle("#fecdd3")}
                          >
                            {busy ? (
                              <Loader2 size={14} className="animate-spin" />
                            ) : (
                              <Square size={14} />
                            )}
                            Cancel
                          </button>
                        ) : null}

                        {canRestart ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void withBusyState(
                                taskId,
                                () => restartDataTask(taskId),
                                "Task restart requested.",
                              )
                            }
                            style={actionButtonStyle("#fde68a")}
                          >
                            {busy ? (
                              <Loader2 size={14} className="animate-spin" />
                            ) : (
                              <Play size={14} />
                            )}
                            Restart
                          </button>
                        ) : null}

                        {canRemove ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => void handleRemoveTask(taskId)}
                            style={actionButtonStyle("#cbd5f5")}
                          >
                            {busy ? (
                              <Loader2 size={14} className="animate-spin" />
                            ) : (
                              <X size={14} />
                            )}
                            Remove
                          </button>
                        ) : null}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function actionButtonStyle(color: string): CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: "8px",
    padding: "8px 12px",
    borderRadius: "12px",
    border: "1px solid rgba(148, 163, 184, 0.16)",
    background: "rgba(15, 23, 42, 0.82)",
    color,
    cursor: "pointer",
    fontWeight: 700,
    fontSize: "0.8rem",
  };
}
