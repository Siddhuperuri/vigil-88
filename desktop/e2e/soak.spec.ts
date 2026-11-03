import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { _electron as electron, expect, test } from "@playwright/test";

/**
 * The whole application under time: the real Electron shell, its renderer, the console and the engine,
 * driven the way a person uses it, while `scripts/verify/proc_sampler.py` measures every process.
 *
 * Skipped unless VIGIL_SOAK_MINUTES is set, because it runs for as long as it is told to. Run it
 * through `scripts/verify/soak-app.ps1`, which starts the sampler and reads both back.
 *
 *   VIGIL_SOAK_MINUTES   how long to run (the plan asks for 480)
 *   VIGIL_SOAK_DIR       where to write app.pid and journey.json (the sampler reads app.pid)
 *   VIGIL_E2E_PACKAGED   optional: an installed VIGIL-88.exe to soak instead of the source tree
 *
 * Nothing here weakens the verdict: a view that does not appear, an engine that stops answering or
 * a console error each fail the run, and the figures are written whether it passes or not.
 */
const MINUTES = Number(process.env["VIGIL_SOAK_MINUTES"] ?? "0");
const DIR = process.env["VIGIL_SOAK_DIR"] ?? path.join(tmpdir(), "vigil-soak");
const PACKAGED = process.env["VIGIL_E2E_PACKAGED"];
const DESKTOP = path.resolve(__dirname, "..");
const ROUND_MS = 60_000;
const VIEWS = [
  "Live wall",
  "Incident desk",
  "Cameras",
  "Zones",
  "Activity",
  "Reports",
  "People",
  "Audit log",
  "Settings",
  "System",
] as const;
const CAMERA =
  "camera_id: soak%N%\nsource:\n  kind: synthetic\n  fps: 15\ntarget_inference_fps: 5\n";
const CONFIG = "version: 1\npaths:\n  data_dir: ./var\ncameras:\n  - $include: cameras/*.yaml\n";

interface Round {
  round: number;
  at_s: number;
  views_ok: number;
  views_failed: string[];
  slowest_view_ms: number;
  health_level: string | null;
  health_ms: number | null;
  live: boolean;
}

test.describe("whole-application soak", () => {
  test.skip(MINUTES <= 0, "set VIGIL_SOAK_MINUTES to run the application soak");
  test.setTimeout((MINUTES + 10) * 60_000);

  test("the shell, the console and the engine stay responsive and bounded", async () => {
    mkdirSync(DIR, { recursive: true });
    const root = mkdtempSync(path.join(tmpdir(), "vigil-soak-app-"));
    const config = path.join(root, "config");
    mkdirSync(path.join(config, "cameras"), { recursive: true });
    writeFileSync(path.join(config, "vigil.yaml"), CONFIG);
    for (const n of [0, 1]) {
      writeFileSync(
        path.join(config, "cameras", `soak${String(n)}.yaml`),
        CAMERA.replace("%N%", String(n)),
      );
    }

    const app = await electron.launch({
      ...(PACKAGED === undefined ? { args: [DESKTOP] } : { executablePath: PACKAGED }),
      env: { ...process.env, VIGIL_ENGINE_CONFIG_DIR: config, ELECTRON_ENABLE_LOGGING: "0" },
      timeout: 120_000,
    });
    const rounds: Round[] = [];
    const consoleErrors: string[] = [];
    try {
      const page = await app.firstWindow();
      page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));
      page.on("console", (m) => {
        if (m.type() === "error") consoleErrors.push(m.text());
      });
      // The sampler follows this pid and its children: Electron's main, renderer, GPU and utility
      // processes, and the engine it supervises.
      const pid = await app.evaluate(() => process.pid);
      writeFileSync(path.join(DIR, "app.pid"), String(pid));

      await expect(page.getByRole("heading", { name: "Set up VIGIL-88" })).toBeVisible();
      await page.getByLabel("Administrator username").fill("soak");
      await page.getByLabel("Password", { exact: true }).fill("correct horse battery staple");
      await page.getByLabel("Repeat the password").fill("correct horse battery staple");
      await page.getByRole("button", { name: "Create administrator" }).click();
      await page.getByRole("checkbox", { name: /stored these codes/ }).check();
      await page.getByRole("button", { name: "Continue" }).click();

      const nav = page.getByRole("navigation", { name: "Main" });
      const began = Date.now();
      let round = 0;
      while (Date.now() - began < MINUTES * 60_000) {
        round += 1;
        const failed: string[] = [];
        let slowest = 0;
        for (const view of VIEWS) {
          const started = Date.now();
          try {
            await nav.getByRole("link", { name: view }).click();
            await expect(page.getByRole("main").getByRole("heading", { level: 1 })).toBeVisible({
              timeout: 15_000,
            });
          } catch {
            failed.push(view);
          }
          slowest = Math.max(slowest, Date.now() - started);
        }
        const probe = Date.now();
        const health = await page
          .evaluate(async () => {
            const r = await fetch("/api/v1/system/health", {
              headers: { "X-Vigil-Request": "1" },
            });
            return r.ok ? ((await r.json()) as { level: string }).level : null;
          })
          .catch(() => null);
        rounds.push({
          round,
          at_s: Math.round((Date.now() - began) / 1000),
          views_ok: VIEWS.length - failed.length,
          views_failed: failed,
          slowest_view_ms: slowest,
          health_level: health,
          health_ms: health === null ? null : Date.now() - probe,
          live: await page
            .getByRole("banner")
            .getByText("LIVE")
            .first()
            .isVisible()
            .catch(() => false),
        });
        // One round a minute: the point is time passing under use, not hammering the console.
        const spent = Date.now() - began - (round - 1) * ROUND_MS;
        await page.waitForTimeout(Math.max(0, ROUND_MS - spent));
      }
    } finally {
      writeFileSync(
        path.join(DIR, "journey.json"),
        JSON.stringify(
          { minutes: MINUTES, rounds, console_errors: consoleErrors.slice(0, 50) },
          null,
          2,
        ),
      );
      await app.close().catch(() => undefined);
    }

    expect(rounds.length).toBeGreaterThan(0);
    expect(rounds.flatMap((r) => r.views_failed)).toEqual([]);
    expect(rounds.every((r) => r.health_level !== null && r.health_level !== "failed")).toBe(true);
    expect(rounds.every((r) => r.live)).toBe(true);
    expect(consoleErrors).toEqual([]);
  });
});
