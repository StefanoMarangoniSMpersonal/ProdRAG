import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ProdRAG",
  description: "Production-patterned RAG — Phase 0 spine",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
