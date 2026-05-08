import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'openai.vivid.fish — status',
  description: 'Local OpenAI-compatible proxy status dashboard',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
