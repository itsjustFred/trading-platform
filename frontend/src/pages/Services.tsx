import { useMemo, useState, type CSSProperties } from "react";

import { createService, deleteService } from "../api/client";
import {
  useServiceLogs,
  useServices,
  useUpdateServiceStatus,
} from "../api/queries";
import type { Service, ServiceStatus } from "../api/types";
import { Modal } from "../components/Modal";
import { useToast } from "../components/ToastProvider";

function relativeTime(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const deltaSeconds = Math.max(
    0,
    Math.floor((Date.now() - date.getTime()) / 1000),
  );
  if (deltaSeconds < 60) return `${deltaSeconds}s ago`;
  if (deltaSeconds < 3600) return `${Math.floor(deltaSeconds / 60)}m ago`;
  if (deltaSeconds < 86400) return `${Math.floor(deltaSeconds / 3600)}h ago`;
  return `${Math.floor(deltaSeconds / 86400)}d ago`;
}

function statusDot(status: ServiceStatus) {
  if (status === "running") return "#22c55e";
  if (status === "paused") return "#f59e0b";
  if (status === "failed") return "#ef4444";
  return "#64748b";
}

function stringifyServiceConfigValue(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (value == null) {
    return "";
  }
  return JSON.stringify(value) ?? "";
}

/** Meta line for a Discord bot service card. */
function discordMetaLine(channelId: string, lastActivity: string): string {
  const channel = channelId ? `#${channelId}` : "—";
  return `Channel: ${channel} · Last Signal: ${lastActivity}`;
}

/** Meta line for a cron-scheduled service card. */
function scheduleMetaLine(cron: string, lastActivity: string): string {
  const schedule = cron || "—";
  return `Schedule: ${schedule} · Next Run: cron-based · Last Run: ${lastActivity}`;
}

