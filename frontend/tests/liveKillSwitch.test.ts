import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { functionSource, stripComments } from "./sourceRegion.ts";

const read = (path: string) => readFile(new URL(path, import.meta.url), "utf8");

// The kill switch is the one control that has to work while everything else is
// on fire, so the checks below are deliberately about the wiring rather than the
// styling: the client must reach the endpoints #316 shipped, the money-moving
// modes must echo the scope back the way the server demands, and the LiveHub
// must offer all three modes behind a real confirmation for the ones that move
// money. A mock would faithfully reproduce a client and server that disagree;
// reading the source is what catches that before someone clicks Flatten.

test("the client reaches the kill-switch endpoints the viewer exposes", async () => {
  const client = await read("../src/api/client.ts");

  assert.match(
    functionSource(client, "getLiveKillSwitch"),
    /["'`]\/live\/kill-switch["'`]/,
    "getLiveKillSwitch must GET /live/kill-switch",
  );
  assert.match(
    functionSource(client, "haltLiveKillSwitch"),
    /["'`]\/live\/kill-switch\/halt["'`]/,
    "haltLiveKillSwitch must POST /live/kill-switch/halt",
  );
  assert.match(
    functionSource(client, "resumeLiveKillSwitch"),
    /["'`]\/live\/kill-switch\/resume["'`]/,
    "resumeLiveKillSwitch must POST /live/kill-switch/resume",
  );
});

test("money-moving kill-switch calls echo the scope back, entries does not need to", async () => {
  const client = await read("../src/api/client.ts");

  // The server answers 428 without confirmed_scope on orders/flatten and on
  // every resume, so a client that stops sending it breaks the controls at
  // runtime - and a hardcoded phrase would satisfy a presence check while
  // proving nothing about *which* scope was confirmed.
  assert.match(
    functionSource(client, "haltLiveKillSwitch"),
    /confirmed_scope/,
    "haltLiveKillSwitch must send confirmed_scope",
  );
  assert.match(
    functionSource(client, "resumeLiveKillSwitch"),
    /confirmed_scope/,
    "resumeLiveKillSwitch must send confirmed_scope",
  );
  assert.doesNotMatch(
    client,
    /confirmed_scope:\s*["'`][^"'`]+["'`]/,
    "confirmed_scope must be the scope argument, not a literal string",
  );
});

test("the halt request carries the chosen mode and an optional reason", async () => {
  const client = await read("../src/api/client.ts");

  const halt = functionSource(client, "haltLiveKillSwitch");
  assert.match(halt, /\bmode\b/, "the halt body must send the mode");
  assert.match(halt, /\breason\b/, "the halt body must send the reason");
  assert.match(halt, /\bscope\b/, "the halt body must send the scope");
});

test("LiveHub renders the kill-switch panel fed by the live state", async () => {
  const hub = stripComments(await read("../src/pages/LiveHub.tsx"));

  assert.match(
    hub,
    /LiveKillSwitchPanel/,
    "LiveHub must render the kill-switch panel",
  );
  assert.match(
    hub,
    /getLiveKillSwitch\(/,
    "LiveHub must poll the kill-switch state, not just render a static panel",
  );
});

test("the panel offers all three modes and gates the money-moving ones", async () => {
  const panel = stripComments(await read("../src/components/LiveKillSwitchPanel.tsx"));

  for (const mode of ["entries", "orders", "flatten"]) {
    assert.match(
      panel,
      new RegExp(`"${mode}"`),
      `the panel must offer the '${mode}' halt mode`,
    );
  }

  // entries is the one-click emergency stop; orders, flatten and resume all
  // move money or re-enable trading, so they must pass through the confirm
  // dialog before the API is called with the scope echoed back.
  assert.match(panel, /\bModal\b/, "the panel must confirm through the shared Modal");
  assert.match(
    panel,
    /haltLiveKillSwitch\([^)]*confirmedScope/,
    "the confirmed halt path must pass confirmedScope through to the client",
  );
  assert.match(
    panel,
    /resumeLiveKillSwitch\([^)]*scope/,
    "the resume path must pass the scope through to the client",
  );
});
