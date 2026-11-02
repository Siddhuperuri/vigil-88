import { defineConfig } from "@playwright/test";

// These tests launch the real Electron app, which launches the real engine. One at a time: they
// share a camera-less engine each, but a second Electron would still contend for the machine.
export default defineConfig({
  testDir: "e2e",
  timeout: 180_000,
  expect: { timeout: 60_000 },
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  use: { trace: "retain-on-failure" },
});
