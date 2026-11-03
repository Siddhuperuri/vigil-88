import { EventEmitter } from "node:events";
import { PassThrough, Writable } from "node:stream";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type ChildLike,
  EngineSupervisor,
  type EngineState,
  type SupervisorOptions,
  lineReader,
} from "./supervisor";

class FakeChild extends EventEmitter implements ChildLike {
  stdout = new PassThrough();
  stderr = new PassThrough();
  stdinClosed = false;
  killed = false;
  stdin = new Writable({
    write: (_chunk, _enc, done) => {
      done();
    },
    final: (done) => {
      this.stdinClosed = true;
      done();
    },
  });
  kill(): boolean {
    this.killed = true;
    queueMicrotask(() => {
      this.exit(null, "SIGKILL");
    });
    return true;
  }
  say(event: Record<string, unknown>): void {
    this.stdout.write(JSON.stringify(event) + "\n");
  }
  ready(port = 5123, setupRequired = true): void {
    this.say({
      event: "ready",
      port,
      url: `http://127.0.0.1:${String(port)}`,
      version: "0.1.0",
      setup_required: setupRequired,
      cameras: 0,
    });
  }
  exit(code: number | null, signal: string | null = null): void {
    this.emit("close", code, signal);
  }
}

let children: FakeChild[];
let clock: number;

function make(overrides: Partial<SupervisorOptions> = {}): EngineSupervisor {
  return new EngineSupervisor(
    { command: "engine", args: ["serve"], env: {}, now: () => clock, ...overrides },
    () => {
      const child = new FakeChild();
      children.push(child);
      return child;
    },
  );
}

const states = (s: EngineSupervisor): EngineState["kind"][] => {
  const seen: EngineState["kind"][] = [];
  s.on("state", (st: EngineState) => seen.push(st.kind));
  return seen;
};

const last = (): FakeChild => {
  const c = children.at(-1);
  if (c === undefined) throw new Error("no child was spawned");
  return c;
};

const flush = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0);
};

beforeEach(() => {
  vi.useFakeTimers();
  children = [];
  clock = 0;
});
afterEach(() => {
  vi.useRealTimers();
});

describe("starting", () => {
  it("is starting until the engine says ready, then reports its port and whether setup is pending", async () => {
    const s = make();
    const seen = states(s);
    s.start();
    expect(s.state).toEqual({ kind: "starting", attempt: 1 });
    last().ready(6000, true);
    await flush();
    expect(s.state).toEqual({
      kind: "running",
      port: 6000,
      url: "http://127.0.0.1:6000",
      version: "0.1.0",
      setupRequired: true,
    });
    expect(seen).toEqual(["starting", "running"]);
  });

  it("reads lines however the chunks fall, and keeps non-JSON output in the log", async () => {
    const s = make();
    s.start();
    const child = last();
    const line = JSON.stringify({
      event: "ready",
      port: 7,
      url: "http://127.0.0.1:7",
      version: "x",
      setup_required: false,
    });
    child.stdout.write(line.slice(0, 12));
    child.stdout.write(line.slice(12) + "\n");
    child.stdout.write("Traceback (most recent call last)\n");
    child.stdout.write('{"not":"an event"}\n');
    await flush();
    expect(s.state.kind).toBe("running");
    expect(s.recentLog()).toEqual(["Traceback (most recent call last)", '{"not":"an event"}']);
  });

  it("does nothing if started twice", () => {
    const s = make();
    s.start();
    s.start();
    expect(children).toHaveLength(1);
  });

  it("kills an engine that never becomes ready and counts it as a failed start", async () => {
    const s = make({ readyTimeoutMs: 5_000 });
    s.start();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(last().killed).toBe(true);
    expect(s.state).toMatchObject({ kind: "backoff", attempt: 1 });
    expect((s.state as { reason: string }).reason).toContain("did not become ready within 5 s");
  });

  it("treats a spawn that throws as a failed start rather than crashing the shell", () => {
    const s = new EngineSupervisor({ command: "nope", args: [], env: {} }, () => {
      throw new Error("ENOENT");
    });
    s.start();
    expect(s.state).toMatchObject({ kind: "backoff", attempt: 1 });
    expect((s.state as { reason: string }).reason).toContain("ENOENT");
  });

  it("reports an error event from the child as the reason when it then exits", async () => {
    const s = make();
    s.start();
    last().emit("error", new Error("spawn uv ENOENT"));
    last().exit(-4058);
    await flush();
    expect((s.state as { reason: string }).reason).toContain("spawn uv ENOENT");
  });
});

