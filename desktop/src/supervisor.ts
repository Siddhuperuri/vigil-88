/**
 * The engine supervisor (plan §5.2): the outer ring of the two-ring restart design.
 *
 * It starts `vigil serve`, waits for the `ready` line, restarts the process with exponential
 * backoff when it dies, and stops it by closing its standard input, which the engine treats as a
 * request to shut down cleanly and release the camera. It holds no secrets and no business logic;
 * everything an operator can do is an API call the engine authorises.
 *
 * Nothing here imports Electron, so every branch is testable with a fake child and fake timers.
 */
import { EventEmitter } from "node:events";
import type { Readable, Writable } from "node:stream";

export type EngineState =
  | { kind: "stopped" }
  | { kind: "starting"; attempt: number }
  | { kind: "running"; port: number; url: string; version: string; setupRequired: boolean }
  | { kind: "backoff"; attempt: number; retryInMs: number; reason: string }
  | { kind: "stopping" };

/** The slice of a child process the supervisor needs. `ChildProcess` satisfies it. */
export interface ChildLike extends EventEmitter {
  stdout: Readable | null;
  stderr: Readable | null;
  stdin: Writable | null;
  kill(signal?: NodeJS.Signals | number): boolean;
}

export type SpawnFn = (
  command: string,
  args: readonly string[],
  env: NodeJS.ProcessEnv,
) => ChildLike;

export interface SupervisorOptions {
  command: string;
  args: readonly string[];
  env: NodeJS.ProcessEnv;
  /** No ready line within this long: the engine is killed and counted as a failed start. */
  readyTimeoutMs?: number;
  /** After closing stdin, how long a clean shutdown may take before the process is killed. */
  stopTimeoutMs?: number;
  /** Delay before restart attempt n is `initialMs * 2^(n-1)`, capped at `maxMs`. */
  backoff?: { initialMs: number; maxMs: number; healthyResetMs: number };
  /** The wall clock, injected so tests do not depend on real time. */
  now?: () => number;
}

const DEFAULTS = {
  readyTimeoutMs: 90_000,
  stopTimeoutMs: 15_000,
  backoff: { initialMs: 1_000, maxMs: 30_000, healthyResetMs: 60_000 },
} as const;

const LOG_LINES = 200;

interface EngineEvent {
  event: string;
  [key: string]: unknown;
}

function isEngineEvent(value: unknown): value is EngineEvent {
  return (
    typeof value === "object" && value !== null && typeof (value as EngineEvent).event === "string"
  );
}

/** Splits a stream into lines whatever size the chunks arrive in. */
export function lineReader(source: Readable, onLine: (line: string) => void): void {
  let pending = "";
  source.setEncoding("utf8");
  source.on("data", (chunk: string) => {
    pending += chunk;
    let at = pending.indexOf("\n");
    while (at >= 0) {
      const line = pending.slice(0, at).replace(/\r$/, "");
      pending = pending.slice(at + 1);
      if (line.length > 0) onLine(line);
      at = pending.indexOf("\n");
    }
  });
  source.on("end", () => {
    if (pending.length > 0) onLine(pending);
    pending = "";
  });
}

export class EngineSupervisor extends EventEmitter {
  private readonly cfg: Required<Omit<SupervisorOptions, "command" | "args" | "env">>;
  private current: EngineState = { kind: "stopped" };
  private child: ChildLike | null = null;
  private attempt = 0;
  private runningSince: number | null = null;
  private lastFailure: string | null = null;
  private wantStopped = true;
  private restartNow = false;
  private readyTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private stopWaiters: (() => void)[] = [];
  private readonly log: string[] = [];

  constructor(
    private readonly opts: SupervisorOptions,
    private readonly spawn: SpawnFn,
  ) {
    super();
    this.cfg = {
      readyTimeoutMs: opts.readyTimeoutMs ?? DEFAULTS.readyTimeoutMs,
      stopTimeoutMs: opts.stopTimeoutMs ?? DEFAULTS.stopTimeoutMs,
      backoff: opts.backoff ?? DEFAULTS.backoff,
      now: opts.now ?? Date.now,
    };
  }

  get state(): EngineState {
    return this.current;
  }

  /** The engine's recent standard error, for the diagnostics bundle and the "what happened" view. */
  recentLog(): readonly string[] {
    return this.log;
  }

  start(): void {
    if (this.current.kind !== "stopped") return;
    this.wantStopped = false;
    this.attempt = 0;
    this.launch();
  }

  /** Restart now, discarding any backoff: the operator asked, so the penalty does not apply. */
  restart(): void {
    this.clearRetry();
    this.wantStopped = false;
    this.attempt = 0;
    if (this.child === null) {
      this.launch();
      return;
    }
    this.restartNow = true;
    this.set({ kind: "stopping" });
    this.closeChild(this.child);
  }

