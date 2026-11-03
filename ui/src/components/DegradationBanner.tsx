import { useTelemetry } from "../state/telemetry";

/** Shown on every screen while the engine is shedding load (01 §9). It states the step, the reason
 * and which cameras are no longer analysed, in words: an operator who believes a camera is being
 * watched when it is not is the failure this exists to prevent. */
export function DegradationBanner() {
  const { snapshot } = useTelemetry();
  const state = snapshot?.degradation;
  if (state === undefined || state.step === 0) return null;
  const suspended = state.suspended_cameras;
  return (
    <section className="notice notice-warning" role="status" aria-label="Analysis is reduced">
      <h2>
        <span aria-hidden="true">▲</span> Analysis reduced: {state.label} (step {state.step})
      </h2>
      <p>{state.reason ?? "The engine cannot keep up."}</p>
      {suspended.length > 0 ? (
        <p>
          <strong>Not being analysed right now:</strong> {suspended.join(", ")}. They are still
          recorded and shown, but nothing will be detected on them until the engine recovers.
        </p>
      ) : (
        <p>Every camera is still analysed, at a lower rate.</p>
      )}
    </section>
  );
}
