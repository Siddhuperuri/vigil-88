import { useCallback, useEffect, useState } from "react";
import { type AuthStatus, EngineUnreachableError, getAuthStatus } from "./api/client";

type View =
  { kind: "loading" } | { kind: "ready"; status: AuthStatus } | { kind: "failed"; message: string };

function describe(error: unknown): string {
  if (error instanceof EngineUnreachableError) {
    return "The engine is not answering. It may be starting or restarting.";
  }
  return error instanceof Error ? error.message : "Something unexpected went wrong.";
}

/**
 * The D0 bootstrap view: proves the renderer can reach the engine through the typed client and
 * that the first thing a person sees is honest about it. The real screens arrive in D1.
 */
export function App() {
  const [view, setView] = useState<View>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getAuthStatus(controller.signal).then(
      (status) => {
        setView({ kind: "ready", status });
      },
      (error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setView({ kind: "failed", message: describe(error) });
      },
    );
    return () => {
      controller.abort();
    };
  }, [attempt]);

  const retry = useCallback(() => {
    setView({ kind: "loading" });
    setAttempt((n) => n + 1);
  }, []);

  return (
    <>
      <header className="bar">
        <span className="brand">VIGIL-88</span>
      </header>
      <main className="page" aria-labelledby="page-title">
        <h1 id="page-title">VIGIL-88</h1>
        {view.kind === "loading" && (
          <p role="status" className="muted">
            Connecting to the engine…
          </p>
        )}
        {view.kind === "failed" && (
          <section role="alert" className="notice" aria-labelledby="failed-title">
            <h2 id="failed-title">The engine is not responding</h2>
            <p>{view.message}</p>
            <button type="button" onClick={retry}>
              Try again
            </button>
          </section>
        )}
        {view.kind === "ready" && (
          <p role="status">
            {view.status.setup_required
              ? "Connected to the engine. First-run setup is required."
              : view.status.authenticated
                ? "Connected to the engine. You are signed in."
                : "Connected to the engine. Sign-in is required."}
          </p>
        )}
      </main>
    </>
  );
}
