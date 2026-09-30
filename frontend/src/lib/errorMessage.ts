/**
 * Pick the message to show for a caught value.
 *
 * The naive `err instanceof Error ? err.message : fallback` is wrong in a way
 * that hides failures: `new Error("")` and `new Error("   ")` are both real
 * Errors with an empty-ish message, and returning that empty string is worse
 * than returning nothing at all. Callers then branch on truthiness, so the
 * error branch never runs, the message never renders, and the UI falls through
 * to its loading or empty state instead - the user watches a spinner for a
 * request that already failed.
 *
 * A non-empty message is still preferred over the fallback: the server said
 * something specific and it is more useful than our generic text.
 */
export function describeError(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim() !== "") {
    return error.message;
  }
  return fallback;
}
