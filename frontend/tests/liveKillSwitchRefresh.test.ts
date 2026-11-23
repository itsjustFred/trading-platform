import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test, { after, beforeEach } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { build, type Plugin } from "esbuild";
import { act, createElement } from "react";

import { installJsdom, killSwitchState } from "./killSwitchTestEnv.ts";

// Behavioural tests for useLiveKillSwitch, the hook LiveHubPage uses for the
// kill-switch panel. The defects this pins were structural: the state read
// used to live inside refreshDeployments AFTER the accounts/deployments
// Promise.all, so a failed accounts fetch skipped it entirely - the last
// state kept rendering as current with an enabled Resume, and a first-load
// failure left "Loading kill-switch state…" forever. The hook gives the read
// its own mount effect and its own timer, so none of that can happen.
//
// The real hook is bundled with esbuild (so the extensionless .ts imports
// resolve the way Vite resolves them), the API client is swapped for a
// recording shim, and a harness component is rendered into jsdom.

const here = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(here, "..");

declare global {
  var __ksRefreshCalls: number;
  var __ksRefreshResponder: (() => unknown) | undefined;
  var __ksHookChange: (() => void) | undefined;
  var __ksWriteState: unknown;
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

const API_SHIM = `
export async function getLiveKillSwitch() {
  globalThis.__ksRefreshCalls = (globalThis.__ksRefreshCalls ?? 0) + 1;
  const responder = globalThis.__ksRefreshResponder;
  if (responder) {
    return responder();
  }
  return { readable: true, global_scope: "global", modes: [], halts: [], halted_accounts: [], global_halted: false, halt_details: [] };
}
`;

const shimPlugin: Plugin = {
  name: "kill-switch-refresh-shims",
  setup(build) {
    build.onResolve({ filter: /api\/client$/ }, () => ({
      path: "api-shim",
      namespace: "ks-refresh-shim",
    }));
    build.onLoad({ filter: /.*/, namespace: "ks-refresh-shim" }, () => ({
      contents: API_SHIM,
      loader: "js",
    }));
  },
};

const HARNESS = `
import { createElement } from "react";
import { KILL_SWITCH_POLL_MS, useLiveKillSwitch } from "../src/hooks/useLiveKillSwitch";

export { KILL_SWITCH_POLL_MS };

export function Harness() {
  const ks = useLiveKillSwitch();
  globalThis.__ksHookChange = () =>
    ks.onStateChange(
      globalThis.__ksWriteState ?? {
        readable: true,
        global_scope: "global",
        modes: ["entries", "orders", "flatten"],
        halts: [],
        halted_accounts: [],
        global_halted: false,
        halt_details: [],
      },
    );
  return createElement(
    "div",
    null,
    createElement("span", { "data-field": "state" }, ks.state ? "loaded" : "null"),
    createElement("span", { "data-field": "error" }, ks.error),
    createElement("span", { "data-field": "stale" }, String(ks.stale)),
    createElement("span", { "data-field": "halted" }, String(ks.state?.global_halted ?? "")),
    createElement("span", { "data-field": "readAt" }, ks.readAt ?? ""),
  );
}
`;

const outDir = path.join(frontendRoot, "node_modules", ".cache", "ks-refresh-test");
mkdirSync(outDir, { recursive: true });
const outfile = path.join(outDir, "harness.mjs");
await build({
  stdin: {
    contents: HARNESS,
    loader: "tsx",
    resolveDir: path.join(frontendRoot, "tests"),
    sourcefile: "ksHarness.tsx",
  },
  bundle: true,
  format: "esm",
  platform: "node",
  jsx: "automatic",
  mainFields: ["module", "main"],
  outfile,
  plugins: [shimPlugin],
  external: ["react", "react/jsx-runtime"],
  logLevel: "silent",
});

const { Harness, KILL_SWITCH_POLL_MS } = (await import(pathToFileURL(outfile).href)) as {
  Harness: () => ReturnType<typeof createElement>;
  KILL_SWITCH_POLL_MS: number;
};

// ── jsdom globals (shared install; react-dom is imported after it) ───────────

const window = installJsdom();

// react-dom must be imported AFTER the DOM globals exist: it decides at module
// init whether `input` events are supported (canUseDOM), and without them it
// falls back to the IE propertychange path where onChange can never fire.
const { createRoot } = await import("react-dom/client");

// ── fixtures and helpers ─────────────────────────────────────────────────────

type CapturedInterval = { fn: () => void; ms: number };
let intervals: CapturedInterval[] = [];
let realSetInterval: typeof window.setInterval;
let realClearInterval: typeof window.clearInterval;

let container: HTMLElement;
let root: ReturnType<typeof createRoot> | null = null;

async function renderHarness() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(createElement(Harness));
    // Let the mount effect's fetch resolve.
    await Promise.resolve();
    await Promise.resolve();
  });
}

