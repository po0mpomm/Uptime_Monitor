import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Uptime Monitor — Live URL Status Dashboard',
  description:
    'Monitor URLs for uptime and response time. Get instant visibility into up/down status, response times, and historical check data for all your registered endpoints.',
  keywords: ['uptime monitor', 'URL monitoring', 'website status', 'ping monitor'],
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
