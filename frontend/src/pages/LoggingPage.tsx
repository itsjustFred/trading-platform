import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { getLogContent, getLogs } from "../api/client";
import { useToast } from "../components/ToastProvider";
import { MultiSelectDropdown } from "../components/MultiSelectDropdown";

const ALL_LOGS = "ALL_LOGS";
const LEVEL_OPTIONS = ["ERROR", "WARNING", "INFO", "DEBUG"] as const;
const DEFAULT_LEVELS: Record<string, boolean> = {
  ERROR: true,
  WARNING: true,
  INFO: true,
  DEBUG: false,
};
const POLLING_PATHS = ["/api/tasks", "/api/data/tasks", "/api/live/accounts", "/api/live/deployments"];
const POLLING_PREFIXES = ["/api/tasks/", "/api/data/tasks/", "/api/live/balances/", "/api/live/stats/", "/api/live/trades/"];
const LOG_FETCH_LIMIT = 50000;

type Level = (typeof LEVEL_OPTIONS)[number];

interface ParsedLogLine {
  raw: string;
  timestampShort: string;
  timestampLong: string;
  level: Level;
  module: string;
  func: string;
  message: string;
  data: Record<string, unknown> | null;
  isJson: boolean;
}

interface VirtualLogRow {
  key: string;
  rowType: "entry" | "detail";
  entry: ParsedLogLine;
  rowId: string;
  index: number;
}

const LOG_TABLE_GRID_TEMPLATE = "130px 110px 170px 170px minmax(420px, 1fr)";

function normalizeLevel(raw: string | undefined): Level {
  const normalized = String(raw ?? "").toUpperCase();
  if (normalized.includes("ERROR")) return "ERROR";
  if (normalized.includes("WARN")) return "WARNING";
  if (normalized.includes("DEBUG")) return "DEBUG";
  return "INFO";
}

function levelColor(level: Level): string {
  if (level === "ERROR") return "#f87171";
  if (level === "WARNING") return "#fbbf24";
  if (level === "DEBUG") return "#9ca3af";
  return "#93c5fd";
}

