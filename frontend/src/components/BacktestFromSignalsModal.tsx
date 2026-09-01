import { useEffect, useMemo, useState } from "react";

import { getSignalProcessors, runBacktestFromSignals } from "../api/client";
import type {
  BacktestFromSignalsSource,
  SignalProcessorOption,
} from "../api/types";
import { Modal } from "./Modal";
import { useToast } from "./ToastProvider";

interface BacktestFromSignalsModalProps {
  open: boolean;
  onClose: () => void;
  onTaskStarted?: (taskId: string) => void;
}

function defaultDateRange(): { start: string; end: string } {
  const end = new Date();
  const start = new Date(end.getTime() - 30 * 24 * 60 * 60 * 1000);
  return {
    start: start.toISOString().slice(0, 10),
    end: end.toISOString().slice(0, 10),
  };
}

// Default signal processor for "backtest from signals". StockKit is the Discord
// source, not a processor, so the fallback is RamJet.
const DEFAULT_SIGNAL_PROCESSOR = "ramjet";

const inputStyle: React.CSSProperties = {
  background: "var(--color-surface2)",
  border: "1px solid var(--color-border)",
  color: "var(--color-text)",
  padding: "8px 10px",
  borderRadius: "4px",
  fontSize: "0.82rem",
  width: "100%",
  outline: "none",
};

const labelStyle: React.CSSProperties = {
  color: "var(--color-text-muted)",
  fontSize: "0.75rem",
  textTransform: "uppercase",
  letterSpacing: "0.5px",
  display: "block",
  marginBottom: "4px",
};

