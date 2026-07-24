// Runs once before each test file (wired via vitest.config.ts `setupFiles`).
// - jest-dom extends `expect` with DOM matchers (toBeInTheDocument, toHaveTextContent, …).
// - afterEach cleanup unmounts anything RTL rendered so tests don't leak DOM into each other.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});

// jsdom doesn't implement scrollIntoView, and the chat page calls it in an effect on every
// turn change. Optional chaining only guards a null ref, not a missing method, so provide a
// no-op to keep that effect from throwing under test.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}
