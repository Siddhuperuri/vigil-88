import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  type ElectronApplication,
  type Page,
  _electron as electron,
  expect,
  test,
} from "@playwright/test";
import { killPids, pidsMatching } from "./processes";

const DESKTOP = path.resolve(__dirname, "..");
const PASSPHRASE = "correct horse battery staple";

interface Launched {
  app: ElectronApplication;
  page: Page;
  marker: string;
  stdout: string[];
}

interface AxeResults {
  violations: { id: string; help: string }[];
}

async function launch(): Promise<Launched> {
  const root = mkdtempSync(path.join(tmpdir(), "vigil-e2e-"));
  const config = path.join(root, "config");
  mkdirSync(path.join(config, "cameras"), { recursive: true });
  writeFileSync(
    path.join(config, "vigil.yaml"),
    "version: 1\npaths:\n  data_dir: ./var\ncameras:\n  - $include: cameras/*.yaml\nlogging:\n  level: ERROR\n",
  );
  const app = await electron.launch({
    args: [DESKTOP, `--user-data-dir=${path.join(root, "user")}`],
    env: { ...process.env, VIGIL_ENGINE_CONFIG_DIR: config, ELECTRON_ENABLE_LOGGING: "0" },
    timeout: 120_000,
  });
  const stdout: string[] = [];
  app.process().stdout?.on("data", (chunk: Buffer) => stdout.push(chunk.toString("utf8")));
  const page = await app.firstWindow();
  return { app, page, marker: config, stdout };
}

/** A short moving clip, made with the engine's own OpenCV so the test needs no fixture file. */
function makeClip(dir: string): string {
  const python = path.resolve(DESKTOP, "..", ".venv", "Scripts", "python.exe");
  if (!existsSync(python)) throw new Error(`the engine's Python is not at ${python}`);
  const clip = path.join(dir, "clip.avi");
  execFileSync(python, [
    "-c",
    [
      "import cv2, numpy as np, sys",
      "w = cv2.VideoWriter(sys.argv[1], cv2.VideoWriter_fourcc(*'MJPG'), 10, (320, 240))",
      "for i in range(60):",
      "    f = np.zeros((240, 320, 3), np.uint8)",
      "    cv2.rectangle(f, (10 + i * 4, 80), (60 + i * 4, 180), (200, 200, 200), -1)",
      "    w.write(f)",
      "w.release()",
    ].join("\n"),
    clip,
  ]);
  return clip;
}

async function createAdministrator(page: Page): Promise<void> {
  await expect(setupHeading(page)).toBeVisible();
  await page.getByLabel("Administrator username").fill("alice");
  await page.getByLabel("Password", { exact: true }).fill(PASSPHRASE);
  await page.getByLabel("Repeat the password").fill(PASSPHRASE);
  await page.getByRole("button", { name: "Create administrator" }).click();
  await page.getByRole("checkbox", { name: /stored these codes/ }).check();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Live wall" })).toBeVisible();
}

const setupHeading = (page: Page) => page.getByRole("heading", { name: "Set up VIGIL-88" });

/**
 * WCAG 2.2 AA in the real window. @axe-core/playwright opens a second page, which Electron does not
 * support, so axe is injected directly. It is the same Chromium either way, and colour contrast,
 * which jsdom cannot compute, is checked here.
 */
async function violations(page: Page): Promise<string[]> {
  await page.evaluate(readFileSync(require.resolve("axe-core/axe.min.js"), "utf8"));
  const results = await page.evaluate(async () => {
    const win = globalThis as unknown as {
      document: unknown;
      axe: { run(context: unknown, options: object): Promise<AxeResults> };
    };
    return win.axe.run(win.document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] },
    });
  });
  return results.violations.map((v) => `${v.id}: ${v.help}`);
}

