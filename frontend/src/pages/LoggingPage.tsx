import { useEffect, useRef, useState } from "react";

import { getLogContent, getLogs } from "../api/client";

const ALL_LOGS = "ALL_LOGS";
const LEVEL_OPTIONS = ["DEBUG", "INFO", "WARN", "ERROR"] as const;

const levelColors: Record<string, string> = {
  ERROR: "#f87171",
  WARNING: "#fbbf24",
  INFO: "#60a5fa",
  DEBUG: "#94a3b8",
};

function normalizeLevelForApi(level: string): string {
  return level === "WARN" ? "WARNING" : level;
}

function detectLineLevel(line: string): string {
  if (line.includes("ERROR")) return "ERROR";
  if (line.includes("WARNING") || line.includes("WARN")) return "WARNING";
  if (line.includes("INFO")) return "INFO";
  if (line.includes("DEBUG")) return "DEBUG";
  return "";
}

export function LoggingPage() {
  const [logFiles, setLogFiles] = useState<string[]>([]);
  const [selectedFile, setSelectedFile] = useState(ALL_LOGS);
  const [lines, setLines] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [selectedLevels, setSelectedLevels] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getLogs()
      .then((files) => {
        const names = files.map((file) => file.name);
        const ordered = [
          ALL_LOGS,
          ...names.filter((name) => name !== ALL_LOGS),
        ];
        const uniqueNames = [...new Set(ordered)];
        setLogFiles(uniqueNames);
      })
      .catch(() => {
        setLogFiles([ALL_LOGS]);
        setSelectedFile(ALL_LOGS);
      });
  }, []);

  useEffect(() => {
    if (logFiles.length > 0 && !logFiles.includes(selectedFile)) {
      setSelectedFile(logFiles[0]);
    }
  }, [logFiles, selectedFile]);

  useEffect(() => {
    if (!selectedFile) return;
    setLoading(true);
    const levelParam =
      selectedLevels.length > 0
        ? selectedLevels.map(normalizeLevelForApi).join(",")
        : undefined;

    getLogContent(selectedFile, 500, levelParam)
      .then((data) => {
        setLines(data.lines || []);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [selectedFile, selectedLevels]);

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [lines]);

  const filtered = lines.filter((line) => {
    if (!search) return true;
    return line.toLowerCase().includes(search.toLowerCase());
  });

  const toggleLevel = (level: string) => {
    setSelectedLevels((current) => {
      if (current.includes(level)) {
        const next = current.filter((item) => item !== level);
        return next.length > 0 ? next : [];
      }
      return [...current.filter((item) => item !== "ALL"), level];
    });
  };

  const allLevelsActive = selectedLevels.length === 0;

  return (
    <div style={{ padding: "24px", display: "grid", gap: "18px" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: "16px",
          alignItems: "flex-end",
          flexWrap: "wrap",
        }}
      >
        <div>
          <h1
            style={{ fontSize: "1.6rem", fontWeight: 700, marginBottom: "6px" }}
          >
            Logging
          </h1>
          <div
            style={{ color: "var(--color-text-muted)", fontSize: "0.86rem" }}
          >
            Inspect a single service log or merge every top-level log into one
            chronological stream.
          </div>
        </div>
        <div style={{ color: "var(--color-text-muted)", fontSize: "0.8rem" }}>
          Showing{" "}
          <strong style={{ color: "var(--color-text)" }}>{selectedFile}</strong>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gap: "14px",
          padding: "16px",
          borderRadius: "16px",
          border: "1px solid var(--color-border)",
          background: "rgba(15, 23, 42, 0.72)",
        }}
      >
        <div
          style={{
            display: "flex",
            gap: "10px",
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <select
            value={selectedFile}
            onChange={(event) => setSelectedFile(event.target.value)}
            style={{
              minWidth: "220px",
              background: "var(--color-surface2)",
              border: "1px solid var(--color-border)",
              color: "var(--color-text)",
              padding: "9px 12px",
              borderRadius: "10px",
              fontSize: "0.84rem",
            }}
          >
            {logFiles.map((file) => (
              <option key={file} value={file}>
                {file === ALL_LOGS ? "ALL LOGS" : file}
              </option>
            ))}
          </select>

          <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
            <LevelToggleButton
              label="ALL"
              active={allLevelsActive}
              onClick={() => setSelectedLevels([])}
            />
            {LEVEL_OPTIONS.map((level) => (
              <LevelToggleButton
                key={level}
                label={level}
                active={!allLevelsActive && selectedLevels.includes(level)}
                disabled={false}
                onClick={() => toggleLevel(level)}
              />
            ))}
          </div>

          <input
            type="text"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search log lines..."
            style={{
              flex: "1 1 260px",
              minWidth: "220px",
              background: "var(--color-surface2)",
              border: "1px solid var(--color-border)",
              color: "var(--color-text)",
              padding: "9px 12px",
              borderRadius: "10px",
              fontSize: "0.84rem",
            }}
          />
        </div>

        <div style={{ color: "var(--color-text-muted)", fontSize: "0.78rem" }}>
          {allLevelsActive
            ? "Level filter: ALL"
            : `Level filter: ${selectedLevels.join(", ")}`}
        </div>
      </div>

      <div
        ref={containerRef}
        style={{
          background: "var(--color-surface)",
          border: "1px solid var(--color-border)",
          borderRadius: "18px",
          padding: "14px",
          height: "calc(100vh - 250px)",
          overflowY: "auto",
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
          fontSize: "0.76rem",
          lineHeight: 1.65,
        }}
      >
        {loading ? (
          <div
            style={{
              color: "var(--color-text-muted)",
              textAlign: "center",
              padding: "40px",
            }}
          >
            Loading logs...
          </div>
        ) : filtered.length === 0 ? (
          <div
            style={{
              color: "var(--color-text-muted)",
              textAlign: "center",
              padding: "40px",
            }}
          >
            {logFiles.length <= 1
              ? "No log files found in ~/logs/"
              : "No matching log entries"}
          </div>
        ) : (
          filtered.map((line, index) => {
            const level = detectLineLevel(line);
            return (
              <div
                key={`${index}:${line.slice(0, 24)}`}
                style={{
                  padding: "2px 0",
                  color: levelColors[level] || "var(--color-text)",
                  borderBottom: "1px solid rgba(48, 54, 61, 0.22)",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                }}
              >
                {line}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function LevelToggleButton({
  label,
  active,
  disabled = false,
  onClick,
}: {
  label: string;
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: "8px 11px",
        borderRadius: "999px",
        border: active
          ? "1px solid rgba(96, 165, 250, 0.42)"
          : "1px solid rgba(148, 163, 184, 0.18)",
        background: active
          ? "rgba(37, 99, 235, 0.18)"
          : "rgba(15, 23, 42, 0.64)",
        color: active ? "#dbeafe" : "var(--color-text-muted)",
        fontSize: "0.76rem",
        fontWeight: 700,
        letterSpacing: "0.06em",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.45 : 1,
      }}
    >
      {label}
    </button>
  );
}
