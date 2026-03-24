import { useEffect, useMemo, useState } from "react";
import { Crosshair, SlidersHorizontal, Sparkles, Target } from "lucide-react";

import { runOptimization } from "../api/client";
import type {
  OptimizationParameterRange,
  OptimizationRunRequest,
  StrategyParameterValue,
} from "../api/types";
import { useToast } from "./ToastProvider";
import { Modal } from "./Modal";

type MetricOption = OptimizationRunRequest["target_metric"];
type ParameterKind = "int" | "float" | "categorical";

interface OptimizeModalProps {
  open: boolean;
  onClose: () => void;
  backtestId: string;
  backtestTitle: string;
  parameters: Record<string, StrategyParameterValue>;
  onTaskStarted?: (taskId: string) => void;
}

interface ParameterDraft {
  kind: ParameterKind;
  currentValue: StrategyParameterValue;
  min: string;
  max: string;
  step: string;
  choices: string;
}

const TARGET_OPTIONS: Array<{ label: string; value: MetricOption }> = [
  { label: "Sharpe", value: "sharpe" },
  { label: "Net Profit", value: "net_profit" },
  { label: "Calmar", value: "calmar" },
  { label: "CAGR", value: "cagr" },
  { label: "Win Rate", value: "win_rate" },
];

function detectParameterKind(value: StrategyParameterValue): ParameterKind {
  if (typeof value === "number") {
    return Number.isInteger(value) ? "int" : "float";
  }
  return "categorical";
}

function formatCurrentValue(value: StrategyParameterValue) {
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (value === null) {
    return "null";
  }
  return String(value);
}

function parseNumericField(raw: string, label: string) {
  const numeric = Number.parseFloat(raw);
  if (!Number.isFinite(numeric)) {
    throw new Error(`${label} must be a valid number.`);
  }
  return numeric;
}

function normalizeChoices(raw: string, currentValue: StrategyParameterValue) {
  return raw
    .split(",")
    .map((choice) => choice.trim())
    .filter(Boolean)
    .map((choice) => {
      if (typeof currentValue === "boolean") {
        const lowered = choice.toLowerCase();
        if (lowered === "true") return true;
        if (lowered === "false") return false;
      }
      if (typeof currentValue === "number") {
        const numeric = Number.parseFloat(choice);
        if (Number.isFinite(numeric)) {
          return Number.isInteger(currentValue) ? Math.trunc(numeric) : numeric;
        }
      }
      return choice;
    });
}

function buildRangePayload(
  drafts: Record<string, ParameterDraft>,
): Record<string, OptimizationParameterRange> {
  const payload: Record<string, OptimizationParameterRange> = {};

  for (const [name, draft] of Object.entries(drafts)) {
    if (draft.kind === "categorical") {
      const choices = normalizeChoices(draft.choices, draft.currentValue);
      if (choices.length > 0) {
        payload[name] = {
          type: "categorical",
          choices,
        };
      }
      continue;
    }

    if (!draft.min.trim() || !draft.max.trim()) {
      continue;
    }

    const minValue = parseNumericField(draft.min, `${name} min`);
    const maxValue = parseNumericField(draft.max, `${name} max`);
    if (minValue > maxValue) {
      throw new Error(`${name}: min cannot be greater than max.`);
    }

    const range: OptimizationParameterRange = {
      type: draft.kind,
      min: draft.kind === "int" ? Math.trunc(minValue) : minValue,
      max: draft.kind === "int" ? Math.trunc(maxValue) : maxValue,
    };

    if (draft.step.trim()) {
      const stepValue = parseNumericField(draft.step, `${name} step`);
      if (stepValue <= 0) {
        throw new Error(`${name}: step must be greater than 0.`);
      }
      range.step = draft.kind === "int" ? Math.max(1, Math.trunc(stepValue)) : stepValue;
    }

    payload[name] = range;
  }

  return payload;
}