function parseTimestamp(input: unknown): { short: string; long: string } {
  const raw = String(input ?? "").trim();
  if (!raw) {
    return { short: "--:--:--", long: "Unknown timestamp" };
  }
  const date = new Date(raw);
  if (!Number.isNaN(date.getTime())) {
    const short = date.toLocaleTimeString("en-GB", { hour12: false });
    const long = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(
      date.getDate(),
    ).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(
      date.getMinutes(),
    ).padStart(2, "0")}:${String(date.getSeconds()).padStart(2, "0")}`;
    return { short, long };
  }

  const match = raw.match(/(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/);
  if (match) {
    return { short: match[2], long: `${match[1]} ${match[2]}` };
  }
  return { short: raw.slice(0, 8), long: raw };
}

function tryParseJson(line: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(line) as unknown;
    if (typeof parsed === "object" && parsed !== null) {
      return parsed as Record<string, unknown>;
    }
    return null;
  } catch {
    return null;
  }
}

function parsePlainLine(line: string): ParsedLogLine {
  const timestampMatch = line.match(/(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})/);
  const levelMatch = line.match(/\b(ERROR|WARNING|WARN|INFO|DEBUG)\b/i);
  const moduleFuncMatch = line.match(
    /\b([A-Za-z_][A-Za-z0-9_.-]*)\.([A-Za-z_][A-Za-z0-9_<>-]*)\b/,
  );
  const tracebackMatch = line.match(
    /File "[^"]*\/([A-Za-z_][A-Za-z0-9_]*)\.py", line \d+, in ([A-Za-z_][A-Za-z0-9_<>-]*)/,
  );
  const uvicornPrefixMatch = line.match(/^\s*(INFO|ERROR|WARNING|DEBUG)\s*:\s*(.*)$/i);
  const level = normalizeLevel(levelMatch?.[1] ?? line);
  const timestamp = parseTimestamp(timestampMatch?.[1] ?? "");
  const moduleName = (
    tracebackMatch?.[1] ||
    moduleFuncMatch?.[1] ||
    (uvicornPrefixMatch ? "uvicorn" : "app")
  ).trim();
  const funcName = (tracebackMatch?.[2] || moduleFuncMatch?.[2] || "unknown").trim();
  const message = (uvicornPrefixMatch?.[2] || line).trim() || line;
  return {
    raw: line,
    timestampShort: timestamp.short,
    timestampLong: timestamp.long,
    level,
    module: moduleName || "app",
    func: funcName || "unknown",
    message,
    data: null,
    isJson: false,
  };
}

function parseLogLine(line: string): ParsedLogLine {
  const json = tryParseJson(line);
  if (!json) {
    return parsePlainLine(line);
  }

  const ts = parseTimestamp(json.timestamp);
  const level = normalizeLevel(String(json.level ?? ""));
  const moduleName = String(json.module ?? "app") || "app";
  const func = String(json.function ?? json.func ?? "—") || "—";
  const message = String(json.message ?? line);
  const data = typeof json.data === "object" && json.data !== null ? (json.data as Record<string, unknown>) : null;

  return {
    raw: line,
    timestampShort: ts.short,
    timestampLong: ts.long,
    level,
    module: moduleName,
    func,
    message,
    data,
    isJson: true,
  };
}

function isPollingLog(entry: ParsedLogLine): boolean {
  const plainMessage = entry.message || "";
  if (
    plainMessage.includes("GET /api/data/tasks") ||
    plainMessage.includes("\"GET /api/data/tasks")
  ) {
    return true;
  }
  if (!entry.isJson) {
    return entry.message.includes("GET /api/data/tasks");
  }
  const maybePath = typeof entry.data?.path === "string" ? entry.data.path : "";
  const path = String(maybePath || "").split("?", 1)[0];
  if (!path) return false;
  if (path.includes("/api/data/tasks")) return true;
  if (POLLING_PATHS.includes(path)) return true;
  return POLLING_PREFIXES.some((prefix) => path.startsWith(prefix));
}

export function LoggingPage() {
  const { showToast } = useToast();
  const [logFiles, setLogFiles] = useState<string[]>([]);
  const [selectedFile, setSelectedFile] = useState(ALL_LOGS);
  const [lines, setLines] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [selectedModules, setSelectedModules] = useState<string[]>([]);
  const [selectedLevels, setSelectedLevels] = useState<Record<string, boolean>>(DEFAULT_LEVELS);
  const [hidePolling, setHidePolling] = useState(true);
  const [autoScroll, setAutoScroll] = useState(true);
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [hasLoadedOnce, setHasLoadedOnce] = useState(false);
  const tableContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getLogs()
      .then((files) => {
        const names = files.map((entry) => entry.name);
        const ordered = [ALL_LOGS, ...names.filter((name) => name !== ALL_LOGS)];
        setLogFiles([...new Set(ordered)]);
      })
      .catch(() => {
        setLogFiles([ALL_LOGS]);
        setSelectedFile(ALL_LOGS);
        showToast("Failed to list log files. Showing aggregate stream only.", "error");
        setHasLoadedOnce(true);
      });
  }, [showToast]);

  const activeLogFile = useMemo(() => {
    if (logFiles.length === 0) {
      return selectedFile;
    }
    if (logFiles.includes(selectedFile)) {
      return selectedFile;
    }
    return logFiles[0];
  }, [logFiles, selectedFile]);

  const selectedLevelList = useMemo(() => {
    return LEVEL_OPTIONS.filter((level) => selectedLevels[level]);
  }, [selectedLevels]);

  const levelParam = useMemo(() => {
    if (selectedLevelList.length === LEVEL_OPTIONS.length) return undefined;
    return selectedLevelList.join(",");
  }, [selectedLevelList]);

  useEffect(() => {
    let warned = false;
    if (!activeLogFile) return;
    let active = true;

    const loadLogs = () =>
      getLogContent(activeLogFile, LOG_FETCH_LIMIT, levelParam)
        .then((payload) => {
          if (!active) return;
          setLines(payload.lines ?? []);
          setLoadError(null);
          setHasLoadedOnce(true);
        })
        .catch(() => {
          if (!active) return;
          setLoadError("Failed to refresh log content.");
          setHasLoadedOnce(true);
          if (!warned) {
            showToast("Failed to refresh log content.", "error");
            warned = true;
          }
        });

    void loadLogs();
    const interval = window.setInterval(() => {
      void loadLogs();
    }, 2500);

    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [activeLogFile, levelParam, showToast]);

  const parsedEntries = useMemo(() => {
    return lines.map(parseLogLine);
  }, [lines]);

  const moduleOptions = useMemo(() => {
    return [...new Set(parsedEntries.map((entry) => entry.module).filter(Boolean))].sort((a, b) =>
      a.localeCompare(b),
    );
  }, [parsedEntries]);

  const selectedModulesEffective = useMemo(() => {
    if (selectedModules.length === 0) return [];
    return selectedModules.filter((module) => moduleOptions.includes(module));
  }, [moduleOptions, selectedModules]);

  const filteredEntries = useMemo(() => {
    const query = search.trim().toLowerCase();
    return parsedEntries.filter((entry) => {
      if (!selectedLevels[entry.level]) return false;

      if (selectedModulesEffective.length > 0 && !selectedModulesEffective.includes(entry.module)) {
        return false;
      }

      if (hidePolling && isPollingLog(entry)) {
        return false;
      }

      if (!query) return true;
      const serialized = `${entry.timestampLong} ${entry.level} ${entry.module} ${entry.func} ${entry.message} ${
        entry.data ? JSON.stringify(entry.data) : ""
      }`.toLowerCase();
      return serialized.includes(query);
    });
  }, [hidePolling, parsedEntries, search, selectedLevels, selectedModulesEffective]);

  const entriesWithIds = useMemo(
    () =>
      filteredEntries.map((entry, index) => ({
        entry,
        index,
        rowId: `${index}:${entry.timestampLong}:${entry.module}`,
      })),
    [filteredEntries],
  );

  const virtualRows = useMemo<VirtualLogRow[]>(() => {
    const rows: VirtualLogRow[] = [];
    for (const item of entriesWithIds) {
      rows.push({
        key: item.rowId,
        rowType: "entry",
        entry: item.entry,
        rowId: item.rowId,
        index: item.index,
      });
      if (item.entry.data && expandedRows.has(item.rowId)) {
        rows.push({
          key: `${item.rowId}:detail`,
          rowType: "detail",
          entry: item.entry,
          rowId: item.rowId,
          index: item.index,
        });
      }
    }
    return rows;
  }, [entriesWithIds, expandedRows]);

  const rowVirtualizer = useVirtualizer({
    count: virtualRows.length,
    getScrollElement: () => tableContainerRef.current,
    estimateSize: (index) => (virtualRows[index]?.rowType === "detail" ? 190 : 42),
    measureElement: (element) => element?.getBoundingClientRect().height ?? 0,
    overscan: 15,
  });

  useEffect(() => {
    if (!autoScroll) return;
    const container = tableContainerRef.current;
    if (!container) return;
    container.scrollTop = container.scrollHeight;
  }, [autoScroll, filteredEntries, virtualRows.length]);

  const loading = !hasLoadedOnce && loadError == null;

  const handleFileChange = (value: string) => {
    setLoadError(null);
    setHasLoadedOnce(false);
    setLines([]);
    setSelectedFile(value);
  };

  const toggleLevel = (level: Level) => {
    setLoadError(null);
    setHasLoadedOnce(false);
    setLines([]);
    setSelectedLevels((current) => ({ ...current, [level]: !current[level] }));
  };

  const toggleExpanded = (entry: ParsedLogLine, index: number) => {
    if (!entry.data) return;
    const id = `${index}:${entry.timestampLong}:${entry.module}`;
    setExpandedRows((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const levelLabel = selectedLevelList.length === LEVEL_OPTIONS.length ? "All levels" : selectedLevelList.join(", ");

  return (
    <div style={{ padding: "22px", background: "#131722", minHeight: "100%" }}>
      <div style={{ marginBottom: "14px" }}>
        <h1 style={{ fontSize: "1.36rem", fontWeight: 700 }}>Logging Terminal</h1>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.78rem" }}>
          Structured logs with module, level and polling-noise filters.
        </p>
      </div>

      <section
        style={{
          border: "1px solid rgba(148, 163, 184, 0.22)",
          background: "rgba(18, 23, 35, 0.95)",
          borderRadius: "13px",
          padding: "12px",
          display: "grid",
          gap: "10px",
          marginBottom: "12px",
        }}
      >
        <div style={{ display: "flex", gap: "10px", flexWrap: "wrap", alignItems: "center" }}>
          <MultiSelectDropdown
            label="Module"
            options={moduleOptions.map((module) => ({ label: module, value: module }))}
            selected={selectedModules}
            onChange={setSelectedModules}
            searchable
            searchPlaceholder="Filter modules..."
            buttonWidth="220px"
          />

          <select
            value={activeLogFile}
            onChange={(event) => handleFileChange(event.target.value)}
            style={{
              borderRadius: "9px",
              border: "1px solid rgba(148, 163, 184, 0.24)",
              background: "#101523",
              color: "var(--color-text)",
              padding: "9px 10px",
              fontSize: "0.76rem",
              minWidth: "230px",
            }}
          >
            {logFiles.map((file) => (
              <option key={file} value={file}>
                {file === ALL_LOGS ? "ALL LOGS" : file}
              </option>
            ))}
          </select>

          <input
            type="text"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search logs..."
            style={{
              flex: "1 1 280px",
              minWidth: "220px",
              borderRadius: "9px",
              border: "1px solid rgba(148, 163, 184, 0.24)",
              background: "#101523",
              color: "var(--color-text)",
              padding: "9px 10px",
              fontSize: "0.76rem",
            }}
          />

          <ToggleButton active={hidePolling} onClick={() => setHidePolling((current) => !current)}>
            Hide API Polling
          </ToggleButton>
          <ToggleButton active={autoScroll} onClick={() => setAutoScroll((current) => !current)}>
            Auto-Scroll
          </ToggleButton>
        </div>

        <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
          {LEVEL_OPTIONS.map((level) => (
            <button
              key={level}
              type="button"
              onClick={() => toggleLevel(level)}
              style={{
                borderRadius: "999px",
                border: selectedLevels[level]
                  ? `1px solid ${levelColor(level)}`
                  : "1px solid rgba(148, 163, 184, 0.24)",
                background: selectedLevels[level] ? `${levelColor(level)}22` : "rgba(17, 24, 39, 0.9)",
                color: selectedLevels[level] ? levelColor(level) : "var(--color-text-muted)",
                padding: "6px 10px",
                fontSize: "0.72rem",
                fontWeight: 700,
                letterSpacing: "0.04em",
                cursor: "pointer",
              }}
            >
              {level}
            </button>
          ))}

          <span style={{ color: "var(--color-text-muted)", fontSize: "0.72rem", marginInlineStart: "8px" }}>
            {levelLabel}
          </span>
        </div>

        <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", color: "var(--color-text-muted)", fontSize: "0.72rem" }}>
          <span>
            Modules: {selectedModulesEffective.length > 0 ? selectedModulesEffective.join(", ") : "All"}
          </span>
          {loadError ? <span style={{ color: "#f87171" }}>• {loadError}</span> : null}
        </div>
      </section>

      <section
        style={{
          borderRadius: "14px",
          border: "1px solid rgba(148, 163, 184, 0.22)",
          background: "rgba(15, 19, 29, 0.98)",
          overflow: "hidden",
        }}
      >
        <div
          ref={tableContainerRef}
          style={{
            maxHeight: "calc(100vh - 280px)",
            overflow: "auto",
            fontFamily: '"SFMono-Regular", "Cascadia Code", "Roboto Mono", ui-monospace, monospace',
          }}
        >
          <div style={{ minWidth: "1080px" }}>
            <div
              style={{
                position: "sticky",
                top: 0,
                zIndex: 3,
                display: "grid",
                gridTemplateColumns: LOG_TABLE_GRID_TEMPLATE,
                background: "#141b2b",
                borderBottom: "1px solid rgba(148, 163, 184, 0.28)",
              }}
            >
              {["Timestamp", "Level", "Module", "Function", "Message"].map((heading) => (
                <div
                  key={heading}
                  style={{
                    color: "var(--color-text-muted)",
                    textTransform: "uppercase",
                    letterSpacing: "0.04em",
                    fontSize: "0.71rem",
                    fontWeight: 700,
                    textAlign: "left",
                    padding: "10px 10px",
                  }}
                >
                  {heading}
                </div>
              ))}
            </div>
            {loading ? (
              <div
                style={{
                  padding: "28px 10px",
                  fontSize: "0.75rem",
                  color: "var(--color-text-muted)",
                  textAlign: "center",
                }}
              >
                Loading logs...
              </div>
            ) : filteredEntries.length === 0 ? (
              <div
                style={{
                  padding: "28px 10px",
                  fontSize: "0.75rem",
                  color: "var(--color-text-muted)",
                  textAlign: "center",
                }}
              >
                No matching log entries.
              </div>
            ) : (
              <div
                style={{
                  height: `${rowVirtualizer.getTotalSize()}px`,
                  position: "relative",
                }}
              >
                {rowVirtualizer.getVirtualItems().map((virtualItem) => {
                  const row = virtualRows[virtualItem.index];
                  if (!row) {
                    return null;
                  }

                  if (row.rowType === "detail") {
                    return (
                      <div
                        key={row.key}
                        data-index={virtualItem.index}
                        ref={rowVirtualizer.measureElement}
                        style={{
                          position: "absolute",
                          top: 0,
                          left: 0,
                          width: "100%",
                          transform: `translateY(${virtualItem.start}px)`,
                          borderBottom: "1px solid rgba(148, 163, 184, 0.14)",
                          background: "rgba(17, 24, 39, 0.78)",
                          padding: "10px",
                        }}
                      >
                        <div
                          style={{
                            display: "grid",
                            gap: "6px",
                            fontSize: "0.71rem",
                            color: "#cbd5e1",
                          }}
                        >
                          <div>
                            <span style={{ color: "var(--color-text-muted)" }}>Timestamp:</span>{" "}
                            {row.entry.timestampLong}
                          </div>
                          <div>
                            <span style={{ color: "var(--color-text-muted)" }}>Format:</span>{" "}
                            {row.entry.isJson ? "JSON" : "Plain text"}
                          </div>
                          <pre
                            style={{
                              margin: 0,
                              background: "rgba(2, 6, 23, 0.72)",
                              border: "1px solid rgba(148, 163, 184, 0.24)",
                              borderRadius: "8px",
                              padding: "10px",
                              color: "#bfdbfe",
                              whiteSpace: "pre-wrap",
                              wordBreak: "break-word",
                            }}
                          >
                            {JSON.stringify(row.entry.data, null, 2)}
                          </pre>
                        </div>
                      </div>
                    );
                  }

                  const expanded = expandedRows.has(row.rowId);
                  return (
                    <div
                      key={row.key}
                      data-index={virtualItem.index}
                      ref={rowVirtualizer.measureElement}
                      onClick={() => toggleExpanded(row.entry, row.index)}
                      style={{
                        position: "absolute",
                        top: 0,
                        left: 0,
                        width: "100%",
                        transform: `translateY(${virtualItem.start}px)`,
                        display: "grid",
                        gridTemplateColumns: LOG_TABLE_GRID_TEMPLATE,
                        cursor: row.entry.data ? "pointer" : "default",
                        borderBottom: "1px solid rgba(148, 163, 184, 0.14)",
                      }}
                      onMouseEnter={(event) => {
                        event.currentTarget.style.background = "rgba(88, 166, 255, 0.07)";
                      }}
                      onMouseLeave={(event) => {
                        event.currentTarget.style.background = "transparent";
                      }}
                    >
                      <div
                        style={{
                          padding: "8px 10px",
                          color: "var(--color-text-muted)",
                          fontSize: "0.72rem",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {row.entry.timestampShort}
                      </div>
                      <div
                        style={{
                          padding: "8px 10px",
                          color: levelColor(row.entry.level),
                          fontSize: "0.72rem",
                          whiteSpace: "nowrap",
                          fontWeight: 700,
                        }}
                      >
                        {row.entry.level}
                      </div>
                      <div
                        style={{
                          padding: "8px 10px",
                          color: "#cbd5e1",
                          fontSize: "0.72rem",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {row.entry.module}
                      </div>
                      <div
                        style={{
                          padding: "8px 10px",
                          color: "#9ca3af",
                          fontSize: "0.72rem",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {row.entry.func}
                      </div>
                      <div
                        style={{
                          padding: "8px 10px",
                          color: "#e5e7eb",
                          fontSize: "0.72rem",
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-word",
                        }}
                      >
                        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                          <span>{row.entry.message}</span>
                          {row.entry.data ? (
                            expanded ? (
                              <ChevronUp size={13} color="#93c5fd" />
                            ) : (
                              <ChevronDown size={13} color="#93c5fd" />
                            )
                          ) : null}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

function ToggleButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        borderRadius: "999px",
        border: active ? "1px solid rgba(96, 165, 250, 0.45)" : "1px solid rgba(148, 163, 184, 0.24)",
        background: active ? "rgba(37, 99, 235, 0.2)" : "rgba(17, 24, 39, 0.9)",
        color: active ? "#bfdbfe" : "var(--color-text-muted)",
        fontSize: "0.72rem",
        padding: "7px 10px",
        cursor: "pointer",
      }}
    >
      {children}
    </button>
  );
}