export function ServicesPage() {
  const { showToast } = useToast();
  const servicesQuery = useServices();
  const statusMutation = useUpdateServiceStatus();

  const [addingOpen, setAddingOpen] = useState(false);
  const [logsService, setLogsService] = useState<Service | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [newName, setNewName] = useState("");
  const [newCron, setNewCron] = useState("0 0 * * 0");
  const [newJobType, setNewJobType] = useState("weekly_data_refresh");

  const logsQuery = useServiceLogs(logsService?.id ?? "", 300, {
    enabled: Boolean(logsService?.id),
  });

  const services = servicesQuery.data ?? [];
  const summary = useMemo(
    () => ({
      running: services.filter((service) => service.status === "running")
        .length,
      paused: services.filter((service) => service.status === "paused").length,
      failed: services.filter((service) => service.status === "failed").length,
    }),
    [services],
  );

  const handleStatus = async (service: Service, status: ServiceStatus) => {
    try {
      await statusMutation.mutateAsync({ serviceId: service.id, status });
      showToast(`${service.name} set to ${status}.`, "success");
    } catch (error) {
      showToast(
        error instanceof Error
          ? error.message
          : "Could not update service status.",
        "error",
      );
    }
  };

  const handleDelete = async (service: Service) => {
    try {
      await deleteService(service.id);
      await servicesQuery.refetch();
      showToast(`${service.name} deleted.`, "success");
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : "Could not delete service.",
        "error",
      );
    }
  };

  const handleCreateScheduledJob = async (
    event: React.SubmitEvent<HTMLFormElement>,
  ) => {
    event.preventDefault();
    const name = newName.trim();
    const cron = newCron.trim();
    if (!name || !cron) {
      showToast("Name and cron expression are required.", "warning");
      return;
    }

    setIsSubmitting(true);
    try {
      await createService({
        name,
        type: "scheduled_job",
        status: "running",
        config: {
          cron,
          job_type: newJobType,
        },
      });
      setAddingOpen(false);
      setNewName("");
      setNewCron("0 0 * * 0");
      setNewJobType("weekly_data_refresh");
      await servicesQuery.refetch();
      showToast(`Scheduled job ${name} added.`, "success");
    } catch (error) {
      showToast(
        error instanceof Error
          ? error.message
          : "Could not create scheduled job.",
        "error",
      );
    } finally {
      setIsSubmitting(false);
    }
  };

  const servicesBody = servicesQuery.isLoading ? (
    <div style={emptyStyle}>Loading services...</div>
  ) : servicesQuery.isError ? (
    <div style={emptyStyle}>Failed to load services.</div>
  ) : services.length === 0 ? (
    <div style={emptyStyle}>No services found.</div>
  ) : (
    <div style={{ display: "grid", gap: "10px" }}>
      {services.map((service) => {
        const isRunning = service.status === "running";
        const cron = stringifyServiceConfigValue(service.config?.cron);
        const channelId = stringifyServiceConfigValue(
          service.config?.channel_id,
        );
        const metaLine =
          service.type === "discord_bot"
            ? discordMetaLine(channelId, relativeTime(service.last_activity))
            : scheduleMetaLine(cron, relativeTime(service.last_activity));
        return (
          <div key={service.id} style={serviceCardStyle}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: "10px",
                flexWrap: "wrap",
              }}
            >
              <div>
                <div style={{ fontWeight: 700, fontSize: "0.92rem" }}>
                  {service.type === "discord_bot" ? "Discord Bot: " : ""}
                  {service.name}
                </div>
                <div style={metaLineStyle}>{metaLine}</div>
              </div>
              <div
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "8px",
                  color: "var(--color-text-muted)",
                }}
              >
                <span
                  style={{
                    width: "10px",
                    height: "10px",
                    borderRadius: "50%",
                    background: statusDot(service.status),
                  }}
                />
                <span
                  style={{ textTransform: "capitalize", fontWeight: 600 }}
                >
                  {service.status}
                </span>
              </div>
            </div>
            <div
              style={{
                display: "flex",
                gap: "8px",
                marginTop: "10px",
                flexWrap: "wrap",
              }}
            >
              <button
                type="button"
                style={secondaryButtonStyle}
                onClick={() =>
                  void handleStatus(
                    service,
                    isRunning ? "paused" : "running",
                  )
                }
                disabled={statusMutation.isPending}
              >
                {isRunning ? "Pause" : "Start"}
              </button>
              <button
                type="button"
                style={secondaryButtonStyle}
                onClick={() => setLogsService(service)}
              >
                Logs
              </button>
              <button
                type="button"
                style={dangerButtonStyle}
                onClick={() => void handleDelete(service)}
              >
                Delete
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );

  return (
    <div style={{ padding: "24px", display: "grid", gap: "14px" }}>
      <Modal
        open={addingOpen}
        onClose={() => setAddingOpen(false)}
        title="Add Scheduled Job"
        width="520px"
      >
        <form
          onSubmit={handleCreateScheduledJob}
          style={{ display: "grid", gap: "12px" }}
        >
          <label style={fieldLabelStyle}>
            Name{" "}
            <input
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              placeholder="Weekly Data Refresh"
              style={fieldInputStyle}
            />
          </label>
          <label style={fieldLabelStyle}>
            Job Type{" "}
            <select
              value={newJobType}
              onChange={(event) => setNewJobType(event.target.value)}
              style={fieldInputStyle}
            >
              <option value="weekly_data_refresh">Weekly Data Refresh</option>
              <option value="signal_bot_health_check">Signal Bot Health Check</option>
            </select>
          </label>
          <label style={fieldLabelStyle}>
            Cron{" "}
            <input
              value={newCron}
              onChange={(event) => setNewCron(event.target.value)}
              placeholder="0 0 * * 0"
              style={fieldInputStyle}
            />
          </label>
          <div
            style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}
          >
            <button
              type="button"
              style={secondaryButtonStyle}
              onClick={() => setAddingOpen(false)}
            >
              Cancel
            </button>
            <button
              type="submit"
              style={primaryButtonStyle}
              disabled={isSubmitting}
            >
              {isSubmitting ? "Saving..." : "Create"}
            </button>
          </div>
        </form>
      </Modal>

      <Modal
        open={Boolean(logsService)}
        onClose={() => setLogsService(null)}
        title={logsService ? `${logsService.name} Logs` : "Service Logs"}
        width="760px"
      >
        <div
          style={{
            maxHeight: "55vh",
            overflow: "auto",
            background: "rgba(2, 6, 23, 0.75)",
            border: "1px solid var(--color-border)",
            borderRadius: "10px",
            padding: "10px",
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            fontSize: "0.78rem",
            whiteSpace: "pre-wrap",
            color: "var(--color-text-muted)",
          }}
        >
          {logsQuery.isLoading
            ? "Loading..."
            : (logsQuery.data?.lines ?? []).join("\n") ||
              "No matching log lines."}
        </div>
      </Modal>

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: "12px",
          flexWrap: "wrap",
        }}
      >
        <div>
          <h1 style={{ margin: 0, fontSize: "1.4rem", fontWeight: 700 }}>
            Services
          </h1>
          <p
            style={{
              margin: "4px 0 0",
              color: "var(--color-text-muted)",
              fontSize: "0.85rem",
            }}
          >
            Persistent bot and scheduled task services.
          </p>
        </div>
        <button
          type="button"
          style={primaryButtonStyle}
          onClick={() => setAddingOpen(true)}
        >
          + Add Scheduled Job
        </button>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
          gap: "10px",
        }}
      >
        <div style={summaryCardStyle}>
          <div style={summaryLabelStyle}>Running</div>
          <div style={summaryValueStyle}>{summary.running}</div>
        </div>
        <div style={summaryCardStyle}>
          <div style={summaryLabelStyle}>Paused</div>
          <div style={summaryValueStyle}>{summary.paused}</div>
        </div>
        <div style={summaryCardStyle}>
          <div style={summaryLabelStyle}>Failed</div>
          <div style={summaryValueStyle}>{summary.failed}</div>
        </div>
      </div>

      {servicesBody}
    </div>
  );
}

