import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Azizam — openai.vivid.fish status',
  description: 'Azizam local AI gateway status dashboard',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
