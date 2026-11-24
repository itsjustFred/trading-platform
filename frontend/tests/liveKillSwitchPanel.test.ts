import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import path from "node:path";
import test, { after, beforeEach } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { build, type Plugin } from "esbuild";
import { act, createElement, type ComponentType } from "react";

import type { LiveAccount, LiveKillSwitchState } from "../src/api/types";
import { installJsdom, killSwitchState } from "./killSwitchTestEnv.ts";

// Behavioural tests for the kill-switch panel: the real component is bundled
// with esbuild (so the .tsx imports resolve the way Vite resolves them), the
// API client and toast context are swapped for recording shims, and the panel
// is rendered into jsdom and clicked. Source-text assertions cannot catch the
// defects this file pins - which buttons a halted row offers, what an
// unreadable or stale state renders, or whether a confirmation actually sends
// the echoed scope - because none of that is visible in the source alone.

const here = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(here, "..");

declare global {
  var __ksApiCalls: Array<Record<string, unknown>>;
  var __ksApiState: unknown;
  var __ksApiResponder: ((call: Record<string, unknown>) => unknown) | undefined;
  var __ksToasts: Array<{ message: string; tone?: string }>;
  var IS_REACT_ACT_ENVIRONMENT: boolean | undefined;
}

const TOAST_SHIM = `
export function useToast() {
  return {
    showToast: (message, tone) => {
      (globalThis.__ksToasts ??= []).push({ message, tone });
    },
  };
}
`;

const API_SHIM = `
export async function haltLiveKillSwitch(scope, mode, options) {
  const call = { fn: "halt", scope, mode, options };
  (globalThis.__ksApiCalls ??= []).push(call);
  const responder = globalThis.__ksApiResponder;
  if (responder) {
    return responder(call);
  }
  return { halted: true, scope, mode, state: globalThis.__ksApiState };
}
export async function resumeLiveKillSwitch(scope, confirmedScope) {
  const call = { fn: "resume", scope, confirmedScope };
  (globalThis.__ksApiCalls ??= []).push(call);
  const responder = globalThis.__ksApiResponder;
  if (responder) {
    return responder(call);
  }
  return { halted: false, lifted: true, scope, state: globalThis.__ksApiState };
}
`;

const shimPlugin: Plugin = {
  name: "kill-switch-test-shims",
  setup(build) {
    build.onResolve({ filter: /toastContext$/ }, () => ({
      path: "toast-shim",
      namespace: "ks-shim",
    }));
    build.onResolve({ filter: /api\/client$/ }, () => ({
      path: "api-shim",
      namespace: "ks-shim",
    }));
    build.onLoad({ filter: /.*/, namespace: "ks-shim" }, (args) => ({
      contents: args.path === "toast-shim" ? TOAST_SHIM : API_SHIM,
      loader: "js",
    }));
  },
};

const outDir = path.join(frontendRoot, "node_modules", ".cache", "ks-panel-test");
mkdirSync(outDir, { recursive: true });
const outfile = path.join(outDir, "panel.mjs");
await build({
  entryPoints: [path.join(frontendRoot, "src", "components", "LiveKillSwitchPanel.tsx")],
  bundle: true,
  format: "esm",
  platform: "node",
  jsx: "automatic",
  // Prefer ESM builds: the default node resolution picks lucide-react's CJS
  // bundle, whose `require` calls an ESM outfile cannot satisfy.
  mainFields: ["module", "main"],
  outfile,
  plugins: [shimPlugin],
  external: ["react", "react/jsx-runtime"],
  logLevel: "silent",
});

const { LiveKillSwitchPanel } = (await import(pathToFileURL(outfile).href)) as {
  LiveKillSwitchPanel: ComponentType<Record<string, unknown>>;
};

// ── jsdom globals (shared install; react-dom is imported after it) ───────────

const window = installJsdom();

