import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

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
