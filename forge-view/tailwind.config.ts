import type { Config } from "tailwindcss";

// The demo styles itself with the fs-* classes in app/demo/demo.css; Tailwind
// supplies layout utilities only.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: { extend: {} },
  plugins: [],
};
export default config;
