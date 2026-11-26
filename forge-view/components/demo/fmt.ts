// Number and date formatting for the /demo factsheets. Plain language first:
// a recruiter should never have to decode "5.59" or "2026-07-06".

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
  "September", "October", "November", "December"];
const MON = MONTHS.map((m) => m.slice(0, 3));

export const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** "5.6%" — magnitude only. */
export function pct(v: number, digits = 1): string {
  return `${Math.abs(v).toFixed(digits)}%`;
}

/** "+5.6%" / "−4.7%" with a real minus sign; "0.0%" stays unsigned. */
export function signedPct(v: number, digits = 1): string {
  const r = Number(Math.abs(v).toFixed(digits));
  if (r === 0) return `${(0).toFixed(digits)}%`;
  return `${v > 0 ? "+" : "−"}${Math.abs(v).toFixed(digits)}%`;
}

/** "+4.9 pts" — a difference between two percentages. */
export function signedPts(v: number, digits = 1): string {
  const r = Number(Math.abs(v).toFixed(digits));
  if (r === 0) return `${(0).toFixed(digits)} pts`;
  return `${v > 0 ? "+" : "−"}${Math.abs(v).toFixed(digits)} pts`;
}

/** "$1.06M", "$101.5K", "$842" — compact dollars. */
export function usdCompact(v: number): string {
  const a = Math.abs(v);
  const sign = v < 0 ? "−" : "";
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(2)}M`;
  if (a >= 1e4) return `${sign}$${(a / 1e3).toFixed(1)}K`;
  return `${sign}$${Math.round(a).toLocaleString("en-US")}`;
}

/** "$1,234" / "−$56", whole dollars. */
export function usd(v: number): string {
  return `${v < 0 ? "−" : ""}$${Math.round(Math.abs(v)).toLocaleString("en-US")}`;
}

export function signedUsd(v: number): string {
  if (Math.round(Math.abs(v)) === 0) return "$0";
  return `${v > 0 ? "+" : "−"}$${Math.round(Math.abs(v)).toLocaleString("en-US")}`;
}

/** Parse "2026-07-06" (or a full ISO timestamp) as a calendar date, no TZ shift. */
export function parseDay(iso: string): { y: number; m: number; d: number } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso ?? "");
  if (!m) return null;
  const y = +m[1], mo = +m[2], d = +m[3];
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
  return { y, m: mo, d };
}

/** "July 6" — or "July 6, 2025" when it is not the reference year. */
export function longDay(iso: string, refYear = new Date().getFullYear()): string {
  const p = parseDay(iso);
  if (!p) return "";
  return `${MONTHS[p.m - 1]} ${p.d}${p.y !== refYear ? `, ${p.y}` : ""}`;
}

/** "Jul 6, 2026" */
export function shortDay(iso: string): string {
  const p = parseDay(iso);
  return p ? `${MON[p.m - 1]} ${p.d}, ${p.y}` : "";
}

/** "Nov 20" */
export function monthDay(iso: string): string {
  const p = parseDay(iso);
  return p ? `${MON[p.m - 1]} ${p.d}` : "";
}

export function monthShort(m: number): string {
  return MON[m - 1] ?? "";
}

export function monthLong(m: number): string {
  return MONTHS[m - 1] ?? "";
}

/** Whole days from today (local) to an ISO date; negative when past. */
export function daysUntil(iso: string, now = new Date()): number | null {
  const p = parseDay(iso);
  if (!p) return null;
  const a = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  const b = Date.UTC(p.y, p.m - 1, p.d);
  return Math.round((b - a) / 86_400_000);
}

/** "today, 2:45 PM" / "Nov 13, 2:45 PM" in the viewer's own time zone. */
export function updatedAt(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  return sameDay ? `today at ${time}` : `${MON[d.getMonth()]} ${d.getDate()} at ${time}`;
}

/** A plain number with a real minus sign: "−0.07", "1.45". */
export function fixed(v: number, digits = 2): string {
  const r = Number(Math.abs(v).toFixed(digits));
  return `${v < 0 && r !== 0 ? "−" : ""}${Math.abs(v).toFixed(digits)}`;
}

/** A percentage with a real minus sign but no plus: "−1.5%", "14.2%". */
export function plainPct(v: number, digits?: number): string {
  const txt = digits == null ? String(Math.abs(v)) : Math.abs(v).toFixed(digits);
  return `${v < 0 && Number(txt) !== 0 ? "−" : ""}${txt}%`;
}

/** Server text can name the platform ("Forge fundamentals cache", "Not used by
 *  any Forge trader"); the demo carries no product name, so drop it. */
export function unbrand(s: string): string {
  return s
    .replace(/\bForge(?:['’]s|-built)\s+/gi, "")
    .replace(/\bForge\s+/g, "")
    .replace(/\bForge\b/g, "the system");
}

/** "a 60% chance" but "an 85% chance", "an 11% chance", "an 18% chance". */
export function article(numberText: string): "a" | "an" {
  return /^(8|11(?!\d)|18(?!\d))/.test(numberText.trim()) ? "an" : "a";
}
