"use client";

// A page of its own, opened over a research page: it covers the screen and
// uses its full width, like every other page in the demo.
// Used by the company detail and the valuation model. Escape or the back link
// returns; focus stays inside while it is open.

import { useEffect, useId, useRef, type ReactNode } from "react";
import { ArrowLeft } from "lucide-react";

export function FullPage({
  eyebrow, title, meta, link, side, onClose, children, back = "Back to equity research",
}: {
  /** The back link's words: where Escape and the link return to. */
  back?: string;
  eyebrow: string;
  title: string;
  meta?: ReactNode;
  /** A link on the right of the top row, e.g. to the other view of this company. */
  link?: ReactNode;
  /** Beside the title on wide screens, below it on phones (e.g. tabs). */
  side?: ReactNode;
  onClose: () => void;
  children: ReactNode;
}) {
  const titleId = useId();
  const backRef = useRef<HTMLButtonElement>(null);
  const pageRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    backRef.current?.focus({ preventScroll: true });
    pageRef.current?.scrollTo({ top: 0 });
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && pageRef.current) {
        const f = pageRef.current.querySelectorAll<HTMLElement>("a[href],button,input,select,summary,[tabindex]:not([tabindex='-1'])");
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
        else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => { document.body.style.overflow = prev; window.removeEventListener("keydown", onKey); };
  }, [onClose, title]);

  return (
    <div className="fixed inset-0 z-50" role="presentation">
      <div ref={pageRef} role="dialog" aria-modal="true" aria-labelledby={titleId}
        className="fs-drawer fs-drawer--full absolute inset-0 overflow-y-auto">
        <div className="sm:sticky top-0 z-20" style={{ background: "var(--fs-canvas)", borderBottom: "1px solid var(--fs-line)" }}>
          <div className="fs-wrap fs-modelwrap pt-4 pb-5">
            <div className="flex items-center justify-between gap-4">
              <button ref={backRef} type="button" onClick={onClose}
                className="fs-back inline-flex items-center gap-1.5 -ml-2 px-2 h-9 rounded-lg bg-transparent border-0 cursor-pointer text-[0.875rem] font-medium">
                <ArrowLeft size={16} aria-hidden /> {back}
              </button>
              {link}
            </div>
            <div className="mt-4 flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
              <div className="min-w-0">
                <p className="fs-note m-0">{eyebrow}</p>
                <h2 id={titleId} className="fs-dash-title m-0 mt-1">{title}</h2>
                {meta && <p className="fs-note m-0 mt-1.5">{meta}</p>}
              </div>
              {side}
            </div>
          </div>
        </div>
        <div className="fs-wrap fs-modelwrap pb-16">{children}</div>
      </div>
    </div>
  );
}