export function OptimizeModal({
  open,
  onClose,
  backtestId,
  backtestTitle,
  parameters,
  onTaskStarted,
}: OptimizeModalProps) {
  const { showToast } = useToast();
  const [parameterDrafts, setParameterDrafts] = useState<Record<string, ParameterDraft>>({});
  const [targetMetric, setTargetMetric] = useState<MetricOption>("sharpe");
  const [nTrials, setNTrials] = useState("100");
  const [trainSplitPct, setTrainSplitPct] = useState("0.7");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      return;
    }

    const nextDrafts = Object.fromEntries(
      Object.entries(parameters || {}).map(([name, currentValue]) => [
        name,
        {
          kind: detectParameterKind(currentValue),
          currentValue,
          min: "",
          max: "",
          step: "",
          choices: typeof currentValue === "boolean" ? "true, false" : "",
        },
      ]),
    );

    setParameterDrafts(nextDrafts);
    setTargetMetric("sharpe");
    setNTrials("100");
    setTrainSplitPct("0.7");
    setError("");
  }, [open, parameters]);

  const parameterEntries = useMemo(
    () => Object.entries(parameterDrafts).sort(([left], [right]) => left.localeCompare(right)),
    [parameterDrafts],
  );

  const configuredCount = useMemo(
    () =>
      parameterEntries.reduce((count, [, draft]) => {
        if (draft.kind === "categorical") {
          return count + (draft.choices.split(",").map((item) => item.trim()).filter(Boolean).length > 0 ? 1 : 0);
        }
        return count + (draft.min.trim() && draft.max.trim() ? 1 : 0);
      }, 0),
    [parameterEntries],
  );

  const updateDraft = (name: string, patch: Partial<ParameterDraft>) => {
    setParameterDrafts((current) => ({
      ...current,
      [name]: { ...current[name], ...patch },
    }));
  };

  const handleStart = async () => {
    if (!backtestId) {
      setError("Backtest ID is missing.");
      return;
    }

    setRunning(true);
    setError("");
    try {
      const parsedTrials = Math.trunc(parseNumericField(nTrials, "Number of trials"));
      if (parsedTrials <= 0) {
        throw new Error("Number of trials must be greater than 0.");
      }

      const parsedSplit = parseNumericField(trainSplitPct, "Train/Test split");
      if (parsedSplit < 0.1 || parsedSplit > 1.0) {
        throw new Error("Train/Test split must be between 0.1 and 1.0.");
      }

      const parameterRanges = buildRangePayload(parameterDrafts);
      if (Object.keys(parameterRanges).length === 0) {
        throw new Error("Configure at least one parameter range or category list.");
      }

      const response = await runOptimization({
        base_backtest_id: backtestId,
        target_metric: targetMetric,
        n_trials: parsedTrials,
        train_split_pct: parsedSplit,
        parameter_ranges: parameterRanges,
      });

      const taskId = response.task_id || response.job_id;
      onTaskStarted?.(taskId);
      showToast("Optimization started", "success");
      onClose();
    } catch (caughtError) {
      const message = caughtError instanceof Error ? caughtError.message : "Could not start optimization.";
      setError(message);
    } finally {
      setRunning(false);
    }
  };

  const inputStyle: React.CSSProperties = {
    width: "100%",
    borderRadius: "12px",
    border: "1px solid rgba(148, 163, 184, 0.18)",
    background: "rgba(15, 23, 42, 0.84)",
    color: "var(--color-text)",
    padding: "12px 14px",
    outline: "none",
    fontSize: "0.88rem",
  };

  const labelStyle: React.CSSProperties = {
    display: "block",
    marginBottom: "6px",
    fontSize: "0.72rem",
    textTransform: "uppercase",
    letterSpacing: "0.12em",
    color: "#8fb9ff",
  };

  return (
    <Modal open={open} onClose={onClose} title="Optimization Sweep" width="min(1080px, 96vw)">
      <div style={{ display: "grid", gap: "18px" }}>
        <div
          style={{
            position: "relative",
            overflow: "hidden",
            borderRadius: "22px",
            border: "1px solid rgba(96, 165, 250, 0.18)",
            background:
              "radial-gradient(circle at top right, rgba(56, 189, 248, 0.18), transparent 34%), linear-gradient(135deg, rgba(15, 23, 42, 0.98), rgba(2, 6, 23, 0.98))",
            padding: "18px 20px",
          }}
        >
          <div
            style={{
              position: "absolute",
              inset: "10px",
              borderRadius: "18px",
              border: "1px dashed rgba(148, 163, 184, 0.14)",
              pointerEvents: "none",
            }}
          />
          <div style={{ position: "relative", display: "flex", justifyContent: "space-between", gap: "16px", flexWrap: "wrap" }}>
            <div>
              <div
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "8px",
                  padding: "6px 10px",
                  borderRadius: "999px",
                  background: "rgba(56, 189, 248, 0.12)",
                  color: "#7dd3fc",
                  fontSize: "0.72rem",
                  letterSpacing: "0.14em",
                  textTransform: "uppercase",
                }}
              >
                <Crosshair size={14} />
                Parameter sweep profile
              </div>
              <h3
                style={{
                  marginTop: "14px",
                  fontSize: "1.5rem",
                  lineHeight: 1.1,
                  fontFamily: '"Iowan Old Style", "Palatino Linotype", Georgia, serif',
                }}
              >
                Tune the live edges behind <span style={{ color: "#fbbf24" }}>{backtestTitle || backtestId}</span>
              </h3>
              <p
                style={{
                  marginTop: "10px",
                  maxWidth: "720px",
                  color: "var(--color-text-muted)",
                  fontSize: "0.9rem",
                  lineHeight: 1.6,
                }}
              >
                Configure numeric corridors or categorical choice sets. Only rows with an active range or category list are sent to the optimizer.
              </p>
            </div>
            <div
              style={{
                minWidth: "220px",
                display: "grid",
                gap: "10px",
                alignContent: "start",
              }}
            >
              <div
                style={{
                  borderRadius: "18px",
                  border: "1px solid rgba(245, 158, 11, 0.18)",
                  background: "rgba(245, 158, 11, 0.08)",
                  padding: "14px 16px",
                }}
              >
                <div style={{ fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "#fbbf24" }}>
                  Active parameters
                </div>
                <div style={{ marginTop: "8px", fontSize: "1.8rem", fontWeight: 700 }}>{configuredCount}</div>
              </div>
              <div
                style={{
                  borderRadius: "18px",
                  border: "1px solid rgba(74, 222, 128, 0.16)",
                  background: "rgba(22, 101, 52, 0.18)",
                  padding: "14px 16px",
                }}
              >
                <div style={{ fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "#86efac" }}>
                  Base backtest
                </div>
                <div style={{ marginTop: "8px", fontSize: "0.95rem", fontWeight: 600, wordBreak: "break-word" }}>
                  {backtestId}
                </div>
              </div>
            </div>
          </div>
        </div>

        <div
          style={{
            borderRadius: "22px",
            border: "1px solid rgba(148, 163, 184, 0.16)",
            background: "linear-gradient(180deg, rgba(15, 23, 42, 0.96), rgba(2, 6, 23, 0.96))",
            padding: "18px",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "12px", marginBottom: "16px", flexWrap: "wrap" }}>
            <div>
              <div style={{ fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.14em", color: "#94a3b8" }}>
                Parameter envelope
              </div>
              <div style={{ marginTop: "4px", fontSize: "1.02rem", fontWeight: 700 }}>
                Min / Max / Step stays visible on every numeric row
              </div>
            </div>
            <div style={{ display: "inline-flex", alignItems: "center", gap: "8px", color: "#f8fafc", fontSize: "0.84rem" }}>
              <SlidersHorizontal size={16} />
              {parameterEntries.length} tracked parameters
            </div>
          </div>

          {parameterEntries.length === 0 ? (
            <div
              style={{
                borderRadius: "18px",
                border: "1px dashed rgba(148, 163, 184, 0.22)",
                padding: "22px",
                color: "var(--color-text-muted)",
                textAlign: "center",
              }}
            >
              This backtest does not expose persisted strategy parameters yet.
            </div>
          ) : (
            <div style={{ display: "grid", gap: "12px", maxHeight: "50vh", overflowY: "auto", paddingRight: "4px" }}>
              {parameterEntries.map(([name, draft]) => {
                const isCategorical = draft.kind === "categorical";
                return (
                  <div
                    key={name}
                    style={{
                      borderRadius: "18px",
                      border: "1px solid rgba(148, 163, 184, 0.14)",
                      background: "linear-gradient(180deg, rgba(17, 24, 39, 0.88), rgba(15, 23, 42, 0.72))",
                      padding: "16px",
                    }}
                  >
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: isCategorical ? "240px minmax(0, 1fr)" : "240px minmax(0, 1fr)",
                        gap: "16px",
                        alignItems: "start",
                      }}
                    >
                      <div>
                        <div style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
                          <div style={{ fontSize: "0.96rem", fontWeight: 700 }}>{name}</div>
                          <span
                            style={{
                              display: "inline-flex",
                              alignItems: "center",
                              gap: "6px",
                              padding: "4px 10px",
                              borderRadius: "999px",
                              background:
                                draft.kind === "categorical"
                                  ? "rgba(192, 132, 252, 0.14)"
                                  : "rgba(59, 130, 246, 0.14)",
                              color: draft.kind === "categorical" ? "#d8b4fe" : "#93c5fd",
                              fontSize: "0.72rem",
                              textTransform: "uppercase",
                              letterSpacing: "0.12em",
                            }}
                          >
                            <Target size={12} />
                            {draft.kind}
                          </span>
                        </div>
                        <div style={{ marginTop: "12px", fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "#94a3b8" }}>
                          Current value
                        </div>
                        <div
                          style={{
                            marginTop: "8px",
                            display: "inline-flex",
                            alignItems: "center",
                            gap: "8px",
                            padding: "8px 12px",
                            borderRadius: "12px",
                            background: "rgba(15, 23, 42, 0.92)",
                            border: "1px solid rgba(148, 163, 184, 0.18)",
                            color: "#f8fafc",
                            fontFamily:
                              '"SFMono-Regular", "Cascadia Code", "Roboto Mono", ui-monospace, monospace',
                            fontSize: "0.82rem",
                          }}
                        >
                          <Sparkles size={14} color="#fbbf24" />
                          {formatCurrentValue(draft.currentValue)}
                        </div>
                      </div>

                      {isCategorical ? (
                        <div>
                          <label style={labelStyle}>Categories (comma-separated)</label>
                          <input
                            value={draft.choices}
                            onChange={(event) => updateDraft(name, { choices: event.target.value })}
                            placeholder={typeof draft.currentValue === "boolean" ? "true, false" : "sma, ema, wma"}
                            style={inputStyle}
                          />
                          <div style={{ marginTop: "8px", fontSize: "0.78rem", color: "var(--color-text-muted)", lineHeight: 1.5 }}>
                            Leave empty to exclude this parameter from the sweep payload.
                          </div>
                        </div>
                      ) : (
                        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: "12px" }}>
                          <div>
                            <label style={labelStyle}>Min</label>
                            <input
                              type="number"
                              value={draft.min}
                              onChange={(event) => updateDraft(name, { min: event.target.value })}
                              placeholder={typeof draft.currentValue === "number" ? String(draft.currentValue) : "0"}
                              step={draft.kind === "int" ? "1" : "0.01"}
                              style={inputStyle}
                            />
                          </div>
                          <div>
                            <label style={labelStyle}>Max</label>
                            <input
                              type="number"
                              value={draft.max}
                              onChange={(event) => updateDraft(name, { max: event.target.value })}
                              placeholder={typeof draft.currentValue === "number" ? String(draft.currentValue) : "0"}
                              step={draft.kind === "int" ? "1" : "0.01"}
                              style={inputStyle}
                            />
                          </div>
                          <div>
                            <label style={labelStyle}>Step</label>
                            <input
                              type="number"
                              value={draft.step}
                              onChange={(event) => updateDraft(name, { step: event.target.value })}
                              placeholder={draft.kind === "int" ? "1" : "optional"}
                              step={draft.kind === "int" ? "1" : "0.01"}
                              style={inputStyle}
                            />
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
            gap: "12px",
            borderRadius: "22px",
            border: "1px solid rgba(148, 163, 184, 0.16)",
            background: "linear-gradient(180deg, rgba(15, 23, 42, 0.94), rgba(2, 6, 23, 0.94))",
            padding: "18px",
          }}
        >
          <div>
            <label style={labelStyle}>Target Metric</label>
            <select value={targetMetric} onChange={(event) => setTargetMetric(event.target.value as MetricOption)} style={inputStyle}>
              {TARGET_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label style={labelStyle}>Number of Trials</label>
            <input
              type="number"
              min="1"
              step="1"
              value={nTrials}
              onChange={(event) => setNTrials(event.target.value)}
              style={inputStyle}
            />
          </div>
          <div>
            <label style={labelStyle}>Train / Test Split %</label>
            <input
              type="number"
              min="0.1"
              max="1.0"
              step="0.05"
              value={trainSplitPct}
              onChange={(event) => setTrainSplitPct(event.target.value)}
              style={inputStyle}
            />
          </div>
        </div>

        {error ? (
          <div
            style={{
              borderRadius: "16px",
              border: "1px solid rgba(251, 113, 133, 0.24)",
              background: "rgba(127, 29, 29, 0.18)",
              color: "#fecdd3",
              padding: "14px 16px",
              lineHeight: 1.5,
            }}
          >
            {error}
          </div>
        ) : null}

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "16px", flexWrap: "wrap" }}>
          <div style={{ color: "var(--color-text-muted)", fontSize: "0.82rem", lineHeight: 1.6 }}>
            The optimizer receives only configured rows. Task progress will stream into the task manager after launch.
          </div>
          <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
            <button
              type="button"
              onClick={onClose}
              style={{
                borderRadius: "999px",
                border: "1px solid rgba(148, 163, 184, 0.18)",
                background: "transparent",
                color: "var(--color-text-muted)",
                padding: "12px 18px",
                cursor: "pointer",
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleStart}
              disabled={running || parameterEntries.length === 0}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "10px",
                borderRadius: "999px",
                border: "1px solid rgba(56, 189, 248, 0.22)",
                background:
                  running
                    ? "rgba(30, 41, 59, 0.85)"
                    : "linear-gradient(135deg, rgba(56, 189, 248, 0.2), rgba(245, 158, 11, 0.22))",
                color: "#f8fafc",
                padding: "12px 18px",
                cursor: running ? "progress" : "pointer",
                boxShadow: running ? "none" : "0 18px 40px rgba(14, 165, 233, 0.18)",
                opacity: running || parameterEntries.length === 0 ? 0.7 : 1,
              }}
            >
              <Target size={16} />
              {running ? "Starting..." : "Start Optimization"}
            </button>
          </div>
        </div>
      </div>
    </Modal>
  );
}
