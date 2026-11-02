/**
 * Electron main process: window, single-instance lock, and the engine supervisor.
 *
 * It owns what a web page cannot do and nothing else. The renderer talks to the engine over HTTP
 * on the engine's own origin; this process only starts and stops that engine and tells the window
 * what happened. No secret is kept here beyond the one-time setup token, and no business logic.
 */
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import path from "node:path";
import { BrowserWindow, app, ipcMain } from "electron";
import type { IpcMainInvokeEvent } from "electron";
import { planEngine } from "./engine-plan";
import { statusPage } from "./status-page";
import { EngineSupervisor, type EngineState } from "./supervisor";

const SHELL_CHANNEL = {
  setup: "shell:setup-token",
  state: "shell:engine-state",
  restart: "shell:restart-engine",
};

let window: BrowserWindow | null = null;
let supervisor: EngineSupervisor | null = null;
let quitting = false;
let setupTokenAvailable = true;
const setupToken = randomBytes(24).toString("base64url");
const startedAt = Date.now();

/** Machine-readable milestones on stdout, for the measurement script and the end-to-end tests. */
function milestone(name: string, extra: Record<string, unknown> = {}): void {
  process.stdout.write(
    JSON.stringify({ shell: name, ms: Date.now() - startedAt, ...extra }) + "\n",
  );
}

function engineOrigin(): string | null {
  const state = supervisor?.state;
  return state?.kind === "running" ? new URL(state.url).origin : null;
}

/** An IPC request is honoured only from a page on the engine's own origin: never from a stray one. */
function fromEngineOrigin(event: IpcMainInvokeEvent): boolean {
  const origin = engineOrigin();
  if (origin === null) return false;
  try {
    return new URL(event.senderFrame?.url ?? "").origin === origin;
  } catch {
    return false;
  }
}

function show(state: EngineState): void {
  const win = window;
  if (win === null || win.isDestroyed()) return;
  win.webContents.send(SHELL_CHANNEL.state, state);
  if (state.kind === "running") {
    const target = new URL(existsUi() ? "/app/" : "/", state.url).toString();
    void win.loadURL(target);
    milestone("engine-running", { url: target });
  } else {
    void win.loadURL(statusPage(state));
  }
}

function existsUi(): boolean {
  return uiDir() !== undefined;
}

function uiDir(): string | undefined {
  const fromEnv = process.env["VIGIL_UI_DIR"];
  if (fromEnv !== undefined && existsSync(fromEnv)) return fromEnv;
  const built = path.resolve(app.getAppPath(), "..", "ui", "dist");
  return existsSync(path.join(built, "index.html")) ? built : undefined;
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    title: "VIGIL-88",
    backgroundColor: "#0a0b0d",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
    },
  });
  win.once("ready-to-show", () => {
    win.show();
    milestone("window-shown");
  });
  // The window may only ever show the engine's origin or the local status page.
  win.webContents.on("will-navigate", (event, url) => {
    const origin = engineOrigin();
    const allowed = url.startsWith("data:text/html") || (origin !== null && url.startsWith(origin));
    if (!allowed) event.preventDefault();
  });
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("did-finish-load", () => {
    milestone("page-loaded", { url: win.webContents.getURL().slice(0, 80) });
  });
  win.webContents.on("page-title-updated", (_event, title) => {
    milestone("title", { title });
  });
  win.on("closed", () => {
    window = null;
  });
  return win;
}

function startEngine(): void {
  const plan = planEngine({
    userDataDir: app.getPath("userData"),
    setupToken,
    uiDir: uiDir(),
    repoRoot: path.resolve(app.getAppPath(), ".."),
    configDirOverride: process.env["VIGIL_ENGINE_CONFIG_DIR"],
    detector: process.env["VIGIL_DETECTOR"],
    environment: process.env,
  });
  supervisor = new EngineSupervisor(
    { command: plan.command, args: plan.args, env: plan.env },
    (command, args, env) =>
      spawn(command, [...args], { env, stdio: ["pipe", "pipe", "pipe"], windowsHide: true }),
  );
  supervisor.on("state", (state: EngineState) => {
    milestone("engine-state", { state: state.kind });
    show(state);
  });
  supervisor.start();
}

function registerIpc(): void {
  ipcMain.handle(SHELL_CHANNEL.setup, (event) => {
    // Only while setup is pending, once, and only to the engine's own page.
    const state = supervisor?.state;
    if (!fromEngineOrigin(event) || state?.kind !== "running" || !state.setupRequired) return null;
    if (!setupTokenAvailable) return null;
    setupTokenAvailable = false;
    return setupToken;
  });
  ipcMain.handle(SHELL_CHANNEL.state, (event) =>
    fromEngineOrigin(event) ? supervisor?.state : null,
  );
  ipcMain.handle(SHELL_CHANNEL.restart, (event) => {
    if (fromEngineOrigin(event)) supervisor?.restart();
  });
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (window === null) return;
    if (window.isMinimized()) window.restore();
    window.focus();
  });
  void app.whenReady().then(() => {
    milestone("ready");
    registerIpc();
    window = createWindow();
    show({ kind: "starting", attempt: 1 });
    startEngine();
  });
  app.on("window-all-closed", () => {
    app.quit();
  });
  // Quitting waits for the engine to close its cameras and database, then exits for real.
  app.on("before-quit", (event) => {
    if (quitting || supervisor === null) return;
    event.preventDefault();
    quitting = true;
    void supervisor.stop().then(() => {
      milestone("engine-stopped");
      app.exit(0);
    });
  });
}
