"use client";

import { useEffect, useState } from "react";

// The API base URL is injected at build/run time. NEXT_PUBLIC_ prefix is what
// makes it available in the browser bundle.
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type HealthDb = {
  status: string;
  postgres_version: string;
  pgvector_installed: boolean;
  pgvector_version: string | null;
};

export default function Home() {
  const [data, setData] = useState<HealthDb | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // This fetch is the "hello world": browser -> FastAPI -> Postgres -> back.
    fetch(`${API_URL}/health/db`)
      .then((res) => {
        if (!res.ok) throw new Error(`API responded ${res.status}`);
        return res.json();
      })
      .then(setData)
      .catch((err) => setError(err.message));
  }, []);

  return (
    <main className="min-h-screen flex items-center justify-center bg-gray-950 text-gray-100 p-8">
      <div className="w-full max-w-lg space-y-4">
        <h1 className="text-2xl font-semibold">ProdRAG · stack check</h1>
        <p className="text-sm text-gray-400">
          Live response from FastAPI <code>/health/db</code> (which queried
          Postgres + pgvector):
        </p>

        {error && (
          <div className="rounded-lg border border-red-800 bg-red-950/50 p-4 text-red-300">
            Failed to reach the API: {error}
          </div>
        )}

        {!error && !data && (
          <div className="rounded-lg border border-gray-800 bg-gray-900 p-4 text-gray-400">
            Loading…
          </div>
        )}

        {data && (
          <pre className="rounded-lg border border-gray-800 bg-gray-900 p-4 text-sm overflow-x-auto">
            {JSON.stringify(data, null, 2)}
          </pre>
        )}
      </div>
    </main>
  );
}
