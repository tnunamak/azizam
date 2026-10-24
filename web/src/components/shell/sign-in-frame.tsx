import type { ReactNode } from "react";
import { BrandMark } from "@/components/product/brand-mark";
import { PRODUCT_NAME } from "@/lib/product";

/**
 * The frame of the sign-in page, at /sign-in/ and at a signed-out "/". There is no header and no navigation: the
 * page's column sits in the middle of the screen (top on a phone), and a footer holds the mark, the theme switch and
 * the link to the product site. The page's own content supplies its `main` landmark.
 */
export function SignInFrame({ children, themeToggle }: { children: ReactNode; themeToggle: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col">
      <main id="main-content" className="flex flex-1 items-center justify-center px-6 py-12 max-sm:items-start max-sm:pt-18">
        {children}
      </main>
      <footer className="text-subtle-foreground flex items-center justify-between gap-4 px-6 py-2 text-label sm:px-8">
        <span className="flex items-center gap-1.5">
          <BrandMark className="size-4" />
          {PRODUCT_NAME}
        </span>
        <span className="flex items-center gap-1">
          {themeToggle}
          <a
            href="https://azizam.dev/"
            className="flex min-h-11 items-center underline underline-offset-[3px]"
          >
            About {PRODUCT_NAME}
          </a>
        </span>
      </footer>
    </div>
  );
}
