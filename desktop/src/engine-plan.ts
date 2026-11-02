/**
 * How the shell launches the engine: where its configuration and data live, and the exact command.
 * Pure functions, so the decisions are tested without starting Electron or a process.
 */
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

export const SETUP_TOKEN_ENV = "VIGIL_SETUP_TOKEN";

export interface EnginePlan {
  command: string;
  args: string[];
  env: NodeJS.ProcessEnv;
  configDir: string;
}

export interface PlanInput {
  /** Per-user application data directory (Electron's `userData`). */
  userDataDir: string;
  /** One-time token the engine requires before it will create the first administrator. */
  setupToken: string;
  /** The built console to serve under /app/, when it exists. */
  uiDir?: string | undefined;
  /** A packaged engine executable. When absent the engine is run from the source checkout. */
  packagedEngine?: string | undefined;
  /** The source checkout root, used in development to run the engine through uv. */
  repoRoot?: string | undefined;
  /** Use this configuration directory instead of the per-user one (tests and the spike). */
  configDirOverride?: string | undefined;
  /** A model to run, for a measurement with the real detector. */
  detector?: string | undefined;
  environment?: NodeJS.ProcessEnv;
}

const MINIMAL_CONFIG = `# Written by the VIGIL-88 desktop shell on first launch. Edit through the app.
version: 1
paths:
  data_dir: ./var # incidents, evidence and accounts live beside this folder, per user
cameras: []
`;

/** The per-user configuration folder, created with a minimal file only if it has none. */
export function ensureEngineConfig(userDataDir: string): string {
  const configDir = path.join(userDataDir, "engine", "config");
  const file = path.join(configDir, "vigil.yaml");
  if (!existsSync(file)) {
    mkdirSync(configDir, { recursive: true });
    writeFileSync(file, MINIMAL_CONFIG, { encoding: "utf8", flag: "wx" });
  }
  return configDir;
}

export function planEngine(input: PlanInput): EnginePlan {
  const configDir = input.configDirOverride ?? ensureEngineConfig(input.userDataDir);
  const serve = [
    "serve",
    "--port",
    "0", // the operating system picks, so there is never a clash and never a guess
    "--stop-on-stdin-eof", // closing our pipe is the shutdown request, and it fires if we crash
    "--config-dir",
    configDir,
    ...(input.uiDir !== undefined ? ["--ui-dir", input.uiDir] : []),
    ...(input.detector !== undefined ? ["--detector", input.detector] : []),
  ];
  const env: NodeJS.ProcessEnv = {
    ...(input.environment ?? {}),
    [SETUP_TOKEN_ENV]: input.setupToken,
    PYTHONUNBUFFERED: "1", // the ready line must reach us now, not when a buffer fills
    PYTHONUTF8: "1",
  };
  if (input.packagedEngine !== undefined) {
    return { command: input.packagedEngine, args: serve, env, configDir };
  }
  const repoRoot = input.repoRoot;
  if (repoRoot === undefined) {
    throw new Error("neither a packaged engine nor a source checkout was given");
  }
  return {
    command: "uv",
    args: ["run", "--project", repoRoot, "vigil", ...serve],
    env,
    configDir,
  };
}
