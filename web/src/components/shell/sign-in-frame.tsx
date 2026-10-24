import type { ReactNode } from "react";
import { PRODUCT_NAME } from "@/lib/product";

/**
 * The frame of the sign-in page, at /sign-in/ and at a signed-out "/". There is no header and no navigation: the
 * page's column sits in the middle of the screen (top on a phone), and a footer holds the product name, the theme
 * switch and the link to the product site. The column's own mark is the only mark. The page's own content supplies
 * its `main` landmark.
 *
 * The footer is 60 px tall, and the main's top padding is 60 px too, so the column's middle is half the screen's
 * height. A phone starts the column below the safe area, at the same 72 px in every state.
 */
export function SignInFrame({ children, themeToggle }: { children: ReactNode; themeToggle: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col">
      <main id="main-content" className="flex flex-1 flex-col px-6 pt-15 max-sm:pt-[calc(env(safe-area-inset-top,0px)_+_4.5rem)] max-sm:pb-12">
        <div className="my-auto flex justify-center max-sm:my-0">{children}</div>
      </main>
      <footer className="text-subtle-foreground flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-6 py-2 text-label sm:px-8">
        <span>{PRODUCT_NAME}</span>
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
