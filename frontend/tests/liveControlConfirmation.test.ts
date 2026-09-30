import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { functionSource } from "./sourceRegion.ts";

const read = (path: string) => readFile(new URL(path, import.meta.url), "utf8");

// The server refuses arming, re-pointing and deleting unless the request
// carries a confirmation, so the client has to send one. If it stops doing so,
// every control in the UI breaks at runtime with a 428 - and a source-level
// check is the only thing that catches it before someone clicks a button.
//
// Reading the source rather than mocking fetch is deliberate: the failure mode
// being guarded is the client and the server disagreeing, which a mock would
// faithfully reproduce.
test("the live client sends a confirmation on every state-changing control", async () => {
  const client = await read("../src/api/client.ts");

  // Status changes (arm / pause / stop). Scoped to the function body, not the file.
  assert.match(
    functionSource(client, "updateLiveDeploymentStatus"),
    /confirmation:\s*\{\s*confirmed_deployment_id:\s*deploymentId\s*\}/,
    "updateLiveDeploymentStatus must send confirmation.confirmed_deployment_id",
  );

  // Delete travels as a query param, because DELETE carries no body.
  assert.match(
    functionSource(client, "deleteLiveDeployment"),
    /confirm_deployment_id=\$\{encodeURIComponent\(deploymentId\)\}/,
    "deleteLiveDeployment must send confirm_deployment_id",
  );

  // The scope is real, rather than a slice that happens to contain the text: neither
  // body may mention the other function. Without this, an extractor that returned the
  // whole file would satisfy both assertions above and this file would look correct.
  // Verified by degrading the extractor to whole-file scope, which must fail here.
  assert.doesNotMatch(
    functionSource(client, "updateLiveDeploymentStatus"),
    /deleteLiveDeployment/,
    "the extractor is returning more than updateLiveDeploymentStatus",
  );
  assert.doesNotMatch(
    functionSource(client, "deleteLiveDeployment"),
    /updateLiveDeploymentStatus/,
    "the extractor is returning more than deleteLiveDeployment",
  );
});

test("the confirmation echoes the deployment rather than a fixed phrase", async () => {
  const client = await read("../src/api/client.ts");

  // A hardcoded phrase would satisfy a server that only checked presence. The
  // server checks the echoed id precisely so the wrong row cannot be confirmed.
  assert.doesNotMatch(
    client,
    /confirmed_deployment_id:\s*["'][^"']+["']/,
    "the confirmation must be the deployment id, not a literal string",
  );
});

test("deleting a live deployment asks the operator first", async () => {
  const hub = await read("../src/pages/LiveHub.tsx");

  const deleteHandler = hub.slice(
    hub.indexOf("const handleDelete"),
    hub.indexOf("const handleDelete") + 2000,
  );

  assert.match(
    deleteHandler,
    /window\.confirm\(/,
    "handleDelete must confirm before calling the API",
  );
});
