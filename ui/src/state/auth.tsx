import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { type AuthStatus, getAuthStatus, sendJson } from "../api/client";
import type { SessionInfo, SetupResult } from "../api/types";
import { messageOf } from "../api/useFetch";

type Load =
  { kind: "loading" } | { kind: "ready"; status: AuthStatus } | { kind: "failed"; message: string };

interface Auth {
  load: Load;
  refresh: () => void;
  login: (username: string, password: string) => Promise<SessionInfo>;
  setup: (token: string, username: string, password: string) => Promise<SetupResult>;
  logout: () => Promise<void>;
  lock: () => Promise<void>;
  unlock: (password: string) => Promise<void>;
}

const AuthContext = createContext<Auth | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getAuthStatus(controller.signal).then(
      (status) => {
        setLoad({ kind: "ready", status });
      },
      (error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setLoad({ kind: "failed", message: messageOf(error) });
      },
    );
    return () => {
      controller.abort();
    };
  }, [tick]);

  const refresh = useCallback(() => {
    setTick((n) => n + 1);
  }, []);

  const value = useMemo<Auth>(
    () => ({
      load,
      refresh,
      login: async (username, password) => {
        const session = await sendJson<SessionInfo>("POST", "/api/v1/auth/login", {
          username,
          password,
        });
        refresh();
        return session;
      },
      // The caller shows the recovery codes first, then calls refresh().
      setup: (token, username, password) =>
        sendJson<SetupResult>("POST", "/api/v1/auth/setup", { token, username, password }),
      logout: async () => {
        await sendJson<undefined>("POST", "/api/v1/auth/logout");
        refresh();
      },
      lock: async () => {
        await sendJson<undefined>("POST", "/api/v1/auth/lock");
        refresh();
      },
      unlock: async (password) => {
        await sendJson<undefined>("POST", "/api/v1/auth/unlock", { password });
        refresh();
      },
    }),
    [load, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const value = useContext(AuthContext);
  if (value === null) throw new Error("useAuth needs an AuthProvider");
  return value;
}
