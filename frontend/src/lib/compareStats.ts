export type CompareDirection = "higher" | "lower";

export interface CompareRow {
  left: number | null;
  right: number | null;
  delta: number | null;
  betterSecond: boolean;
}

/**
 * One side-by-side comparison row.
 *
 * A metric the producer could not compute arrives as `null`. Subtracting it as
 * `0` reported a change that was never measured, so an unavailable side has no
 * delta at all: the caller renders the em dash and a neutral colour.
 */
export function compareRow(
  left: number | null,
  right: number | null,
  better: CompareDirection,
): CompareRow {
  if (left === null || right === null) {
    return { left, right, delta: null, betterSecond: false };
  }
  return {
    left,
    right,
    delta: right - left,
    betterSecond: better === "higher" ? right > left : right < left,
  };
}
