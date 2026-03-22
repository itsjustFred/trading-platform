import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Activity, Pause, Plus, Square, X } from "lucide-react";

import {
  createLiveDeployment,
  deleteLiveDeployment,
  getLiveAccounts,
  getLiveDeployments,
  getStrategies,
  updateLiveDeploymentStatus,
} from "../api/client";
import { Modal } from "../components/Modal";
import { SymbolInput } from "../components/SymbolInput";
import type {
  LiveAccount,
  LiveDeploymentConfig,
  LiveDeploymentStatus,
  StrategyMeta,
} from "../api/types";
import { useToast } from "../components/ToastProvider";

type DeploymentCard = LiveDeploymentConfig & {
  broker: LiveAccount["broker"] | "unknown";
};

const LIVE_POLL_MS = 15000;
const DEFAULT_TIMEFRAME = "1h";
const LIVE_TIMEFRAMES = [
  "tick",
  "1s",
  "5s",
  "15s",
  "30s",
  "1m",
  "5m",
  "15m",
  "30m",
  "1h",
  "4h",
  "1d",
] as const;

type CreateDeploymentForm = {
  strategy_name: string;
  account_id: string;
  symbols_input: string;
  timeframe: string;
  settings_json: string;
};

export function LiveHubPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [accounts, setAccounts] = useState<LiveAccount[]>([]);
  const [cards, setCards] = useState<DeploymentCard[]>([]);
  const [strategies, setStrategies] = useState<StrategyMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [updatingDeploymentIds, setUpdatingDeploymentIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [createOpenAccountId, setCreateOpenAccountId] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  const [strategiesLoading, setStrategiesLoading] = useState(false);
  const [createForm, setCreateForm] = useState<CreateDeploymentForm>({
    strategy_name: "",
    account_id: "",
    symbols_input: "",
    timeframe: DEFAULT_TIMEFRAME,
    settings_json: "{}",
  });

  const refreshDeployments = useCallback(
    async (silent = true) => {
      if (!silent) {
        setLoading(true);
      }
      try {
        const [accountPayload, deploymentsPayload] = await Promise.all([
          getLiveAccounts(),
          getLiveDeployments(),
        ]);
        const brokerByAccount = new Map<string, LiveAccount["broker"]>();
        accountPayload.forEach((account) => {
          brokerByAccount.set(account.id, account.broker);
        });
        const nextCards: DeploymentCard[] = deploymentsPayload.map((deployment) => ({
          ...deployment,
          broker: brokerByAccount.get(deployment.account_id) ?? "unknown",
        }));
        setAccounts(accountPayload);
        setCards(nextCards);
        setError("");
      } catch (issue: unknown) {
        const message =
          issue instanceof Error ? issue.message : "Failed to load live deployments";
        setError(message);
      } finally {
        if (!silent) {
          setLoading(false);
        }
      }
    },
    [],
  );

  const refreshStrategies = useCallback(async () => {
    if (strategiesLoading) {
      return;
    }
    setStrategiesLoading(true);
    try {
      const strategyPayload = await getStrategies();
      setStrategies(strategyPayload);
    } catch {
      setStrategies([]);
      showToast("Could not load strategy list.", "warning");
    } finally {
      setStrategiesLoading(false);
    }
  }, [showToast, strategiesLoading]);

  useEffect(() => {
    if (accounts.length === 0 || createForm.account_id) {
      return;
    }
    setCreateForm((current) => ({ ...current, account_id: accounts[0].id }));
  }, [accounts, createForm.account_id]);

  useEffect(() => {
    let active = true;

    const load = async () => {
      await refreshDeployments(false);
      if (!active) {
        return;
      }
    };

    void load();
    const intervalId = window.setInterval(() => {
      void refreshDeployments(true);
    }, LIVE_POLL_MS);

    return () => {
      active = false;
      if (intervalId) window.clearInterval(intervalId);
    };
  }, [refreshDeployments]);

  const groupedByAccount = useMemo(() => {
    const map = new Map<string, DeploymentCard[]>();
    cards.forEach((card) => {
      const current = map.get(card.account_id) ?? [];
      current.push(card);
      map.set(card.account_id, current);
    });
    return map;
  }, [cards]);

  const accountById = useMemo(() => {
    const map = new Map<string, LiveAccount>();
    accounts.forEach((account) => {
      map.set(account.id, account);
    });
    return map;
  }, [accounts]);

  const setDeploymentBusy = (deploymentId: string, busy: boolean) => {
    setUpdatingDeploymentIds((current) => {
      const next = new Set(current);
      if (busy) {
        next.add(deploymentId);
      } else {
        next.delete(deploymentId);
      }
      return next;
    });
  };

  const accountDisplayName = (account: LiveAccount): string => {
    const resolved = account.name.trim();
    if (resolved) {
      return resolved;
    }
    return `${account.broker} ${account.is_demo ? "Demo" : "Live"}`;
  };

  const handleControl = async (
    action: "play" | "pause" | "stop",
    card: DeploymentCard,
    event: React.MouseEvent<HTMLButtonElement>,
  ) => {
    event.stopPropagation();
    const statusByAction: Record<typeof action, LiveDeploymentStatus> = {
      play: "ACTIVE",
      pause: "PAUSED",
      stop: "STOPPED",
    };
    const targetStatus = statusByAction[action];
    if (card.status === targetStatus) {
      showToast(
        `${card.strategy_name} is already ${targetStatus.toLowerCase()}.`,
        "info",
      );
      return;
    }

    setDeploymentBusy(card.id, true);
    try {
      await updateLiveDeploymentStatus(card.id, targetStatus);
      await refreshDeployments(true);
      showToast(
        `${card.strategy_name} switched to ${targetStatus.toLowerCase()}.`,
        "success",
      );
    } catch (issue: unknown) {
      const message =
        issue instanceof Error ? issue.message : "Failed to update deployment status";
      showToast(message, "error");
    } finally {
      setDeploymentBusy(card.id, false);
    }
  };

  const handleCreateDeployment = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const strategyName = createForm.strategy_name.trim();
    const timeframe = createForm.timeframe.trim() || DEFAULT_TIMEFRAME;
    const accountId = createForm.account_id.trim();
    const symbols = createForm.symbols_input
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);

    if (!strategyName) {
      showToast("Please enter a strategy name.", "warning");
      return;
    }
    if (!accountId) {
      showToast("Please choose an account.", "warning");
      return;
    }
    if (symbols.length === 0) {
      showToast("Please provide at least one symbol.", "warning");
      return;
    }

    let settingsPayload: Record<string, unknown> = {};
    const settingsRaw = createForm.settings_json.trim();
    if (settingsRaw) {
      try {
        const parsed = JSON.parse(settingsRaw);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          settingsPayload = parsed as Record<string, unknown>;
        } else {
          showToast("Settings JSON must be an object.", "warning");
          return;
        }
      } catch {
        showToast("Settings JSON is invalid.", "warning");
        return;
      }
    }

    setIsCreating(true);
    try {
      await createLiveDeployment({
        strategy_name: strategyName,
        account_id: accountId,
        symbols,
        timeframe,
        settings: settingsPayload,
      });
      setCreateOpenAccountId("");
      setCreateForm((current) => ({
        ...current,
        strategy_name: "",
        symbols_input: "",
        timeframe: DEFAULT_TIMEFRAME,
        settings_json: "{}",
      }));
      await refreshDeployments(true);
      showToast(`Created deployment for ${strategyName}.`, "success");
    } catch (issue: unknown) {
      const message =
        issue instanceof Error ? issue.message : "Failed to create deployment";
      showToast(message, "error");
    } finally {
      setIsCreating(false);
    }
  };

  const openDeployModal = (accountId: string) => {
    const account = accountById.get(accountId);
    setCreateForm({
      strategy_name: "",
      account_id: accountId,
      symbols_input: "",
      timeframe: DEFAULT_TIMEFRAME,
      settings_json: "{}",
    });
    setCreateOpenAccountId(account?.id ?? accountId);
    void refreshStrategies();
  };

  const handleDelete = async (card: DeploymentCard, event: React.MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    setDeploymentBusy(card.id, true);
    try {
      await deleteLiveDeployment(card.id);
      await refreshDeployments(true);
      showToast(`${card.strategy_name} deleted.`, "success");
    } catch (issue: unknown) {
      const message =
        issue instanceof Error ? issue.message : "Failed to delete deployment";
      showToast(message, "error");
    } finally {
      setDeploymentBusy(card.id, false);
    }
  };

  return (
    <div style={{ padding: "24px" }}>
      <Modal
        open={Boolean(createOpenAccountId)}
        onClose={() => setCreateOpenAccountId("")}
        title="Deploy Strategy"
        width="620px"
      >
        <form onSubmit={handleCreateDeployment} style={{ display: "grid", gap: "12px" }}>
          <label style={fieldLabelStyle}>
            Account
            <select
              value={createForm.account_id}
              onChange={(event) =>
                setCreateForm((current) => ({
                  ...current,
                  account_id: event.target.value,
                }))
              }
              style={fieldInputStyle}
              required
            >
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {accountDisplayName(account)} · {account.broker} · {account.is_demo ? "Demo" : "Live"}
                </option>
              ))}
            </select>
          </label>

          <label style={fieldLabelStyle}>
            Strategy
            <select
              value={createForm.strategy_name}
              onChange={(event) =>
                setCreateForm((current) => ({
                  ...current,
                  strategy_name: event.target.value,
                }))
              }
              style={fieldInputStyle}
              required
            >
              <option value="">
                {strategiesLoading ? "Loading strategies..." : "Select strategy"}
              </option>
              {strategies.map((strategy) => (
                <option key={strategy.file} value={strategy.file}>
                  {strategy.name}
                </option>
              ))}
            </select>
          </label>

          <label style={fieldLabelStyle}>
            Timeframe
            <select
              value={createForm.timeframe}
              onChange={(event) =>
                setCreateForm((current) => ({
                  ...current,
                  timeframe: event.target.value,
                }))
              }
              style={fieldInputStyle}
              required
            >
              {LIVE_TIMEFRAMES.map((tf) => (
                <option key={tf} value={tf}>
                  {tf}
                </option>
              ))}
            </select>
          </label>

          <label style={fieldLabelStyle}>
            Assets / Symbols
            <SymbolInput
              value={createForm.symbols_input}
              onChange={(value) =>
                setCreateForm((current) => ({
                  ...current,
                  symbols_input: value,
                }))
              }
              placeholder="AAPL, MSFT"
              style={fieldInputStyle}
            />
          </label>

          <label style={fieldLabelStyle}>
            Settings (JSON)
            <textarea
              value={createForm.settings_json}
              onChange={(event) =>
                setCreateForm((current) => ({
                  ...current,
                  settings_json: event.target.value,
                }))
              }
              rows={5}
              style={{ ...fieldInputStyle, resize: "vertical" }}
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
              onClick={() => setCreateOpenAccountId("")}
              style={secondaryButtonStyle}
              disabled={isCreating}
            >
              Cancel
            </button>
            <button
              type="submit"
              style={primaryButtonStyle}
              disabled={isCreating || accounts.length === 0}
            >
              {isCreating ? "Deploying..." : "Deploy"}
            </button>
          </div>
        </form>
      </Modal>

      <div
        style={{
          background:
            "linear-gradient(130deg, rgba(56, 189, 248, 0.12), rgba(34, 197, 94, 0.05) 45%, rgba(13, 17, 23, 0.9))",
          border: "1px solid rgba(56, 189, 248, 0.25)",
          borderRadius: "14px",
          padding: "20px",
          marginBottom: "18px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "14px",
          flexWrap: "wrap",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
          <div
            style={{
              width: "34px",
              height: "34px",
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              borderRadius: "10px",
              background: "rgba(56, 189, 248, 0.18)",
              color: "#7dd3fc",
            }}
          >
            <Activity size={18} />
          </div>
          <div>
            <h1 style={{ fontSize: "1.4rem", margin: 0, fontWeight: 700 }}>Live Hub</h1>
            <p style={{ margin: "2px 0 0", color: "var(--color-text-muted)", fontSize: "0.86rem" }}>
              Connected deployments by account. Open a card to inspect equity and slippage.
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => {
            if (accounts.length === 0) {
              showToast("No accounts available for deployment.", "warning");
              return;
            }
            openDeployModal(accounts[0].id);
          }}
          style={addDeploymentBtnStyle}
        >
          <Plus size={14} />
          Deploy Strategy
        </button>
      </div>

      {loading ? (
        <div style={{ color: "var(--color-text-muted)", textAlign: "center", padding: "48px 0" }}>
          Loading live deployments...
        </div>
      ) : error ? (
        <div style={{ color: "var(--color-red)", textAlign: "center", padding: "48px 0" }}>
          Error: {error}
        </div>
      ) : accounts.length === 0 ? (
        <div style={{ color: "var(--color-text-muted)", textAlign: "center", padding: "48px 0" }}>
          No broker accounts discovered.
        </div>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(330px, 1fr))",
            gap: "12px",
          }}
        >
          {accounts.map((account) => {
            const accountCards = groupedByAccount.get(account.id) ?? [];
            return (
              <div
                key={account.id}
                style={{
                  background:
                    "linear-gradient(150deg, rgba(22, 27, 34, 0.98), rgba(28, 35, 51, 0.92))",
                  border: "1px solid rgba(56, 189, 248, 0.2)",
                  borderRadius: "12px",
                  padding: "16px",
                  display: "grid",
                  gap: "10px",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div>
                    <div style={{ fontSize: "0.98rem", fontWeight: 700 }}>
                      {accountDisplayName(account)}
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                    <span
                      style={{
                        fontSize: "0.72rem",
                        textTransform: "uppercase",
                        borderRadius: "999px",
                        padding: "3px 8px",
                        background:
                          account.broker === "alpaca"
                            ? "rgba(34, 197, 94, 0.16)"
                            : "rgba(234, 179, 8, 0.16)",
                        color: account.broker === "alpaca" ? "#86efac" : "#fde68a",
                      }}
                    >
                      {account.broker}
                    </span>
                    <span
                      style={{
                        fontSize: "0.72rem",
                        borderRadius: "999px",
                        padding: "3px 8px",
                        background: account.is_demo ? "rgba(148,163,184,0.18)" : "rgba(56,189,248,0.16)",
                        color: account.is_demo ? "#cbd5e1" : "#7dd3fc",
                      }}
                    >
                      {account.is_demo ? "Demo" : "Live"}
                    </span>
                  </div>
                </div>

                <div style={{ color: "#e2e8f0", fontSize: "0.9rem" }}>
                  Balance: ${account.balance.toLocaleString("en-US", { maximumFractionDigits: 2 })}
                </div>

                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <strong style={{ fontSize: "0.85rem" }}>Deployments</strong>
                  <button
                    type="button"
                    onClick={() => openDeployModal(account.id)}
                    style={addDeploymentBtnStyle}
                  >
                    <Plus size={13} />
                    Deploy Strategy
                  </button>
                </div>

                {accountCards.length === 0 ? (
                  <div
                    style={{
                      border: "1px dashed var(--color-border)",
                      borderRadius: "10px",
                      padding: "12px",
                      color: "var(--color-text-muted)",
                      fontSize: "0.84rem",
                    }}
                  >
                    No active deployments. Deploy a strategy to get started.
                  </div>
                ) : (
                  <div style={{ display: "grid", gap: "8px" }}>
                    {accountCards.map((card) => (
                      <div
                        key={card.id}
                        onClick={() => navigate(`/live/${encodeURIComponent(card.account_id)}`)}
                        style={{
                          border: "1px solid var(--color-border)",
                          borderRadius: "10px",
                          padding: "10px",
                          cursor: "pointer",
                        }}
                      >
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                          <div style={{ fontWeight: 600, fontSize: "0.87rem" }}>{card.strategy_name}</div>
                          <div style={statusChipStyle(card.status)}>{card.status}</div>
                        </div>
                        <div style={{ color: "var(--color-text-muted)", fontSize: "0.8rem", margin: "6px 0 8px" }}>
                          {card.timeframe} · {card.symbols.join(", ")}
                        </div>
                        <div style={{ display: "flex", gap: "8px" }}>
                          <button
                            type="button"
                            onClick={(event) => void handleControl("pause", card, event)}
                            title="Set status PAUSED"
                            style={controlBtnStyle("pause", card.status === "PAUSED")}
                            disabled={updatingDeploymentIds.has(card.id)}
                          >
                            <Pause size={14} />
                          </button>
                          <button
                            type="button"
                            onClick={(event) => void handleControl("stop", card, event)}
                            title="Set status STOPPED"
                            style={controlBtnStyle("stop", card.status === "STOPPED")}
                            disabled={updatingDeploymentIds.has(card.id)}
                          >
                            <Square size={14} />
                          </button>
                          <button
                            type="button"
                            onClick={(event) => void handleDelete(card, event)}
                            title="Delete deployment"
                            style={controlBtnStyle("stop", false)}
                            disabled={updatingDeploymentIds.has(card.id)}
                          >
                            <X size={14} />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function controlBtnStyle(
  kind: "play" | "pause" | "stop",
  active: boolean,
): React.CSSProperties {
  if (kind === "play") {
    return {
      ...baseControlStyle,
      border: active
        ? "1px solid rgba(34, 197, 94, 0.7)"
        : "1px solid rgba(34, 197, 94, 0.28)",
      color: "#86efac",
      background: active ? "rgba(34, 197, 94, 0.22)" : "rgba(34, 197, 94, 0.1)",
    };
  }
  if (kind === "pause") {
    return {
      ...baseControlStyle,
      border: active
        ? "1px solid rgba(234, 179, 8, 0.7)"
        : "1px solid rgba(234, 179, 8, 0.28)",
      color: "#fde68a",
      background: active ? "rgba(234, 179, 8, 0.22)" : "rgba(234, 179, 8, 0.1)",
    };
  }
  return {
    ...baseControlStyle,
    border: active
      ? "1px solid rgba(248, 113, 113, 0.7)"
      : "1px solid rgba(248, 113, 113, 0.28)",
    color: "#fecaca",
    background: active ? "rgba(248, 113, 113, 0.22)" : "rgba(248, 113, 113, 0.1)",
  };
}

const baseControlStyle: React.CSSProperties = {
  width: "32px",
  height: "30px",
  borderRadius: "8px",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  cursor: "pointer",
  transition: "border-color 120ms ease, background 120ms ease",
};

function statusChipStyle(status: LiveDeploymentStatus): React.CSSProperties {
  if (status === "ACTIVE") {
    return {
      marginBottom: "8px",
      fontSize: "0.7rem",
      display: "inline-flex",
      alignItems: "center",
      borderRadius: "999px",
      padding: "3px 8px",
      border: "1px solid rgba(34, 197, 94, 0.35)",
      background: "rgba(34, 197, 94, 0.16)",
      color: "#86efac",
      letterSpacing: "0.03em",
    };
  }
  if (status === "PAUSED") {
    return {
      marginBottom: "8px",
      fontSize: "0.7rem",
      display: "inline-flex",
      alignItems: "center",
      borderRadius: "999px",
      padding: "3px 8px",
      border: "1px solid rgba(234, 179, 8, 0.35)",
      background: "rgba(234, 179, 8, 0.16)",
      color: "#fde68a",
      letterSpacing: "0.03em",
    };
  }
  return {
    marginBottom: "8px",
    fontSize: "0.7rem",
    display: "inline-flex",
    alignItems: "center",
    borderRadius: "999px",
    padding: "3px 8px",
    border: "1px solid rgba(248, 113, 113, 0.35)",
    background: "rgba(248, 113, 113, 0.16)",
    color: "#fecaca",
    letterSpacing: "0.03em",
  };
}

const addDeploymentBtnStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: "6px",
  border: "1px solid rgba(56, 189, 248, 0.38)",
  background: "rgba(56, 189, 248, 0.14)",
  color: "#bae6fd",
  borderRadius: "10px",
  padding: "8px 12px",
  cursor: "pointer",
  fontSize: "0.84rem",
};

const fieldLabelStyle: React.CSSProperties = {
  display: "grid",
  gap: "6px",
  fontSize: "0.82rem",
  color: "var(--color-text-muted)",
};

const fieldInputStyle: React.CSSProperties = {
  width: "100%",
  borderRadius: "10px",
  border: "1px solid var(--color-border)",
  background: "rgba(2, 6, 23, 0.42)",
  color: "var(--color-text)",
  padding: "10px 12px",
  fontSize: "0.87rem",
  outline: "none",
};

const secondaryButtonStyle: React.CSSProperties = {
  border: "1px solid var(--color-border)",
  background: "rgba(148, 163, 184, 0.08)",
  color: "var(--color-text)",
  borderRadius: "10px",
  padding: "8px 12px",
  cursor: "pointer",
};

const primaryButtonStyle: React.CSSProperties = {
  border: "1px solid rgba(56, 189, 248, 0.45)",
  background: "rgba(56, 189, 248, 0.18)",
  color: "#bae6fd",
  borderRadius: "10px",
  padding: "8px 12px",
  cursor: "pointer",
};
