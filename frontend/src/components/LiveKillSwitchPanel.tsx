import { useState } from "react";
import { OctagonX } from "lucide-react";

import { haltLiveKillSwitch, resumeLiveKillSwitch } from "../api/client";
import type {
  LiveAccount,
  LiveKillSwitchHaltDetail,
  LiveKillSwitchMode,
  LiveKillSwitchState,
} from "../api/types";
import { Modal } from "./Modal";
import { useToast } from "./toastContext";

/**
 * The operator-facing kill switch (#300). It renders the state
 * `GET /api/live/kill-switch` reports - the same state the daemon enforces -
 * and drives the halt/resume endpoints #316 shipped. There is no frontend-only
 * semantics here: `readable: false` is shown as the fail-closed condition it
 * is, never as "nothing halted", and every money-moving action goes through
 * the confirmation dialog because the server demands the echoed scope anyway.
 */

type PendingKillSwitchAction =
  | { kind: "halt"; scope: string; mode: LiveKillSwitchMode }
  | { kind: "resume"; scope: string };

const HALT_MODES: ReadonlyArray<{
  mode: LiveKillSwitchMode;
  label: string;
  title: string;
}> = [
  {
    mode: "entries",
    label: "Halt entries",
    title: "Refuse new entries only. Working orders and open positions are left alone - one click, no confirmation.",
  },
  {
    mode: "orders",
    label: "Halt + orders",
    title: "Refuse new entries and cancel every working order. Open positions are left alone. Requires confirmation.",
  },
  {
    mode: "flatten",
    label: "Halt + flatten",
    title: "Refuse new entries, cancel working orders, close every open position and sweep orphaned protections. Moves money at the broker. Requires confirmation.",
  },
];

function accountLabel(account: LiveAccount): string {
  const resolved = account.name.trim();
  if (resolved) {
    return resolved;
  }
  return `${account.broker} ${account.is_demo ? "Demo" : "Live"}`;
}

function haltDetailFor(
  state: LiveKillSwitchState,
  scope: string,
): LiveKillSwitchHaltDetail | undefined {
  return state.halt_details.find((detail) => detail.scope === scope);
}

function formatReadTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString();
}

function describeHalt(detail: LiveKillSwitchHaltDetail | undefined, mode: string): string {
  const parts = [`HALTED · ${mode}`];
  if (detail?.halted_by && detail.halted_by !== "unknown") {
    parts.push(`by ${detail.halted_by}`);
  }
  if (detail?.halted_at) {
    parts.push(`at ${detail.halted_at}`);
  }
  if (detail?.reason) {
    parts.push(`— ${detail.reason}`);
  }
  return parts.join(" ");
}

function unlistedHaltedScopes(
  state: LiveKillSwitchState,
  accounts: LiveAccount[],
): Array<{ scope: string; label: string; isGlobal: boolean }> {
  // A halt on a scope the account list does not name - a filtered list, a
  // failed accounts fetch, a deleted account's leftover row - is still
  // enforced by the daemon, so the panel must show it rather than
  // under-report what is halted.
  const listed = new Set<string>([
    state.global_scope,
    ...accounts.map((account) => account.id),
  ]);
  const seen = new Set<string>();
  const rows: Array<{ scope: string; label: string; isGlobal: boolean }> = [];
  for (const halt of state.halts) {
    if (listed.has(halt.scope) || seen.has(halt.scope)) {
      continue;
    }
    seen.add(halt.scope);
    rows.push({
      scope: halt.scope,
      label: `unknown account ${halt.scope}`,
      isGlobal: false,
    });
  }
  return rows;
}

// A standing halt can be escalated in place to a stricter mode; the server
// refuses a weaker one. `entries` is the weakest mode, so nothing sits below
// it; `flatten` is the strongest, so nothing sits above it.
const STRICTER_HALT_MODES: Record<string, LiveKillSwitchMode[]> = {
  entries: ["orders", "flatten"],
  orders: ["flatten"],
  flatten: [],
};

const ALL_HALT_MODES: LiveKillSwitchMode[] = HALT_MODES.map(({ mode }) => mode);

function haltModesForScope(ownMode: string | null): LiveKillSwitchMode[] {
  if (ownMode === null) {
    // Not halted on this scope: every mode is available, including on an
    // unreadable state - halting is always safe, and fail-closed operators
    // must be able to halt even when the state cannot be read.
    return ALL_HALT_MODES;
  }
  // Halted: only the stricter modes, which the server accepts as an
  // in-place escalation. An unrecognised stored mode falls back to the full
  // set and lets the server refuse what it must.
  return STRICTER_HALT_MODES[ownMode] ?? ALL_HALT_MODES;
}