// react-dom must be imported AFTER the DOM globals exist: it decides at module
// init whether `input` events are supported (canUseDOM), and without them it
// falls back to the IE propertychange path where onChange can never fire -
// which would make typeReason() a silent no-op.
const { createRoot } = await import("react-dom/client");

// ── fixtures and helpers ─────────────────────────────────────────────────────

const ACCOUNT: LiveAccount = {
  id: "acc-1",
  name: "Main",
  broker: "alpaca",
  balance: 100_000,
  is_demo: true,
};

function haltedState(scope: string, mode: string): LiveKillSwitchState {
  return killSwitchState({
    halts: [{ scope, mode }],
    halt_details: [
      {
        scope,
        mode,
        halted_at: "2026-11-23T10:00:00+00:00",
        halted_by: "operator",
        reason: "",
        history_available: true,
      },
    ],
  });
}

let container: HTMLElement;
let root: ReturnType<typeof createRoot> | null = null;

async function renderPanel(props: Record<string, unknown> = {}) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(
      createElement(LiveKillSwitchPanel, {
        state: killSwitchState(),
        accounts: [ACCOUNT],
        onStateChange: () => {},
        ...props,
      }),
    );
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
  globalThis.__ksApiCalls = [];
  globalThis.__ksApiState = killSwitchState();
  globalThis.__ksApiResponder = undefined;
  globalThis.__ksToasts = [];
}

function row(scope: string): HTMLElement {
  const el = container.querySelector<HTMLElement>(`[data-kill-switch-scope="${scope}"]`);
  assert.ok(el, `expected a kill-switch row for scope ${scope}`);
  return el;
}

function buttonLabels(scope: string): string[] {
  return Array.from(row(scope).querySelectorAll("button")).map((b) =>
    (b.textContent ?? "").trim(),
  );
}

function findButton(scope: string, label: string): HTMLButtonElement {
  const el = Array.from(row(scope).querySelectorAll("button")).find(
    (b) => (b.textContent ?? "").trim() === label,
  );
  assert.ok(el, `expected a '${label}' button on scope ${scope}`);
  return el as HTMLButtonElement;
}

async function click(el: Element) {
  await act(async () => {
    (el as HTMLElement).click();
    await Promise.resolve();
  });
}

function dialogButton(label: string): HTMLButtonElement {
  const el = Array.from(container.querySelectorAll("button")).find(
    (b) => (b.textContent ?? "").trim() === label,
  );
  assert.ok(el, `expected a '${label}' dialog button`);
  return el as HTMLButtonElement;
}

async function typeReason(text: string) {
  const input = container.querySelector<HTMLInputElement>('input[type="text"]');
  assert.ok(input, "expected the reason input in the dialog");
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value",
  )!.set!;
  await act(async () => {
    setter.call(input, text);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    await Promise.resolve();
  });
}

beforeEach(cleanup);
after(cleanup);

// ── escalation (blocker: a halted scope must offer stronger modes) ───────────

test("an entries halt offers orders and flatten escalation next to Resume", async () => {
  await renderPanel({
    state: haltedState("acc-1", "entries"),
    accounts: [ACCOUNT],
  });

  assert.deepEqual(buttonLabels("acc-1").sort(), [
    "Halt + flatten",
    "Halt + orders",
    "Resume",
  ]);
});

test("an orders halt offers only flatten escalation", async () => {
  await renderPanel({
    state: haltedState("acc-1", "orders"),
    accounts: [ACCOUNT],
  });

  assert.deepEqual(buttonLabels("acc-1").sort(), ["Halt + flatten", "Resume"]);
});

test("a flatten halt offers Resume and no further halt", async () => {
  await renderPanel({
    state: haltedState("acc-1", "flatten"),
    accounts: [ACCOUNT],
  });

  assert.deepEqual(buttonLabels("acc-1"), ["Resume"]);
});

