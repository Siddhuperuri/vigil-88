/**
 * Starting with Windows: what to register, and how to tell a start the person did not make.
 * Pure, so the decision is tested without touching the registry.
 */

export const HIDDEN_FLAG = "--hidden";

export interface LoginItem {
  openAtLogin: boolean;
  path: string;
  args: string[];
}

/**
 * The login item for the current build. A packaged app registers itself; a development run registers
 * Electron with the app folder as its first argument, which is the only way that start can work.
 * Either way it starts hidden, in the tray: a machine that boots should watch, not open a window.
 */
export function loginItem(input: {
  enabled: boolean;
  execPath: string;
  appPath: string;
  packaged: boolean;
}): LoginItem {
  return {
    openAtLogin: input.enabled,
    path: input.execPath,
    args: input.packaged ? [HIDDEN_FLAG] : [input.appPath, HIDDEN_FLAG],
  };
}

/** True when the shell was started by the login item rather than by a person. */
export function startedHidden(argv: readonly string[]): boolean {
  return argv.includes(HIDDEN_FLAG);
}
