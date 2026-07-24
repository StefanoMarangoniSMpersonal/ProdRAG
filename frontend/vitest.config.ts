import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Vitest config for the frontend component tests. Three things matter here:
//  - the React plugin gives us JSX transform + Fast Refresh parity with Next's compiler;
//  - `environment: "jsdom"` supplies a DOM so React Testing Library can mount and query;
//  - `setupFiles` runs once before each test file to install the jest-dom matchers.
// The `@/*` alias is re-declared because Vitest resolves modules itself and doesn't read
// tsconfig `paths` — it must match the alias in tsconfig.json (`@/* -> ./*`).
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    // Only our own tests — never node_modules or the Next build output.
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["node_modules", ".next"],
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./", import.meta.url)),
    },
  },
});
