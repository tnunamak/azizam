import type { Viewport } from "next";
import { AppShell } from "@/components/shell/app-shell";
import { EARLY_SCRIPT } from "@/lib/early-fetch";
import { SESSION_HEAD_SCRIPT } from "@/lib/session-end";
import { publicOrigin, siteMetadata } from "@/lib/site-metadata";
import "./globals.css";

export const metadata = siteMetadata(
  publicOrigin(process.env.AZIZAM_PUBLIC_URL),
);

// The browser's own bar takes the chrome colour (tokens.css --chrome) of the
// theme the reader's system prefers.
export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#eef2f3" },
    { media: "(prefers-color-scheme: dark)", color: "#08171c" },
  ],
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Starts the viewer and Home requests before the JavaScript has loaded (lib/early-fetch.ts). */}
        <script dangerouslySetInnerHTML={{ __html: EARLY_SCRIPT }} />
        {/* Hides a page that is entering the back/forward cache, or that opens during a pending sign-out (session-end.ts). */}
        <script dangerouslySetInnerHTML={{ __html: SESSION_HEAD_SCRIPT }} />
      </head>
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
