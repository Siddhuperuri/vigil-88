import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  CLOSE_LOCKED,
  CLOSE_SIGNED_OUT,
  type IncidentNotice,
  type Snapshot,
  parseMessage,
  reconnectDelayMs,
} from "../api/telemetry";

export type Link = "connecting" | "live" | "reconnecting" | "ended";

interface Telemetry {
  link: Link;
  snapshot: Snapshot | null;
  /** When the last message arrived, so the strip can say how stale the numbers are. */
  lastMessageAt: number | null;
  dropped: number;
  /** The newest incident notice; the announcer and the desk react to its sequence changing. */
  notice: IncidentNotice | null;
  noticeSeq: number;
}

const TelemetryContext = createContext<Telemetry | null>(null);

function socketUrl(): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/api/v1/telemetry`;
}

export function TelemetryProvider({
  children,
  onEnded,
}: {
  children: ReactNode;
  /** Called when the engine says the session is over or locked, so auth can be re-read. */
  onEnded: () => void;
}) {
  const [state, setState] = useState<Telemetry>({
    link: "connecting",
    snapshot: null,
    lastMessageAt: null,
    dropped: 0,
    notice: null,
    noticeSeq: 0,
  });
  const ended = useRef(onEnded);
  useEffect(() => {
    ended.current = onEnded;
  });

  useEffect(() => {
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let stopped = false;

    const open = () => {
      socket = new WebSocket(socketUrl());
      socket.onopen = () => {
        attempt = 0;
        setState((s) => ({ ...s, link: "live" }));
      };
      socket.onmessage = (event: MessageEvent<unknown>) => {
        const message = typeof event.data === "string" ? parseMessage(event.data) : null;
        if (message === null) {
          setState((s) => ({ ...s, dropped: s.dropped + 1 }));
          return;
        }
        setState((s) =>
          message.kind === "snapshot"
            ? { ...s, snapshot: message, lastMessageAt: Date.now() }
            : { ...s, notice: message, noticeSeq: s.noticeSeq + 1, lastMessageAt: Date.now() },
        );
      };
      socket.onclose = (event) => {
        if (stopped) return;
        if (event.code === CLOSE_SIGNED_OUT || event.code === CLOSE_LOCKED) {
          setState((s) => ({ ...s, link: "ended" }));
          ended.current();
          return;
        }
        attempt += 1;
        setState((s) => ({ ...s, link: "reconnecting" }));
        timer = setTimeout(open, reconnectDelayMs(attempt));
      };
    };
    open();
    return () => {
      stopped = true;
      if (timer !== null) clearTimeout(timer);
      socket?.close();
    };
  }, []);

  const value = useMemo(() => state, [state]);
  return <TelemetryContext.Provider value={value}>{children}</TelemetryContext.Provider>;
}

export function useTelemetry(): Telemetry {
  const value = useContext(TelemetryContext);
  if (value === null) throw new Error("useTelemetry needs a TelemetryProvider");
  return value;
}
