"use client";

// Theme management: system preference by default, persisted explicit choice,
// brief cross-fade on switch. The no-flash inline script in app/layout.tsx
// sets data-theme before first paint; this provider takes over after mount.

import {
  ReactNode, createContext, useCallback, useContext, useEffect, useState,
} from "react";
import { MotionConfig } from "framer-motion";

export type Theme = "light" | "dark";
export type ThemePref = Theme | "system";

const STORAGE_KEY = "forge-theme";

interface ThemeCtxValue {
  theme: Theme;
  pref: ThemePref;
  setPref: (p: ThemePref) => void;
  toggle: () => void;
}

const ThemeCtx = createContext<ThemeCtxValue>({
  theme: "dark",
  pref: "system",
  setPref: () => {},
  toggle: () => {},
});

function systemTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [pref, setPrefState] = useState<ThemePref>("system");
  const [theme, setTheme] = useState<Theme>("dark");

  // Adopt whatever the no-flash script decided.
  useEffect(() => {
    const stored = localStorage.getItem(STORAGE_KEY);
    setPrefState(stored === "light" || stored === "dark" ? stored : "system");
    const current = document.documentElement.dataset.theme;
    setTheme(current === "light" ? "light" : "dark");
  }, []);

  const apply = useCallback((p: ThemePref) => {
    const t: Theme = p === "system" ? systemTheme() : p;
    const root = document.documentElement;
    root.classList.add("theme-fade");
    root.dataset.theme = t;
    window.setTimeout(() => root.classList.remove("theme-fade"), 400);
    setTheme(t);
  }, []);

  const setPref = useCallback(
    (p: ThemePref) => {
      setPrefState(p);
      if (p === "system") localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, p);
      apply(p);
    },
    [apply],
  );

  // Follow OS changes while on "system".
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => {
      if (pref === "system") apply("system");
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [pref, apply]);

  const toggle = useCallback(
    () => setPref(theme === "dark" ? "light" : "dark"),
    [theme, setPref],
  );

  return (
    <ThemeCtx.Provider value={{ theme, pref, setPref, toggle }}>
      <MotionConfig reducedMotion="user">{children}</MotionConfig>
    </ThemeCtx.Provider>
  );
}

export const useTheme = () => useContext(ThemeCtx);
