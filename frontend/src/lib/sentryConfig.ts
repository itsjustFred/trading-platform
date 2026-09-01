/**
 * Frontend Sentry wiring.
 *
 * The frontend reports to the same shared Sentry project as the backend
 * Python services (see shared/sentry.py); the service is distinguished by
 * the `service: frontend` tag. Release naming mirrors the backend
 * convention (`trading-platform@<version>+<git-sha>`) with a frontend
 * suffix: `trading-platform-frontend@<version>+<git-sha>`.
 */

export const FRONTEND_SERVICE_TAG = "frontend";

type SentryClient = NonNullable<ReturnType<typeof import("@sentry/react").init>>;

const DEFAULT_APP_VERSION = "0.1.0";
const DEFAULT_ENVIRONMENT = "local";
const DEFAULT_SAMPLE_RATE = 1.0;
const UNKNOWN_GIT_SHA = "unknown";

export interface FrontendSentryEnv {
  readonly VITE_SENTRY_DSN?: string;
  readonly VITE_SENTRY_ENVIRONMENT?: string;
  readonly VITE_SENTRY_SAMPLE_RATE?: string;
  readonly VITE_SENTRY_TRACES_SAMPLE_RATE?: string;
}

export interface FrontendSentryOptions {
  readonly dsn: string;
  readonly environment: string;
  readonly release: string;
  readonly sampleRate: number;
  readonly tracesSampleRate?: number;
  readonly initialScope: { tags: { service: string } };
}

function clean(value: string | undefined): string {
  return (value ?? "").trim();
}

function parseRate(value: string | undefined): number | null {
  const raw = clean(value);
  if (raw === "") {
    return null;
  }
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) {
    return null;
  }
  return parsed;
}

/**
 * Build the options passed to `Sentry.init`, or null when Sentry is
 * disabled (no DSN configured, or an unusable error sample rate).
 */
export function resolveFrontendSentryOptions(
  env: FrontendSentryEnv,
): FrontendSentryOptions | null {
  const dsn = clean(env.VITE_SENTRY_DSN);
  if (!dsn) {
    return null;
  }

  const rawSampleRate = clean(env.VITE_SENTRY_SAMPLE_RATE);
  const sampleRate = rawSampleRate === "" ? null : parseRate(rawSampleRate);
  const tracesRaw = clean(env.VITE_SENTRY_TRACES_SAMPLE_RATE);
  const tracesSampleRate = tracesRaw === "" ? undefined : (parseRate(tracesRaw) ?? undefined);

  return {
    dsn,
    environment: clean(env.VITE_SENTRY_ENVIRONMENT) || DEFAULT_ENVIRONMENT,
    release: resolveFrontendSentryRelease({
      appVersion: typeof __APP_VERSION__ === "string" ? __APP_VERSION__ : "",
      gitSha: typeof __GIT_SHA__ === "string" ? __GIT_SHA__ : "",
    }),
    sampleRate: sampleRate ?? DEFAULT_SAMPLE_RATE,
    tracesSampleRate,
    initialScope: { tags: { service: FRONTEND_SERVICE_TAG } },
  };
}

/**
 * `trading-platform-frontend@<version>+<git-sha>`, mirroring the backend
 * release convention in `shared/sentry.py:_detect_release`.
 */
export function resolveFrontendSentryRelease(options: {
  appVersion: string;
  gitSha: string;
}): string {
  const version = clean(options.appVersion) || DEFAULT_APP_VERSION;
  const gitSha = clean(options.gitSha) || UNKNOWN_GIT_SHA;
  if (gitSha === UNKNOWN_GIT_SHA) {
    return `trading-platform-frontend@${version}`;
  }
  return `trading-platform-frontend@${version}+${gitSha}`;
}

/**
 * Initialize the shared Sentry project for the frontend. A missing
 * `VITE_SENTRY_DSN` keeps Sentry disabled so local development stays silent.
 *
 * The SDK's default GlobalHandlers integration instruments
 * `window.onerror` / `window.onunhandledrejection`, so uncaught errors and
 * unhandled rejections are reported automatically once this resolves — no
 * manual `addEventListener("error")` hooks (those would double-report).
 * Returns the active client, or null when Sentry is disabled.
 */
export async function initFrontendSentry(): Promise<SentryClient | null> {
  const options = resolveFrontendSentryOptions(import.meta.env);
  if (!options) {
    return null;
  }

  const { init } = await import("@sentry/react");
  const client = init({
    dsn: options.dsn,
    environment: options.environment,
    release: options.release,
    sampleRate: options.sampleRate,
    tracesSampleRate: options.tracesSampleRate,
    attachStacktrace: true,
    initialScope: options.initialScope,
    sendDefaultPii: false,
  });

  return client ?? null;
}
