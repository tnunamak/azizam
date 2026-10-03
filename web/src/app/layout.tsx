import type { Metadata } from "next";
import { AppShell } from "@/components/shell/app-shell";
import { PRODUCT_NAME } from "@/lib/product";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: PRODUCT_NAME, template: `%s · ${PRODUCT_NAME}` },
  description:
    "Your own AI provider. Connect your apps and manage your gateway.",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