function field(name: string): string {
  return container.querySelector(`[data-field="${name}"]`)?.textContent ?? "";
}

async function tickPoll() {
  const poll = intervals.find((entry) => entry.ms === KILL_SWITCH_POLL_MS);
  assert.ok(poll, `expected a ${KILL_SWITCH_POLL_MS}ms kill-switch poll interval`);
  await act(async () => {
    poll.fn();
    await Promise.resolve();
    await Promise.resolve();
  });
}

// First load succeeds, every later poll fails - the partly-broken-backend
// shape the stale/error tests all need.
function failPollsAfterFirstLoad(state: unknown = killSwitchState()) {
  let calls = 0;
  return () => {
    calls += 1;
    if (calls === 1) {
      return state;
    }
    throw new Error("viewer 500");
  };
}

// Drive the harness's halt/resume write: onStateChange with the state the
// panel set via __ksWriteState (or the default read-back).
async function writeReadBack() {
  await act(async () => {
    globalThis.__ksHookChange?.();
    await Promise.resolve();
  });
}

async function cleanup() {
  if (root) {
    await act(async () => {
      root!.unmount();
    });
    root = null;
  }
  container?.remove();
  document.body.innerHTML = "";
  globalThis.__ksRefreshCalls = 0;
  globalThis.__ksRefreshResponder = undefined;
  globalThis.__ksHookChange = undefined;
  globalThis.__ksWriteState = undefined;
}

beforeEach(async () => {
  intervals = [];
  realSetInterval = window.setInterval.bind(window);
  realClearInterval = window.clearInterval.bind(window);
  // Capture intervals instead of scheduling them: the poll must be driven
  // manually so the test does not wait 15 real seconds.
  window.setInterval = ((fn: () => void, ms?: number) => {
    intervals.push({ fn, ms: ms ?? 0 });
    return intervals.length;
  }) as typeof window.setInterval;
  window.clearInterval = (() => {}) as typeof window.clearInterval;
  await cleanup();
});

after(async () => {
  window.setInterval = realSetInterval;
  window.clearInterval = realClearInterval;
  await cleanup();
});

// ── the read is independent of the accounts/deployments refresh ──────────────

test("the kill-switch state is fetched on mount, on its own", async () => {
  globalThis.__ksRefreshResponder = () => killSwitchState();

  await renderHarness();

  assert.equal(globalThis.__ksRefreshCalls, 1);
  assert.equal(field("state"), "loaded");
  assert.equal(field("stale"), "false");
  assert.equal(field("error"), "");
});

test("a failed refresh marks the last good state stale instead of current", async () => {
  globalThis.__ksRefreshResponder = failPollsAfterFirstLoad(
    killSwitchState({ halts: [{ scope: "acc-1", mode: "entries" }] }),
  );

  await renderHarness();
  assert.equal(field("state"), "loaded");
  assert.equal(field("stale"), "false");

  // The next poll fails (the partly-broken-backend case): the panel must show
  // the last confirmed state AS stale - never a fresh-looking "Trading
  // allowed" with an enabled Resume.
  await tickPoll();

  assert.equal(globalThis.__ksRefreshCalls, 2);
  assert.equal(field("state"), "loaded", "the last good state is retained");
  assert.equal(field("stale"), "true");
  assert.equal(field("error"), "Could not load the kill-switch state.");
});

