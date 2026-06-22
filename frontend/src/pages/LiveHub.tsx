import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { Activity, Pause, Plus, Square, X } from "lucide-react";

import {
  createLiveDeployment,
  deleteLiveDeployment,
  getLiveAccounts,
  getLiveBalances,
  getLiveDeployments,
  getLivePositions,
  getSignalProcessors,
  getLiveStats,
  getLiveTrades,
  getServices,
  getStrategies,
  runBacktestSignals,
  updateLiveDeploymentStatus,
} from "../api/client";
import { Modal } from "../components/Modal";
import { Sparkline } from "../components/Sparkline";
import { SymbolInput } from "../components/SymbolInput";
import { useLiveAccountsStream } from "../hooks/liveStreams";
import type {
  LiveAccount,
  LiveAccountStats,
  LiveBalanceRecord,
  LiveDeploymentConfig,
  LiveDeploymentStatus,
  LiveTradeSlippage,
  SignalProcessorOption,
  Service,
  StrategyMeta,
} from "../api/types";
import { useToast } from "../components/ToastProvider";

type DeploymentCard = LiveDeploymentConfig & {
  broker: LiveAccount["broker"] | "unknown";
};

const LIVE_FALLBACK_POLL_MS = 30_000;
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
  signal_source: "internal" | "discord";
  signal_source_id: string;
  signal_processor: string;
  settings_json: string;
};

type ReplayFormState = {
  account_id: string;
  start_date: string;
  end_date: string;
  timeframe: string;
  signal_processor: string;
  auto_download: boolean;
};

type LiveHubAccountStats = {
  tradesCount: number;
  winRate: number;
  totalPnl: number;
  sparkline: number[];
  openPositions: number;
};


const LiveDeploymentCard = memo(function LiveDeploymentCard({
  card,
  busy,
  onPause,
  onStop,
  onDelete,
}: {
  card: DeploymentCard;
  busy: boolean;
  onPause: (card: DeploymentCard, event: React.MouseEvent<HTMLButtonElement>) => void;
  onStop: (card: DeploymentCard, event: React.MouseEvent<HTMLButtonElement>) => void;
  onDelete: (card: DeploymentCard, event: React.MouseEvent<HTMLButtonElement>) => void;
}) {
  return (
    <div
      key={card.id}
      style={{
        border: "1px solid var(--color-border)",
        borderRadius: "10px",
        padding: "10px",
        cursor: "pointer",
        position: "relative",
      }}
    >
      <Link
        to={"/live/" + encodeURIComponent(card.account_id)}
        aria-label={`Open live account `}
        style={{ position: "absolute", inset: 0, zIndex: 1, borderRadius: "10px" }}
      />
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontWeight: 600, fontSize: "0.87rem" }}>{card.strategy_name}</div>
        <div style={statusChipStyle(card.status)}>{card.status}</div>
      </div>
      <div style={{ color: "var(--color-text-muted)", fontSize: "0.8rem", margin: "6px 0 8px" }}>
        {card.timeframe} · {card.symbols.join(", ")}
      </div>
      <div style={{ color: "var(--color-text-muted)", fontSize: "0.76rem", marginBottom: "8px" }}>
        Source: {card.signal_source === "discord" ? "Discord Bot" : "Internal Strategy"}
        {card.signal_source === "discord"
          ? ` · ${
              String(
                card.signal_source_id ||
                  card.signal_config?.["service_name"] ||
                  card.signal_config?.["channel_id"] ||
                  "—",
              ) || "—"
            }`
          : ""}
        {(() => {
          if (card.signal_source !== "discord" || !card.signal_config) {
            return "";
          }
          const waitValue = card.signal_config["wait_candle_close"];
          if (typeof waitValue !== "boolean") {
            return "";
          }
          return ` · Wait Candle Close: ${waitValue ? "On" : "Off"}`;
        })()}
      </div>
      <div style={{ display: "flex", gap: "8px" }}>
        <button
          type="button"
          onClick={(event) => onPause(card, event)}
          title="Set status PAUSED"
          style={controlBtnStyle("pause", card.status === "PAUSED")}
          disabled={busy}
        >
          <Pause size={14} />
        </button>
        <button
          type="button"
          onClick={(event) => onStop(card, event)}
          title="Set status STOPPED"
          style={controlBtnStyle("stop", card.status === "STOPPED")}
          disabled={busy}
        >
          <Square size={14} />
        </button>
        <button
          type="button"
          onClick={(event) => onDelete(card, event)}
          title="Delete deployment"
          style={controlBtnStyle("stop", false)}
          disabled={busy}
        >
          <X size={14} />
        </button>
      </div>
    </div>
  );
});

