import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The engine serves the built console under /app/ (same origin as the API, so there is no CORS to
// get wrong). In development Vite proxies /api to a running engine instead.
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    proxy: { "/api": { target: process.env["VIGIL_ENGINE_URL"] ?? "http://127.0.0.1:8088" } },
  },
  build: { outDir: "dist", sourcemap: true },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test-setup.ts"],
    css: false,
    restoreMocks: true,
    coverage: {
      provider: "v8",
      include: ["src/**"],
      exclude: [
        "src/**/*.gen.ts",
        "src/**/*.json",
        "src/main.tsx",
        "src/test-*.ts",
        "src/**/*.test.*",
      ],
      // The renderer holds no business logic, so the floor is about the code that exists, not a
      // target to pad towards.
      thresholds: { lines: 90, functions: 90, branches: 85, statements: 90 },
    },
  },
});