test("a first-load failure reports the error with no state to trust", async () => {
  globalThis.__ksRefreshResponder = () => {
    throw new Error("viewer down");
  };

  await renderHarness();

  assert.equal(field("state"), "null");
  assert.equal(field("stale"), "true");
  assert.equal(field("error"), "Could not load the kill-switch state.");
});

test("the state polls on its own timer, not only on account-stream events", async () => {
  globalThis.__ksRefreshResponder = () => killSwitchState();

  await renderHarness();
  assert.equal(globalThis.__ksRefreshCalls, 1);

  // No account-stream message ever arrives in this harness: the kill-switch
  // read must still repeat on its own interval so a halt written elsewhere
  // (CLI, another browser, the daemon's escalation) surfaces here.
  await tickPoll();
  assert.equal(globalThis.__ksRefreshCalls, 2);
});

test("a halt/resume write read-back clears the stale mark", async () => {
  globalThis.__ksRefreshResponder = failPollsAfterFirstLoad();

  await renderHarness();
  await tickPoll();
  assert.equal(field("stale"), "true");

  await writeReadBack();

  assert.equal(field("stale"), "false");
  assert.notEqual(field("readAt"), "");
});

test("LiveHub delegates the kill-switch read to the hook, not to refreshDeployments", async () => {
  // Source pin for the structural half of the fix: the page must not fetch
  // the kill-switch state itself (inside refreshDeployments or anywhere), or
  // a failed accounts/deployments fetch can skip or freeze it again.
  const liveHub = await readFile(
    new URL("../src/pages/LiveHub.tsx", import.meta.url),
    "utf8",
  );
  assert.match(liveHub, /useLiveKillSwitch/);
  assert.doesNotMatch(liveHub, /getLiveKillSwitch/);
});

test("a poll resolving after a halt/resume write cannot overwrite it", async () => {
  // #300 follow-up: the write's read-back is newer than anything an
  // in-flight poll can carry. A poll that started before the write and
  // resolves after it holds pre-write state and must be discarded, or the
  // panel would render a stale "trading allowed" over a fresh halt.
  globalThis.__ksRefreshResponder = () => killSwitchState();

  await renderHarness();
  assert.equal(field("halted"), "false");

  // Hold the next poll's response open.
  let releasePoll: (value: unknown) => void = () => undefined;
  const pending = new Promise<unknown>((resolve) => {
    releasePoll = resolve;
  });
  globalThis.__ksRefreshResponder = () => pending;

  // The poll starts and stays in flight on the held promise.
  await tickPoll();

  // While that poll is still in flight, a halt write returns the server's
  // read-back state.
  globalThis.__ksWriteState = killSwitchState({
    global_halted: true,
    halts: [{ scope: "global", mode: "entries" }],
  });
  await writeReadBack();
  assert.equal(field("halted"), "true");

  // The older poll finally resolves with pre-write state.
  await act(async () => {
    releasePoll(killSwitchState());
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

  assert.equal(
    field("halted"),
    "true",
    "the pre-write poll response must not overwrite the write's read-back",
  );
  assert.equal(field("stale"), "false");
});

test("a successful halt/resume write clears the poll error text", async () => {
  // #300 follow-up: after a failed poll the panel shows the error; a
  // successful halt/resume write is itself a fresh server read-back, so the
  // error text must clear with the state it returns.
  globalThis.__ksRefreshResponder = failPollsAfterFirstLoad();

  await renderHarness();
  await tickPoll();
  assert.equal(field("error"), "Could not load the kill-switch state.");

  await writeReadBack();

  assert.equal(field("error"), "");
  assert.equal(field("stale"), "false");
});
