"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { getHealth } from "@/lib/api";

// A client component (not the layout itself) because it does two browser-only things:
// reads the active route (usePathname) to light the current nav item, and fetches
// /health/db on mount for the connection badge. Keeping it separate lets app/layout.tsx
// stay a server component (fonts + metadata).

type HealthState = "checking" | "ok" | "down";

const NAV = [
  { href: "/", label: "Chat" },
  { href: "/upload", label: "Upload" },
] as const;

function HealthBadge() {
  const [state, setState] = useState<HealthState>("checking");

  useEffect(() => {
    let alive = true;
    getHealth()
      .then((h) => alive && setState(h.status === "ok" ? "ok" : "down"))
      .catch(() => alive && setState("down"));
    return () => {
      alive = false;
    };
  }, []);

  const label =
    state === "ok" ? "backend live" : state === "down" ? "backend down" : "checking…";
  const dot =
    state === "ok"
      ? "bg-signal-ok"
      : state === "down"
        ? "bg-signal-bad"
        : "bg-fog-500 pulse-dot";

  return (
    <span
      className="flex items-center gap-2 font-mono text-[0.7rem] uppercase tracking-[0.18em] text-fog-500"
      title={`GET /health/db — ${label}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} aria-hidden />
      {label}
    </span>
  );
}

export default function SiteHeader() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-20 border-b border-ink-800/80 bg-ink-950/70 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-4xl items-center justify-between px-6">
        <Link href="/" className="group flex items-baseline gap-2">
          <span className="font-display text-2xl leading-none text-fog-100">
            ProdRAG
          </span>
          <span className="font-mono text-[0.65rem] uppercase tracking-[0.3em] text-amber-400 transition-colors group-hover:text-amber-300">
            signal
          </span>
        </Link>

        <nav className="flex items-center gap-1">
          {NAV.map(({ href, label }) => {
            const active =
              href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? "page" : undefined}
                className={`relative rounded-md px-3 py-1.5 text-sm transition-colors ${
                  active
                    ? "text-fog-100"
                    : "text-fog-400 hover:text-fog-100"
                }`}
              >
                {label}
                {active && (
                  <span className="absolute inset-x-3 -bottom-[1px] h-px bg-amber-400" />
                )}
              </Link>
            );
          })}
          <span className="mx-3 h-5 w-px bg-ink-700" aria-hidden />
          <HealthBadge />
        </nav>
      </div>
    </header>
  );
}
