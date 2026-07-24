import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans, Instrument_Serif } from "next/font/google";
import "./globals.css";

import SiteHeader from "@/components/SiteHeader";

// Three self-hosted fonts (next/font downloads + inlines them at build — no runtime
// CDN, no layout shift). Each exposes a CSS variable that globals.css binds to a
// Tailwind font token: display serif for headings, Plex Sans for body, Plex Mono for
// every piece of machine data (chunk ids, scores, timings).
const display = Instrument_Serif({
  weight: "400",
  subsets: ["latin"],
  variable: "--font-instrument-serif",
  display: "swap",
});

const sans = IBM_Plex_Sans({
  weight: ["400", "500", "600"],
  subsets: ["latin"],
  variable: "--font-plex-sans",
  display: "swap",
});

const mono = IBM_Plex_Mono({
  weight: ["400", "500"],
  subsets: ["latin"],
  variable: "--font-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "ProdRAG · Signal",
  description: "A grounded, cited retrieval instrument — ask and ingest.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${display.variable} ${sans.variable} ${mono.variable}`}
    >
      <body className="min-h-screen font-sans antialiased">
        <SiteHeader />
        {children}
      </body>
    </html>
  );
}