  /** Ask the engine to shut down cleanly; force it only if it does not. Resolves once it is gone. */
  stop(): Promise<void> {
    this.wantStopped = true;
    this.clearRetry();
    this.clearReady();
    if (this.child === null) {
      this.set({ kind: "stopped" });
      return Promise.resolve();
    }
    this.set({ kind: "stopping" });
    const done = new Promise<void>((resolve) => this.stopWaiters.push(resolve));
    this.closeChild(this.child);
    return done;
  }

  /** Close stdin (the polite request) and arm a kill for an engine that ignores it. */
  private closeChild(child: ChildLike): void {
    child.stdin?.end();
    const force = setTimeout(() => {
      this.note("engine did not stop in time; killing it");
      child.kill();
    }, this.cfg.stopTimeoutMs);
    child.once("close", () => {
      clearTimeout(force);
    });
  }

  // ------------------------------------------------------------------ internals

  private launch(): void {
    this.attempt += 1;
    this.lastFailure = null;
    this.runningSince = null;
    this.set({ kind: "starting", attempt: this.attempt });
    let child: ChildLike;
    try {
      child = this.spawn(this.opts.command, this.opts.args, this.opts.env);
    } catch (error) {
      this.lastFailure = `could not start the engine: ${error instanceof Error ? error.message : String(error)}`;
      this.scheduleRestart();
      return;
    }
    this.child = child;
    if (child.stdout)
      lineReader(child.stdout, (line) => {
        this.onStdout(line);
      });
    if (child.stderr)
      lineReader(child.stderr, (line) => {
        this.note(line);
      });
    child.on("error", (error: Error) => {
      this.lastFailure = `could not start the engine: ${error.message}`;
    });
    child.once("close", (code: number | null, signal: string | null) => {
      this.onExit(child, code, signal);
    });
    this.readyTimer = setTimeout(() => {
      this.lastFailure = `the engine did not become ready within ${String(this.cfg.readyTimeoutMs / 1000)} s`;
      this.note(this.lastFailure);
      child.kill();
    }, this.cfg.readyTimeoutMs);
  }

  private onStdout(line: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      this.note(line); // an engine that prints something that is not JSON is telling us something
      return;
    }
    if (!isEngineEvent(parsed)) {
      this.note(line);
      return;
    }
    switch (parsed.event) {
      case "ready": {
        this.clearReady();
        this.runningSince = this.cfg.now();
        this.set({
          kind: "running",
          port: Number(parsed["port"]),
          url: String(parsed["url"]),
          version: String(parsed["version"]),
          setupRequired: parsed["setup_required"] === true,
        });
        break;
      }
      case "failed": {
        const message = parsed["message"];
        this.lastFailure = typeof message === "string" ? message : "the engine reported a failure";
        break;
      }
      case "restart_requested":
        // The engine is applying saved configuration. Its exit is a request, not a failure, so
        // it is relaunched at once and the backoff penalty does not apply.
        this.restartNow = true;
        break;
      case "health":
        this.emit("health", parsed["level"], parsed["issues"]);
        break;
      default:
        break;
    }
  }

  private onExit(child: ChildLike, code: number | null, signal: string | null): void {
    if (this.child !== child) return;
    this.child = null;
    this.clearReady();
    const waiters = this.stopWaiters.splice(0);
    if (this.restartNow) {
      this.restartNow = false;
      this.attempt = 0;
      this.launch();
      return;
    }
    if (this.wantStopped) {
      this.set({ kind: "stopped" });
      waiters.forEach((resolve) => {
        resolve();
      });
      return;
    }
    const healthyFor = this.runningSince === null ? 0 : this.cfg.now() - this.runningSince;
    if (healthyFor >= this.cfg.backoff.healthyResetMs) this.attempt = 0; // it had been well
    const how = signal !== null ? `was killed (${signal})` : `exited with code ${String(code)}`;
    this.lastFailure ??= `the engine ${how}`;
    this.scheduleRestart();
  }

  private scheduleRestart(): void {
    const { initialMs, maxMs } = this.cfg.backoff;
    const n = Math.max(1, this.attempt);
    const retryInMs = Math.min(initialMs * 2 ** (n - 1), maxMs);
    const reason = this.lastFailure ?? "the engine stopped";
    this.note(`restarting in ${String(retryInMs)} ms: ${reason}`);
    this.set({ kind: "backoff", attempt: n, retryInMs, reason });
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (!this.wantStopped) this.launch();
    }, retryInMs);
  }

  private set(next: EngineState): void {
    this.current = next;
    this.emit("state", next);
  }

  private note(line: string): void {
    this.log.push(line);
    if (this.log.length > LOG_LINES) this.log.shift();
    this.emit("log", line);
  }

  private clearReady(): void {
    if (this.readyTimer !== null) clearTimeout(this.readyTimer);
    this.readyTimer = null;
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }
}
