/**
 * Safe stringification for values of unknown type (API payloads, config
 * objects, log fields). Shared by the table/log renderers so each page does
 * not grow its own private copy (Sonar S6551 / Duplicated Code).
 */

/** Renders an unknown value without ever producing "[object Object]". */
export function stringifyUnknownValue(value: unknown, fallback = ""): string {
  if (typeof value === "string") {
    return value;
  }
  if (value == null) {
    return fallback;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value) ?? fallback;
}
