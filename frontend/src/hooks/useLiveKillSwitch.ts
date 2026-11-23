import { useCallback, useEffect, useState } from "react";

import { getLiveKillSwitch } from "../api/client";
import type { LiveKillSwitchState } from "../api/types";

/**
 * Kill-switch state for the Live Hub panel (#398).
 *
 * The read runs on its own mount effect and its own timer, deliberately NOT
 * inside the accounts/deployments refresh: a failed accounts or deployments
 * fetch must never freeze or hide the kill-switch panel, and a halt or resume
 * written elsewhere (CLI, another browser, the daemon's own escalation) that
 * causes no account-stream push must still surface here.
 */
export const KILL_SWITCH_POLL_MS = 15_000;

export function useLiveKillSwitch() {
  const [state, setState] = useState<LiveKillSwitchState | null>(null);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [readAt, setReadAt] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    const refresh = async () => {
      try {
        const payload = await getLiveKillSwitch();
        if (!active) {
          return;
        }
        setState(payload);
        setError("");
        setStale(false);
        setReadAt(new Date().toISOString());
      } catch {
        if (!active) {
          return;
        }
        // A failed refresh must not leave the last good state rendering as
        // current: mark it stale so the panel shows "last known" instead of a
        // fresh-looking "Trading allowed". On first load `state` stays null
        // and the panel shows the error with no controls.
        setError("Could not load the kill-switch state.");
        setStale(true);
      }
    };

    void refresh();
    const intervalId = window.setInterval(() => {
      void refresh();
    }, KILL_SWITCH_POLL_MS);

    return () => {
      active = false;
      window.clearInterval(intervalId);
    };
  }, []);

  // A halt/resume write returns the server's read-back state, which is a
  // fresh read: it clears the stale mark a failed poll left behind.
  const onStateChange = useCallback((next: LiveKillSwitchState) => {
    setState(next);
    setStale(false);
    setReadAt(new Date().toISOString());
  }, []);

  return { state, error, stale, readAt, onStateChange };
}
