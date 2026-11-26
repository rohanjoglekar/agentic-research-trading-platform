import "./globals.css";
import type { Viewport } from "next";
import { Archivo } from "next/font/google";
import { ThemeProvider } from "@/lib/theme";

// The pages set everything in one family; the width axis gives the headlines a
// slightly expanded cut without loading a second typeface.
const archivo = Archivo({
  subsets: ["latin"], variable: "--font-archivo", display: "swap", axes: ["wdth"],
});

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#101316" },
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
  ],
};

// Runs before paint so the chosen theme never flashes. The pages are light by
// default: they are read as a report, not a trading terminal. An explicit
// choice (the toggle, persisted) wins.
const themeScript = `(function(){var d=document.documentElement;try{var t=localStorage.getItem("forge-theme");d.dataset.theme=t==="dark"?"dark":"light"}catch(e){d.dataset.theme="light"}})()`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning className={archivo.variable}>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>
        <ThemeProvider>
          <a href="#main" className="skip-link">Skip to content</a>
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
