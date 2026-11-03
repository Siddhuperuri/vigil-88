import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJson } from "../api/client";
import type { IncidentPage, Incident, Role } from "../api/types";
import { SEVERITIES } from "../api/types";
import { Notice, SeverityBadge, when } from "../components/bits";
import { type Route, href } from "../router";
import { useTelemetry } from "../state/telemetry";
import { type ActionName, IncidentDetail } from "./IncidentDetail";

type Scope = "open" | "all";

interface Query {
  scope: Scope;
  minSeverity: string;
  search: string;
}

function path(query: Query, cursor: string | null): string {
  const p = new URLSearchParams({ limit: "50" });
  if (query.scope === "open") p.set("open_only", "true");
  if (query.minSeverity !== "") p.set("min_severity", query.minSeverity);
  if (query.search.trim() !== "") p.set("search", query.search.trim());
  if (cursor !== null) p.set("cursor", cursor);
  return `/api/v1/incidents?${p.toString()}`;
}

type Listing =
  | { state: "loading"; items: Incident[]; total: number | null }
  | { state: "ready"; items: Incident[]; total: number; more: string | null }
  | { state: "failed"; items: Incident[]; total: number | null; message: string };

function useListing(query: Query, refreshKey: number): [Listing, () => void] {
  const [listing, setListing] = useState<Listing>({ state: "loading", items: [], total: null });
  const cursor = useRef<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    cursor.current = null;
    getJson<IncidentPage>(path(query, null), controller.signal).then(
      (page) => {
        cursor.current = page.next_cursor ?? null;
        setListing({
          state: "ready",
          items: page.incidents,
          total: page.total,
          more: page.next_cursor ?? null,
        });
      },
      (error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setListing((l) => ({
          state: "failed",
          items: l.items,
          total: l.total,
          message: error instanceof Error ? error.message : "Could not load incidents.",
        }));
      },
    );
    return () => {
      controller.abort();
    };
  }, [query, refreshKey]);

  const loadMore = useCallback(() => {
    const next = cursor.current;
    if (next === null) return;
    void getJson<IncidentPage>(path(query, next)).then((page) => {
      cursor.current = page.next_cursor ?? null;
      setListing((l) => ({
        state: "ready",
        items: [...l.items, ...page.incidents],
        total: page.total,
        more: page.next_cursor ?? null,
      }));
    });
  }, [query]);
  return [listing, loadMore];
}

const TYPING = new Set(["INPUT", "TEXTAREA", "SELECT"]);

export function Incidents({
  route,
  role,
  onNavigate,
  onShortcuts,
}: {
  route: Extract<Route, { view: "incidents" }>;
  role: Role;
  onNavigate: (id: string | null) => void;
  onShortcuts: () => void;
}) {
  const { noticeSeq } = useTelemetry();
  const [scope, setScope] = useState<Scope>("open");
  const [minSeverity, setMinSeverity] = useState("");
  const [search, setSearch] = useState("");
  const [changes, setChanges] = useState(0);
  const query = useMemo<Query>(
    () => ({ scope, minSeverity, search }),
    [scope, minSeverity, search],
  );
  const [listing, loadMore] = useListing(query, noticeSeq + changes);
  const actionRef = useRef<((action: ActionName) => void) | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);

  const items = listing.items;
  const move = useCallback(
    (delta: number) => {
      if (items.length === 0) return;
      const at = items.findIndex((i) => i.incident_id === route.id);
      const next =
        at < 0
          ? delta > 0
            ? 0
            : items.length - 1
          : Math.min(items.length - 1, Math.max(0, at + delta));
      const target = items[next];
      if (target !== undefined) onNavigate(target.incident_id);
    },
    [items, route.id, onNavigate],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const el = event.target as HTMLElement | null;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (el !== null && (TYPING.has(el.tagName) || el.isContentEditable)) return;
      const actions: Record<string, ActionName> = {
        a: "acknowledge",
        d: "dismiss",
        e: "escalate",
        r: "resolve",
      };
      if (event.key === "j") move(1);
      else if (event.key === "k") move(-1);
      else if (event.key === "/") {
        event.preventDefault();
        searchRef.current?.focus();
      } else if (event.key === "n") {
        event.preventDefault();
        noteRef.current?.focus();
      } else if (event.key === "?") onShortcuts();
      else if (event.key in actions) {
        const action = actions[event.key];
        if (action !== undefined) actionRef.current?.(action);
      } else return;
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [move, onShortcuts]);

  return (
    <div className="desk">
      <section className="queue" aria-labelledby="queue-title">
        <h1 id="queue-title">Incident desk</h1>
        <div className="filters">
          <label>
            Show
            <select
              value={scope}
              onChange={(e) => {
                setScope(e.target.value as Scope);
              }}
            >
              <option value="open">Open</option>
              <option value="all">All</option>
            </select>
          </label>
          <label>
            Severity at least
            <select
              value={minSeverity}
              onChange={(e) => {
                setMinSeverity(e.target.value);
              }}
            >
              <option value="">Any</option>
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <label>
            Search <kbd>/</kbd>
            <input
              ref={searchRef}
              type="search"
              value={search}
              placeholder="notes, reasons, ids"
              onChange={(e) => {
                setSearch(e.target.value);
              }}
            />
          </label>
        </div>
        {listing.state === "failed" && (
          <Notice kind="error" title="Could not refresh the list">
            <p>{listing.message} Showing the last list received.</p>
          </Notice>
        )}
        <p role="status" className="muted">
          {listing.total === null
            ? "Loading incidents…"
            : listing.total === 0
              ? scope === "open"
                ? "No open incidents."
                : "No incidents recorded."
              : `${String(listing.total)} ${listing.total === 1 ? "incident" : "incidents"}`}
        </p>
        <ul className="plain list" aria-label="Incidents">
          {items.map((i) => (
            <li key={i.incident_id}>
              <a
                href={href({ view: "incidents", id: i.incident_id })}
                aria-current={i.incident_id === route.id ? "true" : undefined}
                className={i.status === "confirmed" ? "unacked" : undefined}
              >
                <SeverityBadge severity={i.severity} />
                <span className="ref num">{i.display_ref}</span>
                <span>
                  {i.event_type.replace(/_/g, " ")} · {i.camera_id}
                </span>
                <span className="muted">
                  {i.status.toUpperCase()} · {when(i.confirmed_utc)}
                </span>
              </a>
            </li>
          ))}
        </ul>
        {listing.state === "ready" && listing.more !== null && (
          <button type="button" onClick={loadMore}>
            Load more
          </button>
        )}
      </section>
      <section className="pane" aria-label="Selected incident">
        {route.id === null ? (
          <p className="muted">
            Select an incident, or press <kbd>j</kbd> for the next one. <kbd>?</kbd> lists every
            shortcut.
          </p>
        ) : (
          <IncidentDetail
            key={route.id}
            id={route.id}
            role={role}
            noteRef={noteRef}
            promptRef={promptRef}
            actionRef={actionRef}
            onChanged={() => {
              setChanges((n) => n + 1);
            }}
          />
        )}
      </section>
    </div>
  );
}
