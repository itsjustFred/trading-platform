import { useMemo, useState } from "react";
import { Bot, Sparkles, Wand2 } from "lucide-react";

import { startAgentConvert } from "../api/client";
import type { AgentModelId, AgentProviderId } from "../api/types";
import { LiveTerminal } from "../components/LiveTerminal";

const sampleCode = `//@version=5
strategy("Impulse Cross", overlay=true)
fast = ta.ema(close, 9)
slow = ta.ema(close, 21)
longCondition = ta.crossover(fast, slow)
exitCondition = ta.crossunder(fast, slow)

if longCondition
    strategy.entry("Long", strategy.long)

if exitCondition
    strategy.close("Long")`;

const AI_MODELS: Array<{ label: string; value: AgentModelId }> = [
  { label: "Kimi K2.5 (moonshotai/kimi-k2.5)", value: "moonshotai/kimi-k2.5" },
  {
    label: "Meta Llama 3.3 70B (meta/llama-3.3-70b-instruct)",
    value: "meta/llama-3.3-70b-instruct",
  },
  {
    label: "DeepSeek V3.2 (deepseek-ai/deepseek-v3.2)",
    value: "deepseek-ai/deepseek-v3.2",
  },
  {
    label: "Qwen2.5 Coder 32B (qwen/qwen2.5-coder-32b-instruct)",
    value: "qwen/qwen2.5-coder-32b-instruct",
  },
];

const PROVIDER_OPTIONS: Array<{ label: string; value: AgentProviderId }> = [
  { label: "NVIDIA NIM", value: "nim" },
  { label: "GitHub Copilot SDK", value: "copilot_sdk" },
];

