import { useState, useEffect, useMemo, type CSSProperties } from "react";
import { Modal } from "./Modal";
import {
  getDataCheck,
  runBacktest,
} from "../api/client";
import { SymbolInput } from "./SymbolInput";
import { Play, Loader2 } from "lucide-react";
import { useToast } from "./ToastProvider";
import { useStrategiesCache } from "./StrategiesProvider";
import { useStrategyDetail } from "../api/queries";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

// Canonical timeframe order from smallest to largest (matches shared/constants.py)
const TIMEFRAME_ORDER = ["tick", "s1", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"] as const;
const TIMEFRAME_LABELS: Record<string, string> = {
  tick: "Tick",
  s1: "1 Second",
  "1m": "1 Minute",
  "5m": "5 Minutes",
  "15m": "15 Minutes",
  "30m": "30 Minutes",
  "1h": "1 Hour",
  "4h": "4 Hours",
  "1d": "1 Day",
  "1w": "1 Week",
};

function getModelingTimeframes(mainTf: string): string[] {
  const idx = TIMEFRAME_ORDER.indexOf(mainTf as (typeof TIMEFRAME_ORDER)[number]);
  if (idx < 0) return [];
  // Include all timeframes <= mainTf (the main TF itself plus all smaller ones)
  return TIMEFRAME_ORDER.slice(0, idx + 1);
}

// Availability checks run one request per symbol; debounced so typing a symbol list
// does not fire N requests per keystroke.
const AVAILABILITY_CHECK_DEBOUNCE_MS = 400;

interface CreateBacktestModalProps {
  open: boolean;
  onClose: () => void;
  preselectedStrategy?: string;
  onTaskStarted?: (taskId: string) => void;
}

export function CreateBacktestModal({
  open,
  onClose,
  preselectedStrategy,
  onTaskStarted,
}: CreateBacktestModalProps) {
  const { showToast } = useToast();
  const { strategies, ensureLoaded } = useStrategiesCache();
  const [strategy, setStrategy] = useState("");
  const [engine, setEngine] = useState("native");
  const [symbols, setSymbols] = useState("AAPL");
  const [timeframe, setTimeframe] = useState("1d");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [capital, setCapital] = useState("100000");
  const [riskPct, setRiskPct] = useState("");
  const [maxLeverage, setMaxLeverage] = useState("1.0");
  const [modelingResolution, setModelingResolution] = useState<string>("1d");
  const [strategyParameters, setStrategyParameters] = useState<Record<string, string>>({});
  // Track original type to decide input type and payload conversion
  const [parameterTypes, setParameterTypes] = useState<Record<string, "number" | "string">>({});
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [checkingDataAvailability, setCheckingDataAvailability] = useState(false);
  const [dataAvailabilityWarning, setDataAvailabilityWarning] = useState("");
  const strategyDetailQuery = useStrategyDetail(strategy, { enabled: open && Boolean(strategy) });

  useEffect(() => {
    if (!open) return;
    void ensureLoaded();
  }, [open, ensureLoaded]);

  useEffect(() => {
    if (preselectedStrategy && strategies.length > 0) {
      const match = strategies.find((s) => s.name === preselectedStrategy);
      if (match) setStrategy(match.file || "");
    }
  }, [preselectedStrategy, strategies]);

  // Sync strategy parameters from query detail when strategy changes
  useEffect(() => {
    if (!strategy) {
      setStrategyParameters({});
      setParameterTypes({});
      return;
    }

    setStrategyParameters({});
    setParameterTypes({});
  }, [strategy]);

  useEffect(() => {
    const detail = strategyDetailQuery.data;
    if (!strategy || !detail) {
      return;
    }
    const values: Record<string, string> = {};
    const types: Record<string, "number" | "string"> = {};
    for (const [key, value] of Object.entries(detail.parameters || {})) {
      if (typeof value === "number" && Number.isFinite(value)) {
        values[key] = String(value);
        types[key] = "number";
      } else if (typeof value === "string") {
        values[key] = value;
        types[key] = "string";
      }
    }
    setStrategyParameters(values);
    setParameterTypes(types);
  }, [strategy, strategyDetailQuery.data]);

  // Auto-detect engine from selected strategy (no manual override)
  useEffect(() => {
    if (!strategy) return;
    const meta = strategies.find((item) => item.file === strategy);
    setEngine(meta?.engine === "pynecore" ? "pynecore" : "native");
  }, [strategy, strategies]);

  // Update modeling resolution default when main timeframe changes
  useEffect(() => {
    setModelingResolution(timeframe);
  }, [timeframe]);

  const modelingTimeframes = useMemo(() => getModelingTimeframes(timeframe), [timeframe]);
  const symbolList = useMemo(
    () =>
      symbols
        .split(",")
        .map((symbolValue) => symbolValue.trim().toUpperCase())
        .filter(Boolean),
    [symbols],
  );
  const debouncedAvailabilitySymbols = useDebouncedValue(
    symbolList,
    AVAILABILITY_CHECK_DEBOUNCE_MS,
  );

  useEffect(() => {
    if (!open) {
      setDataAvailabilityWarning("");
      setCheckingDataAvailability(false);
      return;
    }

    if (!debouncedAvailabilitySymbols.length || !timeframe) {
      setDataAvailabilityWarning("");
      setCheckingDataAvailability(false);
      return;
    }

    let cancelled = false;
    setCheckingDataAvailability(true);
    const runCheck = async () => {
      try {
        const results = await Promise.all(
          debouncedAvailabilitySymbols.map(async (symbolValue) => ({
            symbol: symbolValue,
            ...(await getDataCheck(symbolValue, timeframe, start, end)),
          })),
        );
        if (cancelled) {
          return;
        }
        const missingSymbols = results.filter((entry) => !entry.available).map((entry) => entry.symbol);
        if (missingSymbols.length > 0) {
          const preview = missingSymbols.slice(0, 3).join(", ");
          const suffix = missingSymbols.length > 3 ? `, +${missingSymbols.length - 3} more` : "";
          setDataAvailabilityWarning(
            `Data not available, will be downloaded (~2min) — ${preview}${suffix}`,
          );
        } else {
          setDataAvailabilityWarning("");
        }
      } catch {
        if (!cancelled) {
          setDataAvailabilityWarning("");
        }
      } finally {
        if (!cancelled) {
          setCheckingDataAvailability(false);
        }
      }
    };

    void runCheck();
    return () => {
      cancelled = true;
    };
  }, [open, debouncedAvailabilitySymbols, timeframe, start, end]);

  const handleRun = async () => {
    if (!strategy || symbolList.length === 0) {
      return;
    }
    setRunning(true);
    setError("");

    try {
      const parameterPayload: Record<string, number | string> = {};
      for (const [key, rawValue] of Object.entries(strategyParameters)) {
        if (parameterTypes[key] === "number") {
          const parsed = Number.parseFloat(rawValue);
          if (!Number.isFinite(parsed)) {
            setError(`Parameter '${key}' must be a valid number`);
            setRunning(false);
            return;
          }
          parameterPayload[key] = parsed;
        } else {
          parameterPayload[key] = rawValue;
        }
      }

      const leverageValue = Number.parseFloat(maxLeverage);
      if (!Number.isFinite(leverageValue) || leverageValue <= 0) {
        setError("Max leverage must be a positive number");
        setRunning(false);
        return;
      }

      const res = await runBacktest({
        strategy,
        engine,
        symbols: symbolList,
        timeframe,
        start,
        end,
        capital: Number.parseFloat(capital),
        risk_per_trade_pct: riskPct ? Number.parseFloat(riskPct) / 100 : undefined,
        max_leverage: leverageValue,
        modeling_resolution: modelingResolution,
        parameters: parameterPayload,
      });
      const taskId = res.task_id || res.job_id;
      onTaskStarted?.(taskId);
      showToast("Backtest started. Check Task Manager in the top right.", "success");
      onClose();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to start backtest");
    } finally {
      setRunning(false);
    }
  };

  const parameterEntries = Object.entries(strategyParameters);

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

  const engineBadgeStyle: CSSProperties = {
    display: "inline-block",
    padding: "4px 10px",
    borderRadius: "4px",
    fontSize: "0.78rem",
    fontWeight: 600,
    letterSpacing: "0.5px",
    ...ENGINE_BADGE_TONES[engine] ?? ENGINE_BADGE_TONES.default,
  };

  return (
    <Modal open={open} onClose={onClose} title="Create Backtest" width="560px">
      <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
        {/* Strategy + Engine badge */}
        <div>
          <label style={labelStyle}>Strategy</label>
          <select
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
            style={inputStyle}
          >
            <option value="">— Select strategy —</option>
            {strategies.map((s) => (
              <option key={s.file} value={s.file}>
                {s.name}
              </option>
            ))}
          </select>
          {strategy && (
            <div style={{ marginTop: "6px", display: "flex", alignItems: "center", gap: "6px" }}>
              <span style={{ ...labelStyle, marginBottom: 0 }}>Engine</span>
              <span style={engineBadgeStyle}>{engine}</span>
            </div>
          )}
        </div>

        {/* Symbols */}
        <div>
          <label style={labelStyle}>Symbols (comma-separated)</label>
          <SymbolInput
            value={symbols}
            onChange={setSymbols}
            placeholder="AAPL, MSFT"
            style={inputStyle}
          />
        </div>

        {/* Timeframe */}
        <div>
          <label style={labelStyle}>Timeframe</label>
          <select
            value={timeframe}
            onChange={(e) => setTimeframe(e.target.value)}
            style={inputStyle}
          >
            <option value="1m">1m</option>
            <option value="5m">5m</option>
            <option value="15m">15m</option>
            <option value="30m">30m</option>
            <option value="1h">1h</option>
            <option value="4h">4h</option>
            <option value="1d">1d</option>
          </select>
        </div>

        {/* Date range */}
        <div style={{ display: "flex", gap: "10px" }}>
          <div style={{ flex: 1 }}>
            <label style={labelStyle}>Start (optional)</label>
            <input
              type="date"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              style={inputStyle}
            />
          </div>
          <div style={{ flex: 1 }}>
            <label style={labelStyle}>End (optional)</label>
            <input
              type="date"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
              style={inputStyle}
            />
          </div>
        </div>
        <div
          style={{
            fontSize: "0.7rem",
            color: "var(--color-text-muted)",
            marginTop: "-8px",
          }}
        >
          Leave empty to use full available history
        </div>
        {checkingDataAvailability && (
          <div
            style={{
              fontSize: "0.74rem",
              color: "var(--color-text-muted)",
              marginTop: "-6px",
            }}
          >
            Checking data availability...
          </div>
        )}
        {dataAvailabilityWarning && (
          <div
            style={{
              padding: "8px 12px",
              borderRadius: "6px",
              background: "rgba(251, 191, 36, 0.12)",
              border: "1px solid rgba(251, 191, 36, 0.3)",
              color: "#fbbf24",
              fontSize: "0.78rem",
            }}
          >
            {dataAvailabilityWarning}
          </div>
        )}

        {/* Capital */}
        <div>
          <label style={labelStyle}>Initial Capital ($)</label>
          <input
            type="number"
            value={capital}
            onChange={(e) => setCapital(e.target.value)}
            style={inputStyle}
          />
        </div>

        {/* Risk Per Trade */}
        <div>
          <label style={labelStyle}>Risk Per Trade (%)</label>
          <input
            type="number"
            value={riskPct}
            onChange={(e) => setRiskPct(e.target.value)}
            placeholder="e.g. 1 for 1% — leave empty for all-in"
            step="0.1"
            min="0.1"
            max="100"
            style={inputStyle}
          />
          <div
            style={{
              fontSize: "0.7rem",
              color: "var(--color-text-muted)",
              marginTop: "2px",
            }}
          >
            Max loss per trade as % of equity. Empty = all-in sizing.
          </div>
        </div>

        {/* Max Leverage + Modeling Resolution */}
        <div style={{ display: "flex", gap: "10px" }}>
          <div style={{ flex: 1 }}>
            <label style={labelStyle}>Max Leverage</label>
            <input
              type="number"
              value={maxLeverage}
              onChange={(e) => setMaxLeverage(e.target.value)}
              min="1"
              step="0.1"
              style={inputStyle}
            />
            <div
              style={{
                fontSize: "0.7rem",
                color: "var(--color-text-muted)",
                marginTop: "2px",
              }}
            >
              1.0 = no leverage. Higher values only raise the notional cap.
            </div>
          </div>
          <div style={{ flex: 1 }}>
            <label style={labelStyle}>Modeling Resolution</label>
            {modelingTimeframes.length > 0 ? (
              <select
                value={modelingResolution}
                onChange={(e) => setModelingResolution(e.target.value)}
                style={inputStyle}
              >
                {[...modelingTimeframes].reverse().map((tf) => (
                  <option key={tf} value={tf}>
                    {tf === timeframe
                      ? `${TIMEFRAME_LABELS[tf] ?? tf} (No Intrabar)`
                      : (TIMEFRAME_LABELS[tf] ?? tf)}
                  </option>
                ))}
              </select>
            ) : (
              <div
                style={{
                  ...inputStyle,
                  color: "var(--color-text-muted)",
                  fontStyle: "italic",
                  cursor: "not-allowed",
                }}
              >
                No finer resolution available
              </div>
            )}
            <div
              style={{
                fontSize: "0.7rem",
                color: "var(--color-text-muted)",
                marginTop: "2px",
              }}
            >
              Timeframe used for stop/TP precision (MT5-style modeling).
            </div>
          </div>
        </div>

        {/* Strategy Parameters */}
        {parameterEntries.length > 0 && (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: "10px",
              padding: "12px",
              border: "1px solid var(--color-border)",
              borderRadius: "8px",
              background: "var(--color-surface2)",
            }}
          >
            <div style={{ ...labelStyle, marginBottom: "0" }}>Strategy Parameters</div>
            {parameterEntries.map(([key, value]) => (
              <div key={key}>
                <label style={labelStyle}>{key}</label>
                <input
                  type={parameterTypes[key] === "string" ? "text" : "number"}
                  value={value}
                  onChange={(e) =>
                    setStrategyParameters((current) => ({
                      ...current,
                      [key]: e.target.value,
                    }))
                  }
                  step={parameterTypes[key] === "string" ? undefined : "any"}
                  style={inputStyle}
                />
              </div>
            ))}
          </div>
        )}

        {/* Error */}
        {error && (
          <div
            style={{
              padding: "8px 12px",
              borderRadius: "6px",
              background: "rgba(248,81,73,0.08)",
              color: "#f85149",
              fontSize: "0.8rem",
              whiteSpace: "pre-wrap",
              maxHeight: "120px",
              overflowY: "auto",
            }}
          >
            {error}
          </div>
        )}

        {/* Run button */}
        <button
          type="button"
          onClick={handleRun}
          disabled={running || !strategy || symbolList.length === 0}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "6px",
            background:
              running || !strategy || symbolList.length === 0 ? "#333" : "#58a6ff",
            color: "#fff",
            border: "none",
            borderRadius: "6px",
            padding: "10px",
            fontSize: "0.85rem",
            fontWeight: 600,
            cursor: running ? "wait" : "pointer",
          }}
        >
          {running ? (
            <Loader2 size={16} className="animate-spin" />
          ) : (
            <Play size={16} />
          )}
          {running ? "Starting..." : "Run Backtest"}
        </button>
      </div>
    </Modal>
  );
}
