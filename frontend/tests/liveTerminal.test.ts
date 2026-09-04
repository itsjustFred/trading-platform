import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (path: string) => readFile(new URL(path, import.meta.url), "utf8");

test("live terminal resets its feed state when the job id changes", async () => {
  const liveTerminal = await read("../src/components/LiveTerminal.tsx");

  // The reset effect must run for every jobId value — including a direct
  // A→B switch without an intermediate null. An early `if (jobId) return`
  // leaves the previous job's terminal status behind, which keeps the
  // finished-gate closed so the new job's feed socket is never opened.
  const resetEffect = liveTerminal.match(
    /useEffect\(\(\) => \{[^]*?\}, \[jobId\]\);/,
  );
  assert.ok(resetEffect, "feed reset effect keyed on jobId must exist");
  assert.doesNotMatch(resetEffect[0], /if \(jobId\)\s*\{\s*return/);
  assert.match(resetEffect[0], /setLogs\(""\)/);
  assert.match(resetEffect[0], /setTaskStatus\(null\)/);
});

test("live terminal opts out of the heartbeat on the plain-text task feed", async () => {
  const liveTerminal = await read("../src/components/LiveTerminal.tsx");
  const wsHook = await read("../src/hooks/useWebSocket.ts");

  // /ws/tasks/{id} never sends ping/pong frames and stays silent while a job
  // is idle; the default heartbeat (15s ping / 45s timeout) would close the
  // socket and replay the log from position 0 on every reconnect.
  assert.match(
    liveTerminal,
    /heartbeatIntervalMs:\s*0/,
    "task feed must disable the JSON-feed heartbeat",
  );

  // The hook must treat a non-positive interval as "no heartbeat timer".
  const startHeartbeat = wsHook.match(
    /const startHeartbeat = \(\) => \{[^]*?\n {4}\};/,
  );
  assert.ok(startHeartbeat, "startHeartbeat must exist in useWebSocket");
  assert.match(
    startHeartbeat[0],
    /if \(!heartbeatEnabled\)\s*\{\s*return;/,
    "a non-positive heartbeat interval must not start the timer",
  );
});

test("other websocket consumers keep the default heartbeat", async () => {
  const liveStreams = await read("../src/hooks/liveStreams.ts");

  // Only the plain-text task feed disables the heartbeat; the JSON live
  // streams (which the server pings) must still use the default.
  assert.doesNotMatch(liveStreams, /heartbeatIntervalMs:\s*0/);
});
