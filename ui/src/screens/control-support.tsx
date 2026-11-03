import type { ReactNode } from "react";
import { PendingProvider } from "../state/pending";
import { TelemetryProvider } from "../state/telemetry";

export const none = { pending: false, changes: [], can_restart: true };

/** The providers every screen under test needs. */
export function Wrap({ children }: { children: ReactNode }) {
  return (
    <TelemetryProvider onEnded={() => undefined}>
      <PendingProvider>{children}</PendingProvider>
    </TelemetryProvider>
  );
}
