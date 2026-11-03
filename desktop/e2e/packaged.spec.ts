import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { _electron as electron, expect, test } from "@playwright/test";

// The installed app, not the source tree: run with VIGIL_E2E_PACKAGED pointing at VIGIL-88.exe from
// `npm run dist:dir` (or an installed copy). Skipped otherwise, because it needs a build first.
const EXE = process.env["VIGIL_E2E_PACKAGED"];

/** Full image paths of the processes whose command line contains `marker`. */
function enginePaths(marker: string): string[] {
  const script = `Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'vigil-engine*' -and $_.CommandLine -like '*${marker}*' } | ForEach-Object { $_.ExecutablePath }`;
  return execFileSync("powershell", ["-NoProfile", "-NonInteractive", "-Command", script], {
    encoding: "utf8",
    windowsHide: true,
  })
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

test.describe("the installed app", () => {
  test.skip(EXE === undefined, "set VIGIL_E2E_PACKAGED to the built VIGIL-88.exe");

  test("starts the engine it ships with, serves the console it ships with, and stops it on quit", async () => {
    const root = mkdtempSync(path.join(tmpdir(), "vigil-packaged-"));
    const user = path.join(root, "user");
    const app = await electron.launch({
      executablePath: EXE as string,
      args: [`--user-data-dir=${user}`],
      env: { ...process.env, ELECTRON_ENABLE_LOGGING: "0" },
      timeout: 120_000,
    });
    try {
      const page = await app.firstWindow();
      await expect(page.getByRole("heading", { name: "Set up VIGIL-88" })).toBeVisible();
      expect(page.url()).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/app\//);

      // The engine is the one inside the install, and nothing in the person's PATH was needed.
      const running = enginePaths(user);
      expect(running.length).toBeGreaterThan(0);
      for (const exe of running) {
        expect(exe.toLowerCase()).toContain(path.join("resources", "engine").toLowerCase());
      }

      // Per-user data landed under the profile the shell was given, not beside the program.
      const where = await app.evaluate(({ app: a }) => a.getPath("userData"));
      expect(path.resolve(where)).toBe(path.resolve(user));
    } finally {
      await app.close();
    }
    const deadline = Date.now() + 20_000;
    while (enginePaths(user).length > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    expect(enginePaths(user)).toEqual([]);
  });
});
