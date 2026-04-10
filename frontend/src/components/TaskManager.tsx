import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Activity } from "lucide-react";

import { listDataTasks } from "../api/client";
import type { DataTaskStatusResponse } from "../api/types";

const ACTIVE_STATUSES = new Set(["queued", "starting", "running", "paused"]);

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

export function TaskManager() {
  const [tasks, setTasks] = useState<DataTaskStatusResponse[]>([]);

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
      }
    };

    void poll();
    intervalId = window.setInterval(() => {
      void poll();
    }, 3000);

    return () => {
      active = false;
      window.clearInterval(intervalId);
    };
  }, []);

  const orderedTasks = useMemo(() => sortTasksStable(tasks), [tasks]);
  const activeTasks = useMemo(
    () =>
      orderedTasks.filter((task) =>
        ACTIVE_STATUSES.has(String(task.status || "").toLowerCase()),
      ),
    [orderedTasks],
  );
  const runningCount = useMemo(
    () =>
      activeTasks.filter((task) =>
        ["queued", "starting", "running"].includes(
          String(task.status || "").toLowerCase(),
        ),
      ).length,
    [activeTasks],
  );
  const summaryLabel = `${runningCount} running · ${orderedTasks.length} total`;

  return (
    <div style={{ display: "grid", gap: "10px", alignSelf: "start" }}>
      <Link
        to="/tasks"
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
          textDecoration: "none",
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
              Task Hub
            </div>
            <div style={{ marginTop: "4px", fontSize: "0.9rem", fontWeight: 700 }}>
              {summaryLabel}
            </div>
          </div>
        </div>
        <div style={{ color: "#7dd3fc", fontWeight: 700, fontSize: "0.8rem" }}>Open</div>
      </Link>
    </div>
  );
}