test("the global row escalates the same way", async () => {
  await renderPanel({
    state: killSwitchState({
      halts: [{ scope: "global", mode: "entries" }],
      global_halted: true,
      halt_details: [
        {
          scope: "global",
          mode: "entries",
          halted_at: "2026-11-23T10:00:00+00:00",
          halted_by: "operator",
          reason: "",
          history_available: true,
        },
      ],
    }),
    accounts: [ACCOUNT],
  });

  assert.deepEqual(buttonLabels("global").sort(), [
    "Halt + flatten",
    "Halt + orders",
    "Resume",
  ]);
});

// ── unreadable state (should-fix: halt controls stay, Resume hides) ──────────

test("an unreadable state still offers halt buttons and hides Resume", async () => {
  await renderPanel({
    state: killSwitchState({ readable: false, unreadable_reason: "db down" }),
    accounts: [ACCOUNT],
  });

  assert.match(container.textContent ?? "", /STATE UNREADABLE - FAIL CLOSED/);
  for (const scope of ["global", "acc-1"]) {
    assert.deepEqual(buttonLabels(scope).sort(), [
      "Halt + flatten",
      "Halt + orders",
      "Halt entries",
    ]);
  }
  assert.doesNotMatch(container.textContent ?? "", /Trading allowed/);
});

test("an unreadable state with partial halts hides Resume but keeps escalation", async () => {
  // The server reports readable:false WITH the halts it could still see
  // (live_runner/kill_switch.py). A halted row must then offer only the
  // stricter escalation modes - Resume would claim a lift the unreadable
  // state cannot confirm.
  await renderPanel({
    state: killSwitchState({
      readable: false,
      unreadable_reason: "db down",
      halts: [{ scope: "acc-1", mode: "entries" }],
      halt_details: [
        {
          scope: "acc-1",
          mode: "entries",
          halted_at: "2026-11-23T10:00:00+00:00",
          halted_by: "operator",
          reason: "",
          history_available: true,
        },
      ],
    }),
    accounts: [ACCOUNT],
  });

  assert.match(container.textContent ?? "", /STATE UNREADABLE - FAIL CLOSED/);
  assert.deepEqual(buttonLabels("acc-1").sort(), [
    "Halt + flatten",
    "Halt + orders",
  ]);
  assert.equal(
    row("acc-1").textContent?.includes("Resume"),
    false,
    "Resume must not render while the state is unreadable, even on a halted row",
  );
});

// ── stale state (should-fix: a failed refresh is not "Trading allowed") ──────

test("a stale state marks rows last-known and disables Resume", async () => {
  await renderPanel({
    state: haltedState("acc-1", "entries"),
    stale: true,
    staleSince: "2026-11-23T09:59:00+00:00",
    accounts: [ACCOUNT],
  });

  assert.match(container.textContent ?? "", /STATE STALE/);
  const resume = findButton("acc-1", "Resume");
  assert.equal(resume.disabled, true);
  // Halt controls stay usable: halting on stale information is the safe direction.
  assert.equal(findButton("acc-1", "Halt + flatten").disabled, false);
});

test("a stale unhalted row reads last known, not current", async () => {
  await renderPanel({
    state: killSwitchState(),
    stale: true,
    staleSince: "2026-11-23T09:59:00+00:00",
    accounts: [ACCOUNT],
  });

  const status = row("acc-1").textContent ?? "";
  assert.match(status, /last known: Trading allowed/);
  assert.doesNotMatch(status, /(?<!last known: )Trading allowed/);
});

// ── unlisted halted scopes (should-fix: never under-report halts) ────────────

test("a halt on a scope missing from the account list still renders", async () => {
  await renderPanel({
    state: haltedState("ghost-account", "flatten"),
    accounts: [ACCOUNT],
  });

  const ghost = row("ghost-account");
  assert.match(ghost.textContent ?? "", /unknown account ghost-account/);
  assert.match(ghost.textContent ?? "", /HALTED · flatten/);
  assert.deepEqual(buttonLabels("ghost-account"), ["Resume"]);
});

// ── interactions with the (mocked) API ───────────────────────────────────────

