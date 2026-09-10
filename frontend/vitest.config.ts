import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

// A test run must never inherit a global NODE_ENV=production: react-dom then
// resolves to its production build, where `act` does not exist, and every
// @testing-library render dies with "React.act is not a function". Some
// shells/tooling export NODE_ENV=production globally, so normalize it here —
// the config module runs before Vitest forks its workers.
process.env.NODE_ENV = "test";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/tests/setup.ts"],
    include: ["src/**/__tests__/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html", "lcov"],
      include: ["src/lib/**", "src/stores/**"],
      exclude: ["src/**/__tests__/**", "src/tests/**"],
    },
    restoreMocks: true,
  },
});
