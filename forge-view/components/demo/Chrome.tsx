"use client";

// The demo's own frame: five pages and a theme switch. No wordmark (the
// overview's headline is the one title), no footer fine print, no sidebar.

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "@/lib/theme";

const PAGES = [
  { href: "/demo", label: "Overview" },
  { href: "/demo/research", label: "Equity research" },
  { href: "/demo/options-research", label: "Options research" },
  { href: "/demo/equities", label: "Equity trading" },
  { href: "/demo/options", label: "Options trading" },
];

export function TopBar() {
  const path = usePathname() ?? "";
  const { theme, toggle } = useTheme();
  return (
    <header className="fs-wrap pt-6 sm:pt-8">
      {/* On phones the page links are one row that scrolls sideways; the theme
          switch stays top right. */}
      <div className="flex items-start justify-between gap-x-4">
        <nav aria-label="Pages" className="fs-pagenav order-1 min-w-0 flex-1 -ml-2 sm:-ml-3 flex flex-nowrap sm:flex-wrap items-center gap-x-0.5 sm:gap-x-2 overflow-x-auto sm:overflow-visible whitespace-nowrap">
          {PAGES.map((p) => {
            // exact or a sub-path: /demo/options must not light up on /demo/options-research
            const active = path === p.href || (p.href !== "/demo" && path.startsWith(`${p.href}/`));
            return (
              <Link
                key={p.href}
                href={p.href}
                aria-current={active ? "page" : undefined}
                className="relative inline-flex items-center min-h-[44px] px-2 sm:px-3 text-[0.9375rem] font-medium transition-colors duration-150"
                style={{ color: active ? "var(--fs-ink)" : "var(--fs-ink-3)" }}
              >
                {p.label}
                {/* the bar marks the page you are on — structure, not ornament */}
                <span
                  aria-hidden
                  className="absolute left-2 right-2 sm:left-3 sm:right-3 bottom-[6px] h-[2px] rounded-full"
                  style={{ background: active ? "var(--fs-ember)" : "transparent" }}
                />
              </Link>
            );
          })}
        </nav>
        <button
          type="button"
          onClick={toggle}
          aria-label={theme === "dark" ? "Switch to the light theme" : "Switch to the dark theme"}
          className="order-2 ml-auto self-start inline-flex items-center justify-center w-11 h-11 -mr-2 sm:mr-0 rounded-lg transition-colors duration-150 hover:bg-[color:var(--fs-wash)]"
          style={{ color: "var(--fs-ink-2)" }}
        >
          {theme === "dark" ? <Sun size={18} aria-hidden /> : <Moon size={18} aria-hidden />}
        </button>
      </div>
    </header>
  );
}

/** Bottom breathing room only: no fine print. */
export function Footer() {
  return <div aria-hidden className="pb-16" />;
}
