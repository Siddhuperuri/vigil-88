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
import { BrowserWindow, Menu, Notification, Tray, app, ipcMain, nativeImage } from "electron";
import type { IpcMainInvokeEvent } from "electron";
import { planEngine } from "./engine-plan";
import { describeAlert } from "./notify";
import { statusPage } from "./status-page";
import { EngineSupervisor, type EngineState } from "./supervisor";
import { type TrayTone, trayColour, trayIconPng } from "./tray-icon";

const SHELL_CHANNEL = {
  setup: "shell:setup-token",
  state: "shell:engine-state",
  restart: "shell:restart-engine",
  notify: "shell:notify",
};

let window: BrowserWindow | null = null;
let supervisor: EngineSupervisor | null = null;
let quitting = false;
let tray: Tray | null = null;
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
      // An alert tone must sound when an incident arrives, not wait for a click first.
      autoplayPolicy: "no-user-gesture-required",
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
  // Closing the window hides it: monitoring continues from the tray. Quit from the tray menu.
  win.on("close", (event) => {
    if (quitting || tray === null) return;
    event.preventDefault();
    win.hide();
    milestone("window-hidden");
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
    updateTray(state);
    show(state);
  });
  supervisor.start();
}

function toneOf(state: EngineState): TrayTone {
  if (state.kind === "running") return "ok";
  return state.kind === "backoff" || state.kind === "stopped" ? "failed" : "warning";
}

function updateTray(state: EngineState): void {
  if (tray === null) return;
  const tone = toneOf(state);
  tray.setImage(nativeImage.createFromBuffer(trayIconPng(trayColour(tone))));
  const words: Record<EngineState["kind"], string> = {
    running: "engine running",
    starting: "engine starting",
    backoff: "engine stopped, restarting",
    stopping: "engine stopping",
    stopped: "engine stopped",
  };
  tray.setToolTip(`VIGIL-88: ${words[state.kind]}`);
}

function showWindow(): void {
  if (window === null) window = createWindow();
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

function createTray(): void {
  try {
    tray = new Tray(nativeImage.createFromBuffer(trayIconPng(trayColour("warning"))));
  } catch {
    tray = null; // no tray (some Linux desktops): then closing the window quits, as it always did
    return;
  }
  tray.setToolTip("VIGIL-88");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Open VIGIL-88", click: showWindow },
      { type: "separator" },
      {
        label: "Quit and stop monitoring",
        click: () => {
          app.quit();
        },
      },
    ]),
  );
  tray.on("click", showWindow);
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
  ipcMain.handle(SHELL_CHANNEL.notify, (event, raw: unknown) => {
    if (!fromEngineOrigin(event) || !Notification.isSupported()) return false;
    const alert = describeAlert(raw); // re-validated here: the page is not trusted
    if (alert === null) return false;
    const note = new Notification({
      title: alert.title,
      body: alert.body,
      urgency: alert.urgent ? "critical" : "normal",
    });
    note.on("click", () => {
      showWindow();
      const origin = engineOrigin();
      if (origin !== null && window !== null) {
        void window.loadURL(`${origin}/app/#/incidents/${alert.incidentId}`);
      }
    });
    note.show();
    return true;
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
    app.setAppUserModelId("VIGIL-88"); // gives Windows toasts a name and the right icon
    registerIpc();
    createTray();
    window = createWindow();
    show({ kind: "starting", attempt: 1 });
    startEngine();
  });
  // With a tray the app lives on when the window closes. Without one there is nothing to return
  // to, so closing the last window quits, as before.
  app.on("window-all-closed", () => {
    if (tray === null) app.quit();
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