export function BacktestFromSignalsModal({
  open,
  onClose,
  onTaskStarted,
}: Readonly<BacktestFromSignalsModalProps>) {
  const { showToast } = useToast();
  const range = useMemo(() => defaultDateRange(), []);
  const [signalSource, setSignalSource] =
    useState<BacktestFromSignalsSource>("csv");
  const [filepath, setFilepath] = useState("");
  const [accountId, setAccountId] = useState("");
  // Start empty (matches the placeholder option) and let the registry
  // reconciliation below select the default or first advertised processor.
  const [signalProcessor, setSignalProcessor] = useState("");
  const [timeframe, setTimeframe] = useState("");
  const [startDate, setStartDate] = useState(range.start);
  const [endDate, setEndDate] = useState(range.end);
  const [autoDownload, setAutoDownload] = useState(true);
  const [running, setRunning] = useState(false);
  const [processors, setProcessors] = useState<SignalProcessorOption[]>([]);
  const [loadingProcessors, setLoadingProcessors] = useState(false);

  useEffect(() => {
    if (!open) return;
    setStartDate(range.start);
    setEndDate(range.end);
    setLoadingProcessors(true);
    getSignalProcessors()
      .then((options) => {
        const normalized = Array.isArray(options) ? options : [];
        setProcessors(normalized);
        // Reconcile the current selection against the registry. An id that is
        // no longer advertised (e.g. a processor was removed) falls back to
        // the documented default, then to the first advertised option — never
        // silently to an unrelated entry's position in the list.
        setSignalProcessor((current) => {
          if (normalized.some((item) => item.id === current)) {
            return current;
          }
          if (normalized.some((item) => item.id === DEFAULT_SIGNAL_PROCESSOR)) {
            return DEFAULT_SIGNAL_PROCESSOR;
          }
          return normalized.length > 0 ? normalized[0].id : current;
        });
      })
      .catch(() => {
        setProcessors([]);
      })
      .finally(() => setLoadingProcessors(false));
  }, [open, range.end, range.start]);

  const handleStart = async () => {
    if (!startDate || !endDate) {
      showToast("Please select start and end dates.", "warning");
      return;
    }
    if (startDate > endDate) {
      showToast("Start date must be before end date.", "warning");
      return;
    }
    if (signalSource === "csv" && !filepath.trim()) {
      showToast("Please provide a CSV file path.", "warning");
      return;
    }

    setRunning(true);
    try {
      const response = await runBacktestFromSignals({
        signal_source: signalSource,
        filepath: signalSource === "csv" ? filepath.trim() : undefined,
        account_id:
          signalSource === "discord-history"
            ? accountId.trim() || undefined
            : undefined,
        signal_processor: signalProcessor || undefined,
        start_date: startDate,
        end_date: endDate,
        timeframe: timeframe || undefined,
        auto_download: autoDownload,
      });
      const taskId = response.task_id || response.job_id;
      onTaskStarted?.(taskId);
      showToast(`Signal backtest started (task ${taskId}).`, "success");
      onClose();
    } catch (issue: unknown) {
      const message =
        issue instanceof Error
          ? issue.message
          : "Could not start signal backtest";
      showToast(message, "error");
    } finally {
      setRunning(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Backtest from Signals"
      width="560px"
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
        <div>
          <label htmlFor="signals-backtest-source" style={labelStyle}>
            Signal Source
          </label>
          <select
            id="signals-backtest-source"
            value={signalSource}
            onChange={(event) =>
              setSignalSource(event.target.value as BacktestFromSignalsSource)
            }
            style={inputStyle}
          >
            <option value="csv">CSV File Path</option>
            <option value="discord-history">Discord-History</option>
          </select>
        </div>

        {signalSource === "csv" ? (
          <div>
            <label htmlFor="signals-backtest-csv" style={labelStyle}>
              CSV File Path
            </label>
            <input
              id="signals-backtest-csv"
              value={filepath}
              onChange={(event) => setFilepath(event.target.value)}
              placeholder="/home/ubuntu/data/signals/signals_ram_jet.csv"
              style={inputStyle}
            />
          </div>
        ) : (
          <div>
            <label htmlFor="signals-backtest-account" style={labelStyle}>
              Account ID (optional)
            </label>
            <input
              id="signals-backtest-account"
              value={accountId}
              onChange={(event) => setAccountId(event.target.value)}
              placeholder="acct-123"
              style={inputStyle}
            />
          </div>
        )}

        <div>
          <label htmlFor="signals-backtest-processor" style={labelStyle}>
            Signal Processor
          </label>
          <select
            id="signals-backtest-processor"
            value={signalProcessor}
            onChange={(event) => setSignalProcessor(event.target.value)}
            style={inputStyle}
          >
            <option value="">
              {loadingProcessors
                ? "Loading processors..."
                : "No processor override"}
            </option>
            {processors.map((processor) => (
              <option key={processor.id} value={processor.id}>
                {processor.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="signals-backtest-timeframe" style={labelStyle}>
            Replay Timeframe
          </label>
          <select
            id="signals-backtest-timeframe"
            value={timeframe}
            onChange={(event) => setTimeframe(event.target.value)}
            style={inputStyle}
          >
            <option value="">Auto (1m intraday, 1d date-only)</option>
            <option value="1m">1m</option>
            <option value="1d">1d</option>
          </select>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
            gap: "10px",
          }}
        >
          <div>
            <label htmlFor="signals-backtest-start" style={labelStyle}>
              Start Date
            </label>
            <input
              id="signals-backtest-start"
              type="date"
              value={startDate}
              onChange={(event) => setStartDate(event.target.value)}
              style={inputStyle}
            />
          </div>
          <div>
            <label htmlFor="signals-backtest-end" style={labelStyle}>
              End Date
            </label>
            <input
              id="signals-backtest-end"
              type="date"
              value={endDate}
              onChange={(event) => setEndDate(event.target.value)}
              style={inputStyle}
            />
          </div>
        </div>

        <label
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            color: "var(--color-text-muted)",
            fontSize: "0.8rem",
            border: "1px solid var(--color-border)",
            borderRadius: "8px",
            padding: "8px 10px",
            background: "var(--color-surface2)",
          }}
        >
          Auto-download missing market data{" "}
          <input
            type="checkbox"
            checked={autoDownload}
            onChange={(event) => setAutoDownload(event.target.checked)}
          />
        </label>

        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "8px",
            marginTop: "4px",
          }}
        >
          <button
            type="button"
            onClick={onClose}
            style={{
              borderRadius: "8px",
              border: "1px solid var(--color-border)",
              background: "var(--color-surface2)",
              color: "var(--color-text)",
              padding: "8px 12px",
              cursor: "pointer",
            }}
            disabled={running}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void handleStart()}
            style={{
              borderRadius: "8px",
              border: "1px solid rgba(88,166,255,0.45)",
              background: "rgba(88,166,255,0.16)",
              color: "#dbeafe",
              padding: "8px 12px",
              cursor: "pointer",
            }}
            disabled={running}
          >
            {running ? "Starting..." : "Start Backtest"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
