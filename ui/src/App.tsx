import { useCallback, useEffect, useRef, useState } from "react";
import { Alerts } from "./components/Alerts";
import { PendingBar } from "./components/PendingBar";
import { type Command, CommandPalette } from "./components/CommandPalette";
import { Announcer, Shell, VIEWS } from "./components/Shell";
import { Notice } from "./components/bits";
import { href, useRoute } from "./router";
import { Login, Lock, Setup } from "./screens/Gate";
import { Activity } from "./screens/Activity";
import { Audit } from "./screens/Audit";
import { Cameras } from "./screens/Cameras";
import { Incidents } from "./screens/Incidents";
import { People } from "./screens/People";
import { Reports } from "./screens/Reports";
import { System } from "./screens/System";
import { Wall } from "./screens/Wall";
import { Zones } from "./screens/Zones";
import { AuthProvider, useAuth } from "./state/auth";
import { PendingProvider } from "./state/pending";
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
  ["Ctrl K", "Command palette: go to a view or run an action"],
  ["Esc", "Close a dialog or a reason prompt"],
];

const SOUND_KEY = "vigil.alertSound";

function storedSound(): boolean {
  try {
    return window.localStorage.getItem(SOUND_KEY) !== "off"; // on unless the person turned it off
  } catch {
    return true; // storage can be blocked; a missing preference must not silence an alarm
  }
}

function storeSound(on: boolean): void {
  try {
    window.localStorage.setItem(SOUND_KEY, on ? "on" : "off");
  } catch {
    // Not remembering the choice is a convenience lost, not an error.
  }
}

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

function AdminOnly() {
  return (
    <Notice kind="info" title="Administrators only">
      <p>This screen needs the administrator role.</p>
    </Notice>
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
  const [sound, setSound] = useState(() => storedSound());
  const [palette, setPalette] = useState(false);
  const open = useCallback(() => {
    setShortcuts(true);
  }, []);
  const close = useCallback(() => {
    setShortcuts(false);
  }, []);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPalette((p) => !p);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, []);
  const commands: Command[] = [
    ...VIEWS.filter((v) => !v.adminOnly || role === "administrator").map((v) => ({
      id: `go-${v.label}`,
      label: `Go to ${v.label}`,
      run: () => {
        go(v.route);
      },
    })),
    {
      id: "sound",
      label: sound ? "Turn the alert sound off" : "Turn the alert sound on",
      keywords: "mute audio",
      run: () => {
        setSound((on) => {
          storeSound(!on);
          return !on;
        });
      },
    },
    { id: "shortcuts", label: "Show keyboard shortcuts", keywords: "keys help", run: open },
    {
      id: "lock",
      label: "Lock the console",
      run: () => {
        void auth.lock();
      },
    },
    {
      id: "signout",
      label: "Sign out",
      keywords: "log out",
      run: () => {
        void auth.logout();
      },
    },
  ];
  return (
    <TelemetryProvider onEnded={auth.refresh}>
      <PendingProvider>
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
        <Alerts soundOn={sound} />
        <Shell
          route={route}
          user={{ user_id: "", username, role, must_change_password: false, disabled: false }}
          onShortcuts={open}
          sound={sound}
          onSound={() => {
            setSound((on) => {
              storeSound(!on);
              return !on;
            });
          }}
        >
          <PendingBar role={role} />
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
          {route.view === "cameras" && <Cameras route={route} role={role} go={go} />}
          {route.view === "zones" && <Zones route={route} role={role} />}
          {route.view === "activity" && <Activity route={route} />}
          {route.view === "reports" && <Reports userRole={role} />}
          {route.view === "people" &&
            (role === "administrator" ? <People me={username} /> : <AdminOnly />)}
          {route.view === "audit" && (role === "administrator" ? <Audit /> : <AdminOnly />)}
          {route.view === "system" && <System role={role} />}
        </Shell>
        <Shortcuts open={shortcuts} onClose={close} />
        <CommandPalette
          commands={commands}
          open={palette}
          onClose={() => {
            setPalette(false);
          }}
        />
      </PendingProvider>
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