function rowStatusText(
  state: LiveKillSwitchState,
  ownMode: string | null,
  coveredByGlobal: boolean,
  detail: LiveKillSwitchHaltDetail | undefined,
  stale: boolean,
): string {
  const prefix = stale ? "last known: " : "";
  if (ownMode) {
    return `${prefix}${describeHalt(detail, ownMode)}`;
  }
  if (!state.readable) {
    return "Halt state unreadable - the daemon refuses entries (fail closed)";
  }
  if (coveredByGlobal) {
    return `${prefix}Covered by the global halt`;
  }
  return `${prefix}Trading allowed`;
}

export function LiveKillSwitchPanel({
  state,
  loadError,
  stale = false,
  staleSince = null,
  accounts,
  onStateChange,
}: {
  state: LiveKillSwitchState | null;
  loadError?: string;
  stale?: boolean;
  staleSince?: string | null;
  accounts: LiveAccount[];
  onStateChange: (state: LiveKillSwitchState) => void;
}) {
  const { showToast } = useToast();
  const [pending, setPending] = useState<PendingKillSwitchAction | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const scopes: Array<{ scope: string; label: string; isGlobal: boolean }> = state
    ? [
        { scope: state.global_scope, label: "All accounts (global)", isGlobal: true },
        ...accounts.map((account) => ({
          scope: account.id,
          label: `${accountLabel(account)} · ${account.broker} · ${account.is_demo ? "Demo" : "Live"}`,
          isGlobal: false,
        })),
        ...unlistedHaltedScopes(state, accounts),
      ]
    : [];

  const haltModeFor = (scope: string): string | null =>
    state?.halts.find((halt) => halt.scope === scope)?.mode ?? null;

  // `entries` is the deliberate one-click stop: it refuses new entries and can
  // cost nothing, so an incident must not route it through a dialog. Every
  // other action moves money or re-enables trading and is confirmed first -
  // the dialog is the UI's half of the server's confirmed_scope requirement.
  const requestHalt = (scope: string, mode: LiveKillSwitchMode) => {
    if (mode === "entries") {
      void runHalt(scope, mode, undefined, "");
      return;
    }
    setReason("");
    setPending({ kind: "halt", scope, mode });
  };

  const requestResume = (scope: string) => {
    setReason("");
    setPending({ kind: "resume", scope });
  };

  const runHalt = async (
    scope: string,
    mode: LiveKillSwitchMode,
    confirmedScope: string | undefined,
    reasonText: string,
  ) => {
    setBusy(true);
    try {
      const result = await haltLiveKillSwitch(scope, mode, {
        confirmedScope,
        reason: reasonText.trim(),
      });
      onStateChange(result.state);
      const label = scopes.find((entry) => entry.scope === scope)?.label ?? scope;
      showToast(`Kill switch halt written for ${label} (${mode}).`, "success");
      setPending(null);
    } catch (error_: unknown) {
      const message =
        error_ instanceof Error ? error_.message : "Failed to write the kill switch halt";
      showToast(message, "error");
    } finally {
      setBusy(false);
    }
  };

  const runResume = async (scope: string) => {
    setBusy(true);
    try {
      const result = await resumeLiveKillSwitch(scope, scope);
      onStateChange(result.state);
      const label = scopes.find((entry) => entry.scope === scope)?.label ?? scope;
      showToast(
        result.lifted
          ? `Kill switch halt lifted for ${label}.`
          : `No kill switch halt was in force for ${label}.`,
        "success",
      );
      setPending(null);
    } catch (error_: unknown) {
      const message =
        error_ instanceof Error ? error_.message : "Failed to lift the kill switch halt";
      showToast(message, "error");
    } finally {
      setBusy(false);
    }
  };

  const confirmPending = () => {
    if (!pending || busy) {
      return;
    }
    if (pending.kind === "halt") {
      void runHalt(pending.scope, pending.mode, pending.scope, reason);
    } else {
      void runResume(pending.scope);
    }
  };

  const pendingLabel = pending
    ? scopes.find((entry) => entry.scope === pending.scope)?.label ?? pending.scope
    : "";

  return (
    <div
      style={{
        border: "1px solid rgba(248, 113, 113, 0.28)",
        borderRadius: "12px",
        padding: "14px 16px",
        marginBottom: "18px",
        background: "rgba(127, 29, 29, 0.08)",
        display: "grid",
        gap: "10px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "10px", flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <OctagonX size={16} color="#fca5a5" />
          <strong style={{ fontSize: "0.9rem" }}>Kill Switch</strong>
          {state && !state.readable && (
            <span style={unreadableChipStyle} title={state.unreadable_reason ?? undefined}>
              STATE UNREADABLE - FAIL CLOSED
            </span>
          )}
          {state?.readable && state.global_halted && (
            <span style={haltedChipStyle}>GLOBAL HALT IN FORCE</span>
          )}
          {state && stale && (
            <span style={staleChipStyle} title="A refresh failed; this is the last state the server confirmed.">
              STATE STALE{staleSince ? ` - last read ${formatReadTime(staleSince)}` : ""}
            </span>
          )}
        </div>
        {loadError && (
          <span style={{ color: "var(--color-red)", fontSize: "0.78rem" }}>{loadError}</span>
        )}
      </div>

      {state && !state.readable && (
        <div style={{ color: "#fecaca", fontSize: "0.8rem" }}>
          The halt state could not be read
          {state.unreadable_reason ? `: ${state.unreadable_reason}` : ""}. The daemon
          treats this as halted and refuses new entries. Halt controls stay
          available - halting is always safe - but Resume is hidden until the
          state reads cleanly.
        </div>
      )}

      {!state && !loadError && (
        <div style={{ color: "var(--color-text-muted)", fontSize: "0.8rem" }}>
          Loading kill-switch state…
        </div>
      )}

      {state &&
        scopes.map(({ scope, label, isGlobal }) => {
          const ownMode = haltModeFor(scope);
          const detail = haltDetailFor(state, scope);
          const coveredByGlobal =
            !isGlobal && ownMode === null && state.readable && state.global_halted;
          const haltModes = haltModesForScope(ownMode);
          const showResume = ownMode !== null && state.readable;
          const controlsDisabled = busy;
          return (
            <div
              key={scope}
              data-kill-switch-scope={scope}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "10px",
                flexWrap: "wrap",
                borderTop: "1px solid rgba(148, 163, 184, 0.16)",
                paddingTop: "8px",
              }}
            >
              <div style={{ minWidth: "220px", fontSize: "0.84rem" }}>
                <div style={{ fontWeight: 600 }}>{label}</div>
                <div
                  style={{
                    fontSize: "0.74rem",
                    color: ownMode ? "#fecaca" : coveredByGlobal ? "#fde68a" : "var(--color-text-muted)",
                    marginTop: "2px",
                  }}
                >
                  {rowStatusText(state, ownMode, coveredByGlobal, detail, stale)}
                </div>
              </div>
              <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                {showResume && (
                  <button
                    type="button"
                    onClick={() => requestResume(scope)}
                    style={resumeBtnStyle}
                    disabled={controlsDisabled || stale}
                    title="Lift the halt on this scope. Re-enables trading; requires confirmation. Disabled while the state is stale."
                  >
                    Resume
                  </button>
                )}
                {haltModes.map((mode) => {
                  const { label: modeLabel, title } =
                    HALT_MODES.find((entry) => entry.mode === mode) ?? HALT_MODES[0];
                  return (
                    <button
                      key={mode}
                      type="button"
                      onClick={() => requestHalt(scope, mode)}
                      style={mode === "flatten" ? flattenBtnStyle : haltBtnStyle}
                      disabled={controlsDisabled}
                      title={
                        ownMode
                          ? `${title} Escalates the standing '${ownMode}' halt in place.`
                          : title
                      }
                    >
                      {modeLabel}
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}

      <Modal
        open={pending !== null}
        onClose={() => {
          if (!busy) {
            setPending(null);
            setReason("");
          }
        }}
        title={pendingTitle(pending)}
        width="520px"
      >
        {pending && (
          <div style={{ display: "grid", gap: "12px" }}>
            <p style={{ margin: 0, fontSize: "0.86rem", color: "var(--color-text)" }}>
              {pendingDescription(pending, pendingLabel)}
            </p>
            {pending.kind === "halt" && (
              <label style={dialogFieldLabelStyle}>
                Reason (optional){" "}
                <input
                  type="text"
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  style={dialogInputStyle}
                  placeholder="e.g. incident 42, broker discrepancy"
                />
              </label>
            )}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
              <button
                type="button"
                onClick={() => {
                  setPending(null);
                  setReason("");
                }}
                style={dialogSecondaryBtnStyle}
                disabled={busy}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={confirmPending}
                style={
                  pending.kind === "halt" && pending.mode === "flatten"
                    ? flattenConfirmBtnStyle
                    : dialogDangerBtnStyle
                }
                disabled={busy}
              >
                {busy
                  ? "Working…"
                  : pending.kind === "resume"
                    ? "Resume trading"
                    : pending.mode === "flatten"
                      ? "Flatten and halt"
                      : "Confirm halt"}
              </button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}

function pendingTitle(pending: PendingKillSwitchAction | null): string {
  if (!pending) {
    return "";
  }
  if (pending.kind === "resume") {
    return "Resume trading?";
  }
  return pending.mode === "flatten" ? "Flatten and halt?" : "Confirm kill switch halt";
}

function pendingDescription(
  pending: PendingKillSwitchAction,
  scopeLabel: string,
): string {
  if (pending.kind === "resume") {
    return (
      `This lifts the kill switch on ${scopeLabel}. The daemon may trade this ` +
      `scope again on its next tick. The confirmation echoes the scope so a ` +
      `similar-looking row cannot be resumed by mistake.`
    );
  }
  const consequence =
    pending.mode === "flatten"
      ? " It also cancels every working order, closes every open position and sweeps the protections the flatten orphaned - this moves money at the broker."
      : pending.mode === "orders"
        ? " It also cancels every working order on this scope."
        : "";
  return (
    `Halt ${scopeLabel} (${pending.scope}) in '${pending.mode}' mode.${consequence} The halt is ` +
    `persisted and the daemon enforces it on every tick, across restarts.`
  );
}

const unreadableChipStyle: React.CSSProperties = {
  fontSize: "0.68rem",
  borderRadius: "999px",
  padding: "3px 8px",
  border: "1px solid rgba(251, 146, 60, 0.5)",
  background: "rgba(251, 146, 60, 0.16)",
  color: "#fdba74",
  letterSpacing: "0.04em",
};

const haltedChipStyle: React.CSSProperties = {
  fontSize: "0.68rem",
  borderRadius: "999px",
  padding: "3px 8px",
  border: "1px solid rgba(248, 113, 113, 0.5)",
  background: "rgba(248, 113, 113, 0.18)",
  color: "#fecaca",
  letterSpacing: "0.04em",
};

const staleChipStyle: React.CSSProperties = {
  fontSize: "0.68rem",
  borderRadius: "999px",
  padding: "3px 8px",
  border: "1px solid rgba(234, 179, 8, 0.5)",
  background: "rgba(234, 179, 8, 0.16)",
  color: "#fde68a",
  letterSpacing: "0.04em",
};

const haltBtnStyle: React.CSSProperties = {
  border: "1px solid rgba(248, 113, 113, 0.35)",
  background: "rgba(248, 113, 113, 0.1)",
  color: "#fecaca",
  borderRadius: "8px",
  padding: "5px 10px",
  fontSize: "0.76rem",
  cursor: "pointer",
};

const flattenBtnStyle: React.CSSProperties = {
  ...haltBtnStyle,
  border: "1px solid rgba(248, 113, 113, 0.6)",
  background: "rgba(248, 113, 113, 0.2)",
  fontWeight: 600,
};

const resumeBtnStyle: React.CSSProperties = {
  border: "1px solid rgba(234, 179, 8, 0.45)",
  background: "rgba(234, 179, 8, 0.14)",
  color: "#fde68a",
  borderRadius: "8px",
  padding: "5px 10px",
  fontSize: "0.76rem",
  cursor: "pointer",
};

const dialogFieldLabelStyle: React.CSSProperties = {
  display: "grid",
  gap: "6px",
  fontSize: "0.82rem",
  color: "var(--color-text-muted)",
};

const dialogInputStyle: React.CSSProperties = {
  width: "100%",
  borderRadius: "10px",
  border: "1px solid var(--color-border)",
  background: "rgba(2, 6, 23, 0.42)",
  color: "var(--color-text)",
  padding: "10px 12px",
  fontSize: "0.87rem",
  outline: "none",
};

const dialogSecondaryBtnStyle: React.CSSProperties = {
  border: "1px solid var(--color-border)",
  background: "rgba(148, 163, 184, 0.08)",
  color: "var(--color-text)",
  borderRadius: "10px",
  padding: "8px 12px",
  cursor: "pointer",
};

const dialogDangerBtnStyle: React.CSSProperties = {
  border: "1px solid rgba(248, 113, 113, 0.55)",
  background: "rgba(248, 113, 113, 0.18)",
  color: "#fecaca",
  borderRadius: "10px",
  padding: "8px 12px",
  cursor: "pointer",
  fontWeight: 600,
};

const flattenConfirmBtnStyle: React.CSSProperties = {
  ...dialogDangerBtnStyle,
  border: "1px solid rgba(220, 38, 38, 0.7)",
  background: "rgba(220, 38, 38, 0.28)",
};
