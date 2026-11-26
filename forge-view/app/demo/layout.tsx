import type { Metadata } from "next";
import "./demo.css";
import { Footer, TopBar } from "@/components/demo/Chrome";
import Frame from "@/components/demo/Frame";

export const metadata: Metadata = {
  // `absolute` so the root layout's template does not wrap the overview; the
  // dashboards get "Research · Trading research" and so on. The demo carries no
  // product name, so the link preview is overridden here too.
  title: { absolute: "Agentic trading and research", template: "%s · Agentic trading and research" },
  description:
    "Automated trading research: a daily screen of about 600 US stocks, a stock portfolio and an options income book, run by code and measured against the S&P 500. Paper money, live markets.",
  applicationName: "Agentic trading and research",
  openGraph: {
    title: "Agentic trading and research",
    description: "A daily screen of about 600 US stocks and two strategies trading paper money on live markets, measured against the S&P 500.",
    type: "website",
    siteName: "Agentic trading and research",
  },
};

export default function DemoLayout({ children }: { children: React.ReactNode }) {
  return (
    <Frame>
      <TopBar />
      <main id="main" className="fs-wrap fs-main">{children}</main>
      <Footer />
    </Frame>
  );
}
