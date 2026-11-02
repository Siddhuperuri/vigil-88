import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { readFileSync } from "node:fs";
import {
  type ElectronApplication,
  type Page,
  _electron as electron,
  expect,
  test,
} from "@playwright/test";
import { killPids, pidsMatching } from "./processes";

const DESKTOP = path.resolve(__dirname, "..");

interface Launched {
  app: ElectronApplication;
  page: Page;
  marker: string;
  stdout: string[];
}

async function launch(): Promise<Launched> {
  const root = mkdtempSync(path.join(tmpdir(), "vigil-e2e-"));
  const config = path.join(root, "config");
  mkdirSync(config, { recursive: true });
  writeFileSync(
    path.join(config, "vigil.yaml"),
    "version: 1\npaths:\n  data_dir: ./var\ncameras: []\nlogging:\n  level: ERROR\n",
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

interface AxeResults {
  violations: { id: string; help: string }[];
}

test.describe("the real app", () => {
  let session: Launched;
  test.beforeEach(async () => {
    session = await launch();
  });
  test.afterEach(async () => {
    await session.app.close().catch(() => undefined);
  });

  test("starts the engine and shows the console, which says setup is required", async () => {
    await expect(session.page.getByRole("status")).toContainText("First-run setup is required");
    await expect(session.page).toHaveTitle("VIGIL-88");
    expect(session.page.url()).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/app\//);
  });

  test("has no WCAG 2.2 AA violations in a real browser engine, colour contrast included", async () => {
    await expect(session.page.getByRole("status")).toContainText("setup");
    // @axe-core/playwright opens a second page, which Electron does not support, so axe is
    // injected into the real window directly. The engine is the same real Chromium either way.
    await session.page.evaluate(readFileSync(require.resolve("axe-core/axe.min.js"), "utf8"));
    const results = await session.page.evaluate(async () => {
      const page = globalThis as unknown as {
        document: unknown;
        axe: { run(context: unknown, options: object): Promise<AxeResults> };
      };
      return page.axe.run(page.document, {
        runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] },
      });
    });
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  test("gives the page a narrow bridge and no Node", async () => {
    await expect(session.page.getByRole("status")).toContainText("setup");
    const exposure = await session.page.evaluate(() => ({
      bridge: Object.keys((window as unknown as { vigilShell: object }).vigilShell).sort(),
      require: typeof (window as unknown as { require?: unknown }).require,
      process: typeof (window as unknown as { process?: unknown }).process,
    }));
    expect(exposure).toEqual({
      bridge: ["engineState", "restartEngine", "setupToken"],
      require: "undefined",
      process: "undefined",
    });
  });

  test("hands the one-time setup token over once, and only once", async () => {
    await expect(session.page.getByRole("status")).toContainText("setup");
    const first = await session.page.evaluate(() =>
      (
        window as unknown as { vigilShell: { setupToken(): Promise<string | null> } }
      ).vigilShell.setupToken(),
    );
    const second = await session.page.evaluate(() =>
      (
        window as unknown as { vigilShell: { setupToken(): Promise<string | null> } }
      ).vigilShell.setupToken(),
    );
    expect(first).toMatch(/^[\w-]{30,}$/);
    expect(second).toBeNull();
  });

  test("restarts the engine when it is killed, and tells the person what happened", async () => {
    await expect(session.page.getByRole("status")).toContainText("setup");
    const before = pidsMatching(session.marker);
    expect(before.length).toBeGreaterThan(0);
    killPids(before);
    await expect(session.page.getByRole("alert")).toContainText("The engine stopped", {
      timeout: 30_000,
    });
    await expect(session.page.getByRole("status")).toContainText("First-run setup is required", {
      timeout: 90_000,
    });
    const after = pidsMatching(session.marker);
    expect(after.length).toBeGreaterThan(0);
    expect(after.some((pid) => before.includes(pid))).toBe(false); // a new process, not the old one
  });

  test("quitting stops the engine, so no process is left holding the camera", async () => {
    await expect(session.page.getByRole("status")).toContainText("setup");
    expect(pidsMatching(session.marker).length).toBeGreaterThan(0);
    await session.app.close();
    const deadline = Date.now() + 20_000;
    while (pidsMatching(session.marker).length > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    expect(pidsMatching(session.marker)).toEqual([]);
    expect(session.stdout.join("")).toContain('"shell":"engine-stopped"');
  });
});
