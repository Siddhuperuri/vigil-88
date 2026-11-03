import { describe, expect, it } from "vitest";
import { statusPage } from "./status-page";
import type { EngineState } from "./supervisor";

const decode = (page: string): string =>
  decodeURIComponent(page.slice("data:text/html;charset=utf-8,".length));

const kinds: EngineState[] = [
  { kind: "starting", attempt: 1 },
  { kind: "starting", attempt: 3 },
  { kind: "backoff", attempt: 2, retryInMs: 2000, reason: "the engine exited with code 1" },
  { kind: "stopping" },
  { kind: "stopped" },
  { kind: "running", port: 1, url: "http://127.0.0.1:1", version: "x", setupRequired: false },
];

describe("statusPage", () => {
  it("is a local page that carries no script and loads nothing", () => {
    for (const state of kinds) {
      const html = decode(statusPage(state));
      expect(html).not.toMatch(/<script/i);
      expect(html).toContain("default-src 'none'");
    }
  });

  it("names the reason and the wait when the engine is restarting", () => {
    const html = decode(statusPage(kinds[2] as EngineState));
    expect(html).toContain("the engine exited with code 1");
    expect(html).toContain("Trying again in 2 s (attempt 2)");
    expect(html).toContain('role="alert"');
  });

  it("is polite for the states that are expected and urgent for the ones that are not", () => {
    expect(decode(statusPage({ kind: "starting", attempt: 1 }))).toContain('role="status"');
    expect(decode(statusPage({ kind: "stopped" }))).toContain('role="alert"');
  });

  it("mentions the attempt number only after the first", () => {
    expect(decode(statusPage({ kind: "starting", attempt: 1 }))).toContain("a few seconds");
    expect(decode(statusPage({ kind: "starting", attempt: 4 }))).toContain("Attempt 4");
  });

  it("cannot be made to inject markup through an engine failure message", () => {
    const hostile = {
      kind: "backoff",
      attempt: 1,
      retryInMs: 1000,
      reason: '<img src=x onerror="alert(1)">',
    } as const;
    const html = decode(statusPage(hostile));
    expect(html).not.toContain("<img");
    expect(html).toContain("&#60;img");
  });
});
