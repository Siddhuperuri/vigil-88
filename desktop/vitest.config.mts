import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
    restoreMocks: true,
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
      // The Electron entry points are exercised by the Playwright run, which launches the real
      // app; what a unit test can honestly cover is the logic that decides things.
      exclude: ["src/main.ts", "src/preload.ts", "src/**/*.test.ts"],
      thresholds: { lines: 90, functions: 90, branches: 85, statements: 90 },
    },
  },
});
