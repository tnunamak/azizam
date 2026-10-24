import type { ReactNode } from "react";
import { PRODUCT_NAME } from "@/lib/product";

/**
 * The frame of the sign-in page, at /sign-in/ and at a signed-out "/". There is no header and no navigation. The
 * page's column starts at a fixed 20 vh from the top in every state, so the mark stays where it is when the options
 * arrive. A footer holds the theme switch and the link to the product site. The column's own mark is the only mark.
 * The page's own content supplies its `main` landmark.
 *
 * The max() keeps a short landscape phone's column below its safe area. At 390 by 844 and at 1440 by 900 the top is
 * the 20 vh value.
 */
export function SignInFrame({ children, themeToggle }: { children: ReactNode; themeToggle: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col">
      <main id="main-content" className="flex flex-1 flex-col px-6 pt-[max(20vh,calc(env(safe-area-inset-top,0px)_+_1rem))] max-sm:pb-12">
        <div className="flex justify-center">{children}</div>
      </main>
      <footer className="text-subtle-foreground flex flex-wrap items-center justify-end gap-x-4 gap-y-1 px-6 py-2 text-label sm:px-8">
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
  );
}
