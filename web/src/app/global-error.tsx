"use client";

import { useEffect } from "react";

/**
 * The last resort: the root layout itself threw, so there is no shell, no
 * providers and no stylesheet. This renders its own html and body with one
 * inline style element and the same words as the page-level crashed state.
 * The console gets a fixed label and the digest, never the error; nothing is drawn.
 */
const STYLE = `
  :root { color-scheme: light dark; }
  body { margin: 0; min-height: 100dvh; display: grid; place-items: center; padding: 24px;
    box-sizing: border-box; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: #f3f6f6; color: #10232a; }
  main { max-width: 28rem; width: 100%; }
  h1 { font-size: 1.5rem; line-height: 1.25; margin: 0 0 12px; }
  p { margin: 0 0 20px; line-height: 1.5; color: #3d4f55; }
  .actions { display: flex; flex-wrap: wrap; gap: 12px; }
  button, a.home { min-height: 44px; padding: 0 16px; border-radius: 8px; font: inherit; font-weight: 500;
    display: inline-flex; align-items: center; cursor: pointer; text-decoration: none; box-sizing: border-box; }
  button { border: 0; background: #0d4552; color: #fff; }
  a.home { border: 1px solid #d7e0e2; background: transparent; color: #10232a; }
  button:focus-visible, a.home:focus-visible { outline: 2px solid #0d4552; outline-offset: 2px; }
  @media (prefers-color-scheme: dark) {
    body { background: #0c1f25; color: #e3ecee; }
    p { color: #b4c6cb; }
    button { background: #1f6f79; }
    a.home { border-color: #213e46; color: #e3ecee; }
    button:focus-visible, a.home:focus-visible { outline-color: #e3ecee; }
  }
`;

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("Azizam root error", error.digest ?? "no digest");
  }, [error]);
  return (
    <html lang="en">
      <head>
        <title>Azizam</title>
        <style dangerouslySetInnerHTML={{ __html: STYLE }} />
      </head>
      <body>
        <main>
          <h1>Something went wrong</h1>
          <p>
            Azizam could not show this page. Try again. If it keeps happening,
            go Home and tell the person who runs this gateway.
          </p>
          <div className="actions">
            <button type="button" onClick={reset}>
              Try again
            </button>
            {/* A plain link on purpose: the root layout failed, so a full load of the home page is the way out. */}
            {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
            <a className="home" href="/">
              Go home
            </a>
          </div>
        </main>
      </body>
    </html>
  );
}
