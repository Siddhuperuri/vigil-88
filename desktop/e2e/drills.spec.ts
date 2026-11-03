import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  type ElectronApplication,
  type Page,
  _electron as electron,
  expect,
  test,
} from "@playwright/test";
import { pidsMatching } from "./processes";

const DESKTOP = path.resolve(__dirname, "..");
const GOOD = "version: 1\npaths:\n  data_dir: ./var\ncameras:\n  - $include: cameras/*.yaml\n";

interface Run {
  app: ElectronApplication;
  page: Page;
  config: string;
}

/** Failure drills (plan D4): something goes wrong on purpose, and the app must say so and survive. */
async function launch(
  yaml: string,
  options: { env?: Record<string, string>; cameras?: Record<string, string> } = {},
): Promise<Run> {
  const root = mkdtempSync(path.join(tmpdir(), "vigil-drill-"));
  const config = path.join(root, "config");
  mkdirSync(path.join(config, "cameras"), { recursive: true });
  writeFileSync(path.join(config, "vigil.yaml"), yaml);
  for (const [name, body] of Object.entries(options.cameras ?? {})) {
    writeFileSync(path.join(config, "cameras", name), body);
  }
  const app = await electron.launch({
    args: [DESKTOP, `--user-data-dir=${path.join(root, "user")}`],
    env: {
      ...process.env,
      VIGIL_ENGINE_CONFIG_DIR: config,
      ELECTRON_ENABLE_LOGGING: "0",
      ...(process.env["VIGIL_E2E_ENGINE_EXE"] === undefined
        ? {}
        : { VIGIL_ENGINE_EXE: process.env["VIGIL_E2E_ENGINE_EXE"] }),
      ...(options.env ?? {}),
    },
    timeout: 120_000,
  });
  return { app, page: await app.firstWindow(), config };
}

test.describe("failure drills", () => {
  let run: Run | undefined;
  test.afterEach(async () => {
    await run?.app.close().catch(() => undefined);
    run = undefined;
  });

  test("a corrupted configuration is named on screen with the line that is wrong, and mending the file recovers without a restart of the app", async () => {
    run = await launch("version: 1\npaths: [unclosed\n");
    const { page, config } = run;
    // Named, with the parser's own words, instead of a blank window or a generic failure.
    await expect(page.getByRole("alert")).toContainText("The engine stopped", { timeout: 60_000 });
    await expect(page.getByRole("alert")).toContainText("cannot read configuration file");
    await expect(page.getByRole("alert")).toContainText("paths: [unclosed");
    // No engine is left running half-configured.
    expect(pidsMatching(config)).toEqual([]);

    // Mend the file: the supervisor's own retry brings the engine up. Nobody restarts the app.
    writeFileSync(path.join(config, "vigil.yaml"), GOOD);
    await expect(page.getByRole("heading", { name: "Set up VIGIL-88" })).toBeVisible({
      timeout: 90_000,
    });
  });

  test("a missing model stops the engine with the command that fixes it, and does not pretend to detect", async () => {
    run = await launch(GOOD, { env: { VIGIL_DETECTOR: "yolox_s" } });
    const { page, config } = run;
    await expect(page.getByRole("alert")).toContainText("The engine stopped", { timeout: 60_000 });
    await expect(page.getByRole("alert")).toContainText("model file yolox_s.onnx not found");
    await expect(page.getByRole("alert")).toContainText("vigil models fetch yolox_s");
    expect(pidsMatching(config)).toEqual([]);
  });

  test("a camera that cannot be reached is shown as not working, the engine stays up, and quitting is clean", async () => {
    run = await launch(GOOD, {
      cameras: {
        "dead.yaml":
          "camera_id: dead\nsource:\n  kind: rtsp\n  url: rtsp://127.0.0.1:9/nothing\npriority: 5\n",
      },
    });
    const { page, app, config } = run;
    await expect(page.getByRole("heading", { name: "Set up VIGIL-88" })).toBeVisible();
    await page.getByLabel("Administrator username").fill("alice");
    await page.getByLabel("Password", { exact: true }).fill("correct horse battery staple");
    await page.getByLabel("Repeat the password").fill("correct horse battery staple");
    await page.getByRole("button", { name: "Create administrator" }).click();
    await page.getByRole("checkbox", { name: /stored these codes/ }).check();
    await page.getByRole("button", { name: "Continue" }).click();
    await page
      .getByRole("navigation", { name: "Main" })
      .getByRole("link", { name: "System" })
      .click();

    // The engine is up, the camera is named with a state in words, and nothing says ONLINE.
    const row = page.getByRole("row", { name: /^dead / });
    await expect(row).toBeVisible({ timeout: 60_000 });
    await expect(row).not.toContainText("ONLINE");
    await expect(row).toContainText(/RECONNECTING|OFFLINE|FAILED|STARTING/);
    await expect(page.getByRole("banner")).toContainText("LIVE");
    expect(pidsMatching(config).length).toBeGreaterThan(0);

    // Quitting does not hang on the camera that never answered (its open timeout is under the budget).
    const started = Date.now();
    await app.close();
    run = undefined;
    const deadline = Date.now() + 20_000;
    while (pidsMatching(config).length > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    expect(pidsMatching(config)).toEqual([]);
    console.log(`quit with an unreachable camera took ${String(Date.now() - started)} ms`);
  });
});