describe("restarting with backoff", () => {
  it("restarts after 1, 2, 4 seconds and so on, capped at 30", async () => {
    const s = make();
    s.start();
    const delays: number[] = [];
    s.on("state", (st: EngineState) => {
      if (st.kind === "backoff") delays.push(st.retryInMs);
    });
    for (let i = 0; i < 8; i++) {
      last().exit(1);
      await flush();
      const wait = (s.state as { retryInMs: number }).retryInMs;
      await vi.advanceTimersByTimeAsync(wait);
    }
    expect(delays).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
    expect(children).toHaveLength(9);
  });

  it("says why it is restarting: the engine's own failure event wins over an exit code", async () => {
    const s = make();
    s.start();
    last().say({
      event: "failed",
      stage: "startup",
      message: "the API could not start on port 8088",
    });
    last().exit(1);
    await flush();
    expect((s.state as { reason: string }).reason).toBe("the API could not start on port 8088");
  });

  it("describes a signal death and a plain exit differently", async () => {
    const s = make();
    s.start();
    last().exit(null, "SIGKILL");
    await flush();
    expect((s.state as { reason: string }).reason).toBe("the engine was killed (SIGKILL)");
    await vi.advanceTimersByTimeAsync(1000);
    last().exit(3);
    await flush();
    expect((s.state as { reason: string }).reason).toBe("the engine exited with code 3");
  });

  it("forgives the past once the engine has run well for a minute", async () => {
    const s = make();
    s.start();
    for (let i = 0; i < 3; i++) {
      last().exit(1);
      await flush();
      await vi.advanceTimersByTimeAsync((s.state as { retryInMs: number }).retryInMs);
    }
    expect(s.state).toMatchObject({ kind: "starting", attempt: 4 });
    last().ready();
    await flush();
    clock += 61_000; // ran for 61 s, then died
    last().exit(1);
    await flush();
    expect(s.state).toMatchObject({ kind: "backoff", attempt: 1, retryInMs: 1000 });
  });

  it("does not forgive an engine that died soon after it said ready", async () => {
    const s = make();
    s.start();
    last().ready();
    await flush();
    clock += 5_000;
    last().exit(1);
    await flush();
    await vi.advanceTimersByTimeAsync(1000);
    last().ready();
    await flush();
    clock += 5_000;
    last().exit(1);
    await flush();
    expect(s.state).toMatchObject({ kind: "backoff", attempt: 2, retryInMs: 2000 }); // not reset to 1
  });

  it("an operator restart skips the wait and resets the penalty", async () => {
    const s = make();
    s.start();
    for (let i = 0; i < 3; i++) {
      last().exit(1);
      await flush();
      await vi.advanceTimersByTimeAsync((s.state as { retryInMs: number }).retryInMs);
    }
    last().exit(1);
    await flush();
    expect(s.state).toMatchObject({ kind: "backoff", attempt: 4 });
    s.restart();
    expect(s.state).toEqual({ kind: "starting", attempt: 1 });
    expect(children).toHaveLength(5);
  });

  it("restarting a running engine closes it politely and relaunches at once", async () => {
    const s = make();
    s.start();
    const first = last();
    first.ready();
    await flush();
    s.restart();
    expect(first.stdinClosed).toBe(true);
    expect(s.state.kind).toBe("stopping");
    first.exit(0);
    await flush();
    expect(children).toHaveLength(2);
    expect(s.state).toEqual({ kind: "starting", attempt: 1 });
  });

  it("relaunches at once, with no penalty, when the engine says it is restarting to apply changes", async () => {
    const s = make();
    s.start();
    for (let i = 0; i < 3; i++) {
      last().exit(1);
      await flush();
      await vi.advanceTimersByTimeAsync((s.state as { retryInMs: number }).retryInMs);
    }
    last().ready();
    await flush();
    const before = children.length;
    last().say({ event: "restart_requested" });
    last().say({ event: "stopped", clean: true });
    last().exit(0);
    await flush();
    expect(children).toHaveLength(before + 1);
    expect(s.state).toEqual({ kind: "starting", attempt: 1 }); // the earlier failures are forgiven
  });

  it("forwards the engine's health lines", async () => {
    const s = make();
    const got: unknown[] = [];
    s.on("health", (level: string, issues: string[]) => got.push([level, issues]));
    s.start();
    last().say({ event: "health", level: "degraded", issues: ["camera x is degraded"] });
    await flush();
    expect(got).toEqual([["degraded", ["camera x is degraded"]]]);
  });
});

describe("stopping", () => {
  it("closes stdin, which is the engine's request to shut down, and resolves when it is gone", async () => {
    const s = make();
    s.start();
    const child = last();
    child.ready();
    await flush();
    let done = false;
    const stopped = s.stop().then(() => {
      done = true;
    });
    expect(child.stdinClosed).toBe(true);
    expect(s.state.kind).toBe("stopping");
    await flush();
    expect(done).toBe(false); // not until it has actually exited
    child.exit(0);
    await stopped;
    expect(s.state.kind).toBe("stopped");
    expect(child.killed).toBe(false);
  });

  it("kills an engine that ignores the request, after the timeout", async () => {
    const s = make({ stopTimeoutMs: 3_000 });
    s.start();
    const child = last();
    child.ready();
    await flush();
    const stopped = s.stop();
    await vi.advanceTimersByTimeAsync(2_999);
    expect(child.killed).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await stopped;
    expect(child.killed).toBe(true);
    expect(s.state.kind).toBe("stopped");
  });

  it("never restarts after a stop, even if the process dies with a failure code", async () => {
    const s = make();
    s.start();
    const child = last();
    const stopped = s.stop();
    child.exit(1);
    await stopped;
    await vi.advanceTimersByTimeAsync(120_000);
    expect(children).toHaveLength(1);
    expect(s.state.kind).toBe("stopped");
  });

  it("cancels a pending restart", async () => {
    const s = make();
    s.start();
    last().exit(1);
    await flush();
    expect(s.state.kind).toBe("backoff");
    await s.stop();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(children).toHaveLength(1);
    expect(s.state.kind).toBe("stopped");
  });

  it("resolves at once when nothing is running", async () => {
    await expect(make().stop()).resolves.toBeUndefined();
  });
});

describe("the log", () => {
  it("keeps the last 200 lines of standard error and no more", async () => {
    const s = make();
    s.start();
    for (let i = 0; i < 250; i++) last().stderr.write(`line ${String(i)}\n`);
    await flush();
    expect(s.recentLog()).toHaveLength(200);
    expect(s.recentLog()[0]).toBe("line 50");
    expect(s.recentLog().at(-1)).toBe("line 249");
  });

  it("flushes a final line that has no newline", async () => {
    const source = new PassThrough();
    const lines: string[] = [];
    lineReader(source, (l) => lines.push(l));
    source.write("a\r\nb");
    source.end();
    await flush();
    expect(lines).toEqual(["a", "b"]);
  });
});
