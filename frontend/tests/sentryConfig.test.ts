import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { stripComments } from "./sourceRegion.ts";

import {
  FRONTEND_SERVICE_TAG,
  resolveFrontendSentryOptions,
  resolveFrontendSentryRelease,
} from "../src/lib/sentryConfig.ts";

const viteConfig = readFile(new URL("../vite.config.ts", import.meta.url), "utf8").then(stripComments);
const mainTsx = readFile(new URL("../src/main.tsx", import.meta.url), "utf8").then(stripComments);

// Sentry DSNs are public-by-design (project IDs, not credentials), but tests
// use a Sentry-published dummy DSN instead of the live shared project.
const SHARED_DSN = "https://public@sentry.example.com/1";

test("frontend Sentry init is disabled without a DSN so local dev stays silent", () => {
  const options = resolveFrontendSentryOptions({});

  assert.equal(options, null);
});

test("frontend Sentry init uses the shared project DSN with the frontend service tag", () => {
  const options = resolveFrontendSentryOptions({ VITE_SENTRY_DSN: SHARED_DSN });

  assert.ok(options);
  assert.equal(options.dsn, SHARED_DSN);
  assert.equal(options.environment, "local");
  assert.equal(options.sampleRate, 1.0);
  assert.deepEqual(options.initialScope, { tags: { service: FRONTEND_SERVICE_TAG } });
});

test("frontend release naming matches the backend release convention with a frontend suffix", () => {
  assert.equal(
    resolveFrontendSentryRelease({
      appVersion: "0.0.0",
      gitSha: "6caf461",
    }),
    "trading-platform-frontend@0.0.0+6caf461",
  );
  assert.equal(
    resolveFrontendSentryRelease({ appVersion: "1.2.3", gitSha: "" }),
    "trading-platform-frontend@1.2.3",
  );
  assert.equal(
    resolveFrontendSentryRelease({ appVersion: "", gitSha: "6caf461" }),
    "trading-platform-frontend@0.1.0+6caf461",
  );
});

test("release falls back to the backend-style version-only release when no git sha is injected", () => {
  assert.equal(
    resolveFrontendSentryRelease({ appVersion: "2.0.0", gitSha: "unknown" }),
    "trading-platform-frontend@2.0.0",
  );
});

test("empty DSN is treated like a missing DSN instead of producing a broken client", () => {
  assert.equal(resolveFrontendSentryOptions({ VITE_SENTRY_DSN: "   " }), null);
});

test("blank environment and malformed sample rate fall back to safe defaults", () => {
  const options = resolveFrontendSentryOptions({
    VITE_SENTRY_DSN: SHARED_DSN,
    VITE_SENTRY_ENVIRONMENT: "   ",
    VITE_SENTRY_SAMPLE_RATE: "not-a-number",
  });

  assert.ok(options);
  assert.equal(options.environment, "local");
  assert.equal(options.sampleRate, 1.0);
});

test("traces sample rate stays undefined unless a valid rate is configured", () => {
  const disabled = resolveFrontendSentryOptions({
    VITE_SENTRY_DSN: SHARED_DSN,
    VITE_SENTRY_TRACES_SAMPLE_RATE: "",
  });
  assert.ok(disabled);
  assert.equal(disabled.tracesSampleRate, undefined);

  const enabled = resolveFrontendSentryOptions({
    VITE_SENTRY_DSN: SHARED_DSN,
    VITE_SENTRY_TRACES_SAMPLE_RATE: "0.1",
  });
  assert.ok(enabled);
  assert.equal(enabled.tracesSampleRate, 0.1);
});

test("malformed and out-of-range rates fall back to safe defaults instead of reaching the SDK", () => {
  for (const sampleRate of ["not-a-number", "1.5", "-0.1", ""]) {
    const options = resolveFrontendSentryOptions({
      VITE_SENTRY_DSN: SHARED_DSN,
      VITE_SENTRY_SAMPLE_RATE: sampleRate,
    });

    assert.ok(options);
    assert.equal(options.sampleRate, 1.0);
  }

  for (const tracesSampleRate of ["not-a-number", "1.5", "-0.1"]) {
    const options = resolveFrontendSentryOptions({
      VITE_SENTRY_DSN: SHARED_DSN,
      VITE_SENTRY_TRACES_SAMPLE_RATE: tracesSampleRate,
    });

    assert.ok(options);
    assert.equal(options.tracesSampleRate, undefined);
  }
});

// Both sources are read with comments stripped. Every assertion in this file is a
// whole-file presence or absence check, and a comment is not code:
//
// - `doesNotMatch(source, /addEventListener\(\s*"error"/)` is the worse direction. It
//   exists so a manual global handler cannot double-report alongside the Sentry SDK's
//   own, and a comment containing `addEventListener("error"` satisfied it.
// - the init-before-render ordering is an `indexOf` comparison, so a comment
//   mentioning `initFrontendSentry()` ahead of `createRoot(` satisfied the order too.
test("vite injects the app version and git sha define globals used for the Sentry release", async () => {
  const config = await viteConfig;

  assert.match(config, /__APP_VERSION__:\s*JSON\.stringify\(appVersion\)/);
  assert.match(config, /__GIT_SHA__:\s*JSON\.stringify\(gitSha\)/);
  assert.match(config, /require\('\.\/package\.json'\)/);
});

test("main.tsx awaits Sentry init before rendering the app", async () => {
  const source = await mainTsx;

  // The call must be awaited, not fire-and-forget: the SDK's global-handlers
  // integration only captures errors raised after init resolves, so a dropped
  // or backgrounded init silently loses the session's errors.
  assert.match(source, /await\s+initFrontendSentry\(\)/);
  const initIndex = source.indexOf("initFrontendSentry()");
  const renderIndex = source.indexOf("createRoot(");
  assert.ok(initIndex >= 0 && renderIndex > initIndex);
});

test("main.tsx does not register manual global handlers that would double-report", async () => {
  const source = await mainTsx;

  assert.doesNotMatch(source, /addEventListener\(\s*"error"/);
  assert.doesNotMatch(source, /addEventListener\(\s*"unhandledrejection"/);
});
