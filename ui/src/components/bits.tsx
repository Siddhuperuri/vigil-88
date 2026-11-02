import type { ReactNode } from "react";
import { type Severity, asSeverity } from "../api/types";

/** Severity is never colour alone: each band has its own glyph shape and a text label as well. */
const GLYPH: Record<Severity, string> = {
  INFO: "●",
  LOW: "▼",
  MODERATE: "◆",
  HIGH: "▲",
  CRITICAL: "■",
};

export function SeverityBadge({ severity }: { severity: string }) {
  const band = asSeverity(severity);
  return (
    <span className={`sev sev-${band.toLowerCase()}`}>
      <span aria-hidden="true">{GLYPH[band]}</span> {band}
    </span>
  );
}

const STATE_WORDS: Record<string, string> = {
  online: "ONLINE",
  degraded: "DEGRADED",
  reconnecting: "RECONNECTING",
  offline: "OFFLINE",
  failed: "FAILED",
  disabled: "DISABLED",
  initializing: "STARTING",
  analysis_suspended: "ANALYSIS SUSPENDED",
};

/** A camera state, in words, with a shape cue as well as a colour. */
export function StateLabel({ state }: { state: string }) {
  const good = state === "online";
  return (
    <span className={`state ${good ? "state-ok" : "state-bad"}`}>
      <span aria-hidden="true">{good ? "●" : "○"}</span> {STATE_WORDS[state] ?? state.toUpperCase()}
    </span>
  );
}

export function Notice({
  kind,
  title,
  children,
}: {
  kind: "error" | "warning" | "info";
  title: string;
  children?: ReactNode;
}) {
  return (
    <section className={`notice notice-${kind}`} role={kind === "info" ? "status" : "alert"}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}

/** "unavailable" for no data, never zero: a number that is not known is not a small one. */
export function readout(value: number | null | undefined, unit: string, digits = 0): string {
  return value === null || value === undefined ? "unavailable" : `${value.toFixed(digits)}${unit}`;
}

export function when(iso: string | null | undefined): string {
  if (iso === null || iso === undefined) return "unavailable";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "unavailable";
  if (n < 1024) return `${String(n)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit] ?? "TB"}`;
}
