import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SETUP_TOKEN_ENV, ensureEngineConfig, planEngine } from "./engine-plan";

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), "vigil-plan-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("ensureEngineConfig", () => {
  it("creates a minimal per-user configuration with no cameras", () => {
    const configDir = ensureEngineConfig(dir);
    expect(configDir).toBe(path.join(dir, "engine", "config"));
    const text = readFileSync(path.join(configDir, "vigil.yaml"), "utf8");
    expect(text).toContain("version: 1");
    expect(text).toContain("cameras: []");
    expect(text).toContain("data_dir: ./var");
  });

  it("never overwrites what a person (or a later settings screen) has written", () => {
    const configDir = ensureEngineConfig(dir);
    writeFileSync(path.join(configDir, "vigil.yaml"), "version: 1\n# mine\n");
    ensureEngineConfig(dir);
    expect(readFileSync(path.join(configDir, "vigil.yaml"), "utf8")).toContain("# mine");
  });
});

describe("planEngine", () => {
  const base = { setupToken: "tok-123", repoRoot: "/repo" };

  it("runs the packaged engine directly and asks for the safe shutdown protocol", () => {
    const plan = planEngine({ ...base, userDataDir: dir, packagedEngine: "/app/vigil-engine.exe" });
    expect(plan.command).toBe("/app/vigil-engine.exe");
    expect(plan.args.slice(0, 2)).toEqual(["serve", "--port"]);
    expect(plan.args).toContain("--stop-on-stdin-eof");
    expect(plan.args[plan.args.indexOf("--port") + 1]).toBe("0");
  });

  it("runs from a source checkout through uv when nothing is packaged", () => {
    const plan = planEngine({ ...base, userDataDir: dir });
    expect(plan.command).toBe("uv");
    expect(plan.args.slice(0, 4)).toEqual(["run", "--project", "/repo", "vigil"]);
  });

  it("hands the engine its one-time setup token through the environment, never an argument", () => {
    const plan = planEngine({ ...base, userDataDir: dir });
    expect(plan.env[SETUP_TOKEN_ENV]).toBe("tok-123");
    expect(plan.args.join(" ")).not.toContain("tok-123"); // an argument shows up in process lists
  });

  it("keeps the caller's environment and forces unbuffered, UTF-8 output", () => {
    const plan = planEngine({ ...base, userDataDir: dir, environment: { PATH: "/bin" } });
    expect(plan.env).toMatchObject({ PATH: "/bin", PYTHONUNBUFFERED: "1", PYTHONUTF8: "1" });
  });

  it("serves the built console and runs a named detector only when given them", () => {
    const bare = planEngine({ ...base, userDataDir: dir });
    expect(bare.args).not.toContain("--ui-dir");
    expect(bare.args).not.toContain("--detector");
    const full = planEngine({ ...base, userDataDir: dir, uiDir: "/ui/dist", detector: "yolox_s" });
    expect(full.args).toEqual(
      expect.arrayContaining(["--ui-dir", "/ui/dist", "--detector", "yolox_s"]),
    );
  });

  it("uses a configuration override instead of touching the per-user folder", () => {
    const plan = planEngine({ ...base, userDataDir: dir, configDirOverride: "/somewhere/config" });
    expect(plan.configDir).toBe("/somewhere/config");
    expect(plan.args[plan.args.indexOf("--config-dir") + 1]).toBe("/somewhere/config");
  });

  it("refuses to plan when it has no engine to run", () => {
    expect(() => planEngine({ userDataDir: dir, setupToken: "t" })).toThrow(
      /neither a packaged engine/,
    );
  });
});
