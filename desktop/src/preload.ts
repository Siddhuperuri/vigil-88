/**
 * The narrow bridge between the page and the shell. It exposes four calls and nothing else: no
 * Node, no filesystem, no generic IPC. The main process also checks that each request comes from
 * the engine's own origin, so this is not the only line of defence.
 */
import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("vigilShell", {
  /** The one-time setup token, once, while first-run setup is pending. Otherwise null. */
  setupToken: (): Promise<string | null> =>
    ipcRenderer.invoke("shell:setup-token") as Promise<string | null>,
  engineState: (): Promise<unknown> => ipcRenderer.invoke("shell:engine-state"),
  /** Ask for a native notification. The shell re-validates every field and ignores quiet ones. */
  notify: (alert: unknown): Promise<boolean> =>
    ipcRenderer.invoke("shell:notify", alert) as Promise<boolean>,
  restartEngine: (): Promise<void> => ipcRenderer.invoke("shell:restart-engine") as Promise<void>,
});
