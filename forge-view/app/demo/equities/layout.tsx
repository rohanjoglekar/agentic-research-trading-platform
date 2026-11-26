import type { Metadata } from "next";

export const metadata: Metadata = { title: "Equity trading" };

export default function Layout({ children }: { children: React.ReactNode }) {
  return children;
}
