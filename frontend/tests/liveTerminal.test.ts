import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { stripComments } from "./sourceRegion.ts";

const read = (path: string) => readFile(new URL(path, import.meta.url), "utf8");

// Comments are stripped before any assertion here. Every assertion in this file is
// a presence or absence check over raw source, and a comment is not code: it
// satisfies a `match`, and it satisfies a `doesNotMatch` just as well, so an
// absence check could not tell a fix from a note about the fix.
//
// Measured on this file: setting `heartbeatIntervalMs: 0` back to the default
// `15000`, and leaving a comment saying the plain-text feed opts out of the ping,
// passed every test. The socket is then closed by the default 15s/45s heartbeat and
// the log is replayed from position 0 on every reconnect.
test("live terminal resets its feed state when the job id changes", async () => {
  const liveTerminal = stripComments(await read("../src/components/LiveTerminal.tsx"));

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
  const liveTerminal = stripComments(await read("../src/components/LiveTerminal.tsx"));
  const wsHook = stripComments(await read("../src/hooks/useWebSocket.ts"));

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
  const liveStreams = stripComments(await read("../src/hooks/liveStreams.ts"));

  // Only the plain-text task feed disables the heartbeat; the JSON live
  // streams (which the server pings) must still use the default.
  assert.doesNotMatch(liveStreams, /heartbeatIntervalMs:\s*0/);
});

test("terminal hosts dedupe the reported job through a ref, not state", async () => {
  // A state copy of "which job did I already report" is read through the
  // closure, so two status transitions batched into one commit both see the old
  // value and the parent is told the same job finished twice. A ref is read at
  // call time, and keeping the dedupe out of state also stops the callback from
  // being re-created on every status line the terminal receives.
  for (const path of [
    "../src/components/ResampleModal.tsx",
    "../src/components/DownloadModal.tsx",
    "../src/components/SmartHarvestModal.tsx",
  ]) {
    const source = stripComments(await read(path));

    assert.doesNotMatch(
      source,
      /const \[notifiedJobId, setNotifiedJobId\]/,
      `${path} must not track the reported job in state`,
    );
    assert.match(
      source,
      /notifiedJobIdRef\.current === jobId/,
      `${path} must dedupe against the ref`,
    );
    assert.match(
      source,
      /notifiedJobIdRef\.current = jobId;/,
      `${path} must record the reported job in the ref`,
    );
  }
});