test.describe("the real app", () => {
  let session: Launched;
  test.beforeEach(async () => {
    session = await launch();
  });
  test.afterEach(async () => {
    await session.app.close().catch(() => undefined);
  });

  test("starts the engine and shows first-run setup, with the token already in hand", async () => {
    await expect(setupHeading(session.page)).toBeVisible();
    await expect(session.page).toHaveTitle("VIGIL-88");
    expect(session.page.url()).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/app\//);
    // The shell handed the token over, so a person is not asked to find and paste one.
    await expect(session.page.getByLabel("Setup token")).toHaveCount(0);
  });

  test("has no WCAG 2.2 AA violations on the setup screen, colour contrast included", async () => {
    await expect(setupHeading(session.page)).toBeVisible();
    expect(await violations(session.page)).toEqual([]);
  });

  test("gives the page a narrow bridge and no Node", async () => {
    await expect(setupHeading(session.page)).toBeVisible();
    const exposure = await session.page.evaluate(() => ({
      bridge: Object.keys((window as unknown as { vigilShell: object }).vigilShell).sort(),
      require: typeof (window as unknown as { require?: unknown }).require,
      process: typeof (window as unknown as { process?: unknown }).process,
    }));
    expect(exposure).toEqual({
      bridge: ["engineState", "notify", "restartEngine", "setupToken"],
      require: "undefined",
      process: "undefined",
    });
  });

  test("the setup token is single-use: the page took it, so asking again gets nothing", async () => {
    await expect(setupHeading(session.page)).toBeVisible();
    const again = await session.page.evaluate(() =>
      (
        window as unknown as { vigilShell: { setupToken(): Promise<string | null> } }
      ).vigilShell.setupToken(),
    );
    expect(again).toBeNull();
  });

  test("first run end to end: create the administrator, save the codes, use every view, lock, sign out, sign in", async () => {
    const page = session.page;
    await expect(setupHeading(page)).toBeVisible();

    await page.getByLabel("Administrator username").fill("alice");
    await page.getByLabel("Password", { exact: true }).fill(PASSPHRASE);
    await page.getByLabel("Repeat the password").fill(PASSPHRASE);
    await page.getByRole("button", { name: "Create administrator" }).click();

    // The recovery codes appear once, and the way on is blocked until they are confirmed stored.
    await expect(page.getByRole("heading", { name: "Save your recovery codes" })).toBeVisible();
    await expect(
      page.getByRole("list", { name: "Recovery codes" }).getByRole("listitem"),
    ).toHaveCount(10);
    expect(await violations(page)).toEqual([]);
    await expect(page.getByRole("button", { name: "Continue" })).toBeDisabled();
    await page.getByRole("checkbox", { name: /stored these codes/ }).check();
    await page.getByRole("button", { name: "Continue" }).click();

    // Signed in, on the wall, with no camera configured said plainly, and the strip live.
    await expect(page.getByRole("heading", { name: "Live wall" })).toBeVisible();
    await expect(page.getByText("No cameras yet")).toBeVisible();
    await expect(page.getByRole("banner")).toContainText("LIVE", { timeout: 30_000 });
    expect(await violations(page)).toEqual([]);

    await page.getByRole("link", { name: "Incident desk" }).click();
    await expect(page.getByText("No open incidents.")).toBeVisible();
    expect(await violations(page)).toEqual([]);

    // The administrator screens: people (the account just made, marked as me), the audit log with
    // its chain intact, and reports that say so when there is nothing to report.
    await page.getByRole("link", { name: "People" }).click();
    await expect(page.getByRole("rowheader", { name: /alice/ })).toContainText("(you)");
    await expect(page.getByLabel("Role for alice")).toBeDisabled();
    expect(await violations(page)).toEqual([]);

    await page.getByRole("link", { name: "Audit log" }).click();
    await expect(
      page.getByText("setup.completed").or(page.getByText("user.created")),
    ).toBeVisible();
    await page.getByRole("button", { name: "Verify the chain" }).click();
    await expect(page.getByText(/Intact: \d+ rows, none altered\./)).toBeVisible();
    expect(await violations(page)).toEqual([]);

    await page.getByRole("link", { name: "Reports" }).click();
    await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();
    await expect(page.getByRole("term").filter({ hasText: "Incidents" })).toBeVisible();
    await expect(page.getByRole("button", { name: "CSV" })).toBeEnabled();
    expect(await violations(page)).toEqual([]);

    await page.getByRole("link", { name: "System" }).click();
    await expect(page.getByRole("heading", { name: "System" })).toBeVisible();
    await expect(page.getByText("No cameras are configured.")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Storage" })).toBeVisible();
    await expect(
      page
        .getByLabel("Recent log records")
        .or(page.getByText("There is no log file yet."))
        .or(page.getByText("No log records at this level yet.")),
    ).toBeVisible();
    expect(await violations(page)).toEqual([]);

    // The keyboard sheet opens with ? from anywhere outside a field.
    await page.getByRole("link", { name: "Incident desk" }).click();
    // The shortcut is registered by the desk itself, so wait for the desk before pressing the key.
    await expect(page.getByRole("heading", { name: "Incident desk" })).toBeVisible();
    await page.keyboard.press("?");
    await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);

    // The command palette: Ctrl+K, type, Enter goes there; Escape closes it without doing anything.
    await page.keyboard.press("Control+K");
    await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
    expect(await violations(page)).toEqual([]);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.keyboard.press("Control+K");
    await page.keyboard.type("syst");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: "System" })).toBeVisible();
    await page.getByRole("link", { name: "Incident desk" }).click();
    await expect(page.getByRole("heading", { name: "Incident desk" })).toBeVisible();

    // Lock: the console is gated, monitoring is not. The wrong password is refused in place.
    await page.getByRole("button", { name: "Lock console" }).click();
    await expect(page.getByRole("heading", { name: "Console locked" })).toBeVisible();
    expect(await violations(page)).toEqual([]);
    await page.getByLabel("Password").fill("not the password at all");
    await page.getByRole("button", { name: "Unlock" }).click();
    await expect(page.getByRole("alert")).toContainText("does not match");
    await page.getByLabel("Password").fill(PASSPHRASE);
    await page.getByRole("button", { name: "Unlock" }).click();
    await expect(page.getByRole("link", { name: "Incident desk" })).toBeVisible();

    // Sign out, and the engine really ended the session: the sign-in screen is back.
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page.getByRole("heading", { name: "Sign in to VIGIL-88" })).toBeVisible();
    expect(await violations(page)).toEqual([]);

    // A wrong password says the generic thing; the right one gets back in.
    await page.getByLabel("Username").fill("alice");
    await page.getByLabel("Password").fill("definitely wrong passphrase");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert")).toContainText("That username and password do not match.");
    await page.getByLabel("Password").fill(PASSPHRASE);
    await page.getByRole("button", { name: "Sign in" }).click();
    // The view the person was on is still the view they land on.
    await expect(page.getByRole("heading", { name: "Incident desk" })).toBeVisible();
  });

  test("restarts the engine when it is killed, and tells the person what happened", async () => {
    await expect(setupHeading(session.page)).toBeVisible();
    const before = pidsMatching(session.marker);
    expect(before.length).toBeGreaterThan(0);
    killPids(before);
    await expect(session.page.getByRole("alert")).toContainText("The engine stopped", {
      timeout: 30_000,
    });
    await expect(session.page.getByRole("heading", { name: "Set up VIGIL-88" })).toBeVisible({
      timeout: 90_000,
    });
    const after = pidsMatching(session.marker);
    expect(after.length).toBeGreaterThan(0);
    expect(after.some((pid) => before.includes(pid))).toBe(false); // a new process, not the old one
  });

  test("closing the window hides it to the tray and monitoring continues; quitting stops the engine", async () => {
    await expect(setupHeading(session.page)).toBeVisible();
    await session.app.evaluate(({ BrowserWindow }) => {
      BrowserWindow.getAllWindows()[0]?.close();
    });
    const visible = await session.app.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows()[0]?.isVisible(),
    );
    expect(visible).toBe(false);
    expect(pidsMatching(session.marker).length).toBeGreaterThan(0); // the engine is still watching
    expect(session.stdout.join("")).toContain('"shell":"window-hidden"');

    await session.app.evaluate(({ app }) => {
      app.quit();
    });
    const deadline = Date.now() + 20_000;
    while (pidsMatching(session.marker).length > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    expect(pidsMatching(session.marker)).toEqual([]);
  });

  test("quitting stops the engine, so no process is left holding the camera", async () => {
    await expect(setupHeading(session.page)).toBeVisible();
    expect(pidsMatching(session.marker).length).toBeGreaterThan(0);
    await session.app.close();
    const deadline = Date.now() + 20_000;
    while (pidsMatching(session.marker).length > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    expect(pidsMatching(session.marker)).toEqual([]);
    expect(session.stdout.join("")).toContain('"shell":"engine-stopped"');
  });

  test("adds a camera from the console, applies it by restart without signing out, and it comes up online", async () => {
    const page = session.page;
    const clip = makeClip(path.dirname(session.marker));
    await createAdministrator(page);

    await page.getByRole("link", { name: "Cameras" }).click();
    await expect(page.getByText("No cameras yet")).toBeVisible();
    await page.getByRole("link", { name: "Add a camera" }).click();

    // A bad name is refused in place, before anything is sent.
    await page.getByLabel("Camera name").fill("Bad Name");
    await page.getByRole("button", { name: "Save camera" }).click();
    await expect(page.getByText(/Use lower-case letters/)).toBeVisible();

    await page.getByLabel("Camera name").fill("clip");
    await page.getByLabel("Kind of source").selectOption("video_file");
    await page.getByLabel("Video file path").fill(clip);
    await page.getByRole("button", { name: "Test connection" }).click();
    await expect(page.getByText(/320x240/)).toBeVisible({ timeout: 30_000 });
    expect(await violations(page)).toEqual([]);
    await page.getByRole("button", { name: "Save camera" }).click();

    // Saved, listed, and honest that it is not running yet.
    await expect(page.getByRole("rowheader", { name: "clip" })).toBeVisible();
    await expect(page.getByText("Saved. Starts when monitoring restarts.")).toBeVisible();
    await expect(page.getByText("1 saved change is waiting")).toBeVisible();
    expect(await violations(page)).toEqual([]);

    // The cost is stated, and declining changes nothing.
    await page.getByRole("button", { name: /Apply and restart/ }).click();
    const dialog = page.getByRole("dialog", { name: "Restart monitoring?" });
    await expect(dialog).toContainText("Anything that happens in that time is not seen");
    await dialog.getByRole("button", { name: "Not now" }).click();
    await expect(page.getByText("1 saved change is waiting")).toBeVisible();

    const pidsBefore = pidsMatching(session.marker);
    await page.getByRole("button", { name: /Apply and restart/ }).click();
    const started = Date.now();
    await page.getByRole("dialog").getByRole("button", { name: "Restart now" }).click();
    await expect(page.getByText(/Restarting monitoring…/)).toBeVisible();

    // Back, with the camera running, nothing pending, and still signed in.
    await expect(page.getByText(/saved change/)).toHaveCount(0, { timeout: 90_000 });
    await expect(page.getByRole("row", { name: /clip/ })).toContainText("ONLINE", {
      timeout: 90_000,
    });
    const restartMs = Date.now() - started;
    test.info().annotations.push({ type: "restart-ms", description: String(restartMs) });
    console.log(`apply-by-restart took ${String(restartMs)} ms (click to camera ONLINE)`);
    expect(pidsMatching(session.marker).some((pid) => pidsBefore.includes(pid))).toBe(false);
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
    expect(page.url()).toContain("#/cameras"); // back on the screen the change was applied from

    // Zones: draw one on the running camera, save, and the keyboard moves a point.
    await page
      .getByRole("navigation", { name: "Main" })
      .getByRole("link", { name: "Zones" })
      .click();
    await page
      .getByRole("navigation", { name: "Cameras" })
      .getByRole("link", { name: "clip" })
      .click();
    await page.getByRole("button", { name: "Add a zone" }).click();
    const point = page.getByRole("button", { name: /Point 1 of zone-1/ });
    await point.focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByLabel("Point 1 across")).toHaveValue("0.31");
    expect(await violations(page)).toEqual([]);
    await page.getByRole("button", { name: "Save zones" }).click();
    await expect(
      page.getByText("Saved zones are waiting for monitoring to restart."),
    ).toBeVisible();
    await expect(page.getByText(/2 saved changes are waiting/)).toBeVisible();
  });
});
