import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, EngineUnreachableError, getJson } from "./client";

export type Loaded<T> =
  | { state: "loading"; previous: T | null }
  | { state: "ready"; data: T }
  | { state: "failed"; message: string; status: number | null; previous: T | null };

export function messageOf(error: unknown): string {
  if (error instanceof EngineUnreachableError) return "The engine did not answer.";
  return error instanceof Error ? error.message : "Something unexpected went wrong.";
}

/** Loads a GET and keeps the last good data visible while a refresh is in flight or has failed,
 * so a blip never blanks a screen the operator is reading. */
export function useFetch<T>(path: string | null): [Loaded<T>, () => void] {
  const [result, setResult] = useState<Loaded<T>>({ state: "loading", previous: null });
  const last = useRef<T | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (path === null) return;
    const controller = new AbortController();
    setResult({ state: "loading", previous: last.current });
    getJson<T>(path, controller.signal).then(
      (data) => {
        last.current = data;
        setResult({ state: "ready", data });
      },
      (error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setResult({
          state: "failed",
          message: messageOf(error),
          status: error instanceof ApiError ? error.status : null,
          previous: last.current,
        });
      },
    );
    return () => {
      controller.abort();
    };
  }, [path, tick]);

  const reload = useCallback(() => {
    setTick((n) => n + 1);
  }, []);
  return [result, reload];
}
