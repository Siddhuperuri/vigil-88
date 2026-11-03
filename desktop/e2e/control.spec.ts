import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  type ElectronApplication,
  type Page,
  _electron as electron,
  expect,
  test,
} from "@playwright/test";

const DESKTOP = path.resolve(__dirname, "..");
const CONFIG = "version: 1\npaths:\n  data_dir: ./var\ncameras:\n  - $include: cameras/*.yaml\n";
const CAMERA = "camera_id: bench\nname: Bench\nsource:\n  kind: synthetic\n  fps: 15\n";

/**
 * The controls added after the first cut, driven through the real application against the real engine:
 * alert settings, worker restart, diagnostics, per-camera thresholds, crossing lines and applying zone
 * edits without a restart. Each is a screen talking to an endpoint, and a screen and an endpoint that
 * both pass their own tests can still disagree; this is where they meet.
 */
interface Run {
  app: ElectronApplication;
  page: Page;
  config: string;
}

async function launch(): Promise<Run> {
  const root = mkdtempSync(path.join(tmpdir(), "vigil-control-"));
  const config = path.join(root, "config");
  mkdirSync(path.join(config, "cameras"), { recursive: true });
  writeFileSync(path.join(config, "vigil.yaml"), CONFIG);
  writeFileSync(path.join(config, "cameras", "bench.yaml"), CAMERA);
  const app = await electron.launch({
    args: [DESKTOP, `--user-data-dir=${path.join(root, "user")}`],
    env: {
      ...process.env,
      VIGIL_ENGINE_CONFIG_DIR: config,
      ELECTRON_ENABLE_LOGGING: "0",
      ...(process.env["VIGIL_E2E_ENGINE_EXE"] === undefined
        ? {}
        : { VIGIL_ENGINE_EXE: process.env["VIGIL_E2E_ENGINE_EXE"] }),
    },
    timeout: 120_000,
  });
  const page = await app.firstWindow();
  await expect(page.getByRole("heading", { name: "Set up VIGIL-88" })).toBeVisible();
  await page.getByLabel("Administrator username").fill("alice");
  await page.getByLabel("Password", { exact: true }).fill("correct horse battery staple");
  await page.getByLabel("Repeat the password").fill("correct horse battery staple");
  await page.getByRole("button", { name: "Create administrator" }).click();
  await page.getByRole("checkbox", { name: /stored these codes/ }).check();
  await page.getByRole("button", { name: "Continue" }).click();
  return { app, page, config };
}

const go = (page: Page, name: string) =>
  page.getByRole("navigation", { name: "Main" }).getByRole("link", { name }).click();

