// Sentences written from the numbers. The headline of every factsheet is the
// finding itself, in words a recruiter can repeat — so the wording has to stay
// true for every state the data can be in: ahead, behind, level, up, down.

import { isNum, longDay, pct } from "./fmt";

/** Under this many percentage points apart, the two results are "level". */
export const LEVEL_BAND_PTS = 0.5;

function move(v: number): string {
  if (v >= 0.05) return `up ${pct(v)}`;
  if (v <= -0.05) return `down ${pct(v)}`;
  return "flat";
}

/** "ahead of" | "behind" | "level with" — the strategy relative to its benchmark. */
export function standing(ret: number, bench: number): "ahead" | "behind" | "level" {
  const diff = ret - bench;
  if (Math.abs(diff) < LEVEL_BAND_PTS) return "level";
  return diff > 0 ? "ahead" : "behind";
}

/**
 * "Up 5.6% since July 6, ahead of the S&P 500, which is up 0.7%."
 * Returns null until there is a return to report (the page shows nothing
 * rather than a half-sentence).
 */
export function performanceSentence(args: {
  ret: number | null | undefined;
  bench: number | null | undefined;
  since: string | null | undefined;
  refYear?: number;
}): string | null {
  const { ret, bench, since, refYear } = args;
  if (!isNum(ret)) return null;
  const head = move(ret);
  const lead = head.charAt(0).toUpperCase() + head.slice(1);
  const when = since ? longDay(since, refYear) : "";
  const first = when ? `${lead} since ${when}` : lead;
  if (!isNum(bench)) return glue(`${first}.`);
  const rel = standing(ret, bench);
  const relWords = rel === "ahead" ? "ahead of" : rel === "behind" ? "behind" : "about level with";
  return glue(`${first}, ${relWords} the S&P 500, which is ${move(bench)}.`);
}

/** Keep the units of a headline together when it wraps: "July 6", "S&P 500",
 *  "up 0.7%" must never be split across two lines. */
export function glue(text: string): string {
  const NB = "\u00A0";
  return text
    .replace(/S&P 500/g, `S&P${NB}500`)
    .replace(/\b(up|down|Up|Down) (\d)/g, `$1${NB}$2`)
    .replace(/\b(January|February|March|April|May|June|July|August|September|October|November|December) (\d)/g, `$1${NB}$2`);
}
