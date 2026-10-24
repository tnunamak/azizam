import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { PRODUCT_NAME } from "@/lib/product";

/** The frame's way in: the page's controls report here whether they are busy, and the footer waits with them. */
const ReportBusy = createContext<((busy: boolean) => void) | null>(null);

/**
 * A control on the sign-in page calls this with whether a redirect or a submit is running. While one runs, the footer is
 * inert, so its theme switch and its link wait with the page's other controls. Outside the frame it does nothing.
 */
export function useReportSignInBusy(busy: boolean) {
  const report = useContext(ReportBusy);
  useEffect(() => {
    report?.(busy);
  }, [report, busy]);
  // A control that leaves the page while busy must not leave the footer inert.
  useEffect(() => () => report?.(false), [report]);
}

/**
 * The frame of the sign-in page, at /sign-in/ and at a signed-out "/". There is no header and no navigation. The
 * page's column starts at a fixed 20 vh from the top in every state, so the mark stays where it is when the options
 * arrive. A footer holds the theme switch and the link to the product site. The column's own mark is the only mark.
 * The page's own content supplies its `main` landmark. While a redirect or a submit runs, the footer is inert and dimmed,
 * as the page's other controls are.
 *
 * The max() keeps a short landscape phone's column below its safe area. At 390 by 844 and at 1440 by 900 the top is
 * the 20 vh value.
 */
export function SignInFrame({ children, themeToggle }: { children: ReactNode; themeToggle: ReactNode }) {
  const [busy, setBusy] = useState(false);
  return (
    <ReportBusy.Provider value={setBusy}>
      <div className="flex min-h-dvh flex-col">
        <main id="main-content" className="flex flex-1 flex-col px-6 pt-[max(20vh,calc(env(safe-area-inset-top,0px)_+_1rem))] max-sm:pb-12">
          <div className="flex justify-center">{children}</div>
        </main>
        <footer
          inert={busy}
          className={cn(
            "text-subtle-foreground flex flex-wrap items-center justify-end gap-x-4 gap-y-1 px-6 py-2 text-label sm:px-8",
            busy && "opacity-50",
          )}
        >
          <span className="flex items-center gap-1">
            {themeToggle}
            <a
              href="https://azizam.dev/"
              className="flex min-h-11 items-center underline-offset-[3px] hover:underline"
            >
              About {PRODUCT_NAME}
            </a>
          </span>
        </footer>
      </div>
    </ReportBusy.Provider>
  );
}
