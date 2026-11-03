import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { type Route, href } from "../router";
import { type User, asSeverity } from "../api/types";
import { useAuth } from "../state/auth";
import { useTelemetry } from "../state/telemetry";
import { readout } from "./bits";

export const VIEWS: { label: string; route: Route; adminOnly?: boolean }[] = [
  { label: "Live wall", route: { view: "wall", camera: null } },
  { label: "Incident desk", route: { view: "incidents", id: null } },
  { label: "Cameras", route: { view: "cameras", id: null } },
  { label: "Zones", route: { view: "zones", camera: null } },
  { label: "Activity", route: { view: "activity", tab: "timeline" } },
  { label: "Reports", route: { view: "reports" } },
  { label: "People", route: { view: "people" }, adminOnly: true },
  { label: "Audit log", route: { view: "audit" }, adminOnly: true },
  { label: "System", route: { view: "system" } },
];

/** The wall clock, ticking once a second. Staleness is judged against it, not against a clock read
 * during render, so the strip re-evaluates exactly when it redraws. */
function useClock(): { text: string; nowMs: number } {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
    }, 1000);
    return () => {
      clearInterval(timer);
    };
  }, []);
  return { text: new Date(now).toLocaleTimeString([], { hour12: false }), nowMs: now };
}

/** The always-visible strip (08 §3). It states what the numbers are, and when they went stale. */
function StatusStrip() {
  const { snapshot, link, lastMessageAt } = useTelemetry();
  const { text: clock, nowMs } = useClock();
  const cameras = snapshot?.cameras ?? [];
  const online = cameras.filter((c) => c.state === "online").length;
  const sys = snapshot?.system;
  const stale = lastMessageAt !== null && nowMs - lastMessageAt > 6000;
  const linkWords =
    link === "live" && !stale
      ? "LIVE"
      : link === "reconnecting" || stale
        ? "RECONNECTING"
        : link === "connecting"
          ? "CONNECTING"
          : "ENDED";
  const level = snapshot?.health.level ?? null;
  return (
    <header className={`strip ${level === "failed" ? "strip-failed" : ""}`}>
      <span className="brand">VIGIL-88</span>
      <span className={`pill ${linkWords === "LIVE" ? "pill-ok" : "pill-bad"}`}>
        <span aria-hidden="true">{linkWords === "LIVE" ? "●" : "○"}</span> {linkWords}
      </span>
      <span>
        <span className="k">engine</span>{" "}
        <span className="num">{level === null ? "unavailable" : level.toUpperCase()}</span>
      </span>
      <span>
        <span className="k">cameras</span>{" "}
        <span className="num">
          {snapshot === null ? "unavailable" : `${String(online)}/${String(cameras.length)}`}
        </span>
      </span>
      <span>
        <span className="k">GPU</span>{" "}
        <span className="num">
          {readout(sys?.gpu_utilization_percent, "%")}
          {sys?.gpu_memory_used_mb === null || sys === null || sys === undefined
            ? ""
            : ` ${String(Math.round(sys.gpu_memory_used_mb))} MB`}
        </span>
      </span>
      <span>
        <span className="k">CPU</span> <span className="num">{readout(sys?.cpu_percent, "%")}</span>
      </span>
      <span className="clock num" aria-label="Current time">
        {clock}
      </span>
    </header>
  );
}

/** A new HIGH or CRITICAL incident is announced once, as a change in this live region's text. It is
 * derived from the newest notice rather than pushed from an effect, so it cannot lag a render. */
export function Announcer({ muted }: { muted: boolean }) {
  const { notice } = useTelemetry();
  const band = notice === null ? null : asSeverity(notice.incident.severity);
  const loud = notice?.change === "new" && (band === "CRITICAL" || band === "HIGH") && !muted;
  const text = loud
    ? `New ${band} incident ${notice.incident.display_ref} on camera ${notice.incident.camera_id}`
    : "";
  return (
    <div aria-live="assertive" aria-atomic="true" className="sr-only">
      {text}
    </div>
  );
}

export function Shell({
  route,
  user,
  children,
  onShortcuts,
  sound,
  onSound,
}: {
  route: Route;
  user: User;
  children: ReactNode;
  onShortcuts: () => void;
  sound: boolean;
  onSound: () => void;
}) {
  const auth = useAuth();
  return (
    <div className="shell">
      <StatusStrip />
      <nav className="rail" aria-label="Main">
        <ul>
          {VIEWS.filter((item) => item.adminOnly !== true || user.role === "administrator").map(
            (item) => (
              <li key={item.label}>
                <a
                  href={href(item.route)}
                  aria-current={route.view === item.route.view ? "page" : undefined}
                >
                  {item.label}
                </a>
              </li>
            ),
          )}
        </ul>
        <div className="rail-foot">
          <p className="muted">
            {user.username}
            <br />
            <span className="k">{user.role}</span>
          </p>
          <button type="button" aria-pressed={sound} onClick={onSound}>
            Alert sound: {sound ? "on" : "off"}
          </button>
          <button type="button" onClick={onShortcuts}>
            Shortcuts
          </button>
          <button
            type="button"
            onClick={() => {
              void auth.lock();
            }}
          >
            Lock console
          </button>
          <button
            type="button"
            onClick={() => {
              void auth.logout();
            }}
          >
            Sign out
          </button>
        </div>
      </nav>
      <main id="main" className="view" tabIndex={-1}>
        {children}
      </main>
    </div>
  );
}
