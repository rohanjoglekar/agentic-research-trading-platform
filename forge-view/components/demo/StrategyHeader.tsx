"use client";

// The top of a strategy factsheet: its name, the finding written from live
// data, what the strategy is in one paragraph, and when the numbers were last
// refreshed. The headline renders only once there is a result to report, so a
// slow network shows a calm page, never a half-sentence.

import { updatedAt } from "./fmt";

export default function StrategyHeader({
  name, finding, lead, asOf,
}: {
  name: string;
  finding: string | null;
  lead: string;
  asOf?: string | null;
}) {
  const stamp = updatedAt(asOf);
  return (
    <div>
      <p className="m-0 text-[1.0625rem] font-semibold" style={{ color: "var(--fs-ink-2)" }}>{name}</p>
      <h1 className="fs-display mt-3 mb-0" style={{ minHeight: "2.16em" }} aria-live="polite">
        {finding ?? ""}
      </h1>
      <p className="fs-lead mt-6 mb-0">{lead}</p>
      <p className="fs-note mt-4 mb-0">
        Paper account on live markets{stamp ? `, updated ${stamp}` : ""}.
      </p>
    </div>
  );
}
