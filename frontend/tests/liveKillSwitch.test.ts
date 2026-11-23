import assert from "node:assert/strict";
import test, { beforeEach } from "node:test";

import {
  getLiveKillSwitch,
  haltLiveKillSwitch,
  resumeLiveKillSwitch,
} from "../src/api/client.ts";

// Behavioural tests for the kill-switch API client: `client.ts` is imported
// for real and `fetch` is stubbed, so these pin the actual request the browser
// would send - URL, method and JSON body - rather than pattern-matching the
// source. The server (#316) answers 428 without `confirmed_scope` on
// orders/flatten and every resume, so a client that stops sending it breaks
// the controls at runtime; a source check cannot prove which scope was echoed.

type FetchCall = { url: string; method: string; body: Record<string, unknown> };

const calls: FetchCall[] = [];

beforeEach(() => {
  calls.length = 0;
  globalThis.fetch = (async (url: unknown, options?: RequestInit) => {
    calls.push({
      url: String(url),
      method: options?.method ?? "GET",
      body: options?.body ? JSON.parse(String(options.body)) : {},
    });
    return { ok: true, json: async () => ({ state: {} }) } as Response;
  }) as typeof fetch;
});

test("getLiveKillSwitch reads the state the daemon enforces", async () => {
  await getLiveKillSwitch();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/live/kill-switch");
  assert.equal(calls[0].method, "GET");
});

test("haltLiveKillSwitch posts scope, mode, reason and confirmed_scope", async () => {
  await haltLiveKillSwitch("acc-1", "flatten", {
    confirmedScope: "acc-1",
    reason: "incident 42",
  });

  assert.equal(calls.length, 1);
  const call = calls[0];
  assert.equal(call.url, "/api/live/kill-switch/halt");
  assert.equal(call.method, "POST");
  assert.deepEqual(call.body, {
    scope: "acc-1",
    mode: "flatten",
    reason: "incident 42",
    confirmed_scope: "acc-1",
  });
});

test("an unconfirmed halt sends confirmed_scope null, not a literal", async () => {
  await haltLiveKillSwitch("acc-1", "entries");

  const call = calls[0];
  assert.equal(call.body.scope, "acc-1");
  assert.equal(call.body.mode, "entries");
  assert.equal(call.body.confirmed_scope, null);
});

test("resumeLiveKillSwitch echoes the confirmed scope", async () => {
  await resumeLiveKillSwitch("acc-1", "acc-1");

  assert.equal(calls.length, 1);
  const call = calls[0];
  assert.equal(call.url, "/api/live/kill-switch/resume");
  assert.equal(call.method, "POST");
  assert.deepEqual(call.body, { scope: "acc-1", confirmed_scope: "acc-1" });
});