test.describe("the controls, in the real app", () => {
  let run: Run | undefined;
  test.afterEach(async () => {
    await run?.app.close().catch(() => undefined);
    run = undefined;
  });

  test("alert settings save and take effect at once, and a refused policy is explained", async () => {
    run = await launch();
    const { page, config } = run;
    await go(page, "Settings");
    await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Add a rule" }).click();
    await page.getByRole("button", { name: "Add quiet hours" }).click();
    await page.getByRole("button", { name: "Save alert settings" }).click();
    await expect(page.getByText("Saved and in force now.")).toBeVisible();
    const written = readFileSync(path.join(config, "vigil.local.yaml"), "utf8");
    expect(written).toContain("Rule 1");
    expect(written).toContain("Quiet 1");
    // No restart was asked for: alert settings are live.
    await expect(page.getByText(/saved changes? (is|are) waiting/)).toHaveCount(0);

    // Two rules with the same name are refused by the engine, in words, with the edits kept.
    await page.getByRole("button", { name: "Add a rule" }).click();
    await page.getByLabel("Name").nth(1).fill("Rule 1");
    await page.getByRole("button", { name: "Save alert settings" }).click();
    await expect(page.getByRole("alert")).toContainText("not valid");
    await expect(page.getByRole("alert")).toContainText("rule names must be unique");
  });

  test("a worker can be restarted from System, after asking, and the engine says what it did", async () => {
    run = await launch();
    const { page } = run;
    await go(page, "System");
    const restart = page.getByRole("button", { name: "Restart sampler" });
    await expect(restart).toBeVisible();
    await expect(page.getByRole("button", { name: "Restart inference" })).toHaveCount(0);
    await restart.click();
    await page.getByRole("dialog").getByRole("button", { name: "Restart", exact: true }).click();
    await expect(page.getByText(/Restarted sampler: restarted/)).toBeVisible();
  });

  test("the diagnostics bundle is a zip, and the screen says nothing was sent anywhere", async () => {
    run = await launch();
    const { page } = run;
    await go(page, "System");
    // Electron shows its own save dialog for a download, which Playwright cannot drive, so the bytes
    // are fetched the way the page fetches them and the screen's own status is checked as well.
    const signature = await page.evaluate(async () => {
      const r = await fetch("/api/v1/system/diagnostics", { headers: { "X-Vigil-Request": "1" } });
      const bytes = new Uint8Array(await r.arrayBuffer());
      return {
        ok: r.ok,
        type: r.headers.get("content-type"),
        magic: String.fromCharCode(bytes[0] ?? 0, bytes[1] ?? 0),
        text: new TextDecoder("latin1").decode(bytes),
      };
    });
    expect(signature).toMatchObject({ ok: true, type: "application/zip", magic: "PK" });
    expect(signature.text).not.toContain(run.config); // no absolute path of this machine's data
    await expect(
      page.getByText(/no video, no snapshots, no evidence, no accounts or passwords/),
    ).toBeVisible();
  });

  test("a threshold is validated by the module, saved on the camera, and reported as waiting for a restart", async () => {
    run = await launch();
    const { page, config } = run;
    await page.goto(page.url().replace(/#.*$/, "#/tuning/bench"));
    await expect(page.getByRole("heading", { name: "intrusion" })).toBeVisible();
    // Several modules have a base signal; the form is named for its module.
    const card = page.getByRole("form", { name: "intrusion", exact: true });
    const box = card.getByLabel("base signal");
    await box.fill("0.99"); // valid alone, refused with the bonuses
    await card.getByRole("button", { name: "Save intrusion thresholds" }).click();
    await expect(page.getByRole("alert")).toContainText("not valid");
    await box.fill("0.72");
    await card.getByRole("button", { name: "Save intrusion thresholds" }).click();
    await expect(page.getByText(/still uses the old values/).first()).toBeVisible();
    expect(readFileSync(path.join(config, "cameras", "bench.yaml"), "utf8")).toContain("0.72");
  });

  test("a crossing line is saved with the zones, and zone edits apply without a restart", async () => {
    run = await launch();
    const { page } = run;
    await page.goto(page.url().replace(/#.*$/, "#/zones/bench"));
    await page.getByRole("button", { name: "Add a zone" }).click();
    await page.getByRole("button", { name: "Add a crossing line" }).click();
    await page.getByRole("button", { name: "Save zones" }).click();
    await expect(page.getByText(/zones bench added/)).toBeVisible();
    // The camera had no zones, so this edit adds a capability: the engine must say it needs a restart.
    await page.getByRole("button", { name: "Apply zone changes now" }).click();
    await expect(page.getByRole("alert")).toContainText("no running camera uses this zone file yet");
  });

  test("the setup guide is reachable and ends with an honest summary", async () => {
    run = await launch();
    const { page } = run;
    await go(page, "Settings");
    await page.getByRole("link", { name: "Run the setup guide again" }).click();
    await expect(page.getByRole("heading", { name: "Setup guide" })).toBeVisible();
    for (let i = 0; i < 4; i++) await page.getByRole("button", { name: /^Next/ }).click();
    await expect(page.getByRole("heading", { name: "Finish" })).toBeVisible();
  });
});
