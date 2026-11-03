import { describe, expect, it, vi } from "vitest";
import { ApiError, EngineUnreachableError, getJson } from "./client";

describe("getJson", () => {
  it("returns the parsed body on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response('{"a":1}'))),
    );
    expect(await getJson<{ a: number }>("/x")).toEqual({ a: 1 });
  });

  it("turns a refusal into an ApiError carrying the engine's detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response('{"detail":"nope"}', { status: 403 }))),
    );
    const error = await getJson("/x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 403, message: "nope" });
  });

  it("falls back to the status text when the body is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(new Response("<html>", { status: 502, statusText: "Bad Gateway" })),
      ),
    );
    expect(await getJson("/x").catch((e: unknown) => e)).toMatchObject({
      status: 502,
      message: "Bad Gateway",
    });
  });

  it("reports an unreachable engine as such, not as an API error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );
    expect(await getJson("/x").catch((e: unknown) => e)).toBeInstanceOf(EngineUnreachableError);
  });

  it("lets an abort through untouched so a cancelled request is not shown as a failure", async () => {
    const abort = new DOMException("aborted", "AbortError");
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(abort)),
    );
    expect(await getJson("/x").catch((e: unknown) => e)).toBe(abort);
  });

  it("ignores a detail that is not a string and a body that is not an object", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response('{"detail":[1,2]}', { status: 422, statusText: "Unprocessable" }),
        ),
      ),
    );
    expect(await getJson("/x").catch((e: unknown) => e)).toMatchObject({
      message: "Unprocessable",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response("null", { status: 500, statusText: "" }))),
    );
    expect(await getJson("/x").catch((e: unknown) => e)).toMatchObject({
      message: "The engine answered 500.",
    });
  });
});