const summaryCardStyle: CSSProperties = {
  border: "1px solid var(--color-border)",
  borderRadius: "10px",
  padding: "12px",
  background: "rgba(15, 23, 42, 0.4)",
};

const summaryLabelStyle: CSSProperties = {
  color: "var(--color-text-muted)",
  fontSize: "0.76rem",
  textTransform: "uppercase",
  letterSpacing: "0.08em",
};

const summaryValueStyle: CSSProperties = {
  fontSize: "1.5rem",
  fontWeight: 700,
  marginTop: "4px",
};

const serviceCardStyle: CSSProperties = {
  border: "1px solid var(--color-border)",
  borderRadius: "10px",
  padding: "12px",
  background: "rgba(2, 6, 23, 0.45)",
};

const metaLineStyle: CSSProperties = {
  marginTop: "6px",
  color: "var(--color-text-muted)",
  fontSize: "0.8rem",
};

const emptyStyle: CSSProperties = {
  border: "1px dashed var(--color-border)",
  borderRadius: "10px",
  padding: "18px",
  color: "var(--color-text-muted)",
  textAlign: "center",
};

const fieldLabelStyle: CSSProperties = {
  display: "grid",
  gap: "6px",
  fontSize: "0.83rem",
  color: "var(--color-text-muted)",
};

const fieldInputStyle: CSSProperties = {
  width: "100%",
  border: "1px solid var(--color-border)",
  borderRadius: "8px",
  background: "var(--color-surface2)",
  color: "var(--color-text)",
  padding: "8px 10px",
};

const primaryButtonStyle: CSSProperties = {
  border: "1px solid #2563eb",
  background: "#2563eb",
  color: "white",
  borderRadius: "8px",
  padding: "8px 12px",
  fontWeight: 600,
  cursor: "pointer",
};

const secondaryButtonStyle: CSSProperties = {
  border: "1px solid var(--color-border)",
  background: "transparent",
  color: "var(--color-text)",
  borderRadius: "8px",
  padding: "8px 12px",
  fontWeight: 600,
  cursor: "pointer",
};

const dangerButtonStyle: CSSProperties = {
  ...secondaryButtonStyle,
  borderColor: "#ef4444",
  color: "#ef4444",
};
