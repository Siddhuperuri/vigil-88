import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { getJson, sendJson } from "../api/client";
import type { Pending } from "../api/types";
import { messageOf } from "../api/useFetch";
import { useTelemetry } from "./telemetry";

type Phase = "idle" | "applying" | "failed";

interface PendingState {
  pending: Pending | null;
  phase: Phase;
  error: string | null;
  /** Re-read what differs between the saved files and the running engine. */
  refresh: () => void;
  /** Ask the engine to restart so it loads what is saved. Monitoring stops while it does. */
  apply: () => Promise<void>;
}

const PendingContext = createContext<PendingState | null>(null);

/**
 * Saved-but-not-applied configuration. A change is saved at once and takes effect on a controlled
 * restart (ADR 0017), so the console keeps the difference in front of the person until they apply
 * it, and says plainly what applying costs.
 */
export function PendingProvider({ children }: { children: ReactNode }) {
  const { link, snapshot } = useTelemetry();
  const [pending, setPending] = useState<Pending | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  // A reconnect after an engine restart is when the answer changes, so it re-reads too.
  const generation = link === "live" ? (snapshot?.state ?? null) : null;

  useEffect(() => {
    const controller = new AbortController();
    getJson<Pending>("/api/v1/config/pending", controller.signal).then(
      (p) => {
        setPending(p);
      },
      (e: unknown) => {
        if (!(e instanceof DOMException && e.name === "AbortError")) setError(messageOf(e));
      },
    );
    return () => {
      controller.abort();
    };
  }, [tick, generation]);

  // After applying: the engine goes away and comes back. Once the live channel is up again the
  // restart is over, whatever happened in between.
  useEffect(() => {
    if (phase === "applying" && link === "live" && snapshot !== null) {
      const timer = setTimeout(() => {
        setPhase("idle");
        setTick((n) => n + 1);
      }, 1500);
      return () => {
        clearTimeout(timer);
      };
    }
    return undefined;
  }, [phase, link, snapshot]);

  const refresh = useCallback(() => {
    setTick((n) => n + 1);
  }, []);

  const apply = useCallback(async () => {
    setError(null);
    setPhase("applying");
    try {
      await sendJson("POST", "/api/v1/config/apply");
    } catch (e) {
      setPhase("failed");
      setError(messageOf(e));
    }
  }, []);

  const value = useMemo(
    () => ({ pending, phase, error, refresh, apply }),
    [pending, phase, error, refresh, apply],
  );
  return <PendingContext.Provider value={value}>{children}</PendingContext.Provider>;
}

export function usePending(): PendingState {
  const value = useContext(PendingContext);
  if (value === null) throw new Error("usePending needs a PendingProvider");
  return value;
}
