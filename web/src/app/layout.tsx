import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Gateway",
  description: "Admin and portal UI for the AI gateway",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      {/* Brand fonts are self-hosted via fontsource + geist npm packages.
          @font-face rules live in src/app/fonts.css (imported by globals.css),
          registering exact brand family names: Geist, JetBrains Mono,
          Newsreader. woff2 files are bundled same-origin by Next.js static
          export — no external font CDN required. */}
      <body>{children}</body>
    </html>
  );
}