export function LiveHubPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const accountsStream = useLiveAccountsStream();
  const [accounts, setAccounts] = useState<LiveAccount[]>([]);
  const [cards, setCards] = useState<DeploymentCard[]>([]);
  const [statsByAccount, setStatsByAccount] = useState<Record<string, LiveHubAccountStats>>({});
  const [strategies, setStrategies] = useState<StrategyMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [updatingDeploymentIds, setUpdatingDeploymentIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [createOpenAccountId, setCreateOpenAccountId] = useState("");
  const [replayOpenAccountId, setReplayOpenAccountId] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  const [isStartingReplay, setIsStartingReplay] = useState(false);
  const [strategiesLoading, setStrategiesLoading] = useState(false);
  const [servicesLoading, setServicesLoading] = useState(false);
  const [discordServices, setDiscordServices] = useState<Service[]>([]);
  const [signalProcessors, setSignalProcessors] = useState<SignalProcessorOption[]>([]);
  const [signalProcessorsLoading, setSignalProcessorsLoading] = useState(false);
  const [createForm, setCreateForm] = useState<CreateDeploymentForm>({
    strategy_name: "",
    account_id: "",
    symbols_input: "",
    timeframe: DEFAULT_TIMEFRAME,
    signal_source: "internal",
    signal_source_id: "",
    signal_processor: "",
    settings_json: "{}",
  });
  const [replayForm, setReplayForm] = useState<ReplayFormState>({
    account_id: "",
    start_date: "",
    end_date: "",
    timeframe: DEFAULT_TIMEFRAME,
    signal_processor: "",
    auto_download: true,
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
        const statsEntries = await Promise.all(
          accountPayload.map(async (account) => {
            const [statsResult, balancesResult, positionsResult] = await Promise.allSettled([
              getLiveStats(account.id),
              getLiveBalances(account.id),
              getLivePositions(account.id),
            ]);
            const balanceSeries =
              balancesResult.status === "fulfilled" ? balancesResult.value.series : [];
            const openPositions =
              positionsResult.status === "fulfilled"
                ? positionsResult.value.positions.length
                : 0;

            if (statsResult.status === "fulfilled") {
              return [
                account.id,
                normalizeLiveHubStats(statsResult.value, balanceSeries, openPositions),
              ] as const;
            }

            try {
              const tradesPayload = await getLiveTrades(account.id);
              return [
                account.id,
                buildLiveHubFallbackStats(balanceSeries, tradesPayload.trades, openPositions),
              ] as const;
            } catch {
              return [
                account.id,
                buildLiveHubFallbackStats(balanceSeries, [], openPositions),
              ] as const;
            }
          }),
        );
        const nextStatsByAccount: Record<string, LiveHubAccountStats> = {};
        for (const [accountId, stats] of statsEntries) {
          nextStatsByAccount[accountId] = stats;
        }
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
        setStatsByAccount(nextStatsByAccount);
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

  const refreshDiscordServices = useCallback(async () => {
    if (servicesLoading) {
      return;
    }
    setServicesLoading(true);
    try {
      const payload = await getServices("discord_bot");
      setDiscordServices(payload);
    } catch {
      setDiscordServices([]);
      showToast("Could not load Discord bot services.", "warning");
    } finally {
      setServicesLoading(false);
    }
  }, [servicesLoading, showToast]);

  const refreshSignalProcessors = useCallback(async () => {
    if (signalProcessorsLoading) {
      return;
    }
    setSignalProcessorsLoading(true);
    try {
      const payload = await getSignalProcessors();
      setSignalProcessors(payload);
    } catch {
      setSignalProcessors([]);
      showToast("Could not load signal processors.", "warning");
    } finally {
      setSignalProcessorsLoading(false);
    }
  }, [showToast, signalProcessorsLoading]);

  useEffect(() => {
    if (accounts.length === 0 || createForm.account_id) {
      return;
    }
    setCreateForm((current) => ({ ...current, account_id: accounts[0].id }));
  }, [accounts, createForm.account_id]);

  useEffect(() => {
    if (createForm.signal_source !== "discord" || createForm.signal_source_id) {
      return;
    }
    if (discordServices.length === 0) {
      return;
    }
    setCreateForm((current) => ({ ...current, signal_source_id: discordServices[0].name }));
  }, [createForm.signal_source, createForm.signal_source_id, discordServices]);

  useEffect(() => {
    void refreshDeployments(false);
  }, [refreshDeployments]);

  useEffect(() => {
    const payload = accountsStream.data;
    if (!payload) {
      return;
    }
    queryClient.setQueryData(["live-accounts"], payload.accounts);
    setAccounts(payload.accounts);
    void refreshDeployments(true);
  }, [accountsStream.data, queryClient, refreshDeployments]);

  useEffect(() => {
    if (accountsStream.isConnected) {
      return;
    }
    const intervalId = window.setInterval(() => {
      void refreshDeployments(true);
    }, LIVE_FALLBACK_POLL_MS);
    return () => {
      window.clearInterval(intervalId);
    };
  }, [accountsStream.isConnected, refreshDeployments]);

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

  const handleControl = useCallback(async (
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
  }, [refreshDeployments, showToast]);

  const handleCreateDeployment = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const strategyName = createForm.strategy_name.trim();
    const timeframe = createForm.timeframe.trim() || DEFAULT_TIMEFRAME;
    const accountId = createForm.account_id.trim();
    const symbolFilter = createForm.symbols_input
      .split(",")
      .map((item) => item.trim().toUpperCase())
      .filter(Boolean);
    const sourceId = createForm.signal_source_id.trim();
    const selectedProcessor = createForm.signal_processor.trim().toLowerCase();
    const symbols =
      symbolFilter.length > 0
        ? symbolFilter
        : createForm.signal_source === "discord"
          ? ["*"]
          : [];

    if (createForm.signal_source === "internal" && !strategyName) {
      showToast("Please enter a strategy name.", "warning");
      return;
    }
    if (!accountId) {
      showToast("Please choose an account.", "warning");
      return;
    }
    if (createForm.signal_source === "internal" && symbols.length === 0) {
      showToast("Please provide at least one symbol.", "warning");
      return;
    }
    if (createForm.signal_source === "discord" && !sourceId) {
      showToast("Please select a Discord bot service.", "warning");
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
        signal_source_type: createForm.signal_source,
        signal_source_id: createForm.signal_source === "discord" ? sourceId : undefined,
        symbol_filter: symbolFilter,
        signal_source: createForm.signal_source,
        signal_config:
          createForm.signal_source === "discord"
            ? {
                service_name: sourceId,
                ...(selectedProcessor ? { signal_processor: selectedProcessor } : {}),
              }
            : selectedProcessor
              ? { signal_processor: selectedProcessor }
              : {},
        settings: settingsPayload,
      });
      setCreateOpenAccountId("");
      setCreateForm((current) => ({
        ...current,
        strategy_name: "",
        symbols_input: "",
        timeframe: DEFAULT_TIMEFRAME,
        signal_source: "internal",
        signal_source_id: "",
        signal_processor: "",
        settings_json: "{}",
      }));
      await refreshDeployments(true);
      showToast(
        `Created deployment for ${createForm.signal_source === "discord" ? sourceId : strategyName}.`,
        "success",
      );
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
      signal_source: "internal",
      signal_source_id: "",
      signal_processor: "",
      settings_json: "{}",
    });
    setCreateOpenAccountId(account?.id ?? accountId);
    void refreshStrategies();
    void refreshDiscordServices();
    void refreshSignalProcessors();
  };

  const openReplayModal = (accountId: string) => {
    const today = new Date();
    const endDate = today.toISOString().slice(0, 10);
    const startCandidate = new Date(today.getTime() - (30 * 24 * 60 * 60 * 1000));
    const startDate = startCandidate.toISOString().slice(0, 10);
    setReplayForm({
      account_id: accountId,
      start_date: startDate,
      end_date: endDate,
      timeframe: DEFAULT_TIMEFRAME,
      signal_processor: "",
      auto_download: true,
    });
    setReplayOpenAccountId(accountId);
    void refreshSignalProcessors();
  };

  const handleStartSignalReplay = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const accountId = replayForm.account_id.trim();
    if (!accountId) {
      showToast("Please select an account for replay.", "warning");
      return;
    }
    if (!replayForm.start_date || !replayForm.end_date) {
      showToast("Please choose a start and end date.", "warning");
      return;
    }
    if (replayForm.start_date > replayForm.end_date) {
      showToast("Start date must be before end date.", "warning");
      return;
    }

    setIsStartingReplay(true);
    try {
      const response = await runBacktestSignals({
        signal_source: "discord",
        signal_processor: replayForm.signal_processor || undefined,
        account_id: accountId,
        start_date: replayForm.start_date,
        end_date: replayForm.end_date,
        timeframe: replayForm.timeframe || DEFAULT_TIMEFRAME,
        auto_download: replayForm.auto_download,
      });
      const taskId = response.task_id || response.job_id;
      showToast(`Signal replay started (task ${taskId}).`, "success");
      setReplayOpenAccountId("");
      navigate("/backtests");
    } catch (issue: unknown) {
      const message = issue instanceof Error ? issue.message : "Failed to start signal replay";
      showToast(message, "error");
    } finally {
      setIsStartingReplay(false);
    }
  };

  const handlePauseControl = useCallback((card: DeploymentCard, event: React.MouseEvent<HTMLButtonElement>) => {
    void handleControl("pause", card, event);
  }, [handleControl]);

  const handleStopControl = useCallback((card: DeploymentCard, event: React.MouseEvent<HTMLButtonElement>) => {
    void handleControl("stop", card, event);
  }, [handleControl]);

  const handleDelete = useCallback(async (card: DeploymentCard, event: React.MouseEvent<HTMLButtonElement>) => {
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
  }, [refreshDeployments, showToast]);

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
              required={createForm.signal_source === "internal"}
              disabled={createForm.signal_source === "discord"}
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

          <div style={fieldLabelStyle}>
            Signal Source
            <div
              style={{
                border: "1px solid var(--color-border)",
                borderRadius: "8px",
                padding: "10px",
                display: "grid",
                gap: "10px",
                background: "var(--color-surface2)",
              }}
            >
              <label style={{ display: "flex", alignItems: "center", gap: "8px", cursor: "pointer" }}>
                <input
                  type="radio"
                  name="signal_source"
                  checked={createForm.signal_source === "discord"}
                  onChange={() =>
                    setCreateForm((current) => ({
                      ...current,
                      signal_source: "discord",
                      signal_source_id: current.signal_source_id || discordServices[0]?.name || "",
                    }))
                  }
                />
                Discord Bot
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: "8px", cursor: "pointer" }}>
                <input
                  type="radio"
                  name="signal_source"
                  checked={createForm.signal_source === "internal"}
                  onChange={() =>
                    setCreateForm((current) => ({
                      ...current,
                      signal_source: "internal",
                    }))
                  }
                />
                Internal Strategy
              </label>
            </div>
          </div>

          {createForm.signal_source === "discord" ? (
            <label style={fieldLabelStyle}>
              Discord Bot
              <select
                value={createForm.signal_source_id}
                onChange={(event) =>
                  setCreateForm((current) => ({
                    ...current,
                    signal_source_id: event.target.value,
                  }))
                }
                style={fieldInputStyle}
                required
              >
                <option value="">
                  {servicesLoading ? "Loading Discord bots..." : "Select Discord bot"}
                </option>
                {discordServices.map((service) => (
                  <option key={service.id} value={service.name}>
                    {service.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}

          <label style={fieldLabelStyle}>
            Signal Processor (optional)
            <select
              value={createForm.signal_processor}
              onChange={(event) =>
                setCreateForm((current) => ({
                  ...current,
                  signal_processor: event.target.value,
                }))
              }
              style={fieldInputStyle}
            >
              <option value="">
                {signalProcessorsLoading ? "Loading processors..." : "No processor override"}
              </option>
              {signalProcessors.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
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
            Symbol Filter (optional)
            <SymbolInput
              value={createForm.symbols_input}
              onChange={(value) =>
                setCreateForm((current) => ({
                  ...current,
                  symbols_input: value,
                }))
              }
              placeholder="AAPL, TSLA"
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

      <Modal
        open={Boolean(replayOpenAccountId)}
        onClose={() => setReplayOpenAccountId("")}
        title="Backtest Signals"
        width="520px"
      >
        <form onSubmit={handleStartSignalReplay} style={{ display: "grid", gap: "12px" }}>
          <label style={fieldLabelStyle}>
            Account
            <select
              value={replayForm.account_id}
              onChange={(event) =>
                setReplayForm((current) => ({
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

          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "10px" }}>
            <label style={fieldLabelStyle}>
              Start Date
              <input
                type="date"
                value={replayForm.start_date}
                onChange={(event) =>
                  setReplayForm((current) => ({
                    ...current,
                    start_date: event.target.value,
                  }))
                }
                style={fieldInputStyle}
                required
              />
            </label>
            <label style={fieldLabelStyle}>
              End Date
              <input
                type="date"
                value={replayForm.end_date}
                onChange={(event) =>
                  setReplayForm((current) => ({
                    ...current,
                    end_date: event.target.value,
                  }))
                }
                style={fieldInputStyle}
                required
              />
            </label>
          </div>

          <label style={fieldLabelStyle}>
            Signal Processor (optional)
            <select
              value={replayForm.signal_processor}
              onChange={(event) =>
                setReplayForm((current) => ({
                  ...current,
                  signal_processor: event.target.value,
                }))
              }
              style={fieldInputStyle}
            >
              <option value="">
                {signalProcessorsLoading ? "Loading processors..." : "No processor override"}
              </option>
              {signalProcessors.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          <label style={fieldLabelStyle}>
            Replay Timeframe
            <select
              value={replayForm.timeframe}
              onChange={(event) =>
                setReplayForm((current) => ({
                  ...current,
                  timeframe: event.target.value,
                }))
              }
              style={fieldInputStyle}
            >
              {LIVE_TIMEFRAMES.map((tf) => (
                <option key={tf} value={tf}>
                  {tf}
                </option>
              ))}
            </select>
          </label>

          <label
            style={{
              ...fieldLabelStyle,
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            Auto-download missing OHLCV
            <input
              type="checkbox"
              checked={replayForm.auto_download}
              onChange={(event) =>
                setReplayForm((current) => ({
                  ...current,
                  auto_download: event.target.checked,
                }))
              }
            />
          </label>

          <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "4px" }}>
            <button
              type="button"
              onClick={() => setReplayOpenAccountId("")}
              style={secondaryButtonStyle}
              disabled={isStartingReplay}
            >
              Cancel
            </button>
            <button
              type="submit"
              style={primaryButtonStyle}
              disabled={isStartingReplay}
            >
              {isStartingReplay ? "Starting..." : "Start Replay"}
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
                Realtime via WebSocket {accountsStream.isConnected ? "· connected" : "· fallback polling"}.
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
            const accountStats = statsByAccount[account.id];
            const tradesCount = accountStats?.tradesCount ?? 0;
            const winRate = accountStats?.winRate ?? 0;
            const totalPnl = accountStats?.totalPnl ?? 0;
            const sparklinePoints = accountStats?.sparkline ?? [];
            const openPositions = accountStats?.openPositions ?? 0;
            const hasNoTradeData = tradesCount === 0 && sparklinePoints.length === 0;
            const loginId = account.broker_login || account.login || null;
            const ctidId = account.ctid || null;
            const primaryLine =
              account.broker === "ctrader"
                ? `cTrader ${account.is_demo ? "Demo" : "Live"}`
                : accountDisplayName(account);
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
                      {primaryLine}
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
                    <span
                      style={{
                        fontSize: "0.72rem",
                        borderRadius: "999px",
                        padding: "3px 8px",
                        background: "rgba(59, 130, 246, 0.18)",
                        color: "#bfdbfe",
                      }}
                    >
                      Open {openPositions}
                    </span>
                  </div>
                </div>

                <div style={{ color: "#e2e8f0", fontSize: "0.9rem" }}>
                  Balance: ${account.balance.toLocaleString("en-US", { maximumFractionDigits: 2 })}
                </div>
                {(loginId || ctidId) && (
                  <div
                    style={{
                      border: "1px solid rgba(148,163,184,0.26)",
                      borderRadius: "8px",
                      padding: "8px 10px",
                      background: "rgba(15, 23, 42, 0.35)",
                      display: "grid",
                      gap: "4px",
                      fontSize: "0.78rem",
                      lineHeight: 1.35,
                    }}
                  >
                    {loginId && (
                      <div style={{ color: "#e2e8f0" }}>
                        <span style={{ color: "var(--color-text-muted)" }}>Login:</span>{" "}
                        <span style={{ fontWeight: 600 }}>{loginId}</span>
                      </div>
                    )}
                    {ctidId && (
                      <div style={{ color: "#e2e8f0" }}>
                        <span style={{ color: "var(--color-text-muted)" }}>cTID:</span>{" "}
                        <span style={{ fontWeight: 600 }}>{ctidId}</span>
                      </div>
                    )}
                  </div>
                )}

                <div
                  style={{
                    border: "1px solid rgba(56, 189, 248, 0.2)",
                    borderRadius: "10px",
                    padding: "8px 10px 6px",
                    background: "rgba(2, 6, 23, 0.35)",
                  }}
                >
                  {sparklinePoints.length > 1 ? (
                    <Sparkline data={sparklinePoints} width={300} height={42} />
                  ) : (
                    <div
                      style={{
                        height: "42px",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        color: "var(--color-text-muted)",
                        fontSize: "0.75rem",
                      }}
                    >
                      {hasNoTradeData ? "No trades yet" : "Waiting for equity points…"}
                    </div>
                  )}
                </div>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
                    gap: "8px",
                  }}
                >
                  <div style={miniStatStyle}>
                    <span style={miniStatLabelStyle}>Trades</span>
                    <span style={miniStatValueStyle}>{tradesCount}</span>
                  </div>
                  <div style={miniStatStyle}>
                    <span style={miniStatLabelStyle}>Total PnL</span>
                    <span
                      style={{
                        ...miniStatValueStyle,
                        color: totalPnl >= 0 ? "#86efac" : "#fecaca",
                      }}
                    >
                      {formatSignedUsd(totalPnl)}
                    </span>
                  </div>
                  <div style={miniStatStyle}>
                    <span style={miniStatLabelStyle}>Win Rate</span>
                    <span style={winRateBadgeStyle(winRate)}>{formatPercent(winRate)}</span>
                  </div>
                </div>

                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <strong style={{ fontSize: "0.85rem" }}>Deployments</strong>
                  <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                    <button
                      type="button"
                      onClick={() => navigate(`/live/${encodeURIComponent(account.id)}/viewer`)}
                      style={openViewerBtnStyle}
                    >
                      Open Viewer
                    </button>
                    <button
                      type="button"
                      onClick={() => openReplayModal(account.id)}
                      style={replayBtnStyle}
                    >
                      Backtest Signals
                    </button>
                    <button
                      type="button"
                      onClick={() => openDeployModal(account.id)}
                      style={addDeploymentBtnStyle}
                    >
                      <Plus size={13} />
                      Deploy Strategy
                    </button>
                  </div>
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
                      <LiveDeploymentCard
                        key={card.id}
                        card={card}
                        busy={updatingDeploymentIds.has(card.id)}
                        onPause={handlePauseControl}
                        onStop={handleStopControl}
                        onDelete={handleDelete}
                      />
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

const miniStatStyle: React.CSSProperties = {
  border: "1px solid rgba(148, 163, 184, 0.22)",
  borderRadius: "8px",
  padding: "7px 8px",
  display: "grid",
  gap: "4px",
  background: "rgba(2, 6, 23, 0.28)",
};

const miniStatLabelStyle: React.CSSProperties = {
  fontSize: "0.68rem",
  color: "var(--color-text-muted)",
  letterSpacing: "0.04em",
  textTransform: "uppercase",
};

const miniStatValueStyle: React.CSSProperties = {
  fontSize: "0.82rem",
  color: "#e2e8f0",
  fontWeight: 600,
};

function winRateBadgeStyle(rate: number): React.CSSProperties {
  const isStrong = rate >= 55;
  const isWeak = rate <= 45;
  return {
    justifySelf: "start",
    borderRadius: "999px",
    padding: "2px 8px",
    fontSize: "0.72rem",
    fontWeight: 700,
    border: isStrong
      ? "1px solid rgba(34, 197, 94, 0.34)"
      : isWeak
        ? "1px solid rgba(248, 113, 113, 0.34)"
        : "1px solid rgba(148, 163, 184, 0.35)",
    background: isStrong
      ? "rgba(34, 197, 94, 0.14)"
      : isWeak
        ? "rgba(248, 113, 113, 0.14)"
        : "rgba(148, 163, 184, 0.14)",
    color: isStrong ? "#86efac" : isWeak ? "#fecaca" : "#cbd5e1",
  };
}

function formatSignedUsd(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatPercent(value: number): string {
  return `${value.toFixed(1)}%`;
}

function normalizeLiveHubStats(
  stats: LiveAccountStats,
  balances: LiveBalanceRecord[],
  openPositions: number,
): LiveHubAccountStats {
  const tradesCount = asFiniteNumber(stats.total_trades, 0);
  const winRate = toWinRatePercent(stats.win_rate);
  const totalPnl = asFiniteNumber(stats.total_pnl, 0);
  return {
    tradesCount,
    winRate,
    totalPnl,
    openPositions,
    sparkline: balances
      .map((item) => asFiniteNumber(item.equity, Number.NaN))
      .filter((value) => Number.isFinite(value)),
  };
}

function buildLiveHubFallbackStats(
  balances: LiveBalanceRecord[],
  trades: LiveTradeSlippage[],
  openPositions: number,
): LiveHubAccountStats {
  const tradesCount = trades.length;
  const realized = calculateRealizedTradeStats(trades);
  const winRate = tradesCount > 0 ? (realized.winningTrades / tradesCount) * 100 : 0;
  const sparklineSource = balances.map((item) =>
    asFiniteNumber(item.equity, Number.NaN),
  );

  return {
    tradesCount,
    winRate,
    totalPnl: realized.totalPnl,
    openPositions,
    sparkline: sparklineSource.filter((value) => Number.isFinite(value)),
  };
}

function calculateRealizedTradeStats(trades: LiveTradeSlippage[]): {
  totalPnl: number;
  winningTrades: number;
  losingTrades: number;
} {
  const positions = new Map<string, { qty: number; avg: number }>();
  let totalPnl = 0;
  let winningTrades = 0;
  let losingTrades = 0;

  const orderedTrades = [...trades].sort((a, b) => a.execution_time - b.execution_time);
  for (const trade of orderedTrades) {
    const action = String(trade.action || "").toLowerCase();
    const direction = action === "buy" ? 1 : action === "sell" ? -1 : 0;
    if (direction === 0) {
      continue;
    }

    const quantity = Math.abs(asFiniteNumber(trade.filled_qty, 0));
    const price = asFiniteNumber(trade.actual_price, 0);
    if (quantity <= 0 || price <= 0) {
      continue;
    }

    const symbol = String(trade.symbol || "UNKNOWN").toUpperCase();
    const delta = quantity * direction;
    const current = positions.get(symbol) ?? { qty: 0, avg: 0 };
    const currentQty = current.qty;
    const currentAvg = current.avg;

    if (currentQty === 0) {
      positions.set(symbol, { qty: delta, avg: price });
      continue;
    }

    if (currentQty * delta > 0) {
      const combinedQty = Math.abs(currentQty) + Math.abs(delta);
      const nextAvg =
        combinedQty > 0
          ? ((Math.abs(currentQty) * currentAvg) + (Math.abs(delta) * price)) / combinedQty
          : price;
      positions.set(symbol, { qty: currentQty + delta, avg: nextAvg });
      continue;
    }

    const closedQty = Math.min(Math.abs(currentQty), Math.abs(delta));
    if (closedQty > 0) {
      const sideSign = currentQty > 0 ? 1 : -1;
      const realized = (price - currentAvg) * closedQty * sideSign;
      totalPnl += realized;
      if (realized > 0) {
        winningTrades += 1;
      } else if (realized < 0) {
        losingTrades += 1;
      }
    }

    const newQty = currentQty + delta;
    if (newQty === 0) {
      positions.set(symbol, { qty: 0, avg: 0 });
    } else if (currentQty * newQty > 0) {
      positions.set(symbol, { qty: newQty, avg: currentAvg });
    } else {
      positions.set(symbol, { qty: newQty, avg: price });
    }
  }

  return { totalPnl, winningTrades, losingTrades };
}

function toWinRatePercent(value: unknown): number {
  const numeric = asFiniteNumber(value, 0);
  if (numeric > 0 && numeric <= 1) {
    return numeric * 100;
  }
  return numeric;
}

function asFiniteNumber(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) {
      return numeric;
    }
  }
  return fallback;
}

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

const openViewerBtnStyle: React.CSSProperties = {
  border: "1px solid rgba(59, 130, 246, 0.4)",
  background: "rgba(59, 130, 246, 0.16)",
  color: "#bfdbfe",
  borderRadius: "10px",
  padding: "8px 10px",
  fontSize: "0.82rem",
  cursor: "pointer",
};

const replayBtnStyle: React.CSSProperties = {
  border: "1px solid rgba(168, 85, 247, 0.45)",
  background: "rgba(168, 85, 247, 0.16)",
  color: "#e9d5ff",
  borderRadius: "10px",
  padding: "8px 10px",
  fontSize: "0.82rem",
  cursor: "pointer",
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
