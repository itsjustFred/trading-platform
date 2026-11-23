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

export function LiveKillSwitchPanel({
  state,
  loadError,
  accounts,
  onStateChange,
}: {
  state: LiveKillSwitchState | null;
  loadError?: string;
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
      void runHalt(scope, mode, "");
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
  ) => {
    setBusy(true);
    try {
      const result = await haltLiveKillSwitch(scope, mode, {
        confirmedScope,
        reason: reason.trim(),
      });
      onStateChange(result.state);
      showToast(`Kill switch halt written for ${scope} (${mode}).`, "success");
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
      showToast(
        result.lifted
          ? `Kill switch halt lifted for ${scope}.`
          : `No kill switch halt was in force for ${scope}.`,
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
      void runHalt(pending.scope, pending.mode, pending.scope);
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
        </div>
        {loadError && (
          <span style={{ color: "var(--color-red)", fontSize: "0.78rem" }}>{loadError}</span>
        )}
      </div>

      {state && !state.readable && (
        <div style={{ color: "#fecaca", fontSize: "0.8rem" }}>
          The halt state could not be read
          {state.unreadable_reason ? `: ${state.unreadable_reason}` : ""}. The daemon
          treats this as halted and refuses new entries; controls are disabled until
          the state reads cleanly.
        </div>
      )}

      {!state && !loadError && (
        <div style={{ color: "var(--color-text-muted)", fontSize: "0.8rem" }}>
          Loading kill-switch state…
        </div>
      )}

      {state?.readable &&
        scopes.map(({ scope, label, isGlobal }) => {
          const ownMode = haltModeFor(scope);
          const detail = haltDetailFor(state, scope);
          const coveredByGlobal = !isGlobal && ownMode === null && state.global_halted;
          const controlsDisabled = busy || !state.readable;
          return (
            <div
              key={scope}
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
                  {ownMode
                    ? describeHalt(detail, ownMode)
                    : coveredByGlobal
                      ? "Covered by the global halt"
                      : "Trading allowed"}
                </div>
              </div>
              <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                {ownMode ? (
                  <button
                    type="button"
                    onClick={() => requestResume(scope)}
                    style={resumeBtnStyle}
                    disabled={controlsDisabled}
                    title="Lift the halt on this scope. Re-enables trading; requires confirmation."
                  >
                    Resume
                  </button>
                ) : (
                  HALT_MODES.map(({ mode, label: modeLabel, title }) => (
                    <button
                      key={mode}
                      type="button"
                      onClick={() => requestHalt(scope, mode)}
                      style={mode === "flatten" ? flattenBtnStyle : haltBtnStyle}
                      disabled={controlsDisabled}
                      title={title}
                    >
                      {modeLabel}
                    </button>
                  ))
                )}
              </div>
            </div>
          );
        })}

      <Modal
        open={pending !== null}
        onClose={() => {
          if (!busy) {
            setPending(null);
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
                onClick={() => setPending(null)}
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
    `Halt ${scopeLabel} in '${pending.mode}' mode.${consequence} The halt is ` +
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
