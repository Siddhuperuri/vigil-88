import { useCallback, useEffect, useRef, useState } from "react";
import { Announcer, Shell } from "./components/Shell";
import { Notice } from "./components/bits";
import { href, useRoute } from "./router";
import { Login, Lock, Setup } from "./screens/Gate";
import { Incidents } from "./screens/Incidents";
import { System } from "./screens/System";
import { Wall } from "./screens/Wall";
import { AuthProvider, useAuth } from "./state/auth";
import { TelemetryProvider } from "./state/telemetry";

const SHORTCUTS: [string, string][] = [
  ["j / k", "Next / previous incident"],
  ["a", "Acknowledge"],
  ["d", "Dismiss (asks for a reason)"],
  ["e", "Escalate"],
  ["r", "Resolve"],
  ["n", "Write a note"],
  ["/", "Search incidents"],
  ["?", "This list"],
  ["Esc", "Close a dialog or a reason prompt"],
];

function Shortcuts({ open, onClose }: { open: boolean; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return (
    <dialog ref={ref} onClose={onClose} aria-labelledby="shortcuts-title">
      <h2 id="shortcuts-title">Keyboard shortcuts</h2>
      <table>
        <tbody>
          {SHORTCUTS.map(([keys, what]) => (
            <tr key={keys}>
              <th scope="row">
                <kbd>{keys}</kbd>
              </th>
              <td>{what}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <button type="button" className="primary" onClick={onClose}>
        Close
      </button>
    </dialog>
  );
}

function Signed({
  username,
  role,
}: {
  username: string;
  role: "administrator" | "operator" | "viewer";
}) {
  const auth = useAuth();
  const [route, go] = useRoute();
  const [shortcuts, setShortcuts] = useState(false);
  const open = useCallback(() => {
    setShortcuts(true);
  }, []);
  const close = useCallback(() => {
    setShortcuts(false);
  }, []);
  return (
    <TelemetryProvider onEnded={auth.refresh}>
      <a
        className="skip"
        href="#main"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById("main")?.focus();
        }}
      >
        Skip to content
      </a>
      <Announcer muted={false} />
      <Shell
        route={route}
        user={{ user_id: "", username, role, must_change_password: false, disabled: false }}
        onShortcuts={open}
      >
        {route.view === "wall" && <Wall route={route} />}
        {route.view === "incidents" && (
          <Incidents
            route={route}
            role={role}
            onShortcuts={open}
            onNavigate={(id) => {
              go({ view: "incidents", id });
            }}
          />
        )}
        {route.view === "system" && <System role={role} />}
      </Shell>
      <Shortcuts open={shortcuts} onClose={close} />
    </TelemetryProvider>
  );
}

function Gate() {
  const auth = useAuth();
  const { load } = auth;
  if (load.kind === "loading") {
    return (
      <main className="gate" aria-labelledby="gate-title">
        <h1 id="gate-title">VIGIL-88</h1>
        <p role="status" className="muted">
          Connecting to the engine…
        </p>
      </main>
    );
  }
  if (load.kind === "failed") {
    return (
      <main className="gate" aria-labelledby="gate-title">
        <h1 id="gate-title">VIGIL-88</h1>
        <Notice kind="error" title="The engine is not responding">
          <p>{load.message}</p>
          <button type="button" onClick={auth.refresh}>
            Try again
          </button>
        </Notice>
      </main>
    );
  }
  const { status } = load;
  if (status.setup_required) return <Setup />;
  if (!status.authenticated || status.user === null || status.user === undefined) return <Login />;
  if (status.locked) return <Lock username={status.user.username} />;
  return <Signed username={status.user.username} role={status.user.role} />;
}

export function App() {
  useEffect(() => {
    if (window.location.hash === "") window.location.hash = href({ view: "wall", camera: null });
  }, []);
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  );
}
