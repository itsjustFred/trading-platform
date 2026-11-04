import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { functionSource } from "./sourceRegion.ts";

import { stripComments } from "./sourceRegion.ts";

const read = (path: string) => readFile(new URL(path, import.meta.url), "utf8");

test("live deployment controls sit above the card overlay link and offer resume", async () => {
  const liveHub = await read("../src/pages/LiveHub.tsx");

  // Overlay link stays at zIndex 1; interactive controls must be positioned above it
  // (same pattern as PortfoliosPage.tsx), otherwise every click navigates instead.
  assert.match(liveHub, /to=\{"?\/live\/"?|to=\{?["'`]\/live\//);
  assert.match(liveHub, /overlayLinkStyle/);
  const overlayStyle = liveHub.match(/const overlayLinkStyle[^;]*;/);
  assert.ok(overlayStyle, "overlay link style must be a named constant");
  assert.match(overlayStyle[0], /zIndex:\s*1\b/);
  assert.match(liveHub, /zIndex:\s*2\b/);

  // A resume (play) control must exist for PAUSED deployments.
  assert.match(liveHub, /onPlay/);
  assert.match(liveHub, /handleControl\("play"/);
  assert.match(liveHub, /Play/);
});

test("an application-level error boundary wraps the SPA", async () => {
  const app = await read("../src/App.tsx");
  const main = await read("../src/main.tsx");
  await read("../src/components/ErrorBoundary.tsx");

  assert.match(app, /ErrorBoundary/);
  assert.match(main, /ErrorBoundary/);
  // The lazy route subtree must be guarded as well as the root.
  assert.match(app, /<ErrorBoundary[^]*>[\s]*<Routes/);
});

test("live hub does not refetch on every websocket message", async () => {
  const liveHub = await read("../src/pages/LiveHub.tsx");
  const hooks = await read("../src/hooks/useDebouncedValue.ts");

  assert.match(hooks, /useDebouncedValue/);
  assert.match(liveHub, /useDebouncedValue|useDebouncedCallback/);

  // The stream effect must not call refreshDeployments synchronously per emission.
  const streamEffect = liveHub.match(
    /useEffect\(\(\) => \{\s*const payload = accountsStream\.data;[^]*?\}, \[[^\]]*\]\);/,
  );
  assert.ok(streamEffect, "accounts stream effect must exist");
  assert.doesNotMatch(streamEffect[0], /refreshDeployments\(/);
});

test("live detail replay modal fetches signal processors once per open", async () => {
  const liveDetail = await read("../src/pages/LiveDetail.tsx");

  // The old loop guarded on and depended on its own loading flag.
  assert.doesNotMatch(liveDetail, /replayModalOpen, signalProcessorsLoading\]/);
  assert.doesNotMatch(
    liveDetail,
    /if \(!replayModalOpen \|\| signalProcessorsLoading\)/,
  );
  // Fetching must still happen when the modal opens.
  assert.match(liveDetail, /getSignalProcessors/);
});

test("live streams keep the last payload across non-data envelopes", async () => {
  const liveStreams = await read("../src/hooks/liveStreams.ts");

  // Payload state must be keyed by URL/topic so non-data envelopes cannot blank it.
  assert.match(liveStreams, /payload\.url === wsUrl/);
  assert.match(liveStreams, /isDataEnvelopeType/);
  // Error envelopes must surface, not be silently swallowed as data.
  assert.match(liveStreams, /"error"/);
});

test("live terminal delegates to the reconnecting websocket hook", async () => {
  const liveTerminal = await read("../src/components/LiveTerminal.tsx");

  assert.doesNotMatch(liveTerminal, /new WebSocket\(/);
  assert.match(liveTerminal, /useWebSocket/);
});

test("request storms are debounced", async () => {
  const loggingPage = await read("../src/pages/LoggingPage.tsx");
  const downloadModal = await read("../src/components/DownloadModal.tsx");
  const createBacktestModal = await read("../src/components/CreateBacktestModal.tsx");

  // LoggingPage: search must be debounced before it drives the fetch effect.
  assert.match(loggingPage, /useDebouncedValue\(search/);
  assert.doesNotMatch(loggingPage, /\[activeLogFile, levelParam, search, showToast\]/);

  // DownloadModal: genesis coverage must not fire per keystroke.
  assert.match(downloadModal, /useDebouncedValue/);

  // CreateBacktestModal: availability checks must not fire per keystroke.
  assert.match(createBacktestModal, /useDebouncedValue/);
  assert.doesNotMatch(createBacktestModal, /\[open, symbolList, timeframe, start, end\]/);
});

test("StatCard delegates formatting instead of formatting inline", async () => {
  // Formatting inline is how a deliberate label such as "n/a" reached
  // Math.abs/toLocaleString as NaN and rendered "$NaN". The shared formatter
  // handles the pass-through; this pins that the card actually uses it.
  const statCard = await read("../src/components/StatCard.tsx");

  assert.match(statCard, /formatStatCardValue/);
  assert.match(statCard, /formatStatCardValue\(value, format\)/);
  // No inline numeric formatting of a possibly-NaN value.
  assert.doesNotMatch(statCard, /Math\.abs\(numVal\)\.toLocaleString/);
  assert.doesNotMatch(statCard, /\$\$\{abs\.toLocaleString/);
});

test("StatCard treats an infinite value as unavailable, not as a huge gain", async () => {
  // `!Number.isNaN(Infinity)` is true, so the NaN check let an infinite stat
  // through as a measurement: it rendered "Infinity%" through the percent branch
  // and was coloured as a gain. An infinite value is a failed computation.
  const statCard = await read("../src/components/StatCard.tsx");

  assert.match(statCard, /const finite = Number\.isFinite\(numVal\)/);
  assert.doesNotMatch(statCard, /const finite = !Number\.isNaN\(numVal\)/);
});

test("a pause that had to wait out submissions is never reported as a failure", async () => {
  // The backend answers 200 with the new status plus an explicit, possibly
  // non-clean drain outcome. Typing that as LiveDeploymentConfig dropped the
  // outcome fields, so the only thing the UI could show was the generic success
  // toast - or, when the pause was refused, a red error for a stop that had
  // actually worked, which is what led operators to click ACTIVE and re-arm the
  // bot they were trying to stop.
  const types = stripComments(await read("../src/api/types.ts"));
  const client = stripComments(await read("../src/api/client.ts"));
  const liveHub = await read("../src/pages/LiveHub.tsx");

  assert.match(types, /interface LiveDeploymentStatusUpdateResponse/);
  for (const field of [
    "submission_drain_outcome",
    "submission_drain_complete",
    "in_flight_submissions",
    "expired_unreleased_submissions",
  ]) {
    assert.ok(types.includes(field), `the drain outcome field ${field} must be typed`);
  }
  assert.match(types, /INDETERMINATE/);

  // The status endpoint must be typed with the response that carries them.
  //
  // Scoped to the function body. This matched with `[\s\S]*?` across the whole file, so
  // it never established that the type was on *this* endpoint. Measured: pointing
  // `updateLiveDeploymentStatus` at `Record<string, never>` and adding
  // `fetchJSON<LiveDeploymentStatusUpdateResponse>(x)` as a comment at the end of the
  // file passed all ten tests in this file. The drain fields asserted just above are
  // carried by that type, so this is the line that makes those assertions mean anything.
  assert.match(
    functionSource(client, "updateLiveDeploymentStatus"),
    /fetchJSON<LiveDeploymentStatusUpdateResponse>/,
  );

  // And the scope is real: an extractor that returned the whole file would satisfy the
  // line above. Without this, degrading the extractor to whole-file scope would leave
  // this file looking correct - which is exactly what happened to the regex.
  assert.doesNotMatch(
    functionSource(client, "updateLiveDeploymentStatus"),
    /deleteLiveDeployment/,
    "the extractor is returning more than updateLiveDeploymentStatus",
  );

  // A non-clean drain is surfaced as a warning, from the returned result, and
  // the control flow must not fall through to the plain success toast.
  const control = liveHub.match(
    /const handleControl[\s\S]*?\n {2}\}, \[refreshDeployments, showToast\]\);/,
  );
  assert.ok(control, "the deployment control flow must exist");
  assert.match(control[0], /submission_drain_complete === false/);
  assert.match(control[0], /showToast\(\s*result\.warning/);
  assert.match(control[0], /"warning"/);
  assert.match(control[0], /getLiveDeployments\(\)/);

  // The failure branch has to check whether the change actually landed before
  // telling the operator it failed: the request may have been applied with the
  // response lost in flight.
  const catchBranch = control[0].match(/catch \(error_: unknown\) \{[\s\S]*?\n {4}\}/);
  assert.ok(catchBranch, "the control flow must have a failure branch");
  assert.match(catchBranch[0], /deployment\.status === targetStatus/);
  assert.ok(
    catchBranch[0].indexOf('"warning"') < catchBranch[0].indexOf('"error"'),
    "a status change that did land must be reported before the error toast",
  );
});

test("an absent portfolio metric is never coerced to a numeric zero", async () => {
  const page = stripComments(await read("../src/pages/PortfolioDetailPage.tsx"));

  // Each of these metrics can come back null from the API when it could not be
  // computed. Coercing with `?? 0` / `|| 0` before handing the value to StatCard
  // turns that absence into a measurement: the em-dash path inside StatCard never
  // fires because it is handed a real number. PSR and Win Rate in the same block
  // already passed the raw value, so this is an inconsistency, not a convention.
  for (const metric of [
    "total_return",
    "net_profit",
    "sharpe",
    "sortino",
    "cagr",
    "max_drawdown",
    "win_rate",
  ]) {
    const coerced = new RegExp(
      `stats\\.${metric}\\s*(\\?\\?|\\|\\|)\\s*0`,
    );
    assert.ok(
      !coerced.test(page),
      `stats.${metric} must not be coerced to 0: an absent metric has to reach ` +
        `StatCard as null/undefined so it renders the shared em dash`,
    );
  }

  // Ratios keep their conventional 3 decimals through the shared helper, which
  // returns null (not "0.000") when the value is absent.
  assert.match(page, /formatRatioStat\(stats\.sharpe\)/);
  assert.match(page, /formatRatioStat\(stats\.sortino\)/);
  assert.doesNotMatch(page, /\|\|\s*0\)\.toFixed/);

  // The contribution table cell must use the same marker as its neighbours.
  assert.match(page, /row\.sharpe_ratio != null[\s\S]{0,120}UNAVAILABLE_STAT_TEXT/);
});