export function StrategyLabPage() {
  const [strategyName, setStrategyName] = useState("Aurora Cross");
  const [rawCode, setRawCode] = useState(sampleCode);
  const [provider, setProvider] = useState<AgentProviderId>("nim");
  const [model, setModel] = useState<AgentModelId>(AI_MODELS[0].value);
  const [customModel, setCustomModel] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState("idle");
  const [activeProvider, setActiveProvider] = useState<AgentProviderId | null>(null);
  const [activeModel, setActiveModel] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  const statusTone = useMemo(() => {
    if (jobStatus === "completed") return "#86efac";
    if (jobStatus === "failed" || jobStatus === "disconnected") return "#fca5a5";
    if (jobStatus === "running" || jobStatus === "starting") return "#67e8f9";
    return "rgba(196, 181, 253, 0.9)";
  }, [jobStatus]);

  const handleSubmit = async () => {
    if (!rawCode.trim()) {
      setError("Paste Pine, C#, or raw strategy source code before starting the agent.");
      return;
    }

    try {
      setIsSubmitting(true);
      setError("");
      setJobId(null);
      setJobStatus("starting");
      const selectedModel =
        provider === "nim"
          ? model
          : customModel.trim()
            ? customModel.trim()
            : undefined;
      const response = await startAgentConvert({
        code: rawCode,
        strategy_name: strategyName.trim() || undefined,
        provider,
        model: selectedModel,
      });
      const nextJobId = response.job_id ?? response.task_id;
      if (!nextJobId) {
        throw new Error("The conversion agent did not return a job id.");
      }
      setJobId(nextJobId);
      setActiveProvider(provider);
      setActiveModel(selectedModel ?? null);
      setJobStatus(response.status || "running");
    } catch (err: any) {
      setJobStatus("failed");
      setError(err.message || "The conversion agent could not be started.");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100%",
        padding: "24px",
        background:
          "radial-gradient(circle at top left, rgba(16, 185, 129, 0.14), transparent 28%), radial-gradient(circle at top right, rgba(129, 140, 248, 0.14), transparent 32%), linear-gradient(180deg, rgba(8, 12, 27, 0.92) 0%, rgba(13, 17, 23, 1) 100%)",
      }}
    >
      <div
        style={{
          display: "grid",
          gap: "18px",
          marginBottom: "18px",
        }}
      >
        <div
          style={{
            border: "1px solid rgba(148, 163, 184, 0.18)",
            borderRadius: "24px",
            padding: "24px",
            background:
              "linear-gradient(135deg, rgba(15, 23, 42, 0.94) 0%, rgba(17, 24, 39, 0.86) 48%, rgba(8, 47, 73, 0.72) 100%)",
            boxShadow:
              "0 30px 80px rgba(0, 0, 0, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.05)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "16px",
              flexWrap: "wrap",
            }}
          >
            <div style={{ maxWidth: "760px" }}>
              <div
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "8px",
                  padding: "6px 12px",
                  borderRadius: "999px",
                  border: "1px solid rgba(129, 140, 248, 0.24)",
                  color: "#c4b5fd",
                  background: "rgba(79, 70, 229, 0.14)",
                  fontSize: "0.72rem",
                  letterSpacing: "0.18em",
                  textTransform: "uppercase",
                }}
              >
                <Sparkles size={14} /> Strategy Lab
              </div>
              <h1
                style={{
                  marginTop: "16px",
                  fontSize: "2.1rem",
                  lineHeight: 1.05,
                  color: "#f8fafc",
                  fontFamily: '"Iowan Old Style", "Palatino Linotype", "Book Antiqua", Palatino, serif',
                  fontWeight: 700,
                  letterSpacing: "-0.02em",
                }}
              >
                Let AI rewrite raw strategy code into a backtestable trading module.
              </h1>
                <p
                style={{
                  marginTop: "12px",
                  maxWidth: "700px",
                  color: "rgba(226, 232, 240, 0.78)",
                  fontSize: "0.96rem",
                  lineHeight: 1.6,
                }}
                >
                 Drop in Pine Script, QuantConnect C#, or rough pseudo-code. The conversion
                 worker runs a staged 3-step pipeline with your selected LLM provider and
                 model, then launches a validation backtest while the terminal streams
                 every visible step.
                </p>
            </div>

            <div
              style={{
                minWidth: "240px",
                borderRadius: "18px",
                padding: "18px",
                background: "rgba(15, 23, 42, 0.62)",
                border: "1px solid rgba(148, 163, 184, 0.16)",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "10px",
                  marginBottom: "12px",
                }}
              >
                <div
                  style={{
                    width: "38px",
                    height: "38px",
                    borderRadius: "12px",
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    background: "rgba(16, 185, 129, 0.16)",
                    color: "#86efac",
                  }}
                >
                  <Bot size={18} />
                </div>
                <div>
                  <div
                    style={{
                      color: "rgba(255, 255, 255, 0.72)",
                      fontSize: "0.72rem",
                      textTransform: "uppercase",
                      letterSpacing: "0.16em",
                    }}
                  >
                    Agent Status
                  </div>
                  <div
                    style={{
                      color: statusTone,
                      fontSize: "0.9rem",
                      fontWeight: 700,
                    }}
                  >
                    {jobStatus}
                  </div>
                </div>
              </div>
              <div style={{ color: "rgba(226, 232, 240, 0.72)", fontSize: "0.82rem" }}>
                {jobId
                  ? `Live task ${jobId} · ${activeProvider || provider}${activeModel ? ` · ${activeModel}` : ""}`
                  : "Start a run to stream conversion and backtest logs."}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1.05fr) minmax(360px, 0.95fr)",
          gap: "18px",
          alignItems: "start",
        }}
      >
        <section
          style={{
            borderRadius: "24px",
            padding: "22px",
            border: "1px solid rgba(148, 163, 184, 0.18)",
            background:
              "linear-gradient(180deg, rgba(15, 23, 42, 0.94) 0%, rgba(17, 24, 39, 0.88) 100%)",
            boxShadow:
              "0 24px 70px rgba(0, 0, 0, 0.28), inset 0 1px 0 rgba(255, 255, 255, 0.04)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "12px",
              marginBottom: "18px",
              flexWrap: "wrap",
            }}
          >
            <div>
              <div
                style={{
                  color: "#f8fafc",
                  fontSize: "1.1rem",
                  fontWeight: 700,
                  marginBottom: "4px",
                }}
              >
                Source Deck
              </div>
              <div style={{ color: "rgba(226, 232, 240, 0.64)", fontSize: "0.84rem" }}>
                Feed the agent the raw logic. It will decide the framework, write docs,
                and test the result.
              </div>
            </div>
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "8px",
                padding: "8px 12px",
                borderRadius: "999px",
                background: "rgba(16, 185, 129, 0.12)",
                color: "#a7f3d0",
                border: "1px solid rgba(16, 185, 129, 0.18)",
                fontSize: "0.76rem",
              }}
            >
              <Wand2 size={14} /> Pine / C# / pseudo-code accepted
            </div>
          </div>

          <div
            style={{
              display: "grid",
              gap: "14px",
            }}
          >
            <label style={{ display: "grid", gap: "8px" }}>
              <span
                style={{
                  color: "rgba(226, 232, 240, 0.82)",
                  fontSize: "0.78rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.14em",
                }}
              >
                LLM Provider
              </span>
              <select
                value={provider}
                onChange={(event) => {
                  const nextProvider = event.target.value as AgentProviderId;
                  setProvider(nextProvider);
                  if (nextProvider === "nim" && !AI_MODELS.some((option) => option.value === model)) {
                    setModel(AI_MODELS[0].value);
                  }
                }}
                style={{
                  width: "100%",
                  borderRadius: "14px",
                  border: "1px solid rgba(148, 163, 184, 0.18)",
                  background: "rgba(15, 23, 42, 0.68)",
                  color: "#f8fafc",
                  padding: "14px 16px",
                  outline: "none",
                  fontSize: "0.95rem",
                }}
              >
                {PROVIDER_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value} style={{ color: "#020617" }}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>

            <label style={{ display: "grid", gap: "8px" }}>
              <span
                style={{
                  color: "rgba(226, 232, 240, 0.82)",
                  fontSize: "0.78rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.14em",
                }}
              >
                Strategy Name
              </span>
              <input
                type="text"
                value={strategyName}
                onChange={(event) => setStrategyName(event.target.value)}
                placeholder="Aurora Breakout"
                style={{
                  width: "100%",
                  borderRadius: "14px",
                  border: "1px solid rgba(148, 163, 184, 0.18)",
                  background: "rgba(15, 23, 42, 0.68)",
                  color: "#f8fafc",
                  padding: "14px 16px",
                  outline: "none",
                  fontSize: "0.95rem",
                }}
              />
            </label>

            <label style={{ display: "grid", gap: "8px" }}>
              <span
                style={{
                  color: "rgba(226, 232, 240, 0.82)",
                  fontSize: "0.78rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.14em",
                }}
              >
                AI Model
              </span>
              {provider === "nim" ? (
                <select
                  value={model}
                  onChange={(event) => setModel(event.target.value as AgentModelId)}
                  style={{
                    width: "100%",
                    borderRadius: "14px",
                    border: "1px solid rgba(148, 163, 184, 0.18)",
                    background: "rgba(15, 23, 42, 0.68)",
                    color: "#f8fafc",
                    padding: "14px 16px",
                    outline: "none",
                    fontSize: "0.95rem",
                  }}
                >
                  {AI_MODELS.map((option) => (
                    <option key={option.value} value={option.value} style={{ color: "#020617" }}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={customModel}
                  onChange={(event) => setCustomModel(event.target.value)}
                  placeholder="Optional model override (default: gpt-5-mini)"
                  style={{
                    width: "100%",
                    borderRadius: "14px",
                    border: "1px solid rgba(148, 163, 184, 0.18)",
                    background: "rgba(15, 23, 42, 0.68)",
                    color: "#f8fafc",
                    padding: "14px 16px",
                    outline: "none",
                    fontSize: "0.95rem",
                  }}
                />
              )}
            </label>

            <label style={{ display: "grid", gap: "8px" }}>
              <span
                style={{
                  color: "rgba(226, 232, 240, 0.82)",
                  fontSize: "0.78rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.14em",
                }}
              >
                Raw Strategy Code
              </span>
              <textarea
                value={rawCode}
                onChange={(event) => setRawCode(event.target.value)}
                placeholder="Paste Pine Script, LEAN C#, or raw strategy logic..."
                spellCheck={false}
                style={{
                  minHeight: "560px",
                  width: "100%",
                  resize: "vertical",
                  borderRadius: "18px",
                  border: "1px solid rgba(148, 163, 184, 0.18)",
                  background:
                    "linear-gradient(180deg, rgba(2, 6, 23, 0.98) 0%, rgba(15, 23, 42, 0.94) 100%)",
                  color: "#dbeafe",
                  padding: "18px",
                  outline: "none",
                  fontSize: "0.9rem",
                  lineHeight: 1.6,
                  fontFamily:
                    '"IBM Plex Mono", "SFMono-Regular", Menlo, Monaco, Consolas, monospace',
                  boxShadow: "inset 0 1px 0 rgba(255, 255, 255, 0.02)",
                }}
              />
            </label>

            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "14px",
                flexWrap: "wrap",
              }}
              >
                <div style={{ color: "rgba(226, 232, 240, 0.58)", fontSize: "0.8rem" }}>
                  The agent writes a Python strategy file, emits Markdown documentation, and
                  backtests the output in the background after the analysis, meta, and code
                  generation steps complete using provider {provider}
                  {provider === "nim" ? ` (${model})` : customModel.trim() ? ` (${customModel.trim()})` : ""}.
                </div>
              <button
                onClick={handleSubmit}
                disabled={isSubmitting}
                style={{
                  border: "none",
                  borderRadius: "999px",
                  padding: "14px 22px",
                  cursor: isSubmitting ? "wait" : "pointer",
                  color: "#020617",
                  fontWeight: 800,
                  fontSize: "0.88rem",
                  letterSpacing: "0.06em",
                  textTransform: "uppercase",
                  background:
                    "linear-gradient(135deg, #34d399 0%, #38bdf8 52%, #c084fc 100%)",
                  boxShadow: "0 16px 40px rgba(56, 189, 248, 0.24)",
                  opacity: isSubmitting ? 0.72 : 1,
                }}
              >
                {isSubmitting ? "Starting..." : "Auto-Convert & Test"}
              </button>
            </div>

            {error && (
              <div
                style={{
                  borderRadius: "14px",
                  border: "1px solid rgba(248, 113, 113, 0.28)",
                  background: "rgba(127, 29, 29, 0.22)",
                  color: "#fecaca",
                  padding: "12px 14px",
                  fontSize: "0.84rem",
                }}
              >
                {error}
              </div>
            )}
          </div>
        </section>

        <section
          style={{
            display: "grid",
            gap: "14px",
          }}
        >
          <div
            style={{
              borderRadius: "24px",
              padding: "20px",
              border: "1px solid rgba(148, 163, 184, 0.18)",
              background:
                "linear-gradient(180deg, rgba(15, 23, 42, 0.95) 0%, rgba(17, 24, 39, 0.88) 100%)",
            }}
          >
            <div
              style={{
                color: "#f8fafc",
                fontSize: "1rem",
                fontWeight: 700,
                marginBottom: "6px",
              }}
            >
              Live Conversion Feed
            </div>
            <div style={{ color: "rgba(226, 232, 240, 0.64)", fontSize: "0.82rem" }}>
              Watch the generated response, file writes, and runner output as the task
              log updates.
            </div>
          </div>

          <LiveTerminal
            jobId={jobId}
            onStatusChange={setJobStatus}
            emptyMessage={`Waiting for the 3-step ${provider} worker to start...`}
          />
        </section>
      </div>
    </div>
  );
}