test("entries is still the one-click halt: no dialog, no confirmed scope", async () => {
  await renderPanel({ accounts: [ACCOUNT] });

  await click(findButton("acc-1", "Halt entries"));

  assert.equal(globalThis.__ksApiCalls.length, 1);
  const call = globalThis.__ksApiCalls[0];
  assert.equal(call.fn, "halt");
  assert.equal(call.scope, "acc-1");
  assert.equal(call.mode, "entries");
  assert.equal(
    (call.options as Record<string, unknown>).confirmedScope,
    undefined,
    "entries must not echo a confirmed scope",
  );
  assert.doesNotMatch(container.textContent ?? "", /Flatten and halt\?/);
});

test("flatten goes through the dialog and echoes the scope", async () => {
  await renderPanel({ accounts: [ACCOUNT] });

  await click(findButton("acc-1", "Halt + flatten"));
  assert.match(container.textContent ?? "", /Flatten and halt\?/);
  assert.match(
    container.textContent ?? "",
    /acc-1/,
    "the dialog must name the raw scope id next to the label",
  );

  await click(dialogButton("Flatten and halt"));

  assert.equal(globalThis.__ksApiCalls.length, 1);
  const call = globalThis.__ksApiCalls[0];
  assert.equal(call.mode, "flatten");
  assert.equal(
    (call.options as Record<string, unknown>).confirmedScope,
    "acc-1",
    "the confirmed halt must echo the scope the server demands",
  );
});

test("the one-click entries halt never carries a dialog reason", async () => {
  // `reason` state is only non-empty while a halt dialog is open, and every
  // close path resets it - so the entries path must not read it at all. The
  // click below lands while the orders dialog is still open (jsdom does not
  // hit-test the modal overlay): if requestHalt ever forwards `reason`
  // instead of "", this pins it.
  await renderPanel({ accounts: [ACCOUNT] });

  await click(findButton("acc-1", "Halt + orders"));
  await typeReason("typed for orders, not entries");
  await click(findButton("acc-1", "Halt entries"));

  const entriesCall = globalThis.__ksApiCalls.find((call) => call.mode === "entries");
  assert.ok(entriesCall, "expected an entries halt call");
  assert.equal(
    ((entriesCall.options as Record<string, unknown>).reason as string).trim(),
    "",
    "the one-click entries halt must send an empty reason",
  );
});

test("a cancelled dialog does not leak its reason into a later one-click halt", async () => {
  await renderPanel({ accounts: [ACCOUNT] });

  await click(findButton("acc-1", "Halt + orders"));
  await typeReason("typed then cancelled");
  await click(dialogButton("Cancel"));

  await click(findButton("acc-1", "Halt entries"));

  const call = globalThis.__ksApiCalls[0];
  assert.equal(call.mode, "entries");
  assert.equal(
    ((call.options as Record<string, unknown>).reason as string).trim(),
    "",
    "a stale reason must not ride along on the one-click halt",
  );
});

test("escalating an entries halt confirms orders with the scope echoed", async () => {
  await renderPanel({
    state: haltedState("acc-1", "entries"),
    accounts: [ACCOUNT],
  });

  await click(findButton("acc-1", "Halt + orders"));
  await click(dialogButton("Confirm halt"));

  const call = globalThis.__ksApiCalls[0];
  assert.equal(call.fn, "halt");
  assert.equal(call.mode, "orders");
  assert.equal((call.options as Record<string, unknown>).confirmedScope, "acc-1");
});

test("resume confirms through the dialog and echoes the scope", async () => {
  await renderPanel({
    state: haltedState("acc-1", "entries"),
    accounts: [ACCOUNT],
  });

  await click(findButton("acc-1", "Resume"));
  assert.match(container.textContent ?? "", /Resume trading\?/);
  await click(dialogButton("Resume trading"));

  assert.equal(globalThis.__ksApiCalls.length, 1);
  const call = globalThis.__ksApiCalls[0];
  assert.equal(call.fn, "resume");
  assert.equal(call.scope, "acc-1");
  assert.equal(call.confirmedScope, "acc-1");
});

