import assert from "node:assert/strict";

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
/**
 * The body of one exported `const` arrow function, as source text.
 *
 * Shared rather than duplicated, after `source-hygiene.test.ts` turned out to guard
 * the same property - the typed response of `updateLiveDeploymentStatus` - with the
 * same whole-file regex, and to be defeated the same way. Two copies of a helper that
 * finds a function body is the same hazard as two copies of the function body itself.
 */
export { functionSource };

/**
 * Source text with comments removed.
 *
 * A whole-file presence assertion is satisfied by a comment. Measured in
 * `optimizationEvidence.test.ts`: renaming the `In-sample selected score` label to
 * `Selected score` and dropping the `oos_status ?? "unavailable"` coalesce left all 19
 * tests green, because a comment at the end of the file mentioned both. That label is
 * the disclosure that tells an operator the displayed score came from the training
 * window, so a comment standing in for it is not a cosmetic weakness.
 *
 * Comments are tracked rather than stripped by regex because `//` and `/*` occur
 * inside strings - every URL in this codebase - and a regex strip would eat a line
 * that happened to hold `https://`. Regex *literals* are not tracked; the two files this
 * is used against were checked for one containing `//`, and there is none.
 */
export function stripComments(source: string): string {
  const isQuote = (ch: string): boolean => ch === '"' || ch === "'" || ch === "`";
  let out = "";
  let quote: string | null = null;
  let escaped = false;
  let i = 0;
  while (i < source.length) {
    const ch = source[i];
    if (quote) {
      out += ch;
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === quote) quote = null;
      i += 1;
      continue;
    }
    if (isQuote(ch)) {
      quote = ch;
      out += ch;
      i += 1;
      continue;
    }
    if (ch === "/" && source[i + 1] === "/") {
      while (i < source.length && source[i] !== "\n") i += 1;
      continue;
    }
    if (ch === "/" && source[i + 1] === "*") {
      i += 2;
      while (i < source.length && !(source[i] === "*" && source[i + 1] === "/")) i += 1;
      i += 2;
      continue;
    }
    out += ch;
    i += 1;
  }
  return out;
}
