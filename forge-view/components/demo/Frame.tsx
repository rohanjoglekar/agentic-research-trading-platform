"use client";

// The overview reads as a document on white; every other /demo page is a
// dashboard on a wash with panels. One class on the root switches the tokens.

import { usePathname } from "next/navigation";

export default function Frame({ children }: { children: React.ReactNode }) {
  const path = (usePathname() ?? "").replace(/\/+$/, "");
  return <div className={path === "/demo" ? "fs" : "fs fs--dash"}>{children}</div>;
}
