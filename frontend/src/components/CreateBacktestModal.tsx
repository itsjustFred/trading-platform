import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Modal } from "./Modal";
import {
  getStrategies,
  runBacktest,
  getBacktestRunStatus,
} from "../api/client";
import { SymbolInput } from "./SymbolInput";
import { Play, Loader2 } from "lucide-react";

interface CreateBacktestModalProps {
  open: boolean;
  onClose: () => void;
  preselectedStrategy?: string;
}

export function CreateBacktestModal({
  open,
  onClose,
  preselectedStrategy,
}: CreateBacktestModalProps) {
  const navigate = useNavigate();
  const [strategies, setStrategies] = useState<any[]>([]);
  const [strategy, setStrategy] = useState("");
  const [engine, setEngine] = useState("vectorbt");
  const [symbols, setSymbols] = useState("AAPL");
  const [timeframe, setTimeframe] = useState("1d");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [capital, setCapital] = useState("100000");
  const [riskPct, setRiskPct] = useState("");
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (open) {
      getStrategies()
        .then(setStrategies)
        .catch(() => {});
    }
  }, [open]);

  useEffect(() => {
    if (preselectedStrategy && strategies.length > 0) {
      const match = strategies.find((s) => s.name === preselectedStrategy);
      if (match) setStrategy(match.file || "");
    }
  }, [preselectedStrategy, strategies]);

  // Auto-detect engine based on strategy file
  useEffect(() => {
    if (!strategy) return;
    if (strategy.startsWith("pynecore_")) {
      setEngine("pynecore");
    } else {
      setEngine("vectorbt");
    }
  }, [strategy]);

  const handleRun = async () => {
    if (!strategy || !symbols.trim()) return;
    setRunning(true);
    setStatus("Starting backtest...");
    setError("");

    try {
      const symbolList = symbols
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const res = await runBacktest({
        strategy,
        engine,
        symbols: symbolList,
        timeframe,
        start,
        end,
        capital: parseFloat(capital),
        risk_per_trade_pct: riskPct ? parseFloat(riskPct) / 100 : undefined,
      });

      const jobId = res.job_id;
      setStatus("Running backtest...");

      // Poll for completion
      const poll = async () => {
        for (let i = 0; i < 120; i++) {
          await new Promise((r) => setTimeout(r, 2500));
          try {
            const jobStatus = await getBacktestRunStatus(jobId);
            if (jobStatus.status === "completed") {
              setStatus("Completed! Redirecting...");
              setRunning(false);
              onClose();
              // Reload backtests page
              navigate("/backtests");
              // Force page refresh to pick up new backtest
              setTimeout(() => window.location.reload(), 500);
              return;
            } else if (jobStatus.status === "failed") {
              setError(jobStatus.error || "Backtest failed");
              setRunning(false);
              return;
            }
            setStatus(`Running... (${(i + 1) * 2.5}s)`);
          } catch {
            // Continue polling
          }
        }
        setError("Backtest timed out after 5 minutes");
        setRunning(false);
      };
      poll();
    } catch (err: any) {
      setError(err.message || "Failed to start backtest");
      setRunning(false);
    }
  };

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

  return (
    <Modal open={open} onClose={onClose} title="Create Backtest" width="560px">
      <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
        {/* Strategy */}
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
        </div>

        {/* Engine */}
        <div>
          <label style={labelStyle}>Engine</label>
          <select
            value={engine}
            onChange={(e) => setEngine(e.target.value)}
            style={{
              ...inputStyle,
              opacity: strategy.startsWith("pynecore_") ? 0.6 : 1,
            }}
            disabled={strategy.startsWith("pynecore_")}
          >
            <option value="vectorbt">vectorbt</option>
            <option value="pynecore">pynecore</option>
            <option value="lean">lean</option>
          </select>
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

        {/* Status / Error */}
        {status && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              padding: "8px 12px",
              borderRadius: "6px",
              background: "rgba(88,166,255,0.08)",
              color: "#58a6ff",
              fontSize: "0.8rem",
            }}
          >
            {running && <Loader2 size={14} className="animate-spin" />}
            {status}
          </div>
        )}
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
          onClick={handleRun}
          disabled={running || !strategy || !symbols.trim()}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "6px",
            background:
              running || !strategy || !symbols.trim() ? "#333" : "#58a6ff",
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
          {running ? "Running..." : "Run Backtest"}
        </button>
      </div>
    </Modal>
  );
}
