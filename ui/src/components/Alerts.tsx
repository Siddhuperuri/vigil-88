import { useEffect, useRef } from "react";
import { planAlert, playTone, shellBridge } from "../alerts";
import { useTelemetry } from "../state/telemetry";

/** Reacts to a new incident with a tone and, when nobody is looking, a native notification. It
 * renders nothing: the screen-reader announcement lives in `Announcer`. */
export function Alerts({ soundOn }: { soundOn: boolean }) {
  const { notice, noticeSeq } = useTelemetry();
  const handled = useRef(0);
  useEffect(() => {
    if (notice === null || noticeSeq === handled.current) return;
    handled.current = noticeSeq;
    const plan = planAlert(notice.change, notice.incident.severity, document.hasFocus(), soundOn);
    if (plan.tone !== null) playTone(plan.tone);
    if (plan.notify) {
      void shellBridge()?.notify?.({
        severity: notice.incident.severity.toUpperCase(),
        incidentId: notice.incident.incident_id,
        displayRef: notice.incident.display_ref,
        cameraId: notice.incident.camera_id,
        eventType: notice.incident.event_type,
      });
    }
  }, [notice, noticeSeq, soundOn]);
  return null;
}
