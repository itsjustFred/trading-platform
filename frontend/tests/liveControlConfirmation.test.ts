import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (path: string) => readFile(new URL(path, import.meta.url), "utf8");

/**
 * The body of one exported arrow function, as source text.
 *
 * The first version of this file matched with `[\s\S]*?` across the whole file, so it
 * never established that the confirmation was inside the function it names. Measured:
 * deleting `confirmation: { confirmed_deployment_id: deploymentId }` from
 * `updateLiveDeploymentStatus` outright - which makes every live control 428, because
 * the server refuses arming, re-pointing and deleting without the echo - and adding the
 * same text as a comment at the end of the file, left all three tests green.
 *
 * A comment satisfied a safety guard. Reading the source at all is still the right call
 * here, because the failure being guarded is the client and the server disagreeing,
 * which a mock would faithfully reproduce; but the region has to be the function.
 *
 * Three ways to get the region wrong, each producing a slice that looks plausible, and
 * all three were written and caught while making this:
 *
 * - `indexOf("{", start)` finds the `{` inside `${encodeURIComponent(...)}` in the URL
 *   template literal, so the "body" was 33 characters of query string;
 * - skipping the parameter list and taking the next `{` finds the *type argument* of
 *   `fetchJSON<{ status: string; id: string }>(`, because a generic is not quoted and is
 *   indistinguishable from a body to a scanner;
 * - scanning for the statement's `;` at paren depth zero stops at the `;` inside that
 *   same type argument, so brace depth has to be tracked too. `deleteLiveDeployment`
 *   "had" a 27-character body.
 *
 * So: the parameter list is matched on paren depth, the first `=>` past it introduces
 * the body, and the body is a brace-delimited block or an expression ending at a
 * semicolon that is outside quotes and at paren *and* brace depth zero.
 */
const functionSource = (source: string, name: string): string => {
  const marker = `${name} = (`;
  const start = source.indexOf(marker);
  assert.notEqual(start, -1, `${name} is no longer an exported arrow function`);

  const isQuote = (ch: string): boolean => ch === '"' || ch === "'" || ch === "`";

  let quote: string | null = null;
  let escaped = false;
  let parens = 0;
  let arrow = -1;
  for (let i = start + marker.length - 1; i < source.length; i += 1) {
    const ch = source[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === quote) quote = null;
      continue;
    }
    if (isQuote(ch)) {
      quote = ch;
      continue;
    }
    if (ch === "(") parens += 1;
    else if (ch === ")") {
      parens -= 1;
      if (parens === 0) {
        // Past the parameter list. The `=>` may follow a return-type annotation.
        const end = source.indexOf(";", i);
        const rest = source.slice(i + 1, end === -1 ? i + 400 : end);
        const found = rest.indexOf("=>");
        assert.notEqual(found, -1, `${name} is not an arrow function`);
        arrow = i + 1 + found + 2;
        break;
      }
    }
  }
  assert.notEqual(arrow, -1, `${name} has no arrow body`);

  let i = arrow;
  while (i < source.length && /\s/.test(source[i])) i += 1;
  assert.notEqual(i, source.length, `${name} has an empty body`);

  let parenDepth = 0;
  let braceDepth = 0;

  if (source[i] === "{") {
    let depth = 0;
    quote = null;
    escaped = false;
    for (let j = i; j < source.length; j += 1) {
      const ch = source[j];
      if (quote) {
        if (escaped) escaped = false;
        else if (ch === "\\") escaped = true;
        else if (ch === quote) quote = null;
        continue;
      }
      if (isQuote(ch)) {
        quote = ch;
        continue;
      }
      if (ch === "{") depth += 1;
      else if (ch === "}") {
        depth -= 1;
        if (depth === 0) return source.slice(i, j + 1);
      }
    }
    throw new Error(`unbalanced braces in ${name}`);
  }

  quote = null;
  escaped = false;
  for (let j = i; j < source.length; j += 1) {
    const ch = source[j];
    if (quote) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === quote) quote = null;
      continue;
    }
    if (isQuote(ch)) {
      quote = ch;
      continue;
    }
    if (ch === "(" || ch === "[") parenDepth += 1;
    else if (ch === ")" || ch === "]") parenDepth -= 1;
    else if (ch === "{") braceDepth += 1;
    else if (ch === "}") braceDepth -= 1;
    else if (ch === ";" && parenDepth === 0 && braceDepth === 0) return source.slice(i, j + 1);
  }
  throw new Error(`unterminated body in ${name}`);
};

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
